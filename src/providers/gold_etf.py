"""黄金 / 上海金 ETF 选择指标 —— 简报第 13 区块的数据源。

为什么单独做：03 区块是国内**纳指** ETF 的溢价排名，口径已经稳定跑很久了。
黄金这边标的、指标、阈值全都不一样，所以单独一套。

  综合费率   = 管理费率 + 托管费率（每年 %，基金合同定的）
  年化跟踪误差 = std(基金日收益 − 基准日收益) × √252（近 60 个交易日）
  排序：综合费率升序 → 成交额降序 → 跟踪误差升序

三个必须注意的点：

  1) 这里**不用**「溢价率」。2026-10 实测确定：黄金 ETF 的单位净值按上金所
     Au99.99 收盘价（15:30）或上海金午盘定盘价（14:30）估值，而场内价格
     15:00 就定格了 —— 中间这 30 分钟的金价波动被完整记进「价格/净值 − 1」，
     算出来的不是折溢价。证据：
       · 518880 近 19 个交易日：净值日增与 Au99.99 日增的平均绝对偏差 0.0027%
         （净值就是金价 × 0.009509），而价格日增与金价日增的标准差 0.22% ——
         这 0.22% 就是「溢价率」的全部内容，形态是围绕 0 的噪声（均值 −0.057%，
         正溢价天数 9/20）；
       · 09-30 横截面 14 只里 13 只同向为正、离散仅 0.058% —— 共同因子
         （当天尾盘金价方向），不是各家独立的供需折溢价。
     黄金 ETF 走实物申赎 + T+0，套利几乎无摩擦，真实折溢价长期被压在 ±0.05%，
     本来就没有可交易空间。拿它排「哪只买得贵」，排出来的是各家的估值时点差
     与费率，会买错。
     （注：03 区块的纳指 ETF 溢价 11%~15% 是真的 —— 时点差只有 1~2%，远小于
     溢价本身，且受外汇额度约束无法套利抹平。那边不动。）

  2) 分类不能看简称。518600「金ETF广发」的全称是「广发**上海金**交易型开放式
     证券投资基金」，跟踪 SHAU；名字里带「黄金」的才多是 Au99.99。判断一律走
     FundMNDetailInformation 的 INDEXCODE / FULLNAME。

  3) 跟踪误差的基准各归各的：上海金 ETF 对上海金基准价（SHAU），
     Au99.99 ETF 对上金所 Au99.99 现货。用错基准会把「基准差」当成「跟踪差」，
     实测两者日偏差约 0.14%，比真实的跟踪误差还大。
"""
from __future__ import annotations

import json
import logging
import re
import statistics
import subprocess
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
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


# 费率缓存：{code: {"mgmt": 0.5, "cust": 0.1}}。费率写在基金合同里、极少变动，
# 缓存下来就不用每天为同一批标的多打 14 次档案页。
_FEE_CACHE_PATH = Path(__file__).resolve().parents[2] / "data" / "gold_fee_cache.json"
_FEE_CACHE: dict[str, dict] = {}
_FEE_CACHE_DIRTY: set[str] = set()


def _load_fee_cache() -> None:
    global _FEE_CACHE
    try:
        _FEE_CACHE = json.loads(_FEE_CACHE_PATH.read_text("utf-8")) or {}
    except (OSError, json.JSONDecodeError):
        _FEE_CACHE = {}


def _save_fee_cache() -> None:
    if not _FEE_CACHE_DIRTY:
        return
    try:
        _FEE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _FEE_CACHE_PATH.write_text(
            json.dumps(_FEE_CACHE, ensure_ascii=False, indent=1, sort_keys=True),
            encoding="utf-8")
    except OSError as exc:
        logger.debug("  [gold] 费率缓存写入失败：%s", exc)


# 跟踪误差的基准：类别 → (东财 secid, 展示名)。secid 前缀 118 是上金所现货板块。
# 上海金基准价（SHAU）是 10:15 / 14:30 两次集中定盘产生的价；
# Au99.99 是连续竞价、收盘价在 15:30。两者日偏差约 0.14%，所以必须各归各的。
GOLD_BENCH = {
    "上海金": ("118.SHAU", "上海金基准价"),
    "黄金": ("118.AU9999", "上金所 Au99.99"),
}
TRACK_WINDOW = 60        # 跟踪误差窗口（交易日）
MIN_TRACK_DAYS = 20      # 低于这个天数不出跟踪误差，免得 5 天样本抖出一个假排名

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


