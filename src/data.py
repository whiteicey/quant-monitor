"""
数据获取模块 - 多数据源支持（东方财富/新浪/腾讯）
支持日线和分钟级K线，支持股票名称搜索
"""
import pandas as pd
import numpy as np
import requests
import re
import json
import time as _time
from datetime import datetime, timedelta


# ============================================================
# 内存缓存
# ============================================================
_cache = {}  # key -> {"data": ..., "ts": float}
_CACHE_TTL = {
    "daily": 300,     # 日线缓存5分钟
    "weekly": 600,    # 周线缓存10分钟
    "monthly": 600,   # 月线缓存10分钟
    "30min": 60,      # 30min缓存1分钟
    "1h": 120,        # 1h缓存2分钟
    "5min": 30,       # 5min缓存30秒
    "15min": 30,      # 15min缓存30秒
    "search": 600,    # 搜索结果缓存10分钟
    "info": 3600,     # 股票名称缓存1小时
    "sector": 120,     # 板块排名缓存2分钟
    "realtime": 5,    # 实时行情缓存5秒
}


def _cache_get(key, category="daily"):
    """从缓存获取，过期返回None"""
    entry = _cache.get(key)
    if entry is None:
        return None
    ttl = _CACHE_TTL.get(category, 300)
    if _time.time() - entry["ts"] > ttl:
        del _cache[key]
        return None
    return entry["data"]


def _cache_set(key, data):
    """写入缓存(存副本防止外部修改)"""
    import copy as _copy
    if isinstance(data, (pd.DataFrame, pd.Series)):
        stored = data.copy()
    elif isinstance(data, (list, dict)):
        stored = _copy.deepcopy(data)
    else:
        stored = data
    _cache[key] = {"data": stored, "ts": _time.time()}
    if len(_cache) > 200:
        _cache_cleanup()


def _cache_cleanup():
    """Remove expired entries + enforce hard cap"""
    now = _time.time()
    expired = [k for k, v in _cache.items() if now - v["ts"] > 3600]
    for k in expired:
        del _cache[k]
    # 硬上限: 超过500条时删最旧的
    while len(_cache) > 500:
        oldest = min(_cache, key=lambda k: _cache[k]["ts"])
        del _cache[oldest]


def _strip_jsonp(text):
    """Strip JSONP wrapper: callback({...}); → {...}"""
    text = text.strip().rstrip(';')
    # 匹配 callback(...) 格式, 用最外层括号配对
    m = re.match(r'^[a-zA-Z_]\w*\((.+)\)\s*$', text, re.S)
    if m:
        return m.group(1)
    if text.startswith("(") and text.endswith(")"):
        return text[1:-1]
    return text


_sess = None

def _session() -> requests.Session:
    global _sess
    if _sess is None:
        _sess = requests.Session()
        _sess.trust_env = False
        _sess.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
        # 强制IPv4: 部分网络环境IPv6不通导致连接超时
        import urllib3
        import socket
        class IPv4HTTPAdapter(requests.adapters.HTTPAdapter):
            def init_poolmanager(self, *args, **kwargs):
                kwargs["socket_options"] = [(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)]
                # 强制使用AF_INET(IPv4)
                import urllib3.util.connection
                urllib3.util.connection.HAS_IPV6 = False
                super().init_poolmanager(*args, **kwargs)
        adapter = IPv4HTTPAdapter(max_retries=urllib3.Retry(total=2, backoff_factor=0.5,
                                                            status_forcelist=[500, 502, 503, 504]))
        _sess.mount("https://", adapter)
        _sess.mount("http://", adapter)
    return _sess


def _market_prefix(symbol: str) -> tuple:
    """返回 (新浪前缀, 东方财富secid)
    上海: 6xx/9xx/5xx(沪市ETF/基金)
    深圳: 0xx/1xx(深市ETF/LOF)/2xx/3xx
    北交所: 4xx/8xx
    """
    s = symbol.strip()
    if s.startswith(("6", "9", "5")):
        return f"sh{s}", f"1.{s}"
    elif s.startswith(("0", "1", "2", "3")):
        return f"sz{s}", f"0.{s}"
    elif s.startswith("4") or s.startswith("8"):
        return f"bj{s}", f"0.{s}"
    return f"sh{s}", f"1.{s}"


