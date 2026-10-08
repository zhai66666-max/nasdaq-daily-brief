"""派生计算层。

把原始数据变成模板能直接用的「结论 + 文案 + 颜色」。
这里集中了原来散落在两处 HTML 生成代码里的判断逻辑
（nasdaq100-daily-report 的 综合研判/信号文案、global-drawdown-radar 的
report.build_report_context 展示格式化），搬家时逻辑保持不变。
"""
from __future__ import annotations

import math
from typing import Any


# ─── 涨跌配色（中国习惯：涨红跌绿）────────────────────────────────────────────

def updown_colors(convention: str = "cn") -> dict[str, str]:
    if convention == "cn":
        return {"up": "#dc2626", "down": "#16a34a", "flat": "#64748b"}
    return {"up": "#16a34a", "down": "#dc2626", "flat": "#64748b"}


def change_color(value: float | None, convention: str = "cn") -> str:
    c = updown_colors(convention)
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return c["flat"]
    if value > 0:
        return c["up"]
    if value < 0:
        return c["down"]
    return c["flat"]


# ─── 通用格式化 ──────────────────────────────────────────────────────────────

def fmt_vol(v: Any) -> str:
    """成交量中文单位（沿用原 nasdaq100-daily-report 口径）。"""
    try:
        v = float(v or 0)
    except (TypeError, ValueError):
        return "—"
    if v >= 1_000_000_000:
        return f"{v/1_000_000_000:.2f}亿"
    if v >= 1_000_000:
        return f"{v/1_000_000:.2f}百万"
    if v >= 1_000:
        return f"{v/1_000:.2f}千"
    return f"{v:.0f}"


def pct(val: float | None, decimals: int = 1, signed: bool = False) -> str:
    """分数 → 百分数字符串。val=None 返回 N/A。"""
    if val is None:
        return "N/A"
    try:
        val = float(val)
    except (TypeError, ValueError):
        return "N/A"
    if math.isnan(val):
        return "N/A"
    sign = "+" if (signed and val > 0) else ""
    return f"{sign}{val*100:.{decimals}f}%"


def pct_from_percent(val: float | None, decimals: int = 2, signed: bool = False) -> str:
    """已经是百分数（如 -8.72）→ 字符串。"""
    if val is None:
        return "N/A"
    try:
        val = float(val)
    except (TypeError, ValueError):
        return "N/A"
    if math.isnan(val):
        return "N/A"
    sign = "+" if (signed and val > 0) else ""
    return f"{sign}{val:.{decimals}f}%"


def price(val: float | None, decimals: int | None = None) -> str:
    if val is None:
        return "N/A"
    try:
        val = float(val)
    except (TypeError, ValueError):
        return "N/A"
    if math.isnan(val):
        return "N/A"
    if decimals is None:
        decimals = 1 if val >= 100 else 2
    return f"{val:,.{decimals}f}"


def money_cn(val: float | None) -> str:
    """金额（元）→ 亿/万。"""
    if val is None:
        return "—"
    try:
        val = float(val)
    except (TypeError, ValueError):
        return "—"
    if val >= 1e8:
        return f"{val/1e8:.2f}亿"
    if val >= 1e4:
        return f"{val/1e4:.0f}万"
    return f"{val:.0f}"


# ─── 来源 3：纳斯达克100 的派生结论 ──────────────────────────────────────────