def _hist_get(url: str, *, attempts: int = 2, timeout: int = 25, referer: str = ""):
    """带重试的双通道 GET（**curl 优先，失败再走 requests**）—— 两条通道缺一不可。

    两个运行环境的失败面正好互补，各封一边，所以不能只留一条：

      · 本地沙箱（走 HTTP 代理）：requests 会被代理掐断（ProxyError /
        RemoteDisconnected），14 只只成功 1~5 只、每次重试白等 7s、整块从 4s
        变 106s；同一条 URL 换成 curl 就 14/14、每只 0.2~0.3s。
      · GitHub Actions（Azure 海外 IP、无代理）：**反过来**，curl 在这条 URL 上
        全部取不到 —— 2026-10-08 09:07 那次 run 里 14 只全打「历史日线取数失败，
        用缓存（截至 2026-09-30）」，而且是快失败不是超时；requests 无代理直连可通。

    只留 curl 的后果是「本地 14/14、云端 0/14」这种一边静默走缓存：缓存不会自己
    变新，回撤栏会停在旧快照上还不报错（那天恰好休市、缓存值与当日等价，纯属侥幸）。
    所以这里两条都试，谁通用谁。
    """
    # ① curl（本地沙箱的可用通道）
    cmd = ["curl", "-s", "--connect-timeout", "10", "--max-time", str(timeout),
           "-H", f"User-Agent: {HEADERS['User-Agent']}"]
    if referer:
        cmd += ["-H", f"Referer: {referer}"]
    cmd.append(url)
    for i in range(attempts):
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=timeout + 5)
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug("  [gold] curl 失败 %s: %s", url[:60], exc)
            proc = None
        if proc and proc.stdout and proc.stdout.strip():
            return proc.stdout
        if i + 1 < attempts:
            time.sleep(0.6)

    # ② requests 兜底（GitHub Actions 的可用通道）
    hdrs = dict(HEADERS)
    if referer:
        hdrs["Referer"] = referer
    for i in range(attempts):
        try:
            r = requests.get(url, headers=hdrs, timeout=timeout)
            if r.status_code == 200 and (r.text or "").strip():
                logger.debug("  [gold] curl 未取到，requests 兜底成功：%s", url[:60])
                return r.text
        except requests.RequestException as exc:
            logger.debug("  [gold] requests 兜底失败 %s: %s", url[:60], exc)
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
    txt = _hist_get(url)
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


# ── 选择指标：综合费率 + 年化跟踪误差 ─────────────────────────────────────────

def _fee_f10(code: str) -> dict:
    """基金档案页 → 管理费率 / 托管费率（每年 %）。

    页面里形如 `<th>管理费率</th><td>0.50%（每年）</td>`。返回 {"mgmt": x, "cust": y}，
    缺哪个就少哪个 —— 调用方要两个都在才敢加总（只拿到一个说明页面结构变了，
    硬加会得出偏低的假费率）。
    """
    txt = _hist_get(f"https://fundf10.eastmoney.com/jbgk_{code}.html",
                    referer="https://fund.eastmoney.com/", timeout=20, attempts=2)
    if not txt:
        return {}
    out: dict[str, float] = {}
    for label, key in (("管理费率", "mgmt"), ("托管费率", "cust")):
        m = re.search(re.escape(label) + r"</th>\s*<td[^>]*>(.*?)</td>", txt, re.S)
        if not m:
            continue
        plain = re.sub(r"<[^>]+>", "", m.group(1))
        val = re.search(r"([\d.]+)", plain)
        if val:
            try:
                out[key] = float(val.group(1))
            except ValueError:
                pass
    return out


