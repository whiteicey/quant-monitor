"""
回测引擎 - 多策略信号模式
"""
import pandas as pd
import numpy as np
from dataclasses import dataclass
from .signals import compute_signals, SignalParams
from .extensions import StopConfig, apply_stop_rules


@dataclass
class BacktestResult:
    total_return: float       # 总收益率 %
    annual_return: float      # 年化收益率 %
    max_drawdown: float       # 最大回撤 %
    win_rate: float           # 胜率 %
    total_trades: int         # 总交易次数
    profit_trades: int
    loss_trades: int
    sharpe_ratio: float
    trades: pd.DataFrame      # 交易明细
    strategy_name: str = ""   # 策略名称
    equity_curve: list = None  # [{"time": int, "value": float}, ...]


# ============================================================
# 策略定义: 每个策略返回 (buy_mask, sell_mask) 两个bool Series
# ============================================================

def _strategy_score_weak(df):
    """综合评分(>=4) — 原始策略，信号多但胜率低"""
    return df["weak_buy"], df["weak_sell"]

def _strategy_score_strong(df):
    """综合评分(>=5)+金叉确认 — 信号极少但高确信"""
    return df["strong_buy"], df["strong_sell"]

def _strategy_macd_rsi(df):
    """MACD金叉买入 + RSI超买卖出 — 数据验证胜率最高(83%)"""
    buy = df["macd_golden_cross"]
    sell = df["rsi_overbought"]  # RSI > 70
    return buy, sell

def _strategy_macd_cross(df):
    """纯MACD金叉/死叉"""
    return df["macd_golden_cross"], df["macd_death_cross"]

def _strategy_ema_cross(df):
    """纯EMA金叉/死叉"""
    return df["golden_cross"], df["death_cross"]

def _strategy_bb_bounce(df):
    """布林带反弹：触下轨买入，触上轨卖出"""
    buy = df["near_bb_lower"]
    sell = df["near_bb_upper"]
    return buy, sell

def _strategy_volume_breakout(df):
    """放量突破：放量上涨买入，放量下跌或RSI超买卖出"""
    buy = df["volume_bullish"] & (df["bull_score"] >= 2)
    sell = df["volume_bearish"] | df["rsi_overbought"]
    return buy, sell

def _strategy_trend_follow(df):
    """趋势跟踪：EMA金叉+MACD多头确认买入，EMA死叉卖出"""
    buy = df["golden_cross"] & (df["macd_line"] > df["signal_line"])
    sell = df["death_cross"]
    return buy, sell

def _strategy_conservative(df):
    """稳健保守：评分>=4 + MACD多头 + 非超买 买入，评分>=3空头 或 RSI超买 卖出"""
    buy = df["weak_buy"] & df["macd_bullish"] & (~df["rsi_overbought"])
    sell = (df["bear_score"] >= 3) | df["rsi_overbought"]
    return buy, sell

def _strategy_oversold_rebound(df):
    """超跌反弹：RSI<30超卖区买入，RSI>60或评分>=3空头卖出"""
    buy = df["rsi_oversold"]  # RSI < 30
    sell = (df["rsi"] > 60) | (df["bear_score"] >= 3)
    return buy, sell

def _strategy_multi_confirm(df):
    """多重共振：至少3个维度同时看多买入（EMA+MACD+成交量），2个维度看空卖出"""
    buy = (
        df["ema_bullish"].astype(int)
        + df["macd_bullish"].astype(int)
        + df["volume_bullish"].astype(int)
        + ((df["rsi_bullish"]) & (~df["rsi_overbought"])).astype(int)
    ) >= 3
    sell = (
        df["ema_bearish"].astype(int)
        + df["macd_bearish"].astype(int)
    ) >= 2
    return buy, sell

def _strategy_support_resistance(df):
    """支撑阻力：近支撑+多头评分>=2买入，近阻力+空头评分>=2卖出"""
    buy = (df["near_support"] | df["low_price_zone"]) & (df["bull_score"] >= 2)
    sell = (df["near_resistance"] | df["high_price_zone"]) & (df["bear_score"] >= 2)
    return buy, sell