def derive_nasdaq100(raw: dict, convention: str = "cn", thresholds: dict | None = None) -> dict:
    """输入 collect_nasdaq100() 的原始结果，输出模板需要的全部结论。"""
    thresholds = thresholds or {}
    ix = raw["ix"]
    macro = raw["macro"]
    comps = raw["components"]

    cur = ix["current_price"]                  # 页头实时价（盘面快照）
    # 派生指标一律以「已收盘」那根日线的收盘价为锚。盘中那一根还没走完，
    # 拿它算均线/分位会让指标每分钟跟着跳，也会和按收盘算的回撤打架。
    ref = ix.get("idx_close") or cur
    ma50, ma200 = ix.get("ma_50"), ix.get("ma_200")
    rsi = ix.get("rsi")
    vol, avg_vol_20 = ix.get("volume"), ix.get("avg_vol_20")
    vol_ref = ix.get("vol_ref") or vol            # 基准日成交量，与 20 日均量同口径
    low52, high52 = ix["low_52w"], ix["high_52w"]  # 已是收盘口径
    pos52 = ix.get("pos52")
    if pos52 is None:                              # provider 没给才自己算
        pos52 = ((ref - low52) / (high52 - low52) * 100) if (high52 - low52) else 0.0

    ref_up = (ix.get("idx_close_change") or 0) >= 0  # 基准日方向（量能文案用它）
    chg_color = change_color(ix["change"] if ix["change"] != 0 else None, convention)

    # ── RSI 档位
    if rsi:
        if rsi > 70:
            rsi_label, rsi_color = "超买", updown_colors(convention)["up"]
        elif rsi < 30:
            rsi_label, rsi_color = "超卖", updown_colors(convention)["down"]
        elif rsi > 60:
            rsi_label, rsi_color = "偏强", updown_colors(convention)["up"]
        elif rsi < 40:
            rsi_label, rsi_color = "偏弱", updown_colors(convention)["down"]
        else:
            rsi_label, rsi_color = "中性", "#f59e0b"
    else:
        rsi_label, rsi_color = "无数据", "#9ca3af"

    # ── 趋势（双均线）—— 文案与原件一字不差，只是判断改用收盘基准价
    if ma50 and ma200:
        if ref > ma50 > ma200:
            trend_detail = "指数运行在双均线之上，中长期上升趋势明确。回调至均线附近可视为加仓机会。"
            trend_tone = "up"
        elif ref < ma50 < ma200:
            trend_detail = "指数处于双均线之下，中期下行压力较大。建议轻仓观望，等待指数重新站上关键均线。"
            trend_tone = "down"
        elif ref > ma50 and ma50 < ma200:
            trend_detail = "突破50日线但仍在200日线下方，短期反弹但长期趋势未确认。轻仓参与，严格止损。"
            trend_tone = "watch"
        else:
            trend_detail = "指数在均线附近拉锯，方向不明确。控制仓位等待方向选择。"
            trend_tone = "watch"
    else:
        trend_detail, trend_tone = "技术指标数据不完整。", "flat"

    # ── RSI 文案
    if rsi:
        if rsi > 70:
            rsi_detail = f"RSI 超买（{rsi:.1f}），短期获利盘压力大，追高需谨慎。已有仓位可考虑分批止盈。"
        elif rsi < 30:
            rsi_detail = f"RSI 超卖（{rsi:.1f}），超跌反弹概率大，可关注低吸机会。"
        elif rsi > 60:
            rsi_detail = f"RSI 偏强（{rsi:.1f}），动能向上但未过热，适合持有或逢低加仓。"
        elif rsi < 40:
            rsi_detail = f"RSI 偏弱（{rsi:.1f}），买方力量不足，等待企稳信号。"
        else:
            rsi_detail = f"RSI 中性（{rsi:.1f}），短期缺乏方向性信号，观望为主。"
    else:
        rsi_detail = "RSI 数据暂不可用。"

    # ── 量能文案（基准日成交量 vs 20 日均量，同为收盘口径）
    if avg_vol_20 and vol_ref:
        vr = vol_ref / avg_vol_20
        if vr > 1.5:
            vol_detail = f"放量（{vr:.1f}倍均量），" + ("多头资金积极入场。" if ref_up else "恐慌盘涌出，短期仍有下行惯性。")
        elif vr < 0.7:
            vol_detail = f"缩量（{vr*100:.0f}%均量），" + ("追涨意愿不强，上涨可持续性存疑。" if ref_up else "抛压减小，可能是短期见底信号。")
        else:
            vol_detail = "量能正常，交投情绪稳定。"
    else:
        vol_detail = "成交量数据暂不可用。"

    # ── 52 周位置文案
    if pos52 > 85:
        pos52_detail = f"52周分位 {pos52:.1f}% 高位，接近历史高点，上行空间有限。"
    elif pos52 < 15:
        pos52_detail = f"52周分位 {pos52:.1f}% 低位，长期价值区域，适合定投建仓。"
    elif pos52 > 60:
        pos52_detail = f"52周分位 {pos52:.1f}%，中期偏强但距顶不远。"
    else:
        pos52_detail = f"52周分位 {pos52:.1f}%，中期偏弱，关注能否突破 50% 分位。"

    # ── 市场广度文案
    up_c = sum(1 for c in comps if c["change_pct"] > 0)
    dn_c = sum(1 for c in comps if c["change_pct"] < 0)
    total_c = len(comps)
    breadth_pct = (up_c / total_c * 100) if total_c else 0
    if breadth_pct > 60:
        breadth_detail = f"涨跌比 {up_c}:{dn_c}（{breadth_pct:.0f}%上涨），市场广度良好。"
    elif breadth_pct < 40:
        breadth_detail = f"涨跌比 {up_c}:{dn_c}（{breadth_pct:.0f}%上涨），多数个股下跌，警惕权重股拉指数。"
    else:
        breadth_detail = f"涨跌比 {up_c}:{dn_c}（{breadth_pct:.0f}%上涨），涨跌互现，内部分化。"

    # ── 宏观总分
    total_score = sum(m["score"] for m in macro)
    total_max = sum(m["max_score"] for m in macro)
    total_pct = round(total_score / total_max * 100) if total_max else 50
    # 指标缺失时总分不可比，如实标注覆盖率（原项目默认 6 项全在）
    try:
        from src.providers.nasdaq100.core import MACRO_INDICATORS
        macro_expected = len(MACRO_INDICATORS)
    except Exception:                                   # noqa: BLE001
        macro_expected = 6
    macro_available = len(macro)
    macro_complete = macro_available >= macro_expected
    if total_pct >= 70:
        overall_label, overall_pct_label = "偏多", "up"
    elif total_pct >= 40:
        overall_label, overall_pct_label = "中性", "watch"
    else:
        overall_label, overall_pct_label = "偏空", "down"

    # ── QQQ 回撤加仓信号（阈值可在 display.yaml 调）
    th_2x = float(thresholds.get("qqq_add_2x", 10))
    th_5x = float(thresholds.get("qqq_add_5x", 20))
    qqq = next((m for m in macro if "QQQ" in m.get("name", "")), None)
    qqq_dd = abs(qqq.get("drawdown_pct", 0)) if qqq else None
    if qqq_dd is None:
        qqq_alert = {"level": "unknown", "title": "QQQ 回撤加仓监控", "msg": "QQQ 回撤数据暂不可用。",
                     "badge": "", "progress": 0.0, "tone": "flat"}
    elif qqq_dd > th_5x:
        qqq_alert = {
            "level": "critical", "title": "五倍加仓信号", "badge": "5x 加仓", "tone": "extreme",
            "msg": f"QQQ 从历史最高点回撤 {qqq_dd:.2f}%，已突破 {th_5x:.0f}%。按加仓策略，当前对应五倍投入档位。",
            "progress": min(qqq_dd, th_5x * 1.5) / (th_5x * 1.5) * 100,
        }
    elif qqq_dd > th_2x:
        qqq_alert = {
            "level": "warning", "title": "双倍加仓信号", "badge": "2x 加仓", "tone": "deep",
            "msg": f"QQQ 从历史最高点回撤 {qqq_dd:.2f}%，已突破 {th_2x:.0f}%。按加仓策略，当前对应双倍投入档位。",
            "progress": min(qqq_dd, th_5x * 1.5) / (th_5x * 1.5) * 100,
        }
    else:
        qqq_alert = {
            "level": "normal", "title": "QQQ 回撤加仓监控", "badge": "", "tone": "normal",
            "msg": f"QQQ 从历史最高点回撤 {qqq_dd:.2f}%，未触发加仓档位（阈值 {th_2x:.0f}% / {th_5x:.0f}%）。正常定投即可。",
            "progress": min(qqq_dd, th_5x * 1.5) / (th_5x * 1.5) * 100,
        }

    gainers = sorted([c for c in comps if c["change_pct"] > 0],
                     key=lambda x: x["change_pct"], reverse=True)[:5]
    losers = sorted([c for c in comps if c["change_pct"] < 0],
                    key=lambda x: x["change_pct"])[:5]
    most_active = sorted(comps, key=lambda x: x["volume"], reverse=True)[:5]

    # 模板里按行取色（原来模板直接调 change_color()，改成先算好塞进去，
    # 保持「模板只取数据、不做逻辑」的一致约定）
    for group in (gainers, losers, most_active):
        for c in group:
            c["change_color"] = change_color(c.get("change_pct"), convention)
            c["change_display"] = f"{c['change_pct']:+.2f}%"

    return {
        "ix": ix,
        "cur_display": f"{cur:,.2f}",
        # 这个数字对应哪根日线：盘中是实时价、盘前/盘后是收盘价。
        # 页头标出来，才不至于和「回撤」那个按收盘算的数字互相矛盾。
        "bar_label": ix.get("bar_label") or "",
        "bar_phase": ix.get("bar_phase") or "",
        "chg_display": f"{ix['change']:+,.2f}",
        "chg_pct_display": f"{ix['change_pct']:+.2f}%",
        "chg_color": chg_color,
        "rsi_label": rsi_label,
        "rsi_color": rsi_color,
        "pos52": pos52,
        # 收盘口径基准（06 技术指标、04 的 52 周区间/分位都以它为准）
        "idx_close_display": f"{ref:,.2f}",
        "idx_close_label": ix.get("close_label") or "",
        "snapshot_label": ix.get("bar_label") or "",
        # 盘中触发时最后一根 bar 被剔除过 —— 用来决定要不要提示口径差异
        "intraday_dropped": bool(ix.get("intraday_dropped")),
        "vol_ref_display": fmt_vol(vol_ref) if vol_ref else "N/A",
        "vol_ref_above": bool(avg_vol_20 and vol_ref and vol_ref > avg_vol_20),
        # 指数（^NDX）本身没有成交量，官方接口与 Yahoo 都返回 0。
        # 显示成「0」会像数据出错，统一降级为 N/A。
        "vol_display": fmt_vol(vol) if vol else "N/A",
        "vol_available": bool(vol),
        "macro": macro,
        "macro_available": macro_available,
        "macro_expected": macro_expected,
        "macro_complete": macro_complete,
        "components_source": raw.get("components_source") or "Yahoo Finance",
        "total_score": total_score,
        "total_max": total_max,
        "total_pct": total_pct,
        "overall_label": overall_label,
        "overall_tone": overall_pct_label,
        "verdict_rows": [
            {"label": "趋势判断", "text": trend_detail, "tone": trend_tone},
            {"label": "动能分析", "text": rsi_detail,
             "tone": "watch" if not rsi or 30 <= rsi <= 70 else ("up" if rsi > 70 else "down")},
            {"label": "量能分析", "text": vol_detail, "tone": "up" if ref_up else "down"},
            {"label": "位置分析", "text": pos52_detail,
             "tone": "up" if pos52 > 85 else ("down" if pos52 < 15 else "watch")},
            {"label": "市场广度", "text": breadth_detail,
             "tone": "up" if breadth_pct > 60 else ("down" if breadth_pct < 40 else "watch")},
        ],
        "qqq_alert": qqq_alert,
        "gainers": gainers,
        "losers": losers,
        "most_active": most_active,
        "breadth": {"up": up_c, "down": dn_c, "total": total_c, "pct": breadth_pct},
    }


