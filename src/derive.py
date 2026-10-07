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
        "failed_etfs": raw["failed_etfs"],
    }


# ─── 来源 4：黄金 / 上海金 ETF 溢价（13 区块）───────────────────────────────

def derive_gold(raw: dict, convention: str = "cn") -> dict:
    """格式化 + 结论。raw 来自 src/providers/gold_etf.build()。"""
    ranked = raw["ranked"]
    for e in ranked:
        e["premium_display"] = (f"{e['premium']:+.3f}%" if e.get("premium") is not None else "—")
        e["price_display"] = price(e.get("price"), 3)
        e["amount_display"] = money_cn(e.get("amount"))
        e["change_display"] = pct_from_percent(e.get("change_pct"), 2, signed=True)
        e["change_color"] = change_color(e.get("change_pct"), convention)
        e["spread_display"] = (f"{e['spread']:.3f}%" if e.get("spread") is not None else "—")

    def _best(pool):
        """组内「溢价 ≤ 上限 里成交额最大」的那只 —— 便宜又好买。"""
        qualified = [r for r in pool
                     if r.get("premium") is not None
                     and r["premium"] <= raw["premium_ok_max"]
                     and not r["low_liquidity"]]
        return max(qualified, key=lambda r: r.get("amount") or 0) if qualified else None

    sh_best = _best(raw["sh"])
    au_best = _best(raw["au"])
    lowest = ranked[0] if ranked else None

    # 一句话结论：黄金 ETF 溢价常年贴着 0，真正要说的是「有没有异常」和「选哪只」
    abnormal = [r for r in ranked if r["premium"] > 1.0]
    if abnormal:
        verdict = (f"有 {len(abnormal)} 只溢价超过 1%（"
                   + "、".join(f"{r['code']} {r['premium']:.2f}%" for r in abnormal[:3])
                   + "），溢价回落风险大于金价本身波动")
    else:
        verdict = (f"全部 {len(ranked)} 只溢价都在 ±0.2% 以内，套利充分，"
                   f"选哪只主要看流动性而不是溢价")

    return {
        "ranked": ranked,
        "sh": raw["sh"],
        "au": raw["au"],
        "sh_count": raw["sh_count"],
        "au_count": raw["au_count"],
        "sh_best": sh_best,
        "au_best": au_best,
        "lowest": lowest,
        "lowest_display": (f"{lowest['premium']:+.3f}%" if lowest else "—"),
        "lowest_label": (f"{lowest['code']} {lowest['short_name']}" if lowest else "—"),
        "nav_date": raw["nav_date"],
        "basis_label": raw["basis_label"],
        "data_status": raw["data_status"],
        "failed": raw["failed"],
        "premium_ok_max": raw["premium_ok_max"],
        "verdict": verdict,
    }
