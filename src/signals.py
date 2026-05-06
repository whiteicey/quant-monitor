"""
多空信号评分系统 - 完整移植自Pine Script
参数默认值与Pine脚本一致
"""
import pandas as pd
import numpy as np
from dataclasses import dataclass
from . import indicators as ind


@dataclass
class SignalParams:
    fast_length: int = 6
    slow_length: int = 7
    signal_length: int = 4
    rsi_length: int = 14
    bb_length: int = 20
    bb_mult: float = 2.0
    volume_length: int = 5
    atr_length: int = 14


@dataclass
class SignalResult:
    """单根K线的信号结果"""
    bullish_signals: int
    bearish_signals: int
    strong_buy: bool
    strong_sell: bool
    weak_buy: bool
    weak_sell: bool
    rsi_value: float
    macd_value: float
    price_position: float
    trend: str  # "上涨" / "下跌" / "震荡"
    recommended_buy: float
    recommended_sell: float
    stop_loss: float
    support: float
    resistance: float


def compute_signals(df: pd.DataFrame, params: SignalParams = None) -> pd.DataFrame:
    """
    输入: DataFrame with columns: open, high, low, close, volume
    输出: 原始df + 所有信号列
    """
    if params is None:
        params = SignalParams()

    df = df.copy()
    c, h, l, o, v = df["close"], df["high"], df["low"], df["open"], df["volume"]

    # --- 移动平均线 ---
    df["fast_ema"] = ind.ema(c, params.fast_length)
    df["slow_ema"] = ind.ema(c, params.slow_length)
    df["ema_bullish"] = (df["fast_ema"] > df["slow_ema"]) & (c > df["fast_ema"])
    df["ema_bearish"] = (df["fast_ema"] < df["slow_ema"]) & (c < df["fast_ema"])

    # 金叉死叉
    df["golden_cross"] = ind.crossover(df["fast_ema"], df["slow_ema"])
    df["death_cross"] = ind.crossunder(df["fast_ema"], df["slow_ema"])

    # --- MACD ---
    df["macd_line"], df["signal_line"], df["macd_hist"] = ind.macd(
        c, params.fast_length, params.slow_length, params.signal_length
    )
    df["macd_bullish"] = (df["macd_line"] > df["signal_line"]) & (df["macd_line"] > 0)
    df["macd_bearish"] = (df["macd_line"] < df["signal_line"]) & (df["macd_line"] < 0)
    df["macd_golden_cross"] = ind.crossover(df["macd_line"], df["signal_line"])
    df["macd_death_cross"] = ind.crossunder(df["macd_line"], df["signal_line"])

    # --- RSI ---
    df["rsi"] = ind.rsi(c, params.rsi_length)
    df["rsi_overbought"] = df["rsi"] > 70
    df["rsi_oversold"] = df["rsi"] < 30
    df["rsi_bullish"] = (df["rsi"] > 45) & (df["rsi"] < 70) & (df["rsi"] > df["rsi"].shift(1))
    df["rsi_bearish"] = (df["rsi"] < 55) & (df["rsi"] > 30) & (df["rsi"] < df["rsi"].shift(1))

    # --- 布林带 ---
    df["bb_upper"], df["bb_basis"], df["bb_lower"] = ind.bollinger_bands(
        c, params.bb_length, params.bb_mult
    )
    df["bb_bullish"] = (c > df["bb_basis"]) & (c < df["bb_upper"])
    df["bb_bearish"] = (c < df["bb_basis"]) & (c > df["bb_lower"])
    df["near_bb_lower"] = c <= df["bb_lower"] * 1.02
    df["near_bb_upper"] = c >= df["bb_upper"] * 0.98

    # --- 成交量 ---
    df["volume_avg"] = ind.sma(v, params.volume_length)
    df["volume_high"] = v > df["volume_avg"] * 1.5
    df["volume_bullish"] = df["volume_high"] & (c > o) & (c > c.shift(1))
    df["volume_bearish"] = df["volume_high"] & (c < o) & (c < c.shift(1))

    # --- ATR ---
    df["atr"] = ind.atr(h, l, c, params.atr_length)
    df["high_volatility"] = df["atr"] > ind.sma(df["atr"], 20) * 1.2

    # --- 支撑阻力 ---
    df["resistance"] = h.rolling(50).max()
    df["support"] = l.rolling(50).min()
    df["near_resistance"] = c >= df["resistance"] * 0.985
    df["near_support"] = c <= df["support"] * 1.015

    # --- 价格位置 ---
    price_range = df["resistance"] - df["support"]
    df["price_position"] = np.where(
        price_range > 0, (c - df["support"]) / price_range * 100, 50
    )
    df["low_price_zone"] = df["price_position"] < 30
    df["high_price_zone"] = df["price_position"] > 70

    # --- 多空评分(满分7) ---
    df["bull_score"] = (
        df["ema_bullish"].astype(int)
        + df["macd_bullish"].astype(int)
        + ((df["rsi_bullish"]) & (~df["rsi_overbought"])).astype(int)
        + (df["bb_bullish"] | df["near_bb_lower"]).astype(int)
        + df["volume_bullish"].astype(int)
        + (df["near_support"] | df["low_price_zone"]).astype(int)
    )

    df["bear_score"] = (
        df["ema_bearish"].astype(int)
        + df["macd_bearish"].astype(int)
        + ((df["rsi_bearish"]) & (~df["rsi_oversold"])).astype(int)
        + (df["bb_bearish"] | df["near_bb_upper"]).astype(int)
        + df["volume_bearish"].astype(int)
        + (df["near_resistance"] | df["high_price_zone"]).astype(int)
    )

    # --- 交易信号 ---
    df["strong_buy"] = (
        (df["bull_score"] >= 5)
        & (df["bear_score"] <= 1)
        & (df["golden_cross"] | df["macd_golden_cross"])
    )
    df["strong_sell"] = (
        (df["bear_score"] >= 5)
        & (df["bull_score"] <= 1)
        & (df["death_cross"] | df["macd_death_cross"])
    )
    df["weak_buy"] = df["bull_score"] >= 4
    df["weak_sell"] = df["bear_score"] >= 4

    # --- 推荐价位 ---
    df["rec_buy_price"] = l.rolling(20).min() * 0.98
    df["rec_sell_price"] = h.rolling(20).max() * 1.02
    df["stop_loss"] = c * 0.95

    # --- 趋势判断 ---
    df["trend"] = np.where(
        df["ema_bullish"], "上涨",
        np.where(df["ema_bearish"], "下跌", "震荡")
    )

    return df