# ─── 来源 2：全球回撤雷达的派生结论 ──────────────────────────────────────────

def derive_radar(raw: dict, convention: str = "cn") -> dict:
    """输入 collect_drawdown_radar() 的原始结果，输出模板需要的全部内容。"""
    metrics: list[dict] = raw["metrics"]
    alerts: list[dict] = raw["alerts"]
    errors: dict = raw.get("errors") or {}

    deep_count = sum(1 for m in metrics
                     if m.get("dd_historical") is not None and abs(m["dd_historical"]) >= 0.30)
    historic_count = sum(1 for m in metrics
                         if m.get("dd_historical") is not None and abs(m["dd_historical"]) >= 0.40)
    normal_count = sum(1 for m in metrics
                       if m.get("dd_historical") is not None and abs(m["dd_historical"]) < 0.20)

    worst_daily = sorted([m for m in metrics if m.get("daily_change_pct") is not None],
                         key=lambda m: m["daily_change_pct"])[:3]
    worst_dd = sorted([m for m in metrics if m.get("dd_historical") is not None],
                      key=lambda m: m["dd_historical"])[:3]

    rows = []
    for m in metrics:
        chg = m.get("daily_change_pct")
        status_label, status_color, status_emoji = _status_of(m.get("dd_historical"))
        rows.append({
            **m,
            "price_display": price(m.get("current_price")),
            "change_display": pct(chg, 2, signed=True),
            "change_color": change_color(chg, convention),
            "dd_52w_display": pct(m.get("dd_52w"), 1, signed=True),
            "max_dd_52w_display": pct(m.get("max_dd_52w"), 1),
            "dd_5y_display": pct(m.get("dd_5y"), 1, signed=True),
            "dd_historical_display": pct(m.get("dd_historical"), 1, signed=True),
            "max_dd_historical_display": pct(m.get("max_dd_historical"), 1),
            "dd_percentile_display": (f"{m['dd_percentile']:.0f}%"
                                      if m.get("dd_percentile") is not None else "N/A"),
            "vol_20d_display": pct(m.get("vol_20d"), 1) if m.get("vol_20d") is not None else "N/A",
            "dist_from_ath_display": pct(m.get("dist_from_ath"), 1, signed=True),
            "days_display": (f"{m['days_since_ath']}天"
                             if m.get("days_since_ath") is not None else "N/A"),
            "status_label": status_label,
            "status_color": status_color,
            "status_emoji": status_emoji,
        })

    return {
        "rows": rows,
        "alerts": alerts,
        "errors": errors,
        "data_source": raw.get("data_source") or "Yahoo Finance",
        "summary": {
            "total": len(metrics),
            "deep_count": deep_count,
            "historic_count": historic_count,
            "normal_count": normal_count,
            "worst_daily": worst_daily,
            "worst_dd": worst_dd,
        },
        "has_alert": bool(alerts),
        "has_historic": historic_count > 0,
        # 价格/涨跌是盘面快照（09 区块），回撤与分位是收盘口径（10/11 区块）
        "basis_label": raw.get("basis_label") or "",
        "snapshot_label": raw.get("snapshot_label") or "",
        "intraday_dropped": bool(raw.get("intraday_dropped")),
    }