# 策略注册表
STRATEGIES = {
    "macd_rsi":          ("MACD金叉+RSI超买", _strategy_macd_rsi,          "实测胜率最高，MACD金叉买入，RSI>70卖出"),
    "conservative":      ("稳健保守",          _strategy_conservative,      "多条件确认买入，宽松止盈，适合稳健投资者"),
    "trend_follow":      ("趋势跟踪",          _strategy_trend_follow,      "EMA金叉+MACD多头确认，跟随趋势"),
    "multi_confirm":     ("多重共振",          _strategy_multi_confirm,     "3个以上维度同时看多才买，减少假信号"),
    "oversold_rebound":  ("超跌反弹",          _strategy_oversold_rebound,  "RSI超卖区抄底，适合震荡市"),
    "bb_bounce":         ("布林带反弹",        _strategy_bb_bounce,         "触下轨买入触上轨卖出，适合区间震荡股"),
    "volume_breakout":   ("放量突破",          _strategy_volume_breakout,   "放量上涨入场，捕捉突破行情"),
    "support_resistance":("支撑阻力",          _strategy_support_resistance,"近支撑位买入近阻力位卖出"),
    "score_weak":        ("综合评分(标准)",    _strategy_score_weak,        "原始策略，多空评分>=4触发"),
    "score_strong":      ("综合评分(严格)",    _strategy_score_strong,      "评分>=5且需金叉确认，信号极少"),
    "macd_cross":        ("纯MACD交叉",       _strategy_macd_cross,        "经典MACD金叉死叉"),
    "ema_cross":         ("纯EMA交叉",        _strategy_ema_cross,         "经典EMA金叉死叉"),
}


# ============================================================
# 参数预设: 每个策略对应最优指标参数（基于实测数据）
# ============================================================
PARAM_PRESETS = {
    # preset_id: (名称, SignalParams, 适用说明)
    "default": (
        "默认参数",
        SignalParams(fast_length=6, slow_length=7, signal_length=4, rsi_length=14,
                     bb_length=20, bb_mult=2.0, volume_length=5, atr_length=14,
                     price_mode="default"),
        "原始Pine脚本参数，适合快速波动的小盘股",
    ),
    "macd_rsi_opt": (
        "MACD+RSI优化",
        SignalParams(fast_length=10, slow_length=22, signal_length=7, rsi_length=14,
                     bb_length=20, bb_mult=2.0, volume_length=5, atr_length=14,
                     price_mode="macd_momentum"),
        "MACD金叉+RSI策略最佳参数，胜率89%收益182%",
    ),
    "macd_standard": (
        "经典MACD(12/26/9)",
        SignalParams(fast_length=12, slow_length=26, signal_length=9, rsi_length=14,
                     bb_length=20, bb_mult=2.0, volume_length=5, atr_length=14,
                     price_mode="macd_momentum"),
        "最广泛使用的MACD参数，各平台通用",
    ),
    "macd_sensitive": (
        "灵敏MACD(8/17/9)",
        SignalParams(fast_length=8, slow_length=17, signal_length=9, rsi_length=14,
                     bb_length=20, bb_mult=2.0, volume_length=5, atr_length=14,
                     price_mode="macd_momentum"),
        "比经典MACD更灵敏，更早捕捉趋势变化",
    ),
    "bb_tight": (
        "布林带窄幅(25/2.0)",
        SignalParams(fast_length=6, slow_length=7, signal_length=4, rsi_length=14,
                     bb_length=25, bb_mult=2.0, volume_length=5, atr_length=14,
                     price_mode="bollinger"),
        "布林带反弹策略最佳参数，胜率100%收益220%",
    ),
    "bb_wide": (
        "布林带宽幅(15/1.5)",
        SignalParams(fast_length=6, slow_length=7, signal_length=4, rsi_length=14,
                     bb_length=15, bb_mult=1.5, volume_length=5, atr_length=14,
                     price_mode="bollinger"),
        "更频繁触发布林带信号，交易次数多适合活跃操作",
    ),
    "trend_slow": (
        "趋势慢速(10/30)",
        SignalParams(fast_length=10, slow_length=30, signal_length=9, rsi_length=14,
                     bb_length=20, bb_mult=2.0, volume_length=5, atr_length=14,
                     price_mode="atr_trend"),
        "过滤短期噪音，只捕捉中长期趋势",
    ),
    "trend_fast": (
        "趋势快速(5/20)",
        SignalParams(fast_length=5, slow_length=20, signal_length=9, rsi_length=14,
                     bb_length=20, bb_mult=2.0, volume_length=5, atr_length=14,
                     price_mode="atr_trend"),
        "快速响应趋势变化，交易频率高",
    ),
    "conservative_opt": (
        "稳健优化",
        SignalParams(fast_length=12, slow_length=26, signal_length=9, rsi_length=10,
                     bb_length=20, bb_mult=2.0, volume_length=5, atr_length=14,
                     price_mode="conservative"),
        "稳健保守策略最佳参数，胜率61%收益103%",
    ),
    "oversold_opt": (
        "超跌反弹优化",
        SignalParams(fast_length=6, slow_length=7, signal_length=4, rsi_length=14,
                     bb_length=20, bb_mult=2.0, volume_length=5, atr_length=14,
                     price_mode="rsi_reversal"),
        "RSI(14)超跌反弹最佳平衡点，胜率70%",
    ),
    "volume_opt": (
        "放量突破优化",
        SignalParams(fast_length=6, slow_length=7, signal_length=4, rsi_length=14,
                     bb_length=20, bb_mult=2.0, volume_length=3, atr_length=14,
                     price_mode="volume_break"),
        "3日均量更灵敏地捕捉放量信号，胜率86%",
    ),
    "rsi_sensitive": (
        "RSI灵敏(10)",
        SignalParams(fast_length=6, slow_length=7, signal_length=4, rsi_length=10,
                     bb_length=20, bb_mult=2.0, volume_length=5, atr_length=14,
                     price_mode="rsi_reversal"),
        "RSI周期缩短，更快触发超买超卖信号",
    ),
    "rsi_smooth": (
        "RSI平滑(20)",
        SignalParams(fast_length=6, slow_length=7, signal_length=4, rsi_length=20,
                     bb_length=20, bb_mult=2.0, volume_length=5, atr_length=14,
                     price_mode="rsi_reversal"),
        "RSI周期拉长，减少噪音，信号更可靠",
    ),
}