# ============================================================
# 股票搜索 - 支持代码和名称模糊搜索
# ============================================================
def search_stock(keyword: str, limit: int = 20) -> list:
    """
    模糊搜索股票，支持代码或名称
    返回 [{"code": "688110", "name": "东芯股份", "market": "SH"}, ...]
    """
    cached = _cache_get(f"search:{keyword}", "search")
    if cached is not None:
        return cached[:limit]

    sess = _session()

    # 东方财富搜索接口
    try:
        url = "https://searchapi.eastmoney.com/api/suggest/get"
        params = {
            "input": keyword,
            "type": "14",
            "token": "D43BF722C8E33BDC906FB84D85E326E8",
            "count": str(limit),
        }
        r = sess.get(url, params=params, timeout=5)
        if r.status_code == 200:
            data = r.json()
            results = []
            for item in data.get("QuotationCodeTable", {}).get("Data", []) or []:
                code = item.get("Code", "")
                name = item.get("Name", "")
                market_id = item.get("MktNum", "")
                # 只保留A股 (沪深北)
                if market_id in ("33", "17", "80"):
                    mkt = {"33": "SZ", "17": "SH", "80": "BJ"}.get(market_id, "")
                    results.append({"code": code, "name": name, "market": mkt})
            if results:
                _cache_set(f"search:{keyword}", results)
                return results[:limit]
    except Exception:
        pass

    # 备选：新浪搜索
    try:
        url = f"https://suggest3.sinajs.cn/suggest/type=11,12,80&key={keyword}&name=suggestdata"
        r = sess.get(url, headers={"Referer": "https://finance.sina.com.cn"}, timeout=5)
        if r.status_code == 200:
            text = r.text
            match = re.search(r'"(.+)"', text)
            if match:
                raw = match.group(1)
                results = []
                for item in raw.split(";"):
                    parts = item.split(",")
                    if len(parts) >= 4:
                        code = parts[2]
                        name = parts[4] if len(parts) > 4 else parts[1]
                        mkt = "SH" if parts[0].startswith("sh") else "SZ"
                        results.append({"code": code, "name": name, "market": mkt})
                _cache_set(f"search:{keyword}", results)
                return results[:limit]
    except Exception:
        pass

    return []


# ============================================================
# 新浪分钟K线
# ============================================================
PERIOD_MAP_SINA = {
    "1min": "1", "5min": "5", "15min": "15", "30min": "30",
    "60min": "60", "1h": "60", "daily": "240",
    "weekly": "1680", "monthly": "7200",
}

def _fetch_sina_kline(symbol: str, period: str = "daily", datalen: int = 500) -> pd.DataFrame:
    """新浪K线，支持分钟级"""
    sina_sym, _ = _market_prefix(symbol)
    sess = _session()
    scale = PERIOD_MAP_SINA.get(period, "240")

    url = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
    params = {"symbol": sina_sym, "scale": scale, "ma": "no", "datalen": str(datalen)}
    r = sess.get(url, params=params, timeout=10)
    if r.status_code != 200 or not r.text.strip():
        raise ConnectionError("新浪K线数据为空")

    data = json.loads(r.text.strip())
    if not data:
        raise ConnectionError("新浪K线数据为空")

    df = pd.DataFrame(data)
    df = df.rename(columns={"day": "date"})
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date")
    df = df[["open", "high", "low", "close", "volume"]].astype(float)
    return df