def _status_of(dd_historical: float | None) -> tuple[str, str, str]:
    """回撤状态（与 global-drawdown-radar/src/config.py 的 get_drawdown_status 保持一致）。"""
    if dd_historical is None:
        return ("N/A", "#9ca3af", "⚪")
    d = abs(dd_historical)
    if d >= 0.40:
        return ("历史级回撤", "#dc2626", "🔴")
    if d >= 0.30:
        return ("深度回撤", "#ea580c", "🟠")
    if d >= 0.20:
        return ("观察", "#f59e0b", "🟡")
    return ("正常", "#16a34a", "🟢")


# ─── 场内价日期标注（03 与 13 两个溢价区块共用）──────────────────────────────

def quote_stamp(quote_time: str, nav_date: str | None = None) -> dict:
    """把腾讯行情时间戳（如 20260930161457）翻成给人看的日期标注。

    溢价是「两条腿」算出来的：场内价 + 净值基准。只写一个「溢价 X%」
    而不写这两条腿各自是哪天的，读者会把滞后的价当成当日的价 ——
    场内价与净值基准经常不同日（净值 T-1 披露；长假期间场内价还会
    停在节前最后一个交易日），这两种情况看起来都像「异常溢价」。

    返回 dict：
      quote_time / quote_date / quote_hm  原始戳 / 「09-30」/「16:14」
      price_label   「09-30 16:14 行情快照」
      stale         价格日 ≠ 净值基准日
      gap_days      价格日距今天数（解析失败为 None）
      note          需要提示时才非空（两句用「；」连接）
    """
    out = {
        "quote_time": quote_time or "",
        "quote_date": "",
        "quote_hm": "",
        "price_label": "未知",
        "stale": False,
        "gap_days": None,
        "note": "",
    }
    qt = quote_time or ""
    if len(qt) >= 8 and qt[:8].isdigit():
        out["quote_date"] = f"{qt[4:6]}-{qt[6:8]}"
        if len(qt) >= 12 and qt[8:12].isdigit():
            out["quote_hm"] = f"{qt[8:10]}:{qt[10:12]}"
        out["price_label"] = (f"{out['quote_date']} {out['quote_hm']} 行情快照"
                              if out["quote_hm"] else f"{out['quote_date']} 行情快照")

    nav_short = nav_date[5:10] if len(nav_date or "") >= 10 else ""
    out["stale"] = bool(out["quote_date"] and nav_short
                        and out["quote_date"] != nav_short)

    bits = []
    if out["stale"]:
        bits.append(f"场内价与净值基准不同日（{out['quote_date']} vs {nav_date}），"
                    "通常是该日净值尚未披露")
    if len(qt) >= 8 and qt[:8].isdigit():
        try:
            from datetime import date as _date
            out["gap_days"] = (_date.today()
                               - _date(int(qt[:4]), int(qt[4:6]), int(qt[6:8]))).days
        except ValueError:
            out["gap_days"] = None
    if out["gap_days"] is not None and out["gap_days"] >= 3:
        bits.append(f"场内价停在上一个交易日 {out['quote_date']}"
                    f"（距今 {out['gap_days']} 天，休市期间无新成交），"
                    "溢价读的是该日收盘水平")
    out["note"] = "；".join(bits)
    return out


