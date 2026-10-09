from __future__ import annotations
"""
Data Fetcher — yfinance batch download with retry, NaN handling, per-ETF isolation.
"""

import time
import logging
from typing import Optional

import numpy as np
import pandas as pd
import yfinance as yf

from src.providers.drawdown_radar.config import ETF_TICKERS

logger = logging.getLogger(__name__)

# 记录本次实际用到的数据源，供邮件页脚如实标注（不做假设性描述）
LAST_SOURCE = "Yahoo Finance"

# Retry config
YFINANCE_RETRIES = 3
YFINANCE_RETRY_DELAY = 5  # seconds

# ─── Helpers ───────────────────────────────────────────────────────────────────


def _adj_close_series(df_t: pd.DataFrame) -> pd.Series:
    """Adj Close 序列，并把**尾部因 Yahoo 还没算调整因子而缺的当日**用 Close 补出来。

    为什么必须补：原来直接 `df_t["Adj Close"].dropna()`，而 Yahoo 在最新一根日线上
    常把 Adj Close 留成 NaN（调整因子要等收盘结算后才生成），dropna 于是把
    **最新交易日整根丢掉**。表现就是 09 区块标题永远写着 T-1：

      2026-10-08 09:07 那次 run 显示「价格 10-06 收盘」，但美股 10-07 早已收盘
      （美东 10-07 16:00 = 北京 10-08 04:00），NASDAQ 官方接口当天就能取到 10-07。
      同一封邮件里 01 区块的纳指已是 10-07，09 区块却停在 10-06 —— 就是这个原因。

    补法：从最后一个有效复权价出发，按 Close 的日涨跌把尾部外推，
    保持复权序列连续（**不**直接接不复权价，否则除息日会被算成跳空、回撤偏大）。
    """
    adj = df_t["Adj Close"]
    close = df_t["Close"] if "Close" in df_t.columns else None
    last_ok = adj.last_valid_index()
    if close is None or last_ok is None:
        return adj.dropna()

    out = adj.copy()
    missing = out.isna() & (out.index > last_ok)
    if missing.any():
        ratio = (close / close.shift(1)).fillna(1.0)
        ratio.loc[:last_ok] = 1.0          # 累计乘积只在尾部断点之后生效
        out.loc[missing] = float(adj.loc[last_ok]) * ratio.cumprod().loc[missing]
    return out.dropna()


# ─── Public API ────────────────────────────────────────────────────────────────


