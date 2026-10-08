"""数据获取：主数据源 + 备用数据源，失败自动降级"""
import os
import json
import time
import requests

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Referer': 'https://finance.sina.com.cn/',
}


def _http_get(url, headers=None, timeout=10, retries=2):
    """带重试的 GET 请求，失败返回 None"""
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, headers=headers or HEADERS, timeout=timeout)
            if resp.status_code == 200 and resp.text.strip():
                return resp.text
        except Exception:
            pass
        if attempt < retries:
            time.sleep(1.5 * (attempt + 1))
    return None


def fetch_nasdaq_history_nasdaq_api():
    """
    主数据源：NASDAQ 官方 API（分页获取完整历史，1996年起）
    按 1 年一段分页，请求间短暂延时避免限流。
    """
    import pandas as pd
    import datetime as dt

    today = dt.date.today()
    chunks = []
    year = 1996
    max_year = today.year
    while year <= max_year:
        fromdate = f'{year}-01-01'
        todate = f'{year}-12-31'
        url = (f'https://api.nasdaq.com/api/quote/NDX/historical'
               f'?assetclass=index&fromdate={fromdate}&todate={todate}&limit=9999')
        text = _http_get(url, headers={
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
            'Accept': 'application/json',
        }, timeout=20, retries=3)
        if not text:
            year += 1
            continue
        try:
            data = json.loads(text).get('data')
        except json.JSONDecodeError:
            year += 1
            continue
        if not data or not data.get('tradesTable'):
            year += 1
            continue
        rows = data['tradesTable'].get('rows', [])
        for r in rows:
            chunks.append({
                'date': r['date'],
                'open': r['open'],
                'high': r['high'],
                'low': r['low'],
                'close': r['close'],
                'volume': r['volume'],
            })
        year += 1
        time.sleep(0.4)  # 限流保护

    if not chunks:
        raise ValueError('NASDAQ API 无数据')

    df = pd.DataFrame(chunks)
    df['date'] = pd.to_datetime(df['date'], format='%m/%d/%Y').dt.strftime('%Y-%m-%d')
    for col in ['open', 'high', 'low', 'close']:
        df[col] = df[col].astype(str).str.replace(',', '').astype(float)
    df['volume'] = df['volume'].astype(str).str.replace(',', '').replace('--', '0')
    df['volume'] = pd.to_numeric(df['volume'], errors='coerce').fillna(0)
    df = df.drop_duplicates(subset='date').sort_values('date').reset_index(drop=True)
    return df


def fetch_nasdaq_history_cached():
    """备用数据源2：本地缓存 CSV（GitHub 仓库中随代码维护）"""
    import pandas as pd
    from src.paths import NDX_CACHE_CSV
    cache_path = str(NDX_CACHE_CSV)
    if not os.path.exists(cache_path):
        raise ValueError('无缓存文件')
    df = pd.read_csv(cache_path)
    if len(df) < 100:
        raise ValueError('缓存数据过少')
    return df


def fetch_nasdaq_history_chart():
    """首选数据源：NASDAQ 官方 **chart** 接口（一次请求拿全史，1996 年起）。

    为什么放在 historical 前面：2026-10-08 上午实测，两个接口的更新速度差一天 ——
    chart 已有 10-07 收盘，historical（tradesTable 那个页面接口）最新还停在 10-06。
    摘要里那句「当前历史回撤 0.00% …（截至 10-06 收盘）」就是这么来的：
    10-06 恰好是当时序列的历史最高，回撤于是被算成 0。
    两个接口的历史收盘实测完全一致（重叠 7776 天，最大差异 0），换源不改口径。
    """
    from src.providers.common import us_history
    b = us_history.fetch_bundle("NDX", assetclass="index")
    if not b or not b.ok:
        raise ValueError('NASDAQ chart 无数据')
    df = b.df[['date', 'open', 'high', 'low', 'close', 'volume']].copy()
    if len(df) < 100:
        raise ValueError(f'chart 数据过少（{len(df)} 行）')
    return df


def fetch_nasdaq_history_yfinance():
    """备用数据源1：yfinance（GitHub Actions 美国服务器可用）"""
    import yfinance as yf
    df = yf.download('^NDX', period='max', interval='1d', progress=False, auto_adjust=True)
    if df is None or df.empty:
        raise ValueError('yfinance 返回空数据')
    df = df.reset_index()
    df.columns = [str(c).lower() for c in df.columns]
    df = df.rename(columns={'date': 'date'})
    out = df[['date', 'open', 'high', 'low', 'close', 'volume']].copy()
    out['date'] = out['date'].dt.strftime('%Y-%m-%d')
    return out


def fetch_nasdaq_history_tencent(days=640):
    """备用数据源1：腾讯美股K线（仅近640天，历史不足）"""
    url = f'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=usNDX,day,,,{days},qfq'
    text = _http_get(url)
    if not text:
        raise ValueError('腾讯接口无响应')
    data = json.loads(text)
    day = data['data']['usNDX'].get('day', [])
    if not day:
        raise ValueError('腾讯接口返回空K线')
    rows = []
    for d in day:
        # [date, open, close, high, low, volume]
        rows.append({
            'date': d[0],
            'open': float(d[1]),
            'high': float(d[3]),
            'low': float(d[4]),
            'close': float(d[2]),
            'volume': float(d[5]),
        })
    import pandas as pd
    return pd.DataFrame(rows)


_NDX_HISTORY_CACHE = {}
_NDX_HISTORY_LOCK = None


