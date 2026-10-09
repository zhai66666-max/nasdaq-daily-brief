"""统一编排层。

把原来三个独立项目各自的数据抓取 + 计算并行跑一遍，汇总成一个上下文。
三个来源互不阻塞：任何一个失败，其余两个照常产出，邮件照发，
失败的那个区块会显示明确的错误提示（而不是整封邮件挂掉）。

来源对应关系：
  etf_monitor     ← 原 nasdaq-etf-monitor      （纳指历史回撤 × 国内ETF溢价）
  drawdown_radar  ← 原 global-drawdown-radar   （全球 11 类资产回撤雷达）
  nasdaq100       ← 原 nasdaq100-daily-report  （纳指行情 / 宏观 / 技术面 / 成分股）
"""
from __future__ import annotations

import logging
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from src.paths import DATA_DIR

logger = logging.getLogger(__name__)

BEIJING = timezone(timedelta(hours=8))


def beijing_now() -> datetime:
    return datetime.now(BEIJING)


def beijing_date_str() -> str:
    return beijing_now().strftime("%Y-%m-%d")


# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SourceResult:
    key: str
    label: str
    ok: bool = False
    elapsed: float = 0.0
    error: str = ""
    data: dict = field(default_factory=dict)

    def summary(self) -> str:
        if self.ok:
            return f"{self.label}: OK ({self.elapsed:.1f}s)"
        return f"{self.label}: 失败 ({self.elapsed:.1f}s) — {self.error[:120]}"


# ── 来源 1：纳指历史回撤 × 国内 ETF 溢价 ─────────────────────────────────────

def collect_etf_monitor(run_date: str) -> dict:
    from src.providers.etf_monitor import (
        config_loader, data_fetcher, database, drawdown, etf, ranking,
    )

    # 1) 纳指100 完整历史（NASDAQ 官方 API → yfinance → 本地缓存，自动降级）
    ndx_df, ndx_source = data_fetcher.fetch_nasdaq_history()
    # 这条序列的最后一根日线决定了「当前回撤」是拿哪一天的收盘价算的。
    # 它和页头那个大数字并不是同一个源：页头走 yfinance，美股盘中会带上
    # 当天的实时未收盘 bar，而这里的 NASDAQ 官方历史接口要等收盘后才收录
    # 当天 → 盘中手动触发时两者会差一个交易日（页头动、回撤不动）。
    # 所以必须把基准日一并显示出来，否则读者无法判断回撤对应哪天。
    ndx_last_date = str(ndx_df["date"].iloc[-1])[:10] if len(ndx_df) else ""
    ndx_last_close = float(ndx_df["close"].iloc[-1]) if len(ndx_df) else None
    logger.info("  [etf_monitor] 纳指历史: %s, %d 条日线（截至 %s 收盘 %s）",
                ndx_source, len(ndx_df), ndx_last_date, ndx_last_close)

    # 2) 历史回撤
    strategy = config_loader.load_strategy()
    dd = drawdown.summary(
        ndx_df,
        levels=strategy["drawdown_levels"],
        thresholds=strategy["event_thresholds"],
    )
    logger.info("  [etf_monitor] 当前回撤 %.2f%% (分位 %.0f%%)",
                dd["current_drawdown"], dd["percentile"])

    # 3) 国内纳指 ETF 行情与溢价
    etf_cfg = config_loader.load_etfs()
    etf_configs = etf_cfg["etfs"]
    etf_results, ok_etfs, failed_etfs = etf.get_all_etfs(etf_configs)
    logger.info("  [etf_monitor] ETF: 成功 %d / 失败 %d", len(ok_etfs), len(failed_etfs))

    # 4) 排序与综合信号
    ranked = etf.rank_etfs(ok_etfs, strategy["premium_levels"], etf_cfg["liquidity"])
    signal = ranking.build_signal(dd, ranked, strategy["premium_accept_max"])
    recs = ranking.build_recommendations(ranked, etf_cfg["liquidity"])

    # 5) 落库（DuckDB 长期积累）
    try:
        conn = database.get_connection()
        database.init_db(conn)
        database.save_nasdaq(conn, dd["series"])
        database.save_etfs(conn, etf_results, run_date)
        database.save_drawdown_events(conn, dd["events"], run_date)
        conn.close()
    except Exception as exc:                       # 落库失败不影响发信
        logger.warning("  [etf_monitor] DuckDB 写入失败（不影响邮件）: %s", exc)

    basis_set = sorted({e["premium_basis"] for e in ok_etfs if e.get("premium_basis")})
    data_status = f"{len(ok_etfs)}/{len(etf_configs)} 只 ETF 成功"
    if failed_etfs:
        data_status += f"，{len(failed_etfs)} 只异常"

    # 精简 dd（扔掉 series / events 这类大对象，模板不需要）
    dd_slim = {
        "current_drawdown": dd["current_drawdown"],
        "current_peak": dd["current_peak"],
        "percentile": dd["percentile"],
        "level": dd["level"],
        "max_dd": dd["max_dd"],
        "recent_thresholds": dd["recent_thresholds"],
    }

    return {
        "dd": dd_slim,
        "ranked": ranked,
        "recommendations": recs,
        "signal": signal,
        "ndx_source": ndx_source,
        "ndx_count": len(ndx_df),
        "ndx_last_date": ndx_last_date,
        "ndx_last_close": ndx_last_close,
        "data_status": data_status,
        "basis_label": "、".join(basis_set) if basis_set else "未知",
        "failed_etfs": [
            {"code": f["code"], "name": f["name"], "error": f.get("error") or "无数据"}
            for f in failed_etfs
        ],
        "level_list": strategy["drawdown_levels"],
    }