def fetch_all_etfs(
    tickers: list[str] | None = None,
    period: str = "max",
    max_retries: int = 3,
) -> tuple[pd.DataFrame, dict[str, str], dict[str, float]]:
    """
    Batch-download historical data for all ETFs.

    Returns:
      adj_close: DataFrame (dates × tickers) of Adjusted Close prices
      errors:    dict of {ticker: error_message} for failed tickers
      latest_close: dict of {ticker: latest_unadjusted_close_price}
    """
    if tickers is None:
        tickers = ETF_TICKERS

    ticker_str = " ".join(tickers)
    logger.info("Fetching %d ETFs: %s", len(tickers), ticker_str)

    # ── Retry loop ─────────────────────────────────────────────────────────
    # 说明：原来这里重试耗尽后直接 raise，导致「一个数据源挂了雷达整块消失」。
    # 现在改为不抛异常，把没能拿到的标的交给下面的兜底源补齐。
    # 另外接入共享熔断器：Yahoo 已判定不可用就别再耗时间重试。
    from src.providers.common import yf_breaker

    data = None
    if yf_breaker.is_open():
        logger.warning("yfinance 已熔断，雷达直接走兜底源")
    else:
        for attempt in range(1, max_retries + 1):
            try:
                data = yf.download(
                    ticker_str,
                    period=period,
                    auto_adjust=False,  # Get both Close and Adj Close
                    progress=False,
                    group_by="ticker",
                )
                yf_breaker.record_success()
                break
            except Exception as exc:
                logger.warning("Download attempt %d/%d failed: %s", attempt, max_retries, exc)
                yf_breaker.record_failure(str(exc))
                if attempt < max_retries and not yf_breaker.is_open():
                    wait = YFINANCE_RETRY_DELAY * attempt
                    logger.info("Retrying in %d seconds...", wait)
                    time.sleep(wait)
                else:
                    logger.error("yfinance 重试 %d 次仍失败，转入兜底数据源", max_retries)
                    data = None
                    break

    # ── Parse multi-ticker DataFrame ────────────────────────────────────────
    adj_close = pd.DataFrame()
    errors = {}
    latest_close = {}

    for t in tickers:
        try:
            if data is None or data.empty:
                errors[t] = "yfinance 无返回"
                continue
            if len(tickers) == 1:
                # Single ticker: no MultiIndex columns
                df_t = data.copy()
            else:
                if t not in data.columns.levels[0] if hasattr(data.columns, 'levels') else t not in data.columns:
                    errors[t] = "No data returned"
                    continue
                df_t = data[t].copy()

            # Get Adjusted Close（尾部缺的当日用 Close 补，见 _adj_close_series）
            if "Adj Close" in df_t.columns:
                series = _adj_close_series(df_t)
            elif "Close" in df_t.columns:
                series = df_t["Close"].dropna()
            else:
                errors[t] = "No Close/Adj Close columns"
                continue

            if len(series) < 5:
                errors[t] = f"Insufficient data ({len(series)} rows)"
                continue

            # 行情最后一根 vs 复权序列最后一根：两者不同才说明补齐动过手；
            # 若补齐后仍落后行情，就是数据源自己没给这一根（需要换源，不是这里的锅）。
            raw_last = df_t.index[-1]
            logger.info("  %s 行情至 %s｜复权序列至 %s%s", t, raw_last.date(),
                        series.index[-1].date(),
                        "（尾部已按 Close 补齐）"
                        if raw_last.date() != series.index[-1].date() else "")

            series.name = t
            if adj_close.empty:
                adj_close = pd.DataFrame(series)
            else:
                adj_close = adj_close.join(series, how="outer")

            # Get latest unadjusted close for current price display
            if "Close" in df_t.columns:
                raw_close = df_t["Close"].dropna()
                if len(raw_close) > 0:
                    latest_close[t] = float(raw_close.iloc[-1])

        except Exception as exc:
            errors[t] = f"Parse error: {exc}"
            logger.warning("Failed to parse %s: %s", t, exc)

    # ── 兜底：yfinance 没能给出的标的，改用不依赖 Yahoo 的数据源补齐 ────────
    global LAST_SOURCE
    missing = [t for t in tickers if t not in adj_close.columns]
    if missing:
        logger.warning("yfinance 缺失 %d 个标的，启用兜底源：%s", len(missing), missing)
        filled_before = len(missing)
        adj_close, latest_close = _fill_from_fallback(
            missing, adj_close, latest_close, errors)
        filled = filled_before - len([t for t in missing if t in errors])
        if not adj_close.empty and filled >= len(missing):
            LAST_SOURCE = "NASDAQ 官方接口（Yahoo 限流兜底）"
        elif filled:
            LAST_SOURCE = "Yahoo Finance + NASDAQ 官方接口（部分兜底）"
        else:
            LAST_SOURCE = "Yahoo Finance（兜底亦失败）"
    else:
        LAST_SOURCE = "Yahoo Finance 复权价"

    # ── Clean up ────────────────────────────────────────────────────────────
    if not adj_close.empty:
        adj_close = adj_close.sort_index()
        # 先对齐最新交易日（Yahoo 有时还缺当日那根），再 ffill 补齐对其他标的的缺行
        adj_close, latest_close = align_to_latest_session(
            adj_close, latest_close, tickers)
        adj_close = adj_close.ffill()  # Forward-fill missing days

    logger.info(
        "Fetch complete: %d/%d ETFs OK, %d errors",
        len(adj_close.columns) if not adj_close.empty else 0,
        len(tickers),
        len(errors),
    )
    for t, e in errors.items():
        logger.warning("  %s: %s", t, e)

    return adj_close, errors, latest_close


