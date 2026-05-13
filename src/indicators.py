"""
技术指标计算模块 - 完整移植自Pine Script指标系统
"""
import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    result = 100 - (100 / (1 + rs))
    # 价格完全持平(gain=0, loss=0) → RSI=50; 纯涨无跌 → RSI=100
    flat_mask = (avg_gain == 0) & (avg_loss == 0)
    result = result.where(~flat_mask, 50)
    result = result.fillna(100)
    return result


def macd(close: pd.Series, fast: int = 6, slow: int = 7, signal: int = 4):
    """返回 (macd_line, signal_line, histogram)"""
    fast_ema = ema(close, fast)
    slow_ema = ema(close, slow)
    macd_line = fast_ema - slow_ema
    signal_line = ema(macd_line, signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def bollinger_bands(close: pd.Series, period: int = 20, mult: float = 2.0):
    """返回 (upper, basis, lower)"""
    basis = sma(close, period)
    std = close.rolling(window=period).std(ddof=0)
    upper = basis + mult * std
    lower = basis - mult * std
    return upper, basis, lower


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(window=period).mean()


def crossover(a: pd.Series, b: pd.Series) -> pd.Series:
    """a上穿b"""
    return (a > b) & (a.shift(1) <= b.shift(1))


def crossunder(a: pd.Series, b: pd.Series) -> pd.Series:
    """a下穿b"""
    return (a < b) & (a.shift(1) >= b.shift(1))


def kdj(high, low, close, n=9, m1=3, m2=3):
    """
    KDJ指标
    返回 (k, d, j) Series
    """
    rsv = (close - low.rolling(n).min()) / (high.rolling(n).max() - low.rolling(n).min()) * 100
    rsv = rsv.replace([np.inf, -np.inf], np.nan).fillna(50)
    k = rsv.ewm(alpha=1/m1, adjust=False).mean()
    d = k.ewm(alpha=1/m2, adjust=False).mean()
    j = 3 * k - 2 * d
    return k, d, j


def obv(close, volume):
    """
    OBV能量潮指标
    """
    direction = np.sign(close.diff())
    direction.iloc[0] = 0
    return (volume * direction).cumsum()


def vwap(high, low, close, volume):
    """
    VWAP成交量加权平均价
    """
    typical_price = (high + low + close) / 3
    cum_tp_vol = (typical_price * volume).cumsum()
    cum_vol = volume.cumsum()
    result = cum_tp_vol / cum_vol.replace(0, np.nan)
    result = result.ffill().fillna(typical_price)
    return result
