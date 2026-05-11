"""
量化交易接口定义 (Protocol-based)
这些接口为Phase 2/3的策略研究平台和实盘交易预留扩展点
"""
from typing import Protocol, Optional, Tuple
import pandas as pd
from datetime import datetime


class Strategy(Protocol):
    """策略接口 — 所有回测策略应实现此协议"""
    name: str
    description: str
    
    def generate_signals(self, df: pd.DataFrame) -> Tuple[pd.Series, pd.Series]:
        """
        生成买卖信号
        df: compute_signals()输出的DataFrame
        返回: (buy_mask, sell_mask) 两个bool Series
        """
        ...


class ExecutionModel(Protocol):
    """成交模型接口 — 控制回测的成交价格和规则"""
    
    def get_fill_price(self, signal_bar: pd.Series, next_bar: pd.Series, 
                       side: str) -> float:
        """
        计算模拟成交价
        signal_bar: 产生信号的K线
        next_bar: 下一根K线(用于next-bar-open执行)
        side: "buy" 或 "sell"
        返回: 模拟成交价格(含滑点)
        """
        ...
    
    def can_execute(self, bar_date: datetime, last_trade_date: Optional[datetime],
                    side: str) -> bool:
        """
        检查是否满足执行条件(如T+1规则)
        bar_date: 当前K线日期
        last_trade_date: 上次交易日期
        side: "buy" 或 "sell"
        返回: True=可以执行
        """
        ...


class DataProvider(Protocol):
    """数据源接口 — 抽象不同行情数据来源"""
    
    def fetch_ohlcv(self, symbol: str, start: str, end: str, 
                    period: str = "daily") -> pd.DataFrame:
        """获取OHLCV K线数据"""
        ...
    
    def fetch_realtime(self, symbol: str) -> dict:
        """获取实时行情快照"""
        ...
    
    def search(self, keyword: str) -> list:
        """搜索股票"""
        ...


class RiskManager(Protocol):
    """风控接口 — 管理交易风险"""
    
    def check_entry(self, capital: float, price: float, 
                    current_positions: int) -> bool:
        """检查是否允许开仓"""
        ...
    
    def check_exit(self, entry_price: float, current_price: float,
                   max_price: float, atr: float) -> Tuple[Optional[str], Optional[float]]:
        """检查是否触发平仓(止损/止盈)"""
        ...
    
    def max_position_size(self, capital: float, price: float) -> int:
        """计算最大允许仓位"""
        ...


class MetricsCalculator:
    """绩效计算器 — 从回测结果计算各类统计指标"""
    
    @staticmethod
    def sharpe_ratio(returns: pd.Series, risk_free: float = 0.0, 
                     periods: int = 252) -> float:
        """年化Sharpe比率"""
        excess = returns - risk_free / periods
        if excess.std() == 0:
            return 0.0
        return float(excess.mean() / excess.std() * (periods ** 0.5))
    
    @staticmethod
    def max_drawdown(equity_curve: pd.Series) -> float:
        """最大回撤百分比"""
        peak = equity_curve.cummax()
        dd = (equity_curve - peak) / peak
        return float(dd.min() * 100)
    
    @staticmethod
    def calmar_ratio(annual_return: float, max_drawdown: float) -> float:
        """Calmar比率 = 年化收益/最大回撤"""
        if max_drawdown == 0:
            return 0.0
        return annual_return / abs(max_drawdown)
    
    @staticmethod
    def win_rate(trades: list) -> float:
        """胜率"""
        if not trades:
            return 0.0
        wins = sum(1 for t in trades if t.get("pnl", 0) > 0)
        return wins / len(trades) * 100
    
    @staticmethod
    def profit_factor(trades: list) -> float:
        """盈亏比 = 总盈利/总亏损"""
        gross_profit = sum(t["pnl"] for t in trades if t.get("pnl", 0) > 0)
        gross_loss = abs(sum(t["pnl"] for t in trades if t.get("pnl", 0) < 0))
        if gross_loss == 0:
            return float('inf') if gross_profit > 0 else 0.0
        return gross_profit / gross_loss


# ============================================================
# 默认实现（当前行为）
# ============================================================

class CloseExecutionModel:
    """当前默认：收盘价成交，无滑点，无T+1"""
    def get_fill_price(self, signal_bar, next_bar, side):
        return float(signal_bar["close"])
    
    def can_execute(self, bar_date, last_trade_date, side):
        return True


class NextOpenExecutionModel:
    """Phase 1目标：下一bar开盘价成交 + T+1"""
    def __init__(self, slippage_bps=0, enforce_t1=True):
        self.slippage_bps = slippage_bps
        self.enforce_t1 = enforce_t1
    
    def get_fill_price(self, signal_bar, next_bar, side):
        if next_bar is None:
            return float(signal_bar["close"])
        base_price = float(next_bar["open"])
        slip = base_price * self.slippage_bps / 10000
        return base_price + slip if side == "buy" else base_price - slip
    
    def can_execute(self, bar_date, last_trade_date, side):
        if not self.enforce_t1 or side == "buy":
            return True
        if last_trade_date is None:
            return True
        # T+1: 卖出日必须晚于买入日
        return bar_date > last_trade_date
