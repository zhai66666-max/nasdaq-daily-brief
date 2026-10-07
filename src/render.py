"""渲染层：把三个来源的结果 + 派生结论拼成模板上下文，渲染成 HTML。"""
from __future__ import annotations

import logging
from datetime import datetime

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from src import ai as ai_mod
from src import pipeline
from src.derive import (
    change_color, derive_etf_monitor, derive_gold, derive_nasdaq100, derive_radar,
    fmt_vol, money_cn, pct, pct_from_percent, price, updown_colors,
)
from src.paths import TEMPLATES_DIR
from src.settings import load_display

logger = logging.getLogger(__name__)

WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def build_env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
        undefined=StrictUndefined,
    )
    env.filters.update({
        "pct": pct,
        "pctp": pct_from_percent,
        "price": price,
        "vol": fmt_vol,
        "money": money_cn,
    })
    return env


def build_context(results: dict[str, pipeline.SourceResult],
                  ai_out: dict | None = None) -> dict:
    """三个来源的结果 → 单一大上下文。"""
    display = load_display()
    convention = display.get("color_convention", "cn")
    thr = display.get("thresholds", {})
    now = pipeline.beijing_now()

    em = results.get("etf_monitor")
    rd = results.get("drawdown_radar")
    nq = results.get("nasdaq100")
    gd = results.get("gold_etf")

    em_data = derive_etf_monitor(em.data, convention) if (em and em.ok) else None
    rd_data = derive_radar(rd.data, convention) if (rd and rd.ok) else None
    nq_data = derive_nasdaq100(nq.data, convention, thr) if (nq and nq.ok) else None
    gd_data = derive_gold(gd.data, convention) if (gd and gd.ok) else None

    # ── 顶部核心摘要
    # 先填默认值：某个来源失败时模板仍能渲染完整结构，只是对应位置显示 —，
    # 而不是整封邮件因缺字段报错。
    summary: dict = {
        "ndx_price": "—", "ndx_change": "—", "ndx_change_pct": "—",
        "ndx_change_color": change_color(None, convention), "ndx_up": True,
        "ndx_bar_label": "",
        # 收盘口径：页头/04 的盘面快照是实时值，而 52 周区间与分位、均线、
        # RSI、20 日均量以及回撤全部按「已收盘」那根日线算。两个基准都要
        # 在邮件里标出来，否则同一封邮件里两个数字会互相矛盾。
        "indicator_label": "",
        "indicator_dropped": False,
        "hist_dd": None, "hist_dd_display": "—", "hist_percentile": None,
        "hist_level": "—", "hist_emoji": "⚪", "hist_max_dd": None,
        "dd_as_of_short": "", "dd_as_of_full": "",
        "best_etf_label": "—", "best_premium_display": "—", "best_emoji": "⚪",
        "best_lowest_premium": None, "best_qualified": False,
        "premium_ok": False, "add_satisfied": False, "matrix_signal": None,
        "qqq_alert": None,
        "global_deep": 0, "global_historic": 0, "global_normal": 0,
        "global_total": 0, "global_alerts": 0,
    }
    if nq_data:
        ix = nq_data["ix"]
        summary.update({
            "ndx_price": nq_data["cur_display"],
            "ndx_change": nq_data["chg_display"],
            "ndx_change_pct": nq_data["chg_pct_display"],
            "ndx_change_color": nq_data["chg_color"],
            "ndx_up": ix["change"] >= 0,
            "ndx_bar_label": nq_data.get("bar_label") or "",
            "indicator_label": nq_data.get("idx_close_label") or "",
            "indicator_dropped": bool(nq_data.get("intraday_dropped")),
        })
    if em_data:
        dd = em_data["dd"]
        summary.update({
            "hist_dd": dd["current_drawdown"],
            "hist_dd_display": f"{dd['current_drawdown']:.2f}%",
            "hist_percentile": dd["percentile"],
            "hist_level": dd["level"]["label"],
            "hist_emoji": dd["level"]["emoji"],
            "hist_max_dd": dd["max_dd"]["max_drawdown"],
            # 回撤的基准日必须显式展示：它固定是「已收盘」的日线，
            # 而页头是实时价，美股盘中两者天然差一个交易日。
            "dd_as_of_short": em_data.get("dd_as_of_short") or "",
            "dd_as_of_full": em_data.get("dd_as_of_full") or "",
            # 顶部卡片展示「场内实际最低溢价」而非「溢价达标的那只」，
            # 否则全场溢价都超标时卡片会显示「—」，看着像取数失败。
            # 是否达标由 premium_ok 单独表达，用配色区分。
            "best_etf_label": em_data["lowest_label"],
            "best_premium_display": em_data["lowest_premium_display"],
            "best_emoji": em_data["lowest_emoji"],
            "best_lowest_premium": em_data["lowest_premium"],
            "best_qualified": bool(em_data["best"]),
            "premium_ok": bool(em_data["signal"].get("premium_ok")),
            "add_satisfied": bool(em_data["signal"].get("satisfied")),
            "matrix_signal": (em_data["signal"]["matrix"]["signal"]
                              if em_data["signal"].get("matrix") else None),
        })
    if nq_data:
        summary["qqq_alert"] = nq_data["qqq_alert"]
    if rd_data:
        summary.update({
            "global_deep": rd_data["summary"]["deep_count"],
            "global_historic": rd_data["summary"]["historic_count"],
            "global_normal": rd_data["summary"]["normal_count"],
            "global_total": rd_data["summary"]["total"],
            "global_alerts": len(rd_data["alerts"]),
        })

    # ── 今日一句话（纯数据拼装，非 AI 生成，事实可核对）
    bits = []
    if nq_data:
        bar = nq_data.get("bar_label") or ""
        phase = nq_data.get("bar_phase") or ""
        # 盘中不能说「收于」——那个数字是实时价，还没收盘。
        verb = "现报" if phase == "盘中" else "收于"
        tail = f"，{bar}" if bar else ""
        bits.append(f"纳指100 {verb} {nq_data['cur_display']}"
                    f"（{nq_data['chg_pct_display']}{tail}）")
    if em_data:
        dd_basis = ""
        if em_data.get("ndx_last_date"):
            dd_basis = f"（截至 {em_data['dd_as_of_short']}）"
        bits.append(f"当前历史回撤 {em_data['dd']['current_drawdown']:.2f}%"
                    f"、处于历史 {em_data['dd']['percentile']:.0f}% 分位{dd_basis}")
        if em_data["lowest"]:
            qual = ("已达可买区间" if em_data["best"]
                    else f"高于 {thr.get('etf_premium_ok', 2.0)}% 上限")
            bits.append(f"国内纳指ETF 最低溢价 {em_data['lowest_premium_display']}"
                        f"（{em_data['lowest_label']}，{qual}）")
    if rd_data:
        bits.append(f"全球监控的 {rd_data['summary']['total']} 个资产中有 "
                    f"{rd_data['summary']['deep_count']} 个处于深度回撤")
    headline = "；".join(bits) + "。" if bits else ""

    # ── 目录
    toc = []
    if em_data:
        toc += [("act", "01 加仓决策"), ("dd", "02 纳指历史回撤统计"),
                ("etf", "03 国内纳指 ETF 溢价排名")]
    if nq_data:
        toc += [("ov", "04 纳指市场概况"), ("macro", "05 宏观指标仪表盘"),
                ("tech", "06 技术指标"), ("verdict", "07 综合研判"),
                ("movers", "08 成分股涨跌榜")]
    if rd_data:
        toc += [("gtable", "09 全球市场横向对比"), ("gwatch", "10 全球重点观察"),
                ("galert", "11 全球回撤新信号")]
    if ai_out and (ai_out.get("analysis_sections") or ai_out.get("commentary")):
        toc.append(("ai", "12 AI 深度分析"))
    if gd_data:
        toc.append(("gold", "13 黄金 ETF 溢价排名"))

    return {
        "brand": display["brand"],
        "theme": display["theme"],
        "severity": display["severity"],
        "sections": display["sections"],
        "color_convention": convention,
        "ud": updown_colors(convention),
        "thresholds": thr,
        "meta": {
            "date": now.strftime("%Y-%m-%d"),
            "date_cn": f"{now.year} 年 {now.month} 月 {now.day} 日",
            "weekday": WEEKDAY_CN[now.weekday()],
            "run_ts": now.strftime("%Y-%m-%d %H:%M"),
            "generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S%z"),
        },
        "summary": summary,
        "headline": headline,
        "toc": toc,
        "etf_monitor": em_data,
        "radar": rd_data,
        "nasdaq100": nq_data,
        "gold": gd_data,
        "ai": ai_out or {"analysis_sections": [], "commentary": None},
        "sources": [
            {"key": r.key, "label": r.label, "ok": r.ok,
             "elapsed": round(r.elapsed, 1), "error": r.error}
            for r in results.values()
        ],
        "errors": [{"label": r.label, "error": r.error}
                   for r in results.values() if not r.ok],
    }


def render(context: dict) -> str:
    env = build_env()
    tpl = env.get_template("brief.html")
    return tpl.render(**context)