def _fill_from_fallback(
    missing: list[str],
    adj_close: pd.DataFrame,
    latest_close: dict[str, float],
    errors: dict[str, str],
) -> tuple[pd.DataFrame, dict[str, float]]:
    """用 NASDAQ 官方 chart 接口补齐 yfinance 未提供的标的。

    雷达需要的是「长历史复权/收盘价序列」，兜底源给的是未复权收盘价。
    对本项目的用途（回撤幅度、波动率、距历史高点）而言，
    复权与否只影响分红率量级的偏差，不影响结论方向，因此可接受；
    这里在日志里明确标注来源，避免和 yfinance 数据混淆。
    """
    from src.providers.common import us_history

    bundles = us_history.fetch_many(missing, assetclass="etf", max_workers=5)
    for t in missing:
        b = bundles.get(t)
        if not b or not b.ok:
            errors[t] = "yfinance 与兜底源均无数据"
            continue
        series = b.df.set_index(pd.to_datetime(b.df["date"]))["close"].dropna()
        series.name = t
        if series.empty:
            errors[t] = "兜底源返回空序列"
            continue
        if adj_close.empty:
            adj_close = pd.DataFrame(series)
        else:
            adj_close = adj_close.join(series, how="outer")
        if b.last is not None:
            latest_close[t] = float(b.last)
        errors.pop(t, None)                 # 已补齐，从错误清单里移除
        logger.info("  %s ← 兜底源补齐 %d 行（%s 起）",
                    t, len(series), series.index[0].date())
    return adj_close, latest_close


# ─── 额外资产：黄金现货两个基准（走东财，不依赖 Yahoo）────────────────────────


# 净值反推的标定窗口：短窗抗费率漂移、中位数抗异常日（见 _extend_by_fund 的注释）
CALIB_DAYS = 20
MIN_CALIB_DAYS = 10


