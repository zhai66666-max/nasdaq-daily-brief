"""东方财富历史日线取数 —— 给「Yahoo 里没有对应代码」的标的用。

为什么单独一份
--------------------------------------------------------------------------
09 区块（全球回撤雷达）原先只有 Yahoo 一条通道，11 只全是美股 ETF，够用。
本轮加进来的两个黄金基准（上金所 Au99.99、伦敦金现 XAU/USD）都不是 ETF，
Yahoo 里没有对应代码（**上海金尤其没有**），只能走东财。

这带来一个额外好处：黄金列与 ETF 列的数据源**互不依赖**，一边挂了另一边
照常出数，整块不会一起消失。

取数必须双通道（与 gold_etf.py 是同一个结论，两个环境的失败面正好互补）：
  · 本地沙箱走 HTTP 代理：requests 会被代理掐断，同一条 URL 换 curl 就通；
  · GitHub Actions（海外 IP、无代理）：curl 常取不到，requests 直连可通。
只留一条，就会在其中一边**静默走缓存** —— 缓存不会自己变新，回撤栏会停在
旧快照上还不报错。所以两条都试，谁通用谁。
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from src.paths import DATA_DIR

logger = logging.getLogger(__name__)

# 全历史日线：klt=101 日线，fqt=0 不复权（金价无分红，复权与否等价）
EM_URL = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
          "?secid={secid}&fields1=f1&fields2=f51,f53&klt=101&fqt=0"
          "&beg=19900101&end=20500101")
# 同一路径的 80 端口版本：GitHub Actions（Azure 海外 IP）对东财 443 长期不通，
# 单独留一条 http 通道给云端试 —— 多条通道的成本只是一次请求。
EM_URL_HTTP = EM_URL.replace("https://", "http://")
# 新浪国际期货/现货日线（伦敦金现 XAU、纽约金 GC 等）。海外可达性通常好于东财。
SINA_URL = ("https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20t=/"
            "GlobalFuturesService.getGlobalFuturesDailyKLine?symbol={sym}")
REFERER = "https://quote.eastmoney.com/"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0.0.0 Safari/537.36"),
    "Referer": REFERER,
}

_CACHE_PATH: Path = DATA_DIR / "em_hist_cache.json"
_cache: dict[str, dict] = {}
_cache_dirty: set[str] = set()


# ─── 缓存 ─────────────────────────────────────────────────────────────────────

def _load_cache() -> None:
    global _cache
    if _cache:
        return
    try:
        _cache = json.loads(_CACHE_PATH.read_text("utf-8")) or {}
    except (OSError, json.JSONDecodeError):
        _cache = {}


def save_cache() -> None:
    """把本次取到的新日线落盘（只写变过的标的）。"""
    if not _cache_dirty:
        return
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(json.dumps(_cache, ensure_ascii=False),
                               encoding="utf-8")
    except OSError as exc:                                  # noqa: BLE001
        logger.debug("  [em] 日线缓存写入失败：%s", exc)


# ─── 取数 ─────────────────────────────────────────────────────────────────────

def _get(url: str, *, attempts: int = 2, timeout: int = 25) -> str | None:
    """双通道 GET：curl 优先，失败再走 requests。都拿不到返回 None。"""
    cmd = ["curl", "-s", "--connect-timeout", "10", "--max-time", str(timeout),
           "-H", f"User-Agent: {HEADERS['User-Agent']}",
           "-H", f"Referer: {REFERER}", url]
    for i in range(attempts):
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=timeout + 5)
            if proc.stdout and proc.stdout.strip():
                return proc.stdout
        except (OSError, subprocess.SubprocessError) as exc:   # noqa: BLE001
            logger.debug("  [em] curl 失败 %s: %s", url[:70], exc)
        if i + 1 < attempts:
            time.sleep(0.6)

    for i in range(attempts):
        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout)
            if r.status_code == 200 and (r.text or "").strip():
                logger.debug("  [em] curl 未取到，requests 兜底成功：%s", url[:70])
                return r.text
        except requests.RequestException as exc:               # noqa: BLE001
            logger.debug("  [em] requests 兜底失败 %s: %s", url[:70], exc)
        if i + 1 < attempts:
            time.sleep(0.6)
    return None


def _parse_klines(txt: str) -> list[tuple[str, float]]:
    """东财 kline JSON → [(date, close)]，按日期升序。解析不出来返回 []。"""
    try:
        klines = (json.loads(txt).get("data") or {}).get("klines") or []
    except (json.JSONDecodeError, AttributeError, TypeError):
        return []
    rows: list[tuple[str, float]] = []
    for line in klines:
        parts = str(line).split(",")
        if len(parts) < 2:
            continue
        try:
            rows.append((parts[0], float(parts[1])))
        except ValueError:
            continue
    rows.sort(key=lambda r: r[0])
    return rows


def _parse_sina(txt: str) -> list[tuple[str, float]]:
    """新浪 jsonp → [(date, close)]。

    返回形如 `var t=([{"date":"2006-10-09","close":"577.100",...},...]);`；
    国内期货那条用短键 `d`/`c`，一并兼容。
    """
    m = re.search(r"\((\[.*\])\)", txt, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except (json.JSONDecodeError, TypeError):
        return []
    rows: list[tuple[str, float]] = []
    for it in data if isinstance(data, list) else []:
        if not isinstance(it, dict):
            continue
        d = it.get("date") or it.get("d")
        c = it.get("close") or it.get("c")
        if not d or c in (None, ""):
            continue
        try:
            rows.append((str(d)[:10], float(c)))
        except ValueError:
            continue
    rows.sort(key=lambda r: r[0])
    return rows


def fetch_kline(secid: str, *,
                sina_symbol: str = "") -> tuple[list[tuple[str, float]], str, bool]:
    """按序尝试多个源，返回 ([(date, close)], 命中的源名, 是否回落到缓存)。

    顺序：东财 https → 东财 http → 新浪（若该标的配了代码）→ 本地缓存。
    两个运行环境的可达性不同（本地 curl 通、云端要到 80 或新浪才通），
    所以每一档都试一遍，并把**命中的源名带出去**，邮件页脚据此如实标注。

    缓存是最后一道：取不到时回落到上一次的日线，并返回 used_cache=True，
    调用方据此提示「数据截至 X」，绝不把旧数据冒充当日。
    """
    _load_cache()

    candidates: list[tuple[str, str, object]] = [
        ("东财", EM_URL.format(secid=secid), _parse_klines),
        ("东财(80)", EM_URL_HTTP.format(secid=secid), _parse_klines),
    ]
    if sina_symbol:
        candidates.append(("新浪", SINA_URL.format(sym=sina_symbol), _parse_sina))

    for name, url, parser in candidates:
        txt = _get(url)
        rows = parser(txt) if txt else []
        if rows:
            if name != "东财":
                logger.info("  [radar] %s 走「%s」通道取到 %d 行", secid, name, len(rows))
            _cache[secid] = {"as_of": rows[-1][0],
                             "rows": [[d, c] for d, c in rows]}
            _cache_dirty.add(secid)
            return rows, name, False

    cached = _cache.get(secid) or {}
    rows = [(str(d), float(c)) for d, c in (cached.get("rows") or [])]
    if rows:
        logger.warning("  [radar] %s 各源均取数失败，回落到缓存（截至 %s）",
                       secid, cached.get("as_of"))
        return rows, "缓存", True
    return [], "", False


def drop_unclosed(rows: list[tuple[str, float]]) -> list[tuple[str, float]]:
    """剔除最后一根「还没走完」的日线。

    为什么需要：Au99.99 交易到北京时间 15:30，伦敦金现是 24 小时连续交易，
    两个标的的收盘时点都晚于雷达跑批时点（北京 08:45~09:45）。不剔除的话，
    同一张表里美股各列停在 T-1 收盘、黄金列却多出一根只走了几小时的「今日」
    bar —— 「最新价」的基准日就对不上，回撤与分位还会随盘中行情跳动。

    判据：最后一根的日期 >= 北京今天 ⇒ 这根还在走。
    美股 ETF 那边由 bar_basis 用美东日历判断，两套时区各自成立、结论一致。
    """
    if not rows:
        return rows
    today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    if rows[-1][0] >= today:
        return rows[:-1]
    return rows
