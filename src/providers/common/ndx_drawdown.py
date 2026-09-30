"""统一口径：所有「纳斯达克100 距历史高点回撤」都从这里取。

为什么需要它
--------------------------------------------------------------------------
邮件里「纳斯达克回撤」原本有两套口径，会打架：
  · 顶部卡 / 02 区块 / 加仓决策引擎 —— 走 ^NDX **指数**（NASDAQ 官方接口），≈ 1.27%
  · 雷达 09 区块 QQQM 行 —— 走 yfinance **复权**收盘价，≈ 1.48%
  · 05 宏观卡 QQQ 行 —— 走 yfinance QQQ 原始收盘，≈ 1.27%

指数与 ETF 的「历史最高收盘价」日期、价位本就不同，复权再把分红累回历史价，
于是同一个「距高点回撤」能差出 0.2 个百分点。对加仓决策毫无意义，只会造成困惑。

本模块把口径收口到 **NDX 指数**（与加仓决策引擎、02 区块同源），所有展示层
（雷达 QQQM 行、QQQ 宏观卡）都来这里取同一个数，邮件里纳斯达克回撤只有一个值。

实现上复用 etf_monitor 已验证的 fetch_nasdaq_history（降级链：NASDAQ 官方 →
yfinance → 缓存 CSV），再用雷达自己的 compute_* 函数算各维度回撤，保证与雷达
其它行的算法完全一致（expanding-window、复权/原始处理一致）。
"""

from __future__ import annotations

import logging

import pandas as pd

logger = logging.getLogger(__name__)


def get_nasdaq_drawdown(use_cache: bool = True) -> dict:
    """返回纳斯达克100（^NDX 指数）距历史高点的全部回撤字段。

    返回字典字段与 drawdown_radar.compute_all_metrics 的单资产结果一一对应，
    可直接覆盖雷达 QQQM 行的对应键；另外多给一个 current_drawdown（负数 fraction），
    供宏观卡覆盖 drawdown_pct 用。

    任何一步失败都往上抛，调用方负责 try/except 回退到原口径。
    """
    from src.providers.etf_monitor.data_fetcher import fetch_nasdaq_history
    from src.providers.drawdown_radar import drawdown as radar_dd
    from src.providers.drawdown_radar.config import get_drawdown_status
    from src.providers.common import bar_basis

    df, src_name = fetch_nasdaq_history(use_cache=use_cache)
    if df is None or df.empty:
        raise RuntimeError("NDX 历史数据为空")

    series = pd.Series(
        df["close"].astype(float).values,
        index=pd.to_datetime(df["date"]),
        name="^NDX",
    ).sort_index()

    # 与雷达一致的「只用已收盘日线」处理（官方源末行本就收盘，通常为 no-op）
    sc = bar_basis.closed_view(series)

    dd_52w = radar_dd.compute_52w_drawdown(sc)
    max_dd_52w = radar_dd.compute_52w_max_drawdown(sc)
    dd_5y = radar_dd.compute_5y_drawdown(sc)
    dd_historical = radar_dd.compute_historical_drawdown(sc)
    max_dd_historical = radar_dd.compute_historical_max_drawdown(sc)
    dd_percentile = radar_dd.compute_drawdown_percentile(sc)
    cycle_max_dd = radar_dd.compute_current_cycle_max_drawdown(sc)
    vol_20d = radar_dd.compute_20d_annualized_volatility(sc)
    dist_from_ath, ath_date, days_since_ath = radar_dd.compute_distance_from_ath(sc)
    status_label, status_color, status_emoji = get_drawdown_status(dd_historical)

    return {
        # 宏观卡覆盖用：当前历史回撤（负数 fraction，如 -0.0127）
        "current_drawdown": dd_historical,
        # 雷达行覆盖用：与 compute_all_metrics 单资产结果同构
        "dd_52w": dd_52w,
        "max_dd_52w": max_dd_52w,
        "dd_5y": dd_5y,
        "dd_historical": dd_historical,
        "max_dd_historical": max_dd_historical,
        "dd_percentile": dd_percentile,
        "cycle_max_dd": cycle_max_dd,
        "vol_20d": vol_20d,
        "dist_from_ath": dist_from_ath,
        "ath_date": ath_date,
        "days_since_ath": days_since_ath,
        "status_label": status_label,
        "status_color": status_color,
        "status_emoji": status_emoji,
        "source": src_name,
    }