def _nav_full(code: str, keep: int = 260) -> dict[str, float]:
    """全量单位净值序列 {YYYY-MM-DD: 单位净值}，只保留最近 keep 条。

    走天天基金的 pingzhongdata（一次请求拿全史 3000+ 个点、约 550KB），
    而不是 F10 的 lsjz —— 那个接口 pageSize 被硬限制在 20 条（实测传 40/90
    都只回 20 条），要凑够 60 日窗口得翻 3 次页，14 只就是 42 次请求。

    ⚠ 时间戳 x 是「北京时间零点」对应的毫秒 epoch，按 UTC 还原会整体退一天
    （09-30 的净值被标成 09-29），必须按 UTC+8 还原。
    """
    txt = _hist_get(f"https://fund.eastmoney.com/pingzhongdata/{code}.js",
                    referer="https://fund.eastmoney.com/", timeout=30, attempts=2)
    if not txt:
        return {}
    m = re.search(r"var Data_netWorthTrend\s*=\s*(\[.*?\]);", txt, re.S)
    if not m:
        return {}
    try:
        arr = json.loads(m.group(1))
    except json.JSONDecodeError:
        return {}
    if not isinstance(arr, list):
        return {}
    out: dict[str, float] = {}
    for it in arr[-keep:]:
        try:
            ts = int(it["x"]) / 1000
            d = datetime.fromtimestamp(ts, tz=timezone(timedelta(hours=8)))
            val = it["y"]
            if val:
                out[d.strftime("%Y-%m-%d")] = float(val)
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _bench_series(cat: str) -> dict[str, float]:
    """基准金价日线 {日期: 收盘价}。类别没有对应基准时返回 {}。"""
    secid = (GOLD_BENCH.get(cat) or ("", ""))[0]
    if not secid:
        return {}
    txt = _hist_get("https://push2his.eastmoney.com/api/qt/stock/kline/get"
                    f"?secid={secid}&fields1=f1&fields2=f51,f53"
                    "&klt=101&fqt=0&beg=20250101&end=20500101",
                    timeout=25, attempts=2)
    if not txt:
        return {}
    try:
        klines = (json.loads(txt).get("data") or {}).get("klines") or []
    except json.JSONDecodeError:
        return {}
    out: dict[str, float] = {}
    for line in klines:
        parts = line.split(",")
        if len(parts) < 2:
            continue
        try:
            out[parts[0]] = float(parts[1])
        except ValueError:
            continue
    return out


def _track_error(nav: dict[str, float], bench: dict[str, float],
                 window: int = TRACK_WINDOW) -> dict:
    """年化跟踪误差 %：std(基金日收益 − 基准日收益) × √252。

    口径即行业通用的日频超额收益标准差年化。基准与基金各有一套交易日历，
    先取交集日期再算，避免把「只有一边有数据」的日子当成 0 收益。
    """
    days = sorted(set(nav) & set(bench))
    if len(days) < MIN_TRACK_DAYS + 1:
        return {}
    days = days[-(window + 1):]
    excess: list[float] = []
    for i in range(1, len(days)):
        a_nav, b_nav = nav[days[i - 1]], nav[days[i]]
        a_gld, b_gld = bench[days[i - 1]], bench[days[i]]
        if not a_nav or not a_gld:
            continue
        excess.append((b_nav / a_nav - 1) - (b_gld / a_gld - 1))
    if len(excess) < MIN_TRACK_DAYS:
        return {}
    return {
        "te": round(statistics.pstdev(excess) * (252 ** 0.5) * 100, 3),
        "te_days": len(excess),
    }


