from __future__ import annotations
"""
Global Market Drawdown Radar — Configuration
All constants, ETF definitions, thresholds, and paths in one place.
"""

from pathlib import Path

from src.paths import RADAR_STATE_FILE, TEMPLATES_DIR

# ─── Paths ────────────────────────────────────────────────────────────────────
# 合并后统一由 src/paths.py 推导，避免多层 parent 计数出错
ROOT_DIR = Path(__file__).resolve().parents[3]
STATE_FILE = RADAR_STATE_FILE
TEMPLATE_DIR = TEMPLATES_DIR

# ─── ETFs: ticker, display name, market, chinese name ──────────────────────────
# Fixed display order as specified
ETFS = [
    {"ticker": "QQQM", "name_cn": "纳斯达克100", "market": "美国科技股"},
    {"ticker": "SPY",  "name_cn": "标普500",     "market": "美国大盘"},
    {"ticker": "EWJ",  "name_cn": "日本",         "market": "日本"},
    {"ticker": "EWY",  "name_cn": "韩国",         "market": "韩国"},
    {"ticker": "INDA", "name_cn": "印度",         "market": "印度"},
    {"ticker": "EWT",  "name_cn": "中国台湾",     "market": "中国台湾"},
    {"ticker": "EWC",  "name_cn": "加拿大",       "market": "加拿大"},
    {"ticker": "EWW",  "name_cn": "墨西哥",       "market": "墨西哥"},
    {"ticker": "EWA",  "name_cn": "澳大利亚",     "market": "澳大利亚"},
    {"ticker": "EWZ",  "name_cn": "巴西",         "market": "巴西"},
    {"ticker": "GLD",  "name_cn": "黄金",         "market": "黄金"},
]

# ─── 额外资产：黄金现货两个基准（非 ETF，走东财通道）──────────────────────────
# 09 区块在 11 只 ETF 之外并列两个金价基准，用来和 GLD（美股黄金 ETF，美元计价）
# 互相映证：国内看上海金 Au99.99（元/克），国际看伦敦金现 XAU/USD（美元/盎司）。
#
# 它们都不是 ETF，Yahoo 里没有对应代码（**上海金尤其没有**），所以单独走东财：
#   · 118.AU9999  上金所 Au99.99 现货，日线自 2004-01 起
#   · 122.XAU     伦敦金现（东财名「黄金/美元」），日线自 1992-05 起
# 这两个源与 ETF 走的 Yahoo 互不依赖，一边挂了另一边照常出数。
#
# `unit` 会渲染到资产名下（如「上海黄金交易所（元/克）」），因为同一张表里
# 混着「点」「元/克」「美元/盎司」三种量纲，不标单位会被误读。
EXTRA_ASSETS = [
    {"ticker": "Au99.99", "name_cn": "上海黄金交易所", "market": "上海金现货",
     "unit": "元/克", "secid": "118.AU9999"},
    {"ticker": "XAU/USD", "name_cn": "国际现货黄金", "market": "伦敦金现",
     "unit": "美元/盎司", "secid": "122.XAU"},
]

ETF_TICKERS = [e["ticker"] for e in ETFS]          # 仅 yfinance 那 11 只
EXTRA_TICKERS = [e["ticker"] for e in EXTRA_ASSETS]

# 展示顺序 = ETF 在前、黄金现货在后（09 区块的排序与 10 区块的「最深回撤」都按它）
ALL_ASSETS = ETFS + EXTRA_ASSETS
ETF_LOOKUP = {e["ticker"]: e for e in ALL_ASSETS}

# ─── Data windows ─────────────────────────────────────────────────────────────
TRADING_DAYS_52W = 252         # ~1 calendar year of trading days
TRADING_DAYS_5Y  = 252 * 5    # ~5 years of trading days (~1260)
VOLATILITY_WINDOW = 20          # 20 trading days for volatility

# ─── Drawdown thresholds (as positive fractions) ──────────────────────────────
# 0.20 = -20% drawdown, 0.30 = -30%, 0.40 = -40%
THRESHOLDS = [0.20, 0.30, 0.40]
ALERT_COOLDOWN_DAYS = 7  # Don't re-alert for same ETF+threshold within N days

# ─── Drawdown status labels ───────────────────────────────────────────────────
def get_drawdown_status(historical_dd: float | None) -> tuple[str, str, str]:
    """
    Returns (label, color, emoji) based on historical drawdown.
    historical_dd is a negative fraction, e.g. -0.25 = -25%.
    """
    if historical_dd is None:
        return ("N/A", "#9ca3af", "⚪")
    dd = abs(historical_dd)  # work with positive values
    if dd >= 0.40:
        return ("历史级回撤", "#ef4444", "🔴")
    elif dd >= 0.30:
        return ("深度回撤", "#f97316", "🟠")
    elif dd >= 0.20:
        return ("观察", "#f59e0b", "🟡")
    else:
        return ("正常", "#22c55e", "🟢")

# ─── Email / AI 配置 ──────────────────────────────────────────────────────────
# 合并前这里自己读了一遍邮件与 DeepSeek 的环境变量，但合并后
# **所有邮件配置的唯一入口是 `src/settings.py`**，本模块的这几个常量已无任何引用。
#
# 之所以删掉而不是留着：其中一行是
#     SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
# 模块级执行，且 os.environ.get 的默认值只在 **key 不存在** 时生效。
# GitHub Actions 里 `${{ secrets.X }}` 未配置时会注入 **空字符串**（key 存在、值为空），
# 于是 int("") 直接抛 ValueError —— 而这是模块级代码，一旦触发，
# 整个「全球回撤雷达」数据源会被上游的异常隔离吞掉，邮件静默少掉三个区块。
# 删掉它比给它打补丁更安全：会崩的死配置不该留在代码里。
#
# 邮件：见 src/settings.py（load_smtp / deepseek_key），兼容 EMAIL_* 与 SENDER_*/SMTP_* 两套命名
DEEPSEEK_API = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_MODEL = "deepseek-v4-flash"
