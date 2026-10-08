"""ETF 溢价率计算与排序 —— 系统第二核心指标"""
from src.providers.etf_monitor import data_fetcher


def classify_premium(premium, levels):
    """溢价等级分类。levels: [{max, label, emoji}]"""
    for lv in levels:
        if premium <= lv['max']:
            return lv
    return levels[-1]


def get_etf_data(etf_config):
    """
    获取单只 ETF 的完整数据。
    单只失败不抛异常，返回带 error 字段的 dict。
    """
    code = etf_config['code']
    result = {
        'code': code,
        'name': etf_config['name'],
        'short_name': etf_config.get('short_name', ''),
        'price': None,
        'prev_close': None,
        'change_pct': None,
        'volume': None,
        'amount': None,
        'bid1': None,
        'ask1': None,
        'nav': None,
        'nav_date': None,
        'iopv': None,
        'premium': None,
        'premium_basis': None,
        'quote_time': '',
        'source': '',
        'error': None,
    }

    try:
        # 1. 实时行情
        quote = data_fetcher.fetch_etf_quote(code)
        result.update(quote)
        # 腾讯行情把时间戳放在 'time'（形如 20260930161459），
        # 这里统一成 quote_time —— 展示层要拿它标出「场内价是哪天的」。
        result['quote_time'] = quote.get('time') or ''

        # 2. 净值/IOPV
        nav_data = data_fetcher.fetch_etf_nav_and_iopv(code)
        result['nav'] = nav_data['nav']
        result['nav_date'] = nav_data['nav_date']
        result['iopv'] = nav_data.get('iopv')
        result['premium_basis'] = nav_data['basis']

        # 3. 计算溢价率
        # 溢价率 = (场内价格 / 基准 - 1) × 100%
        benchmark = result['iopv'] or result['nav']
        if result['price'] and benchmark:
            result['premium'] = round((result['price'] / benchmark - 1) * 100, 2)
    except Exception as e:
        result['error'] = str(e)

    return result


def get_all_etfs(etf_configs):
    """获取全部 ETF 数据，返回 (成功列表, 失败列表)"""
    results = []
    for cfg in etf_configs:
        data = get_etf_data(cfg)
        if data['error'] or data['premium'] is None:
            results.append(data)  # 也保留，供邮件展示异常
        else:
            results.append(data)
    ok = [r for r in results if r['premium'] is not None]
    failed = [r for r in results if r['premium'] is None]
    return results, ok, failed


def rank_etfs(etf_data_list, premium_levels, liquidity_config):
    """
    ETF 排序：
    第一优先级：溢价率（升序）
    第二优先级：流动性（成交额降序，缺失排后）
    第三优先级：买卖价差（升序）
    """
    ranked = []
    for d in etf_data_list:
        if d['premium'] is None:
            continue
        level = classify_premium(d['premium'], premium_levels)
        spread = None
        if d['bid1'] and d['ask1']:
            spread = round((d['ask1'] - d['bid1']) / d['bid1'] * 100, 4)
        amount = d['amount'] or 0
        ranked.append({
            **d,
            'premium_level': level,
            'spread': spread,
            'low_liquidity': amount < liquidity_config.get('low_threshold', 10000000),
        })

    ranked.sort(key=lambda x: (
        x['premium'],                          # 溢价率优先
        -(x['amount'] or 0),                   # 流动性次之
        x['spread'] if x['spread'] is not None else 999,  # 价差最后
    ))
    return ranked