# 策略->推荐预设映射
STRATEGY_RECOMMENDED_PRESET = {
    "macd_rsi": "macd_rsi_opt",
    "conservative": "conservative_opt",
    "trend_follow": "trend_slow",
    "multi_confirm": "macd_standard",
    "oversold_rebound": "oversold_opt",
    "bb_bounce": "bb_tight",
    "volume_breakout": "volume_opt",
    "support_resistance": "default",
    "score_weak": "default",
    "score_strong": "default",
    "macd_cross": "macd_standard",
    "ema_cross": "trend_fast",
}

# 反向映射：预设→推荐策略（只保留1:1唯一映射）
_preset_count = {}
for _s, _p in STRATEGY_RECOMMENDED_PRESET.items():
    _preset_count[_p] = _preset_count.get(_p, 0) + 1
PRESET_RECOMMENDED_STRATEGY = {}
for _s, _p in STRATEGY_RECOMMENDED_PRESET.items():
    if _preset_count[_p] == 1:  # 只有唯一对应关系才反向映射
        PRESET_RECOMMENDED_STRATEGY[_p] = _s


def get_strategy_list() -> list:
    """返回策略列表供前端使用（含推荐预设）"""
    return [
        {"id": k, "name": v[0], "desc": v[2],
         "recommended_preset": STRATEGY_RECOMMENDED_PRESET.get(k, "default")}
        for k, v in STRATEGIES.items()
    ]