# ─── 来源 1：ETF 溢价与加仓信号展示 ──────────────────────────────────────────

def derive_etf_monitor(raw: dict, convention: str = "cn") -> dict:
    dd = raw["dd"]
    ranked = raw["ranked"]
    signal = raw["signal"]

    for e in ranked:
        e["premium_display"] = (f"{e['premium']:+.2f}%" if e.get("premium") is not None else "—")
        e["price_display"] = price(e.get("price"), 3)
        e["amount_display"] = money_cn(e.get("amount"))
        e["change_display"] = pct_from_percent(e.get("change_pct"), 2, signed=True)
        e["change_color"] = change_color(e.get("change_pct"), convention)

    best = signal.get("best_etf")
    # ranked 已按溢价升序排列，ranked[0] 就是全场最低溢价的那只。
    # 注意与 `best` 的区别：best 只在「溢价 ≤ 可接受上限」时才非空，
    # 因此当全场溢价都超标时 best 为 None。若顶部卡片直接显示 best，
    # 就会变成一个「—」，看起来像取数失败，实际是「贵得不能买」。
    lowest = ranked[0] if ranked else None

    # 回撤的数据基准日。回撤只能用「已收盘」的日线算（这是它的定义决定的，
    # 盘中价会让回撤等级/分位每分钟乱跳），而页头那个指数是实时的 ——
    # 两者在美股盘中本来就会差一个交易日，所以这里把基准日显式写出来。
    last_date = raw.get("ndx_last_date") or ""
    last_close = raw.get("ndx_last_close")
    if last_date:
        as_of_short = f"{last_date[5:]} 收盘"
        as_of_full = (f"{last_date[5:]} 收盘 {last_close:,.2f}"
                      if last_close is not None else as_of_short)
    else:
        as_of_short = as_of_full = "—"

    # 场内价与净值的日期。两者经常不同日：QDII 净值按 T-1 披露，
    # 长假期间场内价还会整体停在节前最后一个交易日（此时这封邮件
    # 里 04 区块的纳指却已经走到假期后的最新收盘 —— 不标日期会
    # 被读成「拿今天的价配前天的净值，所以溢价 10%+」）。
    qtimes = sorted({e.get("quote_time") for e in ranked if e.get("quote_time")})
    nav_dates = sorted({e.get("nav_date") for e in ranked if e.get("nav_date")})
    nav_date = nav_dates[-1] if nav_dates else ""
    stamp = quote_stamp(qtimes[-1] if qtimes else "", nav_date)

    return {
        "dd": dd,
        "ranked": ranked,
        "recommendations": raw["recommendations"],
        "signal": signal,
        "best": best,
        "best_premium_display": (f"{best['premium']:+.2f}%" if best else "—"),
        "best_label": (f"{best['code']} {best['short_name']}" if best else "—"),
        # 场内实际最低溢价（不看阈值），用于顶部卡片与文案
        "lowest": lowest,
        "lowest_premium_display": (f"{lowest['premium']:+.2f}%" if lowest else "—"),
        "lowest_label": (f"{lowest['code']} {lowest['short_name']}" if lowest else "—"),
        "lowest_emoji": (lowest["premium_level"]["emoji"] if lowest else "—"),
        "lowest_premium": (lowest.get("premium") if lowest else None),
        "ndx_source": raw["ndx_source"],
        "ndx_count": raw["ndx_count"],
        "ndx_last_date": last_date,
        "dd_as_of_short": as_of_short,      # 「09-24 收盘」——卡片副标题用
        "dd_as_of_full": as_of_full,        # 「09-24 收盘 30,478.86」——明细行用
        "data_status": raw["data_status"],
        "basis_label": raw["basis_label"],
        "quote_date": stamp["quote_date"],
        "price_label": stamp["price_label"],
        "nav_date": nav_date,
        "price_note": stamp["note"],
        "failed_etfs": raw["failed_etfs"],
    }


