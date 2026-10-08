"""黄金 / 上海金 ETF 溢价排名 —— 简报第 13 区块的数据源。

为什么单独做：03 区块是国内**纳指** ETF 的溢价排名，口径已经稳定跑很久了。
黄金这边的差别只有标的与阈值，公式、排序、降级逻辑全部照抄 etf_monitor，
避免两套实现日后漂移（etf_monitor/etf.py 是唯一口径来源）。

  溢价率 = (场内价格 / 单位净值 - 1) × 100%
  排序：溢价率升序 → 成交额降序 → 买卖价差升序

两个必须注意的点：
  1) 分类不能看简称。518600「金ETF广发」的全称是「广发**上海金**交易型开放式
     证券投资基金」，跟踪 SHAU；名字里带「黄金」的才多是 Au99.99。判断一律走
     FundMNDetailInformation 的 INDEXCODE / FULLNAME。
  2) 阈值不能沿用纳指那套 2%。黄金 ETF 溢价常年只有 ±0.2%，用 2% 分档会把
     14 只全塞进同一档，等于没有分档。
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import time
import urllib.parse
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
ETF_PREFIX = ("51", "52", "56", "58", "159", "150")
LOW_LIQUIDITY = 10_000_000      # 成交额 < 1000 万 → 低流动性（与 03 区块一致）

# 分类兜底表（2026-10 实测）。跟踪标的接口全部失联时用这张表，
# 保证「上海金 / 黄金」不会退化成清一色「黄金」。新增标的不在表内 → 默认黄金。
STATIC_CATEGORY = {
    # 上海金（跟踪 SHAU / 上海金午盘价）
    "159830": "上海金", "159831": "上海金", "159834": "上海金",
    "518600": "上海金", "518680": "上海金", "518860": "上海金", "518890": "上海金",
    # 黄金 Au99.99
    "159812": "黄金", "159934": "黄金", "159937": "黄金",
    "518660": "黄金", "518800": "黄金", "518850": "黄金", "518880": "黄金",
}

# 跟踪标的本地缓存：{code: [分类, 跟踪标的展示串]}。跟踪标的几乎不变，
# 缓存下来避免每天为同一批标的多打 14 次档案页（限频 + 慢）。
_CACHE_PATH = Path(__file__).resolve().parents[2] / "data" / "gold_track_cache.json"
_CACHE: dict[str, list[str]] = {}


def _load_cache() -> None:
    global _CACHE
    try:
        _CACHE = json.loads(_CACHE_PATH.read_text("utf-8")) or {}
    except (OSError, json.JSONDecodeError):
        _CACHE = {}


def _save_cache() -> None:
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(json.dumps(_CACHE, ensure_ascii=False, indent=1),
                               encoding="utf-8")
    except OSError as exc:
        logger.debug("  [gold] 缓存写入失败：%s", exc)


# 历史日线缓存：{code: {hist_high, hist_high_date, hist_last_close, ...}}。
# 沙箱/CI 里对东财的并发连接一多就会被代理掐断（RemoteDisconnected），
# 实测「外层 4 源并发 × 内层 5 路」时 14 只只剩 5 只能取到。所以历史日线
# 改成串行 + 3 次重试，再留这份缓存兜底：取不到时回落到上一次的日线，
# 并把「截至日」如实带出去（不会把旧数据冒充当日）。
_HIST_CACHE_PATH = (Path(__file__).resolve().parents[2]
                    / "data" / "gold_hist_cache.json")
_HIST_CACHE: dict[str, dict] = {}
_HIST_CACHE_DIRTY: set[str] = set()


def _load_hist_cache() -> None:
    global _HIST_CACHE
    try:
        _HIST_CACHE = json.loads(_HIST_CACHE_PATH.read_text("utf-8")) or {}
    except (OSError, json.JSONDecodeError):
        _HIST_CACHE = {}


def _save_hist_cache() -> None:
    if not _HIST_CACHE_DIRTY:
        return
    try:
        _HIST_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _HIST_CACHE_PATH.write_text(
            json.dumps(_HIST_CACHE, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError as exc:
        logger.debug("  [gold] 历史缓存写入失败：%s", exc)

# 黄金 ETF 溢价分级（百分点）：负值是折价，单独一档。
PREMIUM_LEVELS = [
    {"max": 0,    "label": "折价",   "emoji": "🟢"},
    {"max": 0.10, "label": "极低",   "emoji": "🟢"},
    {"max": 0.30, "label": "偏低",   "emoji": "🟢"},
    {"max": 0.60, "label": "正常",   "emoji": "🟡"},
    {"max": 1.00, "label": "偏高",   "emoji": "🟠"},
    {"max": 9.99, "label": "高溢价", "emoji": "🔴"},
]

SEARCH_KEYS = ["黄金", "上海金", "黄金ETF", "金ETF", "上海金ETF"]


def _get(url: str, *, timeout: int = 12, retries: int = 2,
         referer: str = "", gbk: bool = False):
    """带重试的 GET。返回文本或 None（失败不让整块挂掉）。"""
    headers = dict(HEADERS)
    if referer:
        headers["Referer"] = referer
    for i in range(retries + 1):
        try:
            r = requests.get(url, headers=headers, timeout=timeout)
            if r.status_code == 200 and r.text.strip():
                if gbk:
                    r.encoding = "gbk"
                return r.text
        except Exception as exc:                     # noqa: BLE001
            logger.debug("  [gold] 请求失败 %s: %s", url[:60], exc)
        if i < retries:
            time.sleep(1.2 * (i + 1))
    return None


def _search(key: str) -> list[dict]:
    url = ("https://fundsuggest.eastmoney.com/FundSearch/api/FundSearchAPI.ashx?m=1&key="
           + urllib.parse.quote(key))
    txt = _get(url)
    if not txt:
        return []
    try:
        return json.loads(txt).get("Datas") or []
    except json.JSONDecodeError:
        return []


def _detail(code: str) -> dict:
    """东财 mobapi 详情（含 INDEXCODE/FULLNAME）。限频时返回 {}，不抛异常。"""
    url = (f"https://fundmobapi.eastmoney.com/FundMNewApi/FundMNDetailInformation"
           f"?FCODE={code}&deviceid=Wap&plat=Wap&product=EFund&version=2.0.0")
    txt = _get(url)
    if not txt:
        return {}
    try:
        return json.loads(txt).get("Datas") or {}
    except json.JSONDecodeError:
        return {}


def _track_f10(code: str) -> str:
    """从基金档案页（jbgk）抓「跟踪标的」。mobapi 限频时的主力备用通道。"""
    txt = _get(f"https://fundf10.eastmoney.com/jbgk_{code}.html",
               referer="https://fund.eastmoney.com/")
    if not txt:
        return ""
    m = re.search(r"跟踪标的</th>\s*<td[^>]*>(.*?)</td>", txt, re.S)
    if not m:
        return ""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(1))).strip()


def _classify_track(code: str, detail: dict) -> tuple[str, str]:
    """返回 (分类, 跟踪标的展示串)。三级来源：缓存 → mobapi → 档案页 → 内置表。"""
    cached = _CACHE.get(code)
    if cached:
        return cached[0], cached[1]

    idx_code = (detail.get("INDEXCODE") or "").upper()
    idx_name = detail.get("INDEXNAME") or ""
    full = detail.get("FULLNAME") or ""
    if idx_code or idx_name or full:
        if idx_code == "SHAU" or "上海金" in idx_name or "上海金" in full:
            return "上海金", f"上海金（{idx_code or 'SHAU'}）"
        return "黄金", f"{idx_name or 'Au99.99'}（{idx_code or 'AU9999'}）"

    track = _track_f10(code)
    if track:
        if "上海金" in track or "SHAU" in track.upper():
            # 档案页只写「上海金」时补上指数代码，避免出现「上海金（上海金）」
            disp = "上海金（SHAU）" if track.strip() in ("上海金", "SHAU") else f"上海金（{track}）"
            return "上海金", disp
        return "黄金", track

    cat = STATIC_CATEGORY.get(code, "黄金")
    return cat, ("上海金（SHAU）" if cat == "上海金" else "黄金9999（AU9999）")


def _nav_f10(code: str) -> dict:
    """备用净值源。"""
    url = f"https://api.fund.eastmoney.com/f10/lsjz?fundCode={code}&pageIndex=1&pageSize=2"
    txt = _get(url, referer="https://fundf10.eastmoney.com/")
    if not txt:
        return {}
    try:
        lst = json.loads(txt).get("Data", {}).get("LSJZList") or []
    except json.JSONDecodeError:
        return {}
    if not lst:
        return {}
    try:
        return {"nav": float(lst[0]["DWJZ"]), "nav_date": lst[0]["FSRQ"]}
    except (KeyError, ValueError):
        return {}


def _quotes(codes: list[str]) -> dict[str, dict]:
    """批量拉腾讯实时行情。"""
    qry = ",".join(("sh" if c.startswith(("5", "6")) else "sz") + c for c in codes)
    txt = _get(f"https://qt.gtimg.cn/q={qry}", gbk=True)
    if not txt:
        return {}
    out: dict[str, dict] = {}
    for seg in txt.split(";"):
        if '="' not in seg:
            continue
        payload = seg.split('="')[1].rsplit('"', 1)[0]
        f = payload.split("~")
        if len(f) < 40:
            continue

        def _n(i):
            try:
                return float(f[i]) if f[i] else None
            except (ValueError, IndexError):
                return None

        amt_wan = _n(37)                          # 成交额（万元）
        out[f[2]] = {
            "price": _n(3), "prev_close": _n(4), "change_pct": _n(32),
            "amount": amt_wan * 10000 if amt_wan else None,
            "bid1": _n(9), "ask1": _n(19),
            "quote_time": f[30] if len(f) > 30 else "",
        }
    return out


def collect_universe() -> list[dict]:
    """搜出全部黄金类 ETF，补齐跟踪标的与最新净值。"""
    _load_cache()
    seen: dict[str, dict] = {}
    for kw in SEARCH_KEYS:
        for it in _search(kw):
            fb = it.get("FundBaseInfo") or {}
            code = it.get("CODE") or fb.get("FCODE")
            if not code or not code.startswith(ETF_PREFIX):
                continue
            seen[code] = {
                "code": code,
                "short_name": fb.get("SHORTNAME") or it.get("NAME") or "",
                "company": fb.get("JJGS") or "",
                "nav": fb.get("DWJZ"),
                "nav_date": fb.get("FSRQ"),
            }
        time.sleep(0.2)

    out = []
    for code, base in sorted(seen.items()):
        d = {} if code in _CACHE else (_detail(code) or {})
        name = base["short_name"] or d.get("SHORTNAME") or ""
        full = d.get("FULLNAME") or ""
        if any(k in name + full for k in ("黄金股", "黄金产业", "黄金股票")):
            continue                               # 标的是股票指数，不是金价
        category, track = _classify_track(code, d)
        _CACHE.setdefault(code, [category, track])
        nav = base["nav"]
        try:
            nav = float(nav) if nav not in (None, "") else None
        except (TypeError, ValueError):
            nav = None
        if nav is None:                            # 兜底
            f10 = _nav_f10(code)
            nav = f10.get("nav")
            base["nav_date"] = f10.get("nav_date", base["nav_date"])
        out.append({**base, "short_name": name, "full_name": full,
                    "category": category, "track": track, "nav": nav})
        time.sleep(0.15)
    _save_cache()
    return out


def _secid(code: str) -> str:
    """东财 secid：沪市前缀 1，深市前缀 0（与行情接口同一套规则）。"""
    return ("1." if code.startswith(("5", "6")) else "0.") + code


def _curl_get(url: str, *, attempts: int = 2, timeout: int = 25):
    """用 **curl** 取数，不用 requests。

    实测（2026-10）：push2his.eastmoney.com 走 requests 会被沙箱代理掐断
    （ProxyError / RemoteDisconnected），重试也是全失败 —— 14 只里只成功 1-5 只，
    而且每次重试白等 7s，整块从 4s 变成 106s。同一条 URL 换成 curl 就 14/14、
    每只 0.2-0.3s。项目里其余几个东财接口（suggest / fundf10 / 腾讯行情）
    requests 是通的，所以只有这里改，不动已经跑稳的部分。
    """
    for i in range(attempts):
        try:
            proc = subprocess.run(
                ["curl", "-s", "--connect-timeout", "10", "--max-time", str(timeout),
                 "-H", f"User-Agent: {HEADERS['User-Agent']}", url],
                capture_output=True, text=True, timeout=timeout + 5,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug("  [gold] curl 失败 %s: %s", url[:60], exc)
            proc = None
        if proc and proc.stdout and proc.stdout.strip():
            return proc.stdout
        if i + 1 < attempts:
            time.sleep(0.6)
    return None


def _parse_hist(txt: str) -> dict:
    """把日线 JSON 解析成回撤要用的那几项。解析不出来返回 {}。"""
    try:
        klines = (json.loads(txt).get("data") or {}).get("klines") or []
    except (json.JSONDecodeError, AttributeError):
        return {}
    rows = []
    for line in klines:
        parts = line.split(",")
        if len(parts) < 2:
            continue
        try:
            rows.append((parts[0], float(parts[1])))
        except ValueError:
            continue
    if not rows:
        return {}
    high_date, high = max(rows, key=lambda r: r[1])
    last_date, last = rows[-1]
    return {
        "hist_high": high,
        "hist_high_date": high_date,
        "hist_last_close": last,
        "hist_last_date": last_date,
        "hist_first_date": rows[0][0],      # 上市首日 —— 高点窗口起点，说明样本长短
        "hist_bars": len(rows),
    }


def fetch_hist_stats(code: str) -> dict:
    """全历史日线 → 历史最高**收盘**价 / 高点日期 / 最新收盘 / 上市首日。

    两个口径决定，都是为了别算出一个假回撤：
      · 用**收盘价**而非盘中最高价 —— 与 04 区块的回撤定义一致
        （回撤 = 当前收盘 / 历史最高收盘 − 1），盘中极值每天变、还会被长影线带偏。
      · 用**前复权**（fqt=1）而非不复权 —— 黄金 ETF 也分红，不复权序列在除息日
        会凭空跳空，等于伪造一个历史新高，把回撤算小甚至算成 0。

    取数失败时回落到缓存（缓存里的「截至日」会被如实带出去，不会冒充当日数据），
    连缓存都没有才返回 {}，让那一只在表里显示「—」而不是让整块挂掉。
    """
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
           f"?secid={_secid(code)}&fields1=f1,f2,f3,f4,f5&fields2=f51,f53"
           "&klt=101&fqt=1&beg=0&end=20500101")
    txt = _curl_get(url)
    stats = _parse_hist(txt) if txt else {}
    if stats:
        _HIST_CACHE[code] = stats
        _HIST_CACHE_DIRTY.add(code)
        return stats

    cached = _HIST_CACHE.get(code)
    if cached:
        logger.info("  [gold] %s 历史日线取数失败，用缓存（截至 %s）",
                    code, cached.get("hist_last_date"))
        return {**cached, "hist_from_cache": True}
    return {}


def _classify(premium: float) -> dict:
    for lv in PREMIUM_LEVELS:
        if premium <= lv["max"]:
            return lv
    return PREMIUM_LEVELS[-1]


def build() -> dict:
    """取数 → 算溢价 → 排名。返回给 derive_gold 的裸数据。"""
    universe = collect_universe()
    if not universe:
        raise RuntimeError("未搜到任何黄金类 ETF")

    quotes = _quotes([u["code"] for u in universe])

    # 距历史最高收盘的回撤。必须**逐只**取：上市时间不同 → 高点窗口不同
    # （2020 年才上市的上海金 ETF，没有 2013 年以来那段行情）。
    # 走 curl 串行取，14 只约 4s；单只失败回落缓存，缓存也没有就显示「—」。
    _load_hist_cache()
    codes = [u["code"] for u in universe]
    hist = {}
    for c in codes:
        s = fetch_hist_stats(c)
        if s:
            hist[c] = s
    _save_hist_cache()
    if len(hist) < len(codes):
        logger.info("  [gold] 历史日线只取到 %d/%d 只", len(hist), len(codes))

    rows = []
    for u in universe:
        q = quotes.get(u["code"]) or {}
        row = {**u}
        row.update({k: q.get(k) for k in
                    ("price", "prev_close", "change_pct", "amount", "bid1", "ask1",
                     "quote_time")})
        premium = None
        if row.get("price") and u.get("nav"):
            premium = round((row["price"] / u["nav"] - 1) * 100, 3)
        row["premium"] = premium
        row["premium_level"] = _classify(premium) if premium is not None else None
        spread = None
        if row.get("bid1") and row.get("ask1"):
            spread = round((row["ask1"] - row["bid1"]) / row["bid1"] * 100, 4)
        row["spread"] = spread
        row["low_liquidity"] = (row.get("amount") or 0) < LOW_LIQUIDITY
        # 距历史最高收盘的回撤（负值）。用日线序列自己的最后一根收盘，
        # 而不是行情接口的场内价 —— 场内价含溢价，与前复权序列不同口径。
        st = hist.get(u["code"]) or {}
        row.update(st)
        row["dd_from_high"] = (
            round((st["hist_last_close"] / st["hist_high"] - 1) * 100, 2)
            if st.get("hist_high") and st.get("hist_last_close") else None
        )
        rows.append(row)

    ok = [r for r in rows if r["premium"] is not None]
    ok.sort(key=lambda r: (r["premium"], -(r.get("amount") or 0),
                           r["spread"] if r["spread"] is not None else 999))
    failed = [{"code": r["code"], "name": r["short_name"],
               "error": "价格或净值未取到"} for r in rows if r["premium"] is None]

    sh = [r for r in ok if r["category"] == "上海金"]
    au = [r for r in ok if r["category"] == "黄金"]
    nav_dates = sorted({r["nav_date"] for r in ok if r.get("nav_date")})
    nav_date = nav_dates[-1] if nav_dates else ""
    basis = f"{nav_date} 单位净值" if nav_date else "未知"

    # 场内价的那一天。别省这一步：简报在北京 06:45 跑，A 股还没开盘，
    # 行情其实是**上一交易日收盘**；遇上长假（如国庆）会差好几天。
    # 只标净值基准日的话，读者会误以为"用今天的价配 9-30 的净值"，把正常溢价看成异常。
    # 腾讯 quote_time 形如 20260930161457，可直接按字典序取最新。
    qtimes = sorted({r.get("quote_time") for r in ok if r.get("quote_time")})
    quote_time = qtimes[-1] if qtimes else ""
    quote_date = (f"{quote_time[4:6]}-{quote_time[6:8]}"
                  if len(quote_time) >= 8 and quote_time[:8].isdigit() else "")
    quote_hm = (f"{quote_time[8:10]}:{quote_time[10:12]}"
                if len(quote_time) >= 12 and quote_time[8:12].isdigit() else "")
    stale = bool(quote_date and quote_date != (nav_date[5:10] if len(nav_date) >= 10 else ""))

    return {
        "ranked": ok,
        "failed": failed,
        "sh": sh,
        "au": au,
        "nav_date": nav_date,
        "quote_time": quote_time,
        "quote_date": quote_date,
        "quote_hm": quote_hm,
        "stale": stale,
        "basis_label": basis,
        "sh_count": len(sh),
        "au_count": len(au),
        "data_status": f"{len(ok)} 只（上海金 {len(sh)} ／ 黄金 {len(au)}）",
        # 回撤用的日线里，有多少只是回落缓存取到的（取数失败才会 >0）
        "hist_cached": sum(1 for r in ok if r.get("hist_from_cache")),
        "premium_ok_max": 0.3,        # 黄金的可接受上限：0.3%（不是纳指的 2%）
    }
