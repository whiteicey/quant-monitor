"""
TODO预留接口 - 信号提醒/止盈止损/仓位管理/更多指标/多周期共振
这些接口目前为空壳，待后续实现
"""
from dataclasses import dataclass, field
from typing import List, Optional


# ============================================================
# 信号提醒 (Signal Alerts)
# ============================================================
@dataclass
class AlertConfig:
    """提醒配置"""
    enabled: bool = False
    bull_threshold: int = 4      # 多头评分>=此值触发买入提醒
    bear_threshold: int = 4      # 空头评分>=此值触发卖出提醒
    cooldown_seconds: int = 300  # 同一股票提醒冷却时间
    sound: bool = True           # 是否播放声音
    # 推送渠道（后续实现）
    webhook_url: str = ""        # 微信/钉钉/Telegram webhook


@dataclass
class Alert:
    """一条提醒"""
    symbol: str
    name: str
    alert_type: str  # "buy" / "sell" / "strong_buy" / "strong_sell"
    message: str
    timestamp: float = 0


def check_alerts(signal_result, symbol: str, name: str, config: AlertConfig = None) -> List[Alert]:
    """
    检查信号是否触发提醒
    TODO: 实现提醒逻辑、冷却机制、推送
    """
    # 预留接口，返回空列表
    return []


# ============================================================
# 止盈止损 (Stop Loss / Take Profit)
# ============================================================
@dataclass
class StopConfig:
    """止盈止损配置"""
    stop_loss_pct: float = 0.0       # 固定止损百分比 (0=不启用, 0.05=跌5%止损)
    take_profit_pct: float = 0.0     # 固定止盈百分比 (0=不启用)
    trailing_stop_pct: float = 0.0   # 移动止损百分比 (0=不启用)
    atr_stop_mult: float = 0.0      # ATR止损倍数 (0=不启用, 2.0=2倍ATR)


def apply_stop_rules(price: float, low: float, high: float, entry_price: float, 
                     max_price_since_entry: float, atr_value: float, 
                     config: StopConfig) -> tuple:
    """
    检查是否触发止盈止损（使用盘中最高最低价）
    返回: (trigger_type, fill_price) or (None, None)
    trigger_type: "stop_loss" / "take_profit" / "trailing_stop" / None
    fill_price: 模拟成交价（止损价或止盈价，非收盘价）
    """
    if entry_price <= 0:
        return None, None
    
    # 固定止损: 盘中最低价触及止损线
    if config.stop_loss_pct > 0:
        stop_price = entry_price * (1 - config.stop_loss_pct)
        if low <= stop_price:
            return "stop_loss", stop_price
    
    # ATR止损: 盘中最低价触及ATR止损线
    if config.atr_stop_mult > 0 and atr_value > 0:
        atr_stop_price = max(0.01, entry_price - config.atr_stop_mult * atr_value)
        if low <= atr_stop_price:
            return "stop_loss", atr_stop_price
    
    # 移动止损: 从最高点回落超过百分比
    if config.trailing_stop_pct > 0 and max_price_since_entry > 0:
        trail_price = max_price_since_entry * (1 - config.trailing_stop_pct)
        if low <= trail_price:
            return "trailing_stop", trail_price
    
    # 固定止盈: 盘中最高价触及止盈线
    if config.take_profit_pct > 0:
        target_price = entry_price * (1 + config.take_profit_pct)
        if high >= target_price:
            return "take_profit", target_price
    
    return None, None


# ============================================================
# 仓位管理 (Position Management)
# ============================================================
@dataclass
class PositionConfig:
    """仓位管理配置"""
    mode: str = "full"           # "full"(全仓), "fixed_pct"(固定比例), "kelly"(凯利公式), "pyramid"(金字塔)
    position_pct: float = 1.0    # fixed_pct模式下每次买入占总资金的比例
    max_positions: int = 1       # 最大同时持仓数量（多股票时用）
    pyramid_count: int = 3       # 金字塔模式分几次建仓


def calc_position_size(capital: float, price: float, config: PositionConfig,
                       win_rate: float = 0.5, avg_win_loss_ratio: float = 1.0) -> int:
    """
    计算买入股数
    TODO: 实现kelly/pyramid等模式
    """
    if config.mode == "fixed_pct":
        amount = capital * config.position_pct
    elif config.mode == "kelly":
        # Kelly: f = (p*b - q) / b, where p=win_rate, q=1-p, b=avg_win_loss_ratio
        kelly_pct = max(0, (win_rate * avg_win_loss_ratio - (1 - win_rate)) / avg_win_loss_ratio)
        kelly_pct = min(kelly_pct, 0.5)  # 半Kelly
        amount = capital * kelly_pct
    else:  # full
        amount = capital

    shares = int(amount / price / 100) * 100
    if shares == 0 and int(amount / price) >= 1:
        shares = int(amount / price)
    return shares


# ============================================================
# 更多技术指标 (Additional Indicators)
# ============================================================
def kdj(high, low, close, n=9, m1=3, m2=3):
    """
    KDJ指标
    TODO: 实现并集成到compute_signals
    返回 (k, d, j) Series
    """
    import pandas as pd
    rsv = (close - low.rolling(n).min()) / (high.rolling(n).max() - low.rolling(n).min()) * 100
    rsv = rsv.fillna(50)
    k = rsv.ewm(alpha=1/m1, adjust=False).mean()
    d = k.ewm(alpha=1/m2, adjust=False).mean()
    j = 3 * k - 2 * d
    return k, d, j


def obv(close, volume):
    """
    OBV能量潮指标
    TODO: 实现并集成到compute_signals
    """
    import numpy as np
    import pandas as pd
    direction = np.sign(close.diff())
    direction.iloc[0] = 0
    return (volume * direction).cumsum()


def vwap(high, low, close, volume):
    """
    VWAP成交量加权平均价
    TODO: 实现并集成到compute_signals（分钟级数据有意义）
    """
    typical_price = (high + low + close) / 3
    cum_tp_vol = (typical_price * volume).cumsum()
    cum_vol = volume.cumsum()
    import numpy as np
    result = cum_tp_vol / cum_vol.replace(0, np.nan)
    return result


# ============================================================
# 多周期共振 (Multi-Timeframe Confirmation)
# ============================================================
@dataclass
class MTFConfig:
    """多周期共振配置"""
    enabled: bool = False
    periods: list = field(default_factory=lambda: ["daily", "weekly"])
    require_all: bool = True  # True=所有周期同向才确认, False=多数同向即可


def compute_mtf_signals(symbol: str, config: MTFConfig = None):
    """
    多周期共振信号计算
    TODO: 获取多个周期数据，分别计算信号，合并确认
    返回 {"daily": SignalResult, "weekly": SignalResult, "confirmed_bull": bool, "confirmed_bear": bool}
    """
    # 预留接口
    return {
        "confirmed_bull": False,
        "confirmed_bear": False,
        "details": {},
    }
