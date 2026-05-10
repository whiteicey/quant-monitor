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
    """写入缓存"""
    _cache[key] = {"data": data, "ts": _time.time()}


def _session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    s.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
    return s


def _market_prefix(symbol: str) -> tuple:
    """返回 (新浪前缀, 东方财富secid)"""
    s = symbol.strip()
    if s.startswith(("6", "9")):
        return f"sh{s}", f"1.{s}"
    elif s.startswith(("0", "3", "2")):
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
    all_data = []
    end_dt = end_date if end_date else datetime.now().strftime("%Y%m%d")

    for offset in range(0, 3000, 300):
        url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
        params = {
            "_var": "kline_dayqfq",
            "param": f"{sina_sym},day,{start_date},{end_dt},300,qfq",
        }
        try:
            r = sess.get(url, params=params, timeout=10)
            if r.status_code == 200:
                json_str = r.text.split("=", 1)[1] if "=" in r.text else r.text
                data = json.loads(json_str)
                stock_data = data.get("data", {}).get(sina_sym, {})
                klines = stock_data.get("qfqday") or stock_data.get("day") or []
                if not klines:
                    break
                for k in klines:
                    all_data.append({"date": k[0], "open": k[1], "close": k[2], "high": k[3], "low": k[4], "volume": k[5]})
                if len(klines) < 300:
                    break
        except Exception:
            break

    if not all_data:
        raise ConnectionError("腾讯财经数据获取失败")

    df = pd.DataFrame(all_data)
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date")
    df = df[["open", "high", "low", "close", "volume"]].astype(float)
    df = df.sort_index()
    df = df[~df.index.duplicated(keep="first")]
    return df


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
                      period: str = "daily") -> pd.DataFrame:
    """
    获取K线数据
    period: "5min", "15min", "30min", "1h", "daily", "weekly", "monthly"
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
    # 新浪实时数据字段：
    # 0:名称 1:今开 2:昨收 3:当前价 4:最高 5:最低
    # 6:买一 7:卖一 8:成交量(股) 9:成交额
    # 30:日期 31:时间
    return {
        "name": parts[0],
        "open": float(parts[1]),
        "yesterday_close": float(parts[2]),
        "price": float(parts[3]),
        "high": float(parts[4]),
        "low": float(parts[5]),
        "volume": float(parts[8]),
        "amount": float(parts[9]),
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
