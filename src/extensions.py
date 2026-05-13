"""
止盈止损/仓位管理/更多指标/多周期共振
"""
from dataclasses import dataclass, field
from typing import List, Optional


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
    
    # 移动止损: 需先盈利超过trailing_stop_pct才激活, 然后从最高点回落超过百分比
    if config.trailing_stop_pct > 0 and max_price_since_entry > 0:
        # 激活条件: 最高价必须超过entry_price才开始跟踪
        if max_price_since_entry > entry_price:
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
    mode: str = "full"           # "full"(全仓), "fixed_pct"(固定比例), "kelly"(凯利公式)
    position_pct: float = 1.0    # fixed_pct模式下每次买入占总资金的比例
    max_positions: int = 1       # 最大同时持仓数量（多股票时用）
    pyramid_count: int = 3       # 预留字段


def calc_position_size(capital: float, effective_price: float, config: PositionConfig = None,
                       win_rate: float = 0.5, avg_win_loss_ratio: float = 1.0) -> int:
    """
    计算买入股数
    effective_price: 含手续费的单价 (price * (1 + commission))
    """
    if capital <= 0 or effective_price <= 0:
        return 0
    if config is None or config.mode == "full":
        amount = capital
    elif config.mode == "fixed_pct":
        amount = capital * config.position_pct
    elif config.mode == "kelly":
        p, b = win_rate, avg_win_loss_ratio
        kelly_pct = max(0, (p * b - (1 - p)) / b) if b > 0 else 0
        kelly_pct = kelly_pct / 2  # 半Kelly: 用一半仓位降低波动
        kelly_pct = min(kelly_pct, 0.5)  # 硬上限50%
        amount = capital * kelly_pct if kelly_pct > 0.01 else capital * 0.01  # 极小edge时最少1%
    else:
        amount = capital

    shares = int(amount / effective_price / 100) * 100
    if shares == 0 and int(amount / effective_price) >= 1:
        shares = int(amount / effective_price)
    return shares


# ============================================================
# 更多技术指标 (Additional Indicators)
# ============================================================
# Re-export from indicators for backward compatibility
from .indicators import kdj, obv, vwap


# ============================================================
# 多周期共振 (Multi-Timeframe Confirmation)
# ============================================================
@dataclass
class MTFConfig:
    """多周期共振配置"""
    enabled: bool = False
    periods: list = field(default_factory=lambda: ["daily", "weekly"])
    require_all: bool = True  # True=所有周期同向才确认, False=多数同向即可


def compute_mtf_signals(symbol: str, start: str = "20240101", end: str = "", config: MTFConfig = None):
    """
    多周期共振信号计算
    返回 {"confirmed_bull": bool, "confirmed_bear": bool, "details": {period: {...}}}
    """
    if config is None:
        config = MTFConfig()
    
    from .data import fetch_stock_daily, merge_realtime_bar, fetch_realtime_quote
    from .signals import compute_signals, get_latest_signal, SignalParams
    from dataclasses import asdict
    
    details = {}
    for period in config.periods:
        try:
            df = fetch_stock_daily(symbol, start, end, period=period)
            try:
                q = fetch_realtime_quote(symbol)
                df = merge_realtime_bar(df, q)
            except Exception:
                pass
            sig_df = compute_signals(df)
            signal = get_latest_signal(sig_df)
            details[period] = {
                "trend": signal.trend,
                "bull_score": signal.bullish_signals,
                "bear_score": signal.bearish_signals,
                "rsi": signal.rsi_value,
                "strong_buy": signal.strong_buy,
                "weak_buy": signal.weak_buy,
                "strong_sell": signal.strong_sell,
                "weak_sell": signal.weak_sell,
            }
        except Exception:
            details[period] = {"trend": "--", "bull_score": 0, "bear_score": 0}
    
    bull_votes = sum(1 for p in config.periods if details.get(p, {}).get("trend") == "上涨")
    bear_votes = sum(1 for p in config.periods if details.get(p, {}).get("trend") == "下跌")
    threshold = len(config.periods) if config.require_all else max(1, len(config.periods) // 2 + 1)
    
    return {
        "confirmed_bull": bull_votes >= threshold,
        "confirmed_bear": bear_votes >= threshold,
        "bull_votes": bull_votes,
        "bear_votes": bear_votes,
        "total": len(config.periods),
        "details": details,
    }