def build() -> dict:
    """取数 → 算综合费率与年化跟踪误差 → 排名。返回给 derive_gold 的裸数据。"""
    universe = collect_universe()
    if not universe:
        raise RuntimeError("未搜到任何黄金类 ETF")
    codes = [u["code"] for u in universe]

    quotes = _quotes(codes)

    # 距历史最高收盘的回撤。必须**逐只**取：上市时间不同 → 高点窗口不同
    # （2020 年才上市的上海金 ETF，没有 2013 年以来那段行情）。
    # 走 curl 串行取，14 只约 4s；单只失败回落缓存，缓存也没有就显示「—」。
    _load_hist_cache()
    hist = {}
    for c in codes:
        s = fetch_hist_stats(c)
        if s:
            hist[c] = s
    _save_hist_cache()
    if len(hist) < len(codes):
        logger.info("  [gold] 历史日线只取到 %d/%d 只", len(hist), len(codes))

    # 费率：缓存优先（费率写在基金合同里、极少变动），未命中才去打档案页
    _load_fee_cache()
    fees: dict[str, dict] = {}
    for c in codes:
        f = _FEE_CACHE.get(c)
        if not f:
            f = _fee_f10(c)
            if f:
                _FEE_CACHE[c] = f
                _FEE_CACHE_DIRTY.add(c)
                time.sleep(0.15)
        fees[c] = f
    _save_fee_cache()
    got_fee = sum(1 for f in fees.values() if f.get("mgmt") is not None)
    if got_fee < len(codes):
        logger.info("  [gold] 费率只取到 %d/%d 只", got_fee, len(codes))

    # 基准金价：上海金 / Au99.99 各一条。取不到只让跟踪误差留空，
    # 不影响行情与回撤（这两块不依赖基准）。
    benches = {cat: _bench_series(cat) for cat in GOLD_BENCH}
    for cat, series in benches.items():
        if not series:
            logger.info("  [gold] 基准金价未取到：%s", cat)

    # 逐只净值序列 → 年化跟踪误差。每只一个 ~550KB 的全量净值文件，
    # 串行 + 小间隔，避免被东财限频（限频时是静默返回空，不会报错）。
    tracks: dict[str, dict] = {}
    for u in universe:
        nv = _nav_full(u["code"])
        tracks[u["code"]] = (_track_error(nv, benches.get(u["category"]) or {})
                             if nv else {})
        time.sleep(0.15)
    got_te = sum(1 for t in tracks.values() if t.get("te") is not None)
    if got_te < len(codes):
        logger.info("  [gold] 跟踪误差只算到 %d/%d 只", got_te, len(codes))

    rows = []
    for u in universe:
        q = quotes.get(u["code"]) or {}
        row = {**u}
        row.update({k: q.get(k) for k in
                    ("price", "prev_close", "change_pct", "amount", "bid1", "ask1",
                     "quote_time")})
        # 综合费率 = 管理费 + 托管费。两个都拿到才算：只拿到一个说明档案页结构
        # 变了，硬加会得出偏低的假费率，宁可显示「—」。
        f = fees.get(u["code"]) or {}
        if f.get("mgmt") is not None and f.get("cust") is not None:
            row["fee_mgmt"], row["fee_cust"] = f["mgmt"], f["cust"]
            row["fee_total"] = round(f["mgmt"] + f["cust"], 2)
        else:
            row["fee_mgmt"] = row["fee_cust"] = row["fee_total"] = None
        row["fee_cached"] = bool(_FEE_CACHE.get(u["code"]))

        t = tracks.get(u["code"]) or {}
        row["te"] = t.get("te")
        row["te_days"] = t.get("te_days")
        row["te_bench"] = (GOLD_BENCH.get(u["category"]) or ("", ""))[1]

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

    # 排序：费率低的在前（这是确定性成本，直接决定长期持有谁更划算）；
    # 同费率看成交额（买得动）；再同看跟踪误差（跟得稳）。
    ok = [r for r in rows if r["fee_total"] is not None]
    ok.sort(key=lambda r: (r["fee_total"], -(r.get("amount") or 0),
                           r["te"] if r.get("te") is not None else 999))
    failed = [{"code": r["code"], "name": r["short_name"],
               "error": "费率或行情未取到"} for r in rows if r["fee_total"] is None]

    sh = [r for r in ok if r["category"] == "上海金"]
    au = [r for r in ok if r["category"] == "黄金"]
    nav_dates = sorted({r["nav_date"] for r in ok if r.get("nav_date")})
    nav_date = nav_dates[-1] if nav_dates else ""
    # 跟踪误差窗口：各只上市时间不同，实际窗口长度会不一样，取出现最多的那个展示
    wins = [r["te_days"] for r in ok if r.get("te_days")]
    te_days = max(set(wins), key=wins.count) if wins else 0

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
        "te_label": (f"近 {te_days} 个交易日" if te_days else "未取到"),
        "te_window": te_days,
        "sh_count": len(sh),
        "au_count": len(au),
        "data_status": f"{len(ok)} 只（上海金 {len(sh)} ／ 黄金 {len(au)}）",
        # 回撤用的日线里，有多少只是回落缓存取到的（取数失败才会 >0）
        "hist_cached": sum(1 for r in ok if r.get("hist_from_cache")),
        # 费率有多少只是走缓存（非当日现取）· 跟踪误差有多少只没算出来
        "fee_cached": sum(1 for r in ok if r.get("fee_cached")),
        "te_missing": sum(1 for r in ok if r.get("te") is None),
    }