# ── 来源 2：全球市场回撤雷达 ─────────────────────────────────────────────────

def collect_drawdown_radar() -> dict:
    from src.providers.drawdown_radar import data_fetcher as radar_fetch
    from src.providers.drawdown_radar.config import ETF_TICKERS
    from src.providers.drawdown_radar.data_fetcher import fetch_all_etfs, validate_data
    from src.providers.drawdown_radar.drawdown import compute_all_metrics
    from src.providers.drawdown_radar.signal import detect_breaches
    from src.providers.drawdown_radar.state import load_state, save_state

    adj_close, errors, latest_close = fetch_all_etfs(ETF_TICKERS, period="max")
    if adj_close is None or adj_close.empty:
        raise RuntimeError("全部 ETF 数据获取失败")

    for w in validate_data(adj_close):
        logger.warning("  [radar] 数据警告: %s", w)

    metrics = compute_all_metrics(adj_close, latest_close)
    logger.info("  [radar] 计算完成 %d 个资产", len(metrics))

    gold_src = ""          # 黄金现货追加成功后填数据源，供页脚如实标注

    # ── 追加黄金现货两个基准：上金所 Au99.99（元/克）、伦敦金现 XAU/USD（美元/盎司）
    #    走东财通道，与上面的 Yahoo 链完全独立（Yahoo 里没有上海金）。
    #
    #    **单独算，不 join 进 adj_close**：黄金的交易日历与美股不同（含中国节假日，
    #    伦敦金又几乎全年无休），并进同一张表再 ffill，黄金独有的日期会把重复的
    #    ETF 价格铺进那些行，而 20 日波动率这类「只看尾部 N 根」的指标会被这些
    #    重复值拉低。各算各的，只把结果并起来，就没有这个问题。
    try:
        import pandas as pd

        from src.providers.drawdown_radar.config import ALL_ASSETS
        from src.providers.drawdown_radar.data_fetcher import fetch_extra_assets

        extra = fetch_extra_assets()
        if extra["series"]:
            gmetrics = compute_all_metrics(pd.DataFrame(extra["series"]),
                                           extra["latest"])
            metrics = metrics + gmetrics
            _order = {e["ticker"]: i for i, e in enumerate(ALL_ASSETS)}
            metrics.sort(key=lambda r: _order.get(r["ticker"], 99))
            gold_src = "东方财富（黄金现货）"
            logger.info("  [radar] 追加黄金现货 %d 个：%s",
                        len(gmetrics), "、".join(extra["series"]))
        if extra["errors"]:
            errors.update(extra["errors"])
        if extra["cached"]:
            logger.warning("  [radar] 黄金现货回落缓存：%s", "、".join(extra["cached"]))
    except Exception as exc:                        # noqa: BLE001
        logger.warning("  [radar] 黄金现货追加失败（不影响 ETF 各行）：%s", exc)

    # ── 统一口径：纳斯达克100（QQQM）行的「距高点回撤」改走 NDX 指数，
    #    与 02 区块、加仓决策引擎同源，消除「ETF 复权 1.48%」vs「指数 1.27%」双口径打架。
    #    价格/涨跌仍显示 QQQM 实时值，只覆盖回撤这一族字段；失败则沿用原 ETF 复权口径。
    try:
        from src.providers.common.ndx_drawdown import get_nasdaq_drawdown
        _ndx = get_nasdaq_drawdown()
        _ndx_keys = ("dd_52w", "max_dd_52w", "dd_5y", "dd_historical",
                     "max_dd_historical", "dd_percentile", "cycle_max_dd",
                     "vol_20d", "dist_from_ath", "ath_date", "days_since_ath",
                     "status_label", "status_color", "status_emoji")
        _overridden = False
        for _m in metrics:
            if _m.get("ticker") == "QQQM":
                for _k in _ndx_keys:
                    if _k in _ndx:
                        _m[_k] = _ndx[_k]
                _m["caliber"] = "NDX指数"
                _overridden = True
                logger.info("  [radar] QQQM 行回撤已统一为 NDX 指数口径（历史回撤 %.2f%%，来源 %s）",
                            (_ndx["dd_historical"] or 0) * 100, _ndx.get("source"))
                break
        if not _overridden:
            logger.warning("  [radar] 未找到 QQQM 行，跳过 NDX 统一口径覆盖")
    except Exception as exc:                        # noqa: BLE001
        logger.warning("  [radar] NDX 统一口径覆盖失败，沿用 ETF 复权口径：%s", exc)

    # 回撤那一族锚在「已收盘」那根日线的收盘价，价格/涨跌是盘面快照 ——
    # 两个基准都带出来给邮件标注。不标的话，02 区块的纳指回撤锁死、
    # 而 10 区块的各资产回撤随盘中跳动，同一封邮件里看着自相矛盾。
    basis_dates = sorted({m.get("basis_date") for m in metrics if m.get("basis_date")})
    snap_dates = sorted({m.get("data_date") for m in metrics if m.get("data_date")})
    intraday = any(m.get("intraday_dropped") for m in metrics)
    if not basis_dates:
        basis_label = ""
    elif len(basis_dates) == 1:
        basis_label = f"{basis_dates[0][5:]} 收盘"
    else:
        basis_label = f"{basis_dates[-1][5:]} 收盘（{len(basis_dates)} 个基准日）"
    snapshot_label = (f"{snap_dates[-1][5:]} {'盘中' if intraday else '收盘'}"
                      if snap_dates else "")
    logger.info("  [radar] 价格基准 %s｜回撤基准 %s%s", snapshot_label or "—",
                basis_label or "—",
                "（已剔除盘中未收盘 bar）" if intraday else "")

    state = load_state()
    alerts, updated_state = detect_breaches(metrics, state)
    try:
        save_state(updated_state)
    except Exception as exc:
        logger.warning("  [radar] 状态保存失败（不影响邮件）: %s", exc)

    return {"metrics": metrics, "alerts": alerts, "errors": errors,
            "data_source": (f"{radar_fetch.LAST_SOURCE} ＋ {gold_src}"
                            if gold_src else radar_fetch.LAST_SOURCE),
            "basis_label": basis_label, "snapshot_label": snapshot_label,
            "intraday_dropped": intraday}