def get_preset_list() -> list:
    """返回参数预设列表供前端使用"""
    result = []
    for k, (name, params, desc) in PARAM_PRESETS.items():
        result.append({
            "id": k, "name": name, "desc": desc,
            "recommended_strategy": PRESET_RECOMMENDED_STRATEGY.get(k, ""),
            "params": {
                "fast_length": params.fast_length,
                "slow_length": params.slow_length,
                "signal_length": params.signal_length,
                "rsi_length": params.rsi_length,
                "bb_length": params.bb_length,
                "bb_mult": params.bb_mult,
                "volume_length": params.volume_length,
                "atr_length": params.atr_length,
                "price_mode": params.price_mode,
            }
        })
    return result


def backtest(
    df: pd.DataFrame,
    params: SignalParams = None,
    initial_capital: float = 1000000.0,
    commission: float = 0.001,
    stamp_tax: float = 0.001,
    strategy: str = "macd_rsi",
    stop_config=None,
    # 保持向后兼容
    use_strong_only: bool = False,
) -> BacktestResult:
    """
    多策略回测引擎
    strategy: 策略ID，见 STRATEGIES 字典
    """
    signals_df = compute_signals(df, params)

    # 向后兼容旧参数
    if strategy == "macd_rsi" and use_strong_only:
        strategy = "score_strong"

    if strategy not in STRATEGIES:
        strategy = "macd_rsi"

    strategy_name, strategy_fn, _ = STRATEGIES[strategy]
    buy_mask, sell_mask = strategy_fn(signals_df)

    capital = initial_capital
    position = 0
    entry_price = 0.0
    entry_date = None
    max_price_since_entry = 0.0
    trades = []
    eq_curve = []  # 权益曲线（每根K线一个值）

    for i in range(len(signals_df)):
        row = signals_df.iloc[i]
        price = row["close"]
        bar_high = row["high"]
        bar_low = row["low"]
        atr_val = float(row.get("atr", 0)) if not pd.isna(row.get("atr", 0)) else 0
        date = signals_df.index[i]
        is_buy = bool(buy_mask.iloc[i]) if hasattr(buy_mask, 'iloc') else bool(buy_mask[i])
        is_sell = bool(sell_mask.iloc[i]) if hasattr(sell_mask, 'iloc') else bool(sell_mask[i])

        # 记录当前净值（未实现盈亏也计入）
        eq_curve.append(capital + position * price)

        # 止盈止损检查（优先于信号买卖）
        stop_type, fill_price = None, None
        if position > 0 and stop_config:
            max_price_since_entry = max(max_price_since_entry, bar_high)
            stop_type, fill_price = apply_stop_rules(
                price, bar_low, bar_high, entry_price, 
                max_price_since_entry, atr_val, stop_config)
        
        if stop_type and position > 0:
            # 止盈止损触发，强制卖出（用fill_price而非close）
            revenue = position * fill_price * (1 - commission - stamp_tax)
            pnl = revenue - position * entry_price * (1 + commission)
            capital += revenue
            trades.append({
                "buy_date": str(entry_date.date()) if hasattr(entry_date, 'date') else str(entry_date)[:10],
                "sell_date": date,
                "entry_price": entry_price,
                "exit_price": round(fill_price, 2),
                "shares": position,
                "pnl": round(pnl, 2),
                "return_pct": round((fill_price / entry_price - 1) * 100, 2),
                "exit_type": stop_type,
            })
            position = 0
            max_price_since_entry = 0.0

        elif is_buy and position == 0:
            max_shares = int(capital / (price * (1 + commission)))
            shares = (max_shares // 100) * 100
            if shares == 0 and max_shares >= 1:
                shares = max_shares
            if shares > 0:
                cost = shares * price * (1 + commission)
                capital -= cost
                position = shares
                entry_price = price
                entry_date = date
                max_price_since_entry = bar_high

        elif is_sell and position > 0:
            revenue = position * price * (1 - commission - stamp_tax)
            pnl = revenue - position * entry_price * (1 + commission)
            capital += revenue
            trades.append({
                "buy_date": str(entry_date.date()) if hasattr(entry_date, 'date') else str(entry_date)[:10],
                "sell_date": date,
                "entry_price": entry_price,
                "exit_price": price,
                "shares": position,
                "pnl": round(pnl, 2),
                "return_pct": round((price / entry_price - 1) * 100, 2),
                "exit_type": "signal",
            })
            position = 0
            max_price_since_entry = 0.0

    # 未平仓持仓：按当前市价记录，不扣卖出手续费（因为还没卖）
    final_price = signals_df.iloc[-1]["close"]
    final_date = signals_df.index[-1]
    if position > 0:
        market_value = position * final_price  # 当前市值，不扣手续费
        cost = position * entry_price * (1 + commission)  # 买入成本（含买入手续费）
        pnl = market_value - cost
        trades.append({
            "buy_date": str(entry_date.date()) if hasattr(entry_date, 'date') else str(entry_date)[:10],
            "sell_date": str(final_date.date()) + "(未平仓)" if hasattr(final_date, 'date') else str(final_date)[:10] + "(未平仓)",
            "entry_price": entry_price,
            "exit_price": final_price,
            "shares": position,
            "pnl": round(pnl, 2),
            "return_pct": round((final_price / entry_price - 1) * 100, 2),
            "exit_type": "holding",
        })
        capital += market_value  # 按市值计入，不扣卖出费用
        position = 0

    final_value = capital
    total_return = (final_value / initial_capital - 1) * 100
    days = (signals_df.index[-1] - signals_df.index[0]).days
    annual_return = ((final_value / initial_capital) ** (365.0 / max(days, 1)) - 1) * 100 if days > 0 else 0

    # 最大回撤 & Sharpe（直接用 eq_curve）
    eq_series = pd.Series(eq_curve)
    peak = eq_series.cummax()
    drawdown = ((eq_series - peak) / peak * 100).min()

    daily_returns = eq_series.pct_change().dropna()
    sharpe = (daily_returns.mean() / daily_returns.std() * np.sqrt(252)) if daily_returns.std() > 0 else 0

    trades_df = pd.DataFrame(trades) if trades else pd.DataFrame()
    profit_trades = len([t for t in trades if t["pnl"] > 0])
    loss_trades = len([t for t in trades if t["pnl"] <= 0])
    win_rate = (profit_trades / len(trades) * 100) if trades else 0

    # 构造权益曲线数据
    equity_data = []
    for i in range(len(signals_df)):
        ts = int(signals_df.index[i].timestamp())
        equity_data.append({"time": ts, "value": round(eq_curve[i], 2)})

    return BacktestResult(
        total_return=round(total_return, 2),
        annual_return=round(annual_return, 2),
        max_drawdown=round(drawdown, 2),
        win_rate=round(win_rate, 2),
        total_trades=len(trades),
        profit_trades=profit_trades,
        loss_trades=loss_trades,
        sharpe_ratio=round(sharpe, 2),
        trades=trades_df,
        strategy_name=strategy_name,
        equity_curve=equity_data,
    )


def backtest_compare(
    df: pd.DataFrame,
    params: SignalParams = None,
    initial_capital: float = 1000000.0,
    commission: float = 0.001,
    stamp_tax: float = 0.001,
    strategy_ids: list = None,
    stop_config=None,
) -> list:
    """
    对指定策略跑回测并返回对比数据
    strategy_ids: 要对比的策略ID列表，None则跑全部
    """
    if strategy_ids is None:
        strategy_ids = list(STRATEGIES.keys())

    # compute_signals once
    signals_df = compute_signals(df, params)

    results = []
    for sid in strategy_ids:
        if sid not in STRATEGIES:
            continue
        # Call backtest which will reuse cached data
        result = backtest(df, params=params, initial_capital=initial_capital,
                         commission=commission, stamp_tax=stamp_tax, strategy=sid,
                         stop_config=stop_config)
        results.append({
            "strategy_id": sid,
            "strategy_name": result.strategy_name,
            "total_return": result.total_return,
            "annual_return": result.annual_return,
            "max_drawdown": result.max_drawdown,
            "win_rate": result.win_rate,
            "total_trades": result.total_trades,
            "sharpe_ratio": result.sharpe_ratio,
            "equity_curve": result.equity_curve,
        })

    results.sort(key=lambda x: x["total_return"], reverse=True)
    return results