# ============================================================
# 腾讯K线
# ============================================================
def _fetch_tencent_daily(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    sina_sym, _ = _market_prefix(symbol)
    sess = _session()
    end_dt = end_date if end_date else datetime.now().strftime("%Y%m%d")

    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    params = {
        "_var": "kline_dayqfq",
        "param": f"{sina_sym},day,{start_date},{end_dt},800,qfq",
    }
    try:
        r = sess.get(url, params=params, timeout=10)
        if r.status_code == 200:
            json_str = r.text.split("=", 1)[1] if "=" in r.text else r.text
            data = json.loads(json_str)
            stock_data = data.get("data", {}).get(sina_sym, {})
            klines = stock_data.get("qfqday") or stock_data.get("day") or []
            if klines:
                all_data = [{"date": k[0], "open": k[1], "close": k[2], "high": k[3], "low": k[4], "volume": k[5]} for k in klines]
                df = pd.DataFrame(all_data)
                df["date"] = pd.to_datetime(df["date"])
                df = df.set_index("date")
                df = df[["open", "high", "low", "close", "volume"]].astype(float)
                df = df.sort_index()
                df = df[~df.index.duplicated(keep="first")]
                return df
    except Exception:
        pass
    raise ConnectionError("腾讯财经数据获取失败")


# ============================================================
# 东方财富 (AKShare)
# ============================================================
def _fetch_eastmoney(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    import akshare as ak
    df = ak.stock_zh_a_hist(symbol=symbol, period="daily", start_date=start_date, end_date=end_date, adjust="qfq")
    df = df.rename(columns={"日期": "date", "开盘": "open", "最高": "high", "最低": "low", "收盘": "close", "成交量": "volume"})
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date")
    return df[["open", "high", "low", "close", "volume"]].astype(float)


# ============================================================
# 统一接口
# ============================================================
def fetch_stock_daily(symbol: str, start_date: str = "20230101", end_date: str = "",
                      period: str = "daily", validate: bool = True) -> pd.DataFrame:
    """
    获取K线数据
    period: "5min", "15min", "30min", "1h", "daily", "weekly", "monthly"
    validate: 是否进行数据质量校验(默认开启)
    """
    df = _fetch_stock_daily_raw(symbol, start_date, end_date, period)
    if validate and period in ("daily", "weekly", "monthly") and len(df) > 0:
        df, report = validate_ohlcv(df)
        if report["nan_filled"] > 0:
            print(f"  [数据校验] 填充了 {report['nan_filled']} 个缺失值")
        if report["suspended"]:
            print(f"  [数据校验] {len(report['suspended'])} 个停牌日")
        if report["ohlc_fixed"] > 0:
            print(f"  [数据校验] 修复了 {report['ohlc_fixed']} 条OHLC逻辑异常")
    return df


def _fetch_stock_daily_raw(symbol: str, start_date: str = "20230101", end_date: str = "",
                           period: str = "daily") -> pd.DataFrame:
    """获取原始K线数据(不校验)
    """
    if not end_date:
        end_date = pd.Timestamp.now().strftime("%Y%m%d")

    cache_key = f"kline:{symbol}:{start_date}:{end_date}:{period}"
    cached = _cache_get(cache_key, period)
    if cached is not None:
        return cached.copy()

    # 非日线数据：新浪直接获取
    if period != "daily":
        try:
            datalen = {"weekly": 1000, "monthly": 500}.get(period, 2000)
            df = _fetch_sina_kline(symbol, period, datalen)
            start_dt = pd.to_datetime(start_date)
            end_dt = pd.to_datetime(end_date)
            df = df[(df.index >= start_dt) & (df.index <= end_dt + pd.Timedelta(days=1))]
            if len(df) > 0:
                print(f"  [数据源: 新浪财经 {period}] 获取到 {len(df)} 条数据")
                _cache_set(cache_key, df)
                return df
        except Exception as e:
            raise ConnectionError(f"{period} K线获取失败: {e}")

    # 日线：多数据源fallback
    errors = []
    try:
        df = _fetch_eastmoney(symbol, start_date, end_date)
        if len(df) > 0:
            print(f"  [数据源: 东方财富] 获取到 {len(df)} 条数据")
            _cache_set(cache_key, df)
            return df
    except Exception as e:
        errors.append(f"东方财富: {e}")

    try:
        df = _fetch_sina_kline(symbol, "daily", 5000)
        start_dt, end_dt = pd.to_datetime(start_date), pd.to_datetime(end_date)
        df = df[(df.index >= start_dt) & (df.index <= end_dt)]
        if len(df) > 0:
            print(f"  [数据源: 新浪财经] 获取到 {len(df)} 条数据")
            _cache_set(cache_key, df)
            return df
    except Exception as e:
        errors.append(f"新浪财经: {e}")

    try:
        df = _fetch_tencent_daily(symbol, start_date, end_date)
        if len(df) > 0:
            print(f"  [数据源: 腾讯财经] 获取到 {len(df)} 条数据")
            _cache_set(cache_key, df)
            return df
    except Exception as e:
        errors.append(f"腾讯财经: {e}")

    raise ConnectionError(f"所有数据源均失败:\n" + "\n".join(errors))


# ============================================================
# 数据质量校验
# ============================================================

def validate_ohlcv(df: pd.DataFrame, max_gap_days: int = 3, 
                   max_daily_change: float = 0.22) -> tuple:
    """
    校验并清洗OHLCV数据
    
    检测项:
    1. NaN/缺失值 — forward-fill, 连续缺失超过max_gap_days则标记
    2. 异常价格 — 单日涨跌幅 > max_daily_change (A股涨跌停20%, ST 5%, 留2%余量)
    3. 零成交量 — 标记为停牌日
    4. OHLC逻辑 — high >= low, high >= open/close, low <= open/close
    
    参数:
        df: OHLCV DataFrame (DatetimeIndex)
        max_gap_days: 允许的最大连续缺失天数
        max_daily_change: 单日涨跌幅异常阈值 (0.22 = 22%)
    
    返回:
        (cleaned_df, report_dict)
        report_dict: {"nan_filled": int, "anomalies": list, "suspended": list, "ohlc_fixed": int}
    """
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return df, {"nan_filled": 0, "anomalies": [], "suspended": [], "ohlc_fixed": 0}
    
    cleaned = df.copy()
    report = {"nan_filled": 0, "anomalies": [], "suspended": [], "ohlc_fixed": 0}
    
    # 1. NaN检测与填充
    nan_count = cleaned[["open", "high", "low", "close", "volume"]].isna().sum().sum()
    if nan_count > 0:
        # 检查连续NaN
        close_nan = cleaned["close"].isna()
        if close_nan.any():
            groups = (close_nan != close_nan.shift()).cumsum()
            for _, grp in close_nan.groupby(groups):
                if grp.all() and len(grp) > max_gap_days:
                    dates = grp.index.strftime("%Y-%m-%d").tolist()
                    report["anomalies"].append(
                        f"连续{len(grp)}天数据缺失: {dates[0]}~{dates[-1]}")
        
        cleaned[["open", "high", "low", "close"]] = cleaned[["open", "high", "low", "close"]].ffill().bfill()
        cleaned["volume"] = cleaned["volume"].fillna(0)
        report["nan_filled"] = int(nan_count)
    
    # 2. 异常价格检测 (单日涨跌幅)
    pct_change = cleaned["close"].pct_change().abs()
    anomaly_mask = pct_change > max_daily_change
    if anomaly_mask.any():
        for date in cleaned.index[anomaly_mask]:
            change = pct_change.loc[date]
            report["anomalies"].append(
                f"{date.strftime('%Y-%m-%d')}: 涨跌幅{change:.1%}")
    
    # 3. 零成交量 (停牌)
    zero_vol = cleaned["volume"] == 0
    if zero_vol.any():
        suspended_dates = cleaned.index[zero_vol].strftime("%Y-%m-%d").tolist()
        report["suspended"] = suspended_dates
        # 停牌日不删除, 但在回测时这些bar的信号应被忽略
        # 这里给cleaned加一个标记列
        cleaned["suspended"] = zero_vol
    else:
        cleaned["suspended"] = False
    
    # 4. OHLC逻辑修复
    fix_count = 0
    # high < low 直接互换
    bad_hl = cleaned["high"] < cleaned["low"]
    if bad_hl.any():
        cleaned.loc[bad_hl, ["high", "low"]] = cleaned.loc[bad_hl, ["low", "high"]].values
        fix_count += int(bad_hl.sum())
    # high应该 >= open, close
    bad_high = (cleaned["high"] < cleaned[["open", "close"]].max(axis=1))
    if bad_high.any():
        cleaned.loc[bad_high, "high"] = cleaned.loc[bad_high, ["open", "close", "high"]].max(axis=1)
        fix_count += int(bad_high.sum())
    # low应该 <= open, close
    bad_low = (cleaned["low"] > cleaned[["open", "close"]].min(axis=1))
    if bad_low.any():
        cleaned.loc[bad_low, "low"] = cleaned.loc[bad_low, ["open", "close", "low"]].min(axis=1)
        fix_count += int(bad_low.sum())
    report["ohlc_fixed"] = fix_count
    
    return cleaned, report


def fetch_stock_info(symbol: str) -> str:
    """获取股票名称"""
    cached = _cache_get(f"info:{symbol}", "info")
    if cached is not None:
        return cached
    sina_sym, _ = _market_prefix(symbol)
    try:
        sess = _session()
        r = sess.get(f"https://hq.sinajs.cn/list={sina_sym}", headers={"Referer": "https://finance.sina.com.cn"}, timeout=5)
        if r.status_code == 200:
            match = re.search(r'"(.+?),', r.text)
            if match:
                name = match.group(1)
                _cache_set(f"info:{symbol}", name)
                return name
    except Exception:
        pass
    try:
        import akshare as ak
        info = ak.stock_individual_info_em(symbol=symbol)
        name_row = info[info["item"] == "股票简称"]
        if not name_row.empty:
            name = name_row.iloc[0]["value"]
            _cache_set(f"info:{symbol}", name)
            return name
    except Exception:
        pass
    return symbol


def fetch_realtime_quote(symbol: str) -> dict:
    """
    获取实时行情快照
    返回 {"name","open","high","low","price","volume","date","time","yesterday_close"}
    """
    sina_sym, _ = _market_prefix(symbol)
    sess = _session()
    r = sess.get(
        f"https://hq.sinajs.cn/list={sina_sym}",
        headers={"Referer": "https://finance.sina.com.cn"},
        timeout=5,
    )
    if r.status_code != 200:
        raise ConnectionError("实时行情获取失败")

    match = re.search(r'"(.+)"', r.text)
    if not match:
        raise ConnectionError("实时行情解析失败")

    parts = match.group(1).split(",")
    if len(parts) < 32:
        raise ConnectionError("实时行情数据字段不足")
    # 新浪实时数据字段：
    # 0:名称 1:今开 2:昨收 3:当前价 4:最高 5:最低
    # 6:买一 7:卖一 8:成交量(股) 9:成交额
    # 30:日期 31:时间
    return {
        "name": parts[0],
        "open": float(parts[1]) if parts[1] else 0,
        "yesterday_close": float(parts[2]) if parts[2] else 0,
        "price": float(parts[3]) if parts[3] else 0,
        "high": float(parts[4]) if parts[4] else 0,
        "low": float(parts[5]) if parts[5] else 0,
        "volume": float(parts[8]) if parts[8] else 0,
        "amount": float(parts[9]) if parts[9] else 0,
        "date": parts[30],
        "time": parts[31],
    }


def fetch_realtime_quotes_batch(symbols: list) -> dict:
    """
    批量获取实时行情（一次HTTP请求）
    symbols: ["688110", "600519", ...]
    返回 {symbol: {name,open,high,low,price,volume,amount,date,time,yesterday_close}, ...}
    """
    if not symbols:
        return {}

    sina_list = []
    sym_map = {}  # sina_sym -> original symbol
    for s in symbols:
        sina_sym, _ = _market_prefix(s)
        sina_list.append(sina_sym)
        sym_map[sina_sym] = s

    sess = _session()
    r = sess.get(
        f"https://hq.sinajs.cn/list={','.join(sina_list)}",
        headers={"Referer": "https://finance.sina.com.cn"},
        timeout=10,
    )
    if r.status_code != 200:
        raise ConnectionError("批量行情获取失败")

    results = {}
    for line in r.text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        # var hq_str_sh688110="东芯股份,..."
        m = re.match(r'var hq_str_(\w+)="(.+)"', line)
        if not m:
            continue
        sina_sym = m.group(1)
        symbol = sym_map.get(sina_sym)
        if not symbol:
            continue
        parts = m.group(2).split(",")
        if len(parts) < 32 or not parts[3]:
            continue
        try:
            results[symbol] = {
                "name": parts[0],
                "open": float(parts[1]) if parts[1] else 0,
                "yesterday_close": float(parts[2]) if parts[2] else 0,
                "price": float(parts[3]) if parts[3] else 0,
                "high": float(parts[4]) if parts[4] else 0,
                "low": float(parts[5]) if parts[5] else 0,
                "volume": float(parts[8]) if parts[8] else 0,
                "amount": float(parts[9]) if parts[9] else 0,
                "date": parts[30],
                "time": parts[31],
            }
        except (ValueError, IndexError):
            continue

    return results


def merge_realtime_bar(df, quote):
    """
    将实时行情合并到K线数据的最后一根bar（或追加新bar）
    df: 历史K线DataFrame (index=datetime, columns=open/high/low/close/volume)
    quote: fetch_realtime_quote()返回的dict
    返回: 合并后的DataFrame (新副本)
    """
    if quote is None or quote.get("price", 0) <= 0 or len(df) == 0:
        return df

    df = df.copy()
    last_idx = df.index[-1]
    today_str = quote.get("date", "")
    last_date_str = str(last_idx.date()) if hasattr(last_idx, 'date') else str(last_idx)[:10]

    if today_str == last_date_str:
        df.loc[last_idx, "close"] = quote["price"]
        df.loc[last_idx, "high"] = max(df.loc[last_idx, "high"], quote["high"])
        df.loc[last_idx, "low"] = min(df.loc[last_idx, "low"], quote["low"])
        df.loc[last_idx, "volume"] = quote["volume"]
    else:
        new_idx = pd.to_datetime(today_str)
        new_row = pd.DataFrame({
            "open": [quote["open"]], "high": [quote["high"]],
            "low": [quote["low"]], "close": [quote["price"]],
            "volume": [quote["volume"]],
        }, index=[new_idx])
        new_row.index.name = df.index.name
        df = pd.concat([df, new_row])

    return df


def fetch_sector_info(symbol: str) -> dict:
    """获取股票所属板块信息"""
    cached = _cache_get(f"sector_info:{symbol}", "info")
    if cached is not None:
        return cached
    
    _, secid = _market_prefix(symbol)
    sess = _session()
    try:
        r = sess.get(
            f"https://push2.eastmoney.com/api/qt/stock/get",
            params={"secid": secid, "fields": "f57,f58,f127,f128,f129", "cb": ""},
            timeout=5,
        )
        r.encoding = "utf-8"
        data = json.loads(_strip_jsonp(r.text)).get("data", {})
        result = {
            "industry": data.get("f127", "") or "",
            "region": data.get("f128", "") or "",
            "concepts": [c.strip() for c in (data.get("f129", "") or "").split(",") if c.strip()],
        }
        _cache_set(f"sector_info:{symbol}", result)
        return result
    except Exception:
        return {"industry": "", "region": "", "concepts": []}


def fetch_sector_ranking(top_n: int = 5) -> dict:
    """获取板块涨跌排名"""
    cached = _cache_get("sector_ranking", "sector")
    if cached is not None:
        return cached
    
    sess = _session()
    result = {"strongest": [], "weakest": []}
    
    try:
        # 最强板块
        r = sess.get(
            "https://push2.eastmoney.com/api/qt/clist/get",
            params={"pn": "1", "pz": str(top_n), "fid": "f3", "fs": "m:90+t:2",
                    "fields": "f2,f3,f4,f12,f14", "po": "1", "cb": ""},
            timeout=5,
        )
        r.encoding = "utf-8"
        data = json.loads(_strip_jsonp(r.text)).get("data", {}).get("diff", {})
        items = list(data.values()) if isinstance(data, dict) else (data if isinstance(data, list) else [])
        for item in items[:top_n]:
            result["strongest"].append({
                "name": item.get("f14", ""),
                "change_pct": round(item.get("f3", 0) / 100, 4),
            })
        
        # 最弱板块
        r2 = sess.get(
            "https://push2.eastmoney.com/api/qt/clist/get",
            params={"pn": "1", "pz": str(top_n), "fid": "f3", "fs": "m:90+t:2",
                    "fields": "f2,f3,f4,f12,f14", "po": "0", "cb": ""},
            timeout=5,
        )
        r2.encoding = "utf-8"
        data2 = json.loads(_strip_jsonp(r2.text)).get("data", {}).get("diff", {})
        items = list(data2.values()) if isinstance(data2, dict) else (data2 if isinstance(data2, list) else [])
        for item in items[:top_n]:
            result["weakest"].append({
                "name": item.get("f14", ""),
                "change_pct": round(item.get("f3", 0) / 100, 4),
            })
    except Exception:
        pass
    
    # 只缓存非空结果, 避免暂时性网络故障被缓存
    if result["strongest"] or result["weakest"]:
        _cache_set("sector_ranking", result)
    return result