# ── 来源 3：纳斯达克100 深度数据 ─────────────────────────────────────────────

def collect_nasdaq100() -> dict:
    from src.providers.nasdaq100 import core

    ix = core.fetch_nasdaq100_data()
    # 两个基准都打进日志：页头与 04 区块的盘面快照是实时值，
    # 而 04 的 52 周区间/分位、06 技术指标、回撤一律走收盘口径。
    # 之前排查「指数变了但回撤没动」就是靠这一行，别省。
    logger.info("  [nasdaq100] 盘面 %s (%+.2f%%)｜指标基准 %s 收盘 %s%s",
                f"{ix['current_price']:,.2f}", ix["change_pct"],
                ix.get("idx_close_date") or "—",
                f"{ix['idx_close']:,.2f}" if ix.get("idx_close") else "—",
                "（已剔除盘中未收盘 bar）" if ix.get("intraday_dropped") else "")

    macro_raw = core.fetch_all_macro_indicators()
    macro = [core.score_macro_indicator(v) for v in macro_raw.values() if v]
    logger.info("  [nasdaq100] 宏观指标 %d 项", len(macro))

    comps = core.fetch_top_components()
    logger.info("  [nasdaq100] 成分股 %d 只", len(comps))

    return {"ix": ix, "macro": macro, "components": comps,
            "components_source": core.LAST_COMPONENTS_SOURCE}