def _extend_by_fund(rows: list[tuple[str, float]],
                    fund_code: str) -> tuple[list[tuple[str, float]], str]:
    """金价取数失败时，用黄金 ETF 的净值序列把日线补到最新。

    依据：基金净值 = 金价 × 每份含金量（常数系数）。gold_etf 已实证两者的日偏差
    只有 0.0027%（净值即金价 × 0.009509），所以净值除回系数就是金价。

    做法：拿**已有的真实金价**与净值序列求重叠期，用最近 `CALIB_DAYS` 个重叠日
    标定系数 k = median(净值 ÷ 金价)，再把净值序列里比缓存更新的那些日子换算回金价。
    标定用的是真实金价，所以不额外引入口径假设。

    **窗口为什么取这么短（20 天）**：基金的费率磨损会让「净值 ÷ 金价」这个比例
    逐年缓慢下移（518880 约 0.5%/年），用长窗口等于取了一个偏旧的水平。
    2026-10-09 实测同一段数据、只换标定窗口，反推误差：

        5 天 0.018% ｜ 20 天 0.042% ｜ 60 天 0.093% ｜ 250 天 0.332%

    短窗抗漂移、中位数抗个别异常日，20 天是两者的平衡点。

    为什么需要它：`push2his` 在云端是**限频型**失败（请求密度一高就大面积被拒，
    2026-10-09 实测：radar 2 个请求能通、gold_etf 16 个连续请求几乎全被拒），
    而「回落缓存」**并不会更新缓存** —— 不补的话回撤会一直停在旧快照上。
    净值走的是另一个域名（fund.eastmoney.com），云端实测可达，正好互补。

    返回 (新序列, 说明)；没有可补的返回 (原序列, "")。
    """
    if not rows or not fund_code:
        return rows, ""
    from src.providers.common import em_history

    nav = em_history.fund_nav_series(fund_code)
    if not nav:
        return rows, ""
    au = dict(rows)
    overlap = sorted(d for d in nav if d in au and au[d])
    if len(overlap) < MIN_CALIB_DAYS:
        logger.warning("  [radar] 净值反推跳过：与 %s 的重叠期只有 %d 天",
                       fund_code, len(overlap))
        return rows, ""
    ratios = sorted(nav[d] / au[d] for d in overlap[-CALIB_DAYS:])
    k = ratios[len(ratios) // 2]
    if not k or k <= 0:
        return rows, ""
    last = rows[-1][0]
    added = [(d, round(nav[d] / k, 2)) for d in sorted(nav) if d > last]
    if not added:
        return rows, ""
    logger.info("  [radar] 净值反推：按 %s 补 %d 天（系数 %.6f，重叠 %d 天）",
                fund_code, len(added), k, len(overlap))
    return rows + added, f"净值反推{fund_code}"


def fetch_extra_assets() -> dict:
    """取「Yahoo 里没有对应代码」的额外资产日线（黄金现货两个基准），走东财。

    与 ETF 那条链完全独立：不经过 yfinance，也不参与 NASDAQ 官方源对齐
    （官方源没有金价，硬塞进去只会白跑一轮请求）。

    返回 dict：
      series  {ticker: pd.Series}  索引为 tz-naive DatetimeIndex
      latest  {ticker: float}      最新一根**已收盘**日线的收盘价
      errors  {ticker: 原因}       真正没取到的（会在 09 区块显示 N/A）
      cached  [ticker]             本次回落到了本地缓存
    """
    from src.providers.common import em_history
    from src.providers.drawdown_radar.config import EXTRA_ASSETS

    series: dict[str, pd.Series] = {}
    latest: dict[str, float] = {}
    errors: dict[str, str] = {}
    cached: list[str] = []
    sources: dict[str, str] = {}

    for a in EXTRA_ASSETS:
        t, secid = a["ticker"], a["secid"]
        try:
            rows, src, used_cache = em_history.fetch_kline(
                secid, sina_symbol=a.get("sina_symbol", ""))
        except Exception as exc:                            # noqa: BLE001
            errors[t] = f"取数异常：{exc}"
            logger.warning("  [radar] %s 取数异常：%s", t, exc)
            continue

        # 黄金收得比雷达跑批晚（上金所 15:30、伦敦金 24 小时），
        # 最后一根若还在走就必须剔除，否则「最新价」基准日与 ETF 对不上。
        rows = em_history.drop_unclosed(rows)
        note = ""
        if used_cache:
            # 回落缓存不会让缓存变新 —— 用云端可达的基金净值把缺口补上
            rows, note = _extend_by_fund(rows, a.get("fallback_fund", ""))
            if note:
                rows = em_history.drop_unclosed(rows)
        if len(rows) < 30:
            errors[t] = f"日线不足（{len(rows)} 行）"
            logger.warning("  [radar] %s 日线不足：%d 行", t, len(rows))
            continue

        idx = pd.to_datetime([d for d, _ in rows])
        s = pd.Series([c for _, c in rows], index=idx, name=t).dropna()
        if len(s) < 30:
            errors[t] = f"日线有效值不足（{len(s)} 行）"
            continue
        series[t] = s
        latest[t] = float(s.iloc[-1])
        sources[t] = src + (f"＋{note}" if note else "")
        if used_cache:
            cached.append(t)
        logger.info("  [radar] %s（%s）日线 %d 行，至 %s｜源 %s%s",
                    t, secid, len(s), s.index[-1].date(), sources[t],
                    "（回落缓存）" if used_cache else "")

    em_history.save_cache()
    return {"series": series, "latest": latest, "errors": errors,
            "cached": cached, "sources": sources}


def _extend_from_official(series: pd.Series,
                          off_close: pd.Series) -> tuple[pd.Series, list[str]]:
    """用官方**未复权**收盘把复权序列外推到更晚的交易日（比值法）。

    比值法的依据：非除息日，未复权价与复权价的日间涨跌完全相同，
    所以 `adj_t = adj_{t-1} × close_t / close_{t-1}` 与原复权序列等价；
    只有正好落在除息日那天会有分红率量级的偏差（远小于「整整少一天」）。

    返回 (新序列, 被补的日期列表)；没有可补的返回原序列与空列表。
    """
    if series is None or off_close is None or off_close.empty:
        return series, []
    clean = series.dropna()
    if clean.empty:
        return series, []
    last_ok = clean.index[-1]
    new_pts = off_close.index[off_close.index > last_ok]
    if len(new_pts) == 0:
        return series, []

    out = series.copy()
    val = float(clean.iloc[-1])
    added: list[str] = []
    for d in new_pts:
        pos = off_close.index.get_loc(d)
        if pos <= 0:
            continue
        c_now, c_prev = off_close.iloc[pos], off_close.iloc[pos - 1]
        if pd.isna(c_now) or pd.isna(c_prev) or not c_prev:
            continue
        val = val * float(c_now) / float(c_prev)
        out.loc[d] = val
        added.append(str(pd.Timestamp(d).date()))
    return out, added


def align_to_latest_session(
    adj_close: pd.DataFrame,
    latest_close: dict[str, float],
    tickers: list[str],
) -> tuple[pd.DataFrame, dict[str, float]]:
    """把各标的的日线对齐到「最新一个已收盘的美股交易日」。

    为什么需要：定时任务在美股收盘后约 5 小时（北京 09:0x）跑，而 Yahoo 的日线
    有时那一天还没生成 —— 2026-10-08 09:07 那次 run，11 只全停在 10-06，
    同一封邮件里 01 区块（走 NASDAQ 官方源）已经是 10-07，09 区块却写着
    「价格 10-06 收盘」，两个区块自相矛盾。这不是「多等一会儿就好」：
    定时送达时间（北京 08:45~09:45，全年漂移）正好压在 Yahoo 的更新边界上。

    做法：用 NASDAQ 官方 chart 接口（不依赖 Yahoo，云端可达）取同一批标的的
    最新日线，只把 yfinance 缺的那几根按比值法补上；官方源整体失败就原样返回，
    不让这一步拖垮雷达。
    """
    if adj_close is None or adj_close.empty:
        return adj_close, latest_close
    try:
        from src.providers.common import us_history
        bundles = us_history.fetch_many(list(tickers), assetclass="etf", max_workers=5)
    except Exception as exc:                        # noqa: BLE001
        logger.warning("  [radar] 最新交易日对齐跳过（官方源不可用）：%s", exc)
        return adj_close, latest_close

    aligned: list[str] = []
    checked: list[str] = []
    for t in list(adj_close.columns):
        b = bundles.get(t)
        if not b or not b.ok:
            continue
        off = b.df.set_index(pd.to_datetime(b.df["date"]))["close"].dropna()
        # 时区对齐：yfinance 的日线索引可能是 tz-aware，官方源是 tz-naive
        _tz = getattr(adj_close.index, "tz", None)
        if _tz is not None and off.index.tz is None:
            off.index = off.index.tz_localize(_tz)
        elif _tz is None and off.index.tz is not None:
            off.index = off.index.tz_localize(None)

        _yf_last = adj_close[t].last_valid_index()
        checked.append(f"{t} yfinance {str(_yf_last.date()) if _yf_last is not None else '—'}"
                       f"/官方 {str(off.index[-1].date())}")

        series, added = _extend_from_official(adj_close[t], off)
        if added:
            adj_close[t] = series
            if b.last:
                latest_close[t] = float(b.last)
            aligned.append(f"{t}→{added[-1]}")

    if aligned:
        logger.warning("  [radar] yfinance 日线落后，已按 NASDAQ 官方接口对齐最新交易日：%s",
                       "、".join(aligned))
    elif checked:
        # 没有落后也要留一行：既证明官方通道当时可用，也便于下次一眼看清两个源的日期
        logger.info("  [radar] 最新交易日核对一致（%d 只）：%s", len(checked), checked[0])
    return adj_close, latest_close


def validate_data(prices: pd.DataFrame, min_rows: int = 20) -> list[str]:
    """Check data quality and return warnings."""
    warnings = []
    for t in prices.columns:
        series = prices[t].dropna()
        if len(series) < min_rows:
            warnings.append(f"{t}: only {len(series)} valid rows (min {min_rows})")
        if len(series) > 0:
            last_date = series.index[-1]
            days_stale = (pd.Timestamp.now(tz=last_date.tz) - last_date).days
            if days_stale > 5:
                warnings.append(f"{t}: last data is {last_date.date()} ({days_stale} days ago)")
    return warnings