def get_latest_signal(df: pd.DataFrame) -> SignalResult:
    """获取最新一根K线的信号结果"""
    row = df.iloc[-1]

    def _safe(val, ndigits=2):
        v = float(val)
        if pd.isna(v):
            return 0.0
        return round(v, ndigits)

    return SignalResult(
        bullish_signals=int(row["bull_score"]) if not pd.isna(row["bull_score"]) else 0,
        bearish_signals=int(row["bear_score"]) if not pd.isna(row["bear_score"]) else 0,
        strong_buy=bool(row["strong_buy"]) if not pd.isna(row["strong_buy"]) else False,
        strong_sell=bool(row["strong_sell"]) if not pd.isna(row["strong_sell"]) else False,
        weak_buy=bool(row["weak_buy"]) if not pd.isna(row["weak_buy"]) else False,
        weak_sell=bool(row["weak_sell"]) if not pd.isna(row["weak_sell"]) else False,
        rsi_value=_safe(row["rsi"]),
        macd_value=_safe(row["macd_line"], 4),
        price_position=_safe(row["price_position"], 1),
        trend=row["trend"] if isinstance(row["trend"], str) else "震荡",
        recommended_buy=_safe(row["rec_buy_price"]),
        recommended_sell=_safe(row["rec_sell_price"]),
        stop_loss=_safe(row["stop_loss"]),
        support=_safe(row["support"]),
        resistance=_safe(row["resistance"]),
    )