# ─── 来源 4：黄金 / 上海金 ETF 溢价（13 区块）───────────────────────────────

def derive_gold(raw: dict, convention: str = "cn") -> dict:
    """格式化 + 结论。raw 来自 src/providers/gold_etf.build()。"""
    ranked = raw["ranked"]
    for e in ranked:
        e["fee_display"] = (f"{e['fee_total']:.2f}%"
                            if e.get("fee_total") is not None else "—")
        e["te_display"] = (f"{e['te']:.3f}%"
                           if e.get("te") is not None else "—")
        # 溢价率照列，方便核对有没有异常个券。常态 ±0.05% 是估值时点差带来的
        # 噪声，超过 0.10% 才值得看一眼 —— 也只是「看一眼」，不代表能套利，
        # 也不参与排序（排序按费率）。
        e["premium_display"] = (f"{e['premium']:+.3f}%"
                                if e.get("premium") is not None else "—")
        e["premium_alert"] = bool(e.get("premium") is not None
                                  and abs(e["premium"]) > 0.10)
        # 费率档位（表格着色用）：≤0.20 低 / ≤0.30 中 / 其余高
        ft = e.get("fee_total")
        e["fee_level"] = ("low" if ft is not None and ft <= 0.20 else
                          "mid" if ft is not None and ft <= 0.30 else
                          ("high" if ft is not None else None))
        # 跟踪误差是否明显偏离同类。黄金 ETF 常态在 0.02%~0.06%，
        # 超过 0.08% 就已经是同类数倍，值得单独标出来。
        e["te_alert"] = bool(e.get("te") is not None and e["te"] > 0.08)
        e["price_display"] = price(e.get("price"), 3)
        e["amount_display"] = money_cn(e.get("amount"))
        e["change_display"] = pct_from_percent(e.get("change_pct"), 2, signed=True)
        e["change_color"] = change_color(e.get("change_pct"), convention)
        e["spread_display"] = (f"{e['spread']:.3f}%" if e.get("spread") is not None else "—")
        # 距历史最高收盘的回撤（负值）。费率说「长期持有谁更省」，回撤说
        # 「现在的位置高不高」—— 两件事要分开看。
        e["dd_display"] = (f"{e['dd_from_high']:+.2f}%"
                           if e.get("dd_from_high") is not None else "—")
        e["dd_color"] = change_color(e.get("dd_from_high"), convention)
        e["high_date_short"] = (e["hist_high_date"][5:]
                               if e.get("hist_high_date") else "")

    def _best(pool):
        """组内「费率最低 + 流动性达标」的那只；同费率取成交额大的（买得动）。"""
        qualified = [r for r in pool
                     if r.get("fee_total") is not None and not r["low_liquidity"]]
        if not qualified:
            return None
        return min(qualified, key=lambda r: (r["fee_total"],
                                             -(r.get("amount") or 0)))

    sh_best = _best(raw["sh"])
    au_best = _best(raw["au"])
    cheapest = ranked[0] if ranked else None

    # 一句话结论：黄金 ETF 之间没有可套利的折溢价，真正决定长期收益的是
    # 「费率」这个确定性成本，其次是「跟不跟得住」。
    fee_vals = sorted({r["fee_total"] for r in ranked
                       if r.get("fee_total") is not None})
    te_vals = [r["te"] for r in ranked if r.get("te") is not None]

    if len(fee_vals) >= 2:
        n_low = sum(1 for r in ranked if r.get("fee_total") == fee_vals[0])
        n_high = sum(1 for r in ranked if r.get("fee_total") == fee_vals[-1])
        verdict = (f"费率分 {len(fee_vals)} 档：最低 {fee_vals[0]:.2f}%（{n_low} 只）"
                   f"到最高 {fee_vals[-1]:.2f}%（{n_high} 只），每年差 "
                   f"{fee_vals[-1] - fee_vals[0]:.2f} 个百分点 —— 这是持有成本上的"
                   "确定性差距，比折溢价能给你的空间大一个量级")
    elif fee_vals:
        verdict = f"全部 {len(ranked)} 只费率一致（{fee_vals[0]:.2f}%），选哪只看流动性与跟踪误差"
    else:
        verdict = "费率未取到，本次无法比较持有成本"

    if te_vals:
        te_med = sorted(te_vals)[len(te_vals) // 2]
        worst = max((r for r in ranked if r.get("te") is not None),
                    key=lambda r: r["te"])
        if worst["te"] > te_med * 2.5:
            verdict += (f"。跟踪误差中位 {te_med:.3f}%，而 {worst['code']} "
                        f"{worst['short_name']} 达 {worst['te']:.3f}%（同类数倍）"
                        f"且成交额最低，不建议")
        else:
            verdict += (f"。跟踪误差全部落在 {min(te_vals):.3f}%~{max(te_vals):.3f}%，"
                        "跟得都很紧，这一项不构成区分")

    # 折溢价：照实把区间摆出来，同时讲清它是噪声 —— 否则读者会拿它去挑
    # 「哪只便宜」，而它量的是当天尾盘 30 分钟的金价方向。
    prem = [r["premium"] for r in ranked if r.get("premium") is not None]
    premium_note = raw.get("premium_basis", "")
    if prem:
        n_pos = sum(1 for p in prem if p > 0)
        verdict += (f"。本次 {len(prem)} 只的折溢价落在 {min(prem):+.3f}%~"
                    f"{max(prem):+.3f}%（{n_pos} 只为正），极差只有 "
                    f"{max(prem) - min(prem):.3f} 个百分点 —— 净值估值时点与"
                    "场内收盘相差 30 分钟，这列主要还是噪声，不是可套利空间；"
                    "挑哪只便宜请以费率与跟踪误差为准")
    else:
        # 宁可留空也不给伪数：净值披露滞后时用旧净值比当日的价，算出来的是
        # 期间的金价涨跌幅（今天实测 −1.8% 全 14 只同向），会被读成「全场折价」。
        verdict += (f"。折溢价本次留空（显示「—」）：净值只披露到 "
                    f"{raw.get('nav_date') or '未知日期'}，而场内价是 "
                    f"{raw.get('quote_date') or '未知日期'}，两者不同日，且拿不到"
                    "两端金价日线做校准 —— 与其给一个由期间涨跌冒充的假折价，"
                    "不如如实留空")

    # 价格位置：距历史最高收盘的回撤。
    # 与费率是两件独立的事 —— 费率说「长期持有谁更省」，回撤说
    # 「这份资产自己离最高点还差多远」。费率再低也挡不住 -28% 的位置。
    # 高点日取「日线最长的那只」：它经历过完整周期，最能代表金价本身；
    # 新上市标的窗口太短，高点会失真。
    dds = sorted(r["dd_from_high"] for r in ranked
                 if r.get("dd_from_high") is not None)
    dd_median = (dds[len(dds) // 2] if len(dds) % 2
                 else (dds[len(dds) // 2 - 1] + dds[len(dds) // 2]) / 2) if dds else None
    longest = max((r for r in ranked if r.get("hist_bars")),
                  key=lambda r: r["hist_bars"], default=None)
    dd_high_date = longest["hist_high_date"] if longest else ""
    dd_span = (f"{dds[0]:.1f}% ~ {dds[-1]:.1f}%" if len(dds) > 1
               else (f"{dds[0]:.1f}%" if dds else "—"))
    if dd_median is not None:
        verdict += (f"。价格位置上，{len(dds)} 只距历史最高收盘的回撤在 {dd_span}"
                    f"（中位 {dd_median:.1f}%，高点 {dd_high_date}）——"
                    "回撤看的是「位置高不高」，费率看的是「持有贵不贵」，两者要分开判断")

    # 场内价的那一天（简报 06:45 跑时是上一交易日收盘，长假后会差好几天）。
    # 两条腿的日期都要显式说出来，否则滞后的价会被读成「今天的价」、
    # 常态溢价会被读成「异常溢价」。
    stamp = quote_stamp(raw.get("quote_time", ""), raw.get("nav_date") or "")
    price_label = stamp["price_label"]
    price_note = stamp["note"]

    return {
        "ranked": ranked,
        "sh": raw["sh"],
        "au": raw["au"],
        "sh_count": raw["sh_count"],
        "au_count": raw["au_count"],
        "sh_best": sh_best,
        "au_best": au_best,
        "cheapest": cheapest,
        "cheapest_display": (f"{cheapest['fee_total']:.2f}%" if cheapest else "—"),
        "cheapest_label": (f"{cheapest['code']} {cheapest['short_name']}"
                           if cheapest else "—"),
        "nav_date": raw["nav_date"],
        "quote_date": raw.get("quote_date", ""),
        "premium_note": premium_note,
        "premium_missing": raw.get("premium_missing", 0),
        "price_label": price_label,
        "stale": raw.get("stale", False),
        "stale_note": price_note,
        "price_note": price_note,
        "te_label": raw.get("te_label", ""),
        "te_window": raw.get("te_window", 0),
        "te_bench_note": raw.get("te_bench_note", ""),
        "fee_cached_count": raw.get("fee_cached", 0),
        "te_missing_count": raw.get("te_missing", 0),
        "fee_range": (f"{fee_vals[0]:.2f}% ~ {fee_vals[-1]:.2f}%"
                      if len(fee_vals) >= 2 else
                      (f"{fee_vals[0]:.2f}%" if fee_vals else "—")),
        "te_range": (f"{min(te_vals):.3f}% ~ {max(te_vals):.3f}%"
                     if te_vals else "—"),
        "data_status": raw["data_status"],
        "failed": raw["failed"],
        # 距历史最高收盘的回撤（组级）
        "dd_median": dd_median,
        "dd_median_display": (f"{dd_median:.1f}%" if dd_median is not None else "—"),
        "dd_high_date": dd_high_date,
        "dd_high_date_short": dd_high_date[5:] if dd_high_date else "",
        "dd_span": dd_span,
        "dd_count": len(dds),
        # 指标卡上的进度条：回撤越深条越长（绝对值，卡在 100 以内）
        "dd_bar": (min(abs(dd_median), 100.0) if dd_median is not None else None),
        "dd_as_of": (longest["hist_last_date"][5:] if longest else ""),
        # 取数失败回落缓存的只数（>0 时口径行要说明，否则各行的截至日不一致）
        "dd_cached_count": raw.get("hist_cached", 0),
        "verdict": verdict,
    }