def fetch_nasdaq_history(use_cache=True):
    """获取纳斯达克100历史数据，自动降级：NASDAQ官方 → yfinance → 缓存

    合并后 etf_monitor 与 nasdaq100 两处都会要这份全历史，
    加一层进程内缓存 + 锁（两者是并发跑的），
    避免同一次运行重复打三十多次接口。
    """
    import datetime as dt
    import threading

    global _NDX_HISTORY_LOCK
    if _NDX_HISTORY_LOCK is None:
        _NDX_HISTORY_LOCK = threading.Lock()

    key = dt.date.today().isoformat()
    if use_cache and key in _NDX_HISTORY_CACHE:
        df, name = _NDX_HISTORY_CACHE[key]
        return df.copy(), name + "(cached)"

    with _NDX_HISTORY_LOCK:                     # 并发时只有一个线程真正去抓
        if use_cache and key in _NDX_HISTORY_CACHE:
            df, name = _NDX_HISTORY_CACHE[key]
            return df.copy(), name + "(cached)"

        errors = []
        for name, fn in [('nasdaq_chart', fetch_nasdaq_history_chart),
                         ('nasdaq_api', fetch_nasdaq_history_nasdaq_api),
                         ('yfinance', fetch_nasdaq_history_yfinance),
                         ('cached_csv', fetch_nasdaq_history_cached)]:
            try:
                df = fn()
                if df is not None and len(df) > 100:
                    _NDX_HISTORY_CACHE[key] = (df, name)
                    return df, name
            except Exception as e:
                errors.append(f'{name}: {e}')
    raise RuntimeError(f'所有纳斯达克数据源失败: {"; ".join(errors)}')


def fetch_etf_quote_tencent(code):
    """主数据源：腾讯实时行情"""
    # 判断市场前缀
    prefix = 'sh' if code.startswith(('5', '6')) else 'sz'
    url = f'https://qt.gtimg.cn/q={prefix}{code}'
    text = _http_get(url, headers={'User-Agent': 'Mozilla/5.0'})
    if not text or '=' not in text:
        raise ValueError('腾讯行情无响应')
    # 解析 v_sz159941="51~纳指ETF广发~159941~1.667~1.698~1.675~..."
    payload = text.split('="')[1].rsplit('"', 1)[0]
    fields = payload.split('~')
    if len(fields) < 40:
        raise ValueError(f'腾讯行情字段不足: {len(fields)}')

    def _f(i):
        try:
            return float(fields[i]) if fields[i] else None
        except (ValueError, IndexError):
            return None

    amount_wan = _f(37)  # 成交额（万元）
    return {
        'price': _f(3),          # 最新价
        'prev_close': _f(4),     # 昨收
        'open': _f(5),           # 今开
        'volume': int(_f(6) or 0),  # 成交量（手）
        'amount': amount_wan * 10000 if amount_wan else None,  # 成交额（元）
        'change_pct': _f(32),    # 涨跌幅%
        'bid1': _f(9),           # 买一价
        'ask1': _f(19),          # 卖一价
        'time': fields[30] if len(fields) > 30 else '',  # 行情时间
    }


def fetch_etf_quote_eastmoney(code):
    """备用数据源：东方财富实时行情"""
    prefix = '1' if code.startswith(('5', '6')) else '0'
    url = (f'https://push2.eastmoney.com/api/qt/stock/get?secid={prefix}.{code}'
           f'&fields=f43,f44,f45,f46,f47,f48,f57,f58,f60,f169,f170')
    text = _http_get(url)
    if not text:
        raise ValueError('东财行情无响应')
    data = json.loads(text).get('data', {})
    if not data:
        raise ValueError('东财行情无数据')
    # ETF 价格以 1000 为缩放
    price = data.get('f43', 0) / 1000
    prev_close = data.get('f60', 0) / 1000
    return {
        'price': price,
        'prev_close': prev_close,
        'open': data.get('f46', 0) / 1000,
        'volume': data.get('f47', 0),
        'amount': data.get('f48'),
        'change_pct': data.get('f170', 0) / 100,
        'bid1': None,
        'ask1': None,
        'time': '',
    }


def fetch_etf_quote(code):
    """获取 ETF 实时行情，自动降级"""
    errors = []
    for name, fn in [('tencent', fetch_etf_quote_tencent),
                     ('eastmoney', fetch_etf_quote_eastmoney)]:
        try:
            quote = fn(code)
            if quote and quote.get('price'):
                quote['source'] = name
                return quote
        except Exception as e:
            errors.append(f'{name}: {e}')
    raise RuntimeError(f'ETF {code} 行情获取失败: {"; ".join(errors)}')


def fetch_etf_nav_eastmoney(code):
    """主数据源：东财基金最新净值（T-1，非实时IOPV）"""
    url = (f'https://api.fund.eastmoney.com/f10/lsjz?fundCode={code}'
           f'&pageIndex=1&pageSize=1')
    text = _http_get(url, headers={
        'User-Agent': 'Mozilla/5.0',
        'Referer': 'https://fundf10.eastmoney.com/',
    })
    if not text:
        raise ValueError('东财净值无响应')
    data = json.loads(text)
    ls = data.get('Data', {}).get('LSJZList', [])
    if not ls:
        raise ValueError('东财净值无数据')
    r = ls[0]
    return {
        'nav': float(r['DWJZ']),
        'nav_date': r['FSRQ'],
    }


def fetch_etf_nav_and_iopv(code):
    """
    获取 ETF 净值基准。
    免费数据源无可靠实时IOPV接口 → 使用东财最新净值（T-1）并明确标注。
    （数据质量要求：不能把静态NAV伪装成实时IOPV）
    """
    result = {'nav': None, 'nav_date': None, 'iopv': None, 'basis': None}

    nav_data = fetch_etf_nav_eastmoney(code)
    result['nav'] = nav_data['nav']
    result['nav_date'] = nav_data['nav_date']
    result['basis'] = f'T-1净值({nav_data["nav_date"]})'

    return result