# ── 来源 4：黄金 / 上海金 ETF 溢价排名（13 区块）────────────────────────────
# 独立于 etf_monitor：那边是纳指 ETF（T+1 净值 + 2% 阈值），这边是黄金
# （最新已披露净值 + 0.3% 阈值），标的分开、阈值分开，公式与排序共用同一套口径。
# 分类不看简称，走跟踪标的（东财基金档案）——「金ETF」系列其实是上海金。

def collect_gold_etf() -> dict:
    from src.providers import gold_etf

    data = gold_etf.build()
    logger.info("  [gold_etf] %s｜跟踪误差窗口 %s｜费率缓存命中 %d",
                data["data_status"], data.get("te_label", ""),
                data.get("fee_cached", 0))
    return data


# ─────────────────────────────────────────────────────────────────────────────

SOURCES = [
    ("etf_monitor",    "纳指回撤 × ETF溢价", collect_etf_monitor),
    ("drawdown_radar", "全球回撤雷达",       collect_drawdown_radar),
    ("nasdaq100",      "纳指深度数据",       collect_nasdaq100),
    ("gold_etf",       "黄金ETF选基",        collect_gold_etf),
]


def collect_all(run_date: str, max_workers: int = 4) -> dict[str, SourceResult]:
    """并发跑四个来源，单源失败被隔离。"""
    results: dict[str, SourceResult] = {}
    t_all = time.time()

    def _timed(fn, *args):
        """量每个来源自己的耗时。

        注意：不能用「as_completed 回调时刻 - 提交时刻」来算，
        那样算出来的是「等待其他来源的时间」，三个来源会得到近乎相同的假数值。
        必须在这里包一层，量函数自身的执行时间。
        """
        t0 = time.time()
        try:
            return fn(*args), None, time.time() - t0
        except Exception as exc:                # noqa: BLE001
            return None, exc, time.time() - t0

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {}
        for key, label, fn in SOURCES:
            # etf_monitor 需要 run_date 参数
            args = (run_date,) if fn is collect_etf_monitor else ()
            futures[ex.submit(_timed, fn, *args)] = (key, label)

        for fut in as_completed(futures):
            key, label = futures[fut]
            data, exc, elapsed = fut.result()
            if exc is None:
                results[key] = SourceResult(key, label, ok=True,
                                            elapsed=elapsed, data=data)
            else:
                tb = traceback.format_exc(limit=3)
                logger.error("  [%s] 失败: %s", key, exc)
                logger.debug(tb)
                results[key] = SourceResult(key, label, ok=False, elapsed=elapsed,
                                            error=f"{type(exc).__name__}: {exc}")

    logger.info("全部来源耗时 %.1fs", time.time() - t_all)
    for r in results.values():
        logger.info("  %s", r.summary())
    return results
