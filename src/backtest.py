"""
回测引擎 - 多策略信号模式
Phase 1 重构: 修复前视偏差/T+1/印花税/最低佣金/滑点/回撤熔断/基准对比/样本外测试
"""
import pandas as pd
import numpy as np
from dataclasses import dataclass
from .signals import compute_signals, SignalParams
from .extensions import StopConfig, apply_stop_rules, PositionConfig, calc_position_size


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
    benchmark_curve: list = None  # 买入持有基准 [{"time": int, "value": float}, ...]
    drawdown_breaker_triggered: bool = False  # 是否触发回撤熔断
    # 样本外测试结果
    oos_total_return: float = None   # out-of-sample 总收益率 %
    oos_sharpe_ratio: float = None
    oos_max_drawdown: float = None
    oos_trades: int = None


# ============================================================
# 交易成本计算工具
# ============================================================

def _calc_commission(amount: float, rate: float, min_commission: float = 5.0) -> float:
    """计算佣金，不低于最低佣金(默认5元)"""
    return max(amount * rate, min_commission)


def _calc_buy_cost(shares: int, price: float, commission_rate: float,
                   min_commission: float = 5.0) -> float:
    """买入总成本 = 股数*价格 + 佣金(无印花税)"""
    amount = shares * price
    comm = _calc_commission(amount, commission_rate, min_commission)
    return amount + comm


def _calc_sell_revenue(shares: int, price: float, commission_rate: float,
                       stamp_tax_rate: float = 0.0005,
                       min_commission: float = 5.0) -> float:
    """卖出净收入 = 股数*价格 - 佣金 - 印花税(卖方单边)"""
    amount = shares * price
    comm = _calc_commission(amount, commission_rate, min_commission)
    stamp = amount * stamp_tax_rate
    return amount - comm - stamp


def _apply_slippage(price: float, side: str, slippage_bps: float) -> float:
    """应用滑点: 买入价格上滑, 卖出价格下滑"""
    slip = price * slippage_bps / 10000
    return price + slip if side == "buy" else price - slip


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


def _strategy_mtf_confirm(df):
    """多周期共振：日线买入信号 + 周线趋势确认"""
    import pandas as pd
    daily_buy = df["macd_golden_cross"] | (df["bull_score"] >= 4)
    weekly_bull = df.get("weekly_ema_bullish", pd.Series(True, index=df.index)).fillna(True)
    buy = daily_buy & weekly_bull
    daily_sell = df["death_cross"] | (df["bear_score"] >= 4)
    weekly_bear = df.get("weekly_ema_bearish", pd.Series(False, index=df.index)).fillna(False)
    sell = daily_sell | weekly_bear
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
    "mtf_confirm":       ("多周期共振",          _strategy_mtf_confirm,       "日线信号+周线趋势确认，减少假信号"),
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
    "mtf_confirm": "macd_standard",
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


def _merge_weekly_signals(daily_df, weekly_signals_df):
    """将周线信号forward-fill到日线索引"""
    weekly_cols = ["ema_bullish", "ema_bearish", "macd_bullish", "macd_bearish", 
                   "bull_score", "bear_score"]
    available = [c for c in weekly_cols if c in weekly_signals_df.columns]
    if not available:
        return daily_df
    weekly_subset = weekly_signals_df[available].copy()
    weekly_subset.columns = [f"weekly_{c}" for c in available]
    merged = weekly_subset.reindex(daily_df.index, method="ffill")
    return pd.concat([daily_df, merged], axis=1)


def backtest(
    df: pd.DataFrame,
    params: SignalParams = None,
    initial_capital: float = 1000000.0,
    commission: float = 0.00025,
    stamp_tax: float = 0.0005,
    strategy: str = "macd_rsi",
    stop_config=None,
    position_config=None,
    weekly_signals_df=None,
    _precomputed_signals=None,
    # Phase 1 新增参数
    slippage_bps: float = 0.0,          # 滑点(基点), 如 5 = 0.05%
    min_commission: float = 5.0,         # 最低佣金(元)
    max_drawdown_limit: float = 0.0,     # 最大回撤熔断(0=不启用, 0.20=20%)
    oos_split: float = 0.0,             # 样本外比例(0=不启用, 0.3=后30%测试)
    # 保持向后兼容
    use_strong_only: bool = False,
) -> BacktestResult:
    """
    多策略回测引擎 (Phase 1 重构)
    
    修复项:
    1. 前视偏差: 信号在bar[i]产生, 在bar[i+1]的开盘价成交
    2. T+1: 买入当日不能卖出(卖出日必须>买入日)
    3. 印花税: 卖方单边0.05%(stamp_tax), 买方不收
    4. 最低佣金: 每笔不低于min_commission(默认5元)
    5. 滑点: slippage_bps基点, 买入加价卖出减价
    6. 回撤熔断: max_drawdown_limit, 触发后停止开新仓
    7. 基准对比: 返回buy&hold基准曲线
    8. 样本外测试: oos_split比例切分
    """
    if _precomputed_signals is not None:
        signals_df = _precomputed_signals.copy()
    else:
        signals_df = compute_signals(df, params)

    if weekly_signals_df is not None:
        signals_df = _merge_weekly_signals(signals_df, weekly_signals_df)

    # 向后兼容旧参数
    if strategy == "macd_rsi" and use_strong_only:
        strategy = "score_strong"

    if strategy not in STRATEGIES:
        strategy = "macd_rsi"

    strategy_name, strategy_fn, _ = STRATEGIES[strategy]
    buy_mask, sell_mask = strategy_fn(signals_df)

    # ---- 样本外测试: 只在训练集上跑, 测试集单独跑 ----
    oos_result = None
    if oos_split > 0:
        split_idx = int(len(signals_df) * (1 - oos_split))
        if split_idx > 50 and split_idx < len(signals_df) - 10:
            # 训练集
            train_signals = signals_df.iloc[:split_idx]
            train_buy = buy_mask.iloc[:split_idx]
            train_sell = sell_mask.iloc[:split_idx]
            # 测试集: 递归调用自身(不再split)
            test_signals = signals_df.iloc[split_idx:]
            oos_result = _run_backtest_core(
                test_signals, strategy_fn, initial_capital,
                commission, stamp_tax, min_commission, slippage_bps,
                max_drawdown_limit, stop_config, position_config)
            # 训练集继续下面的主逻辑
            signals_df = train_signals
            buy_mask = train_buy
            sell_mask = train_sell

    # ---- 主回测 ----
    result = _run_backtest_core(
        signals_df, strategy_fn, initial_capital,
        commission, stamp_tax, min_commission, slippage_bps,
        max_drawdown_limit, stop_config, position_config)

    # ---- 基准曲线: 买入持有 ----
    benchmark_curve = _calc_benchmark_curve(signals_df, initial_capital)

    # ---- 组装结果 ----
    trades = result["trades"]
    eq_curve = result["eq_curve"]
    breaker_triggered = result["breaker_triggered"]

    final_value = eq_curve[-1] if eq_curve else initial_capital
    total_return = (final_value / initial_capital - 1) * 100
    days = (signals_df.index[-1] - signals_df.index[0]).days
    annual_return = ((final_value / initial_capital) ** (365.0 / max(days, 1)) - 1) * 100 if days > 0 else 0

    eq_series = pd.Series(eq_curve)
    peak = eq_series.cummax()
    drawdown = ((eq_series - peak) / peak * 100).min()

    daily_returns = eq_series.pct_change().dropna()
    sharpe = (daily_returns.mean() / daily_returns.std() * np.sqrt(252)) if daily_returns.std() > 0 else 0

    trades_df = pd.DataFrame(trades) if trades else pd.DataFrame()
    profit_trades = len([t for t in trades if t["pnl"] > 0])
    loss_trades = len([t for t in trades if t["pnl"] <= 0])
    win_rate = (profit_trades / len(trades) * 100) if trades else 0

    # 权益曲线数据
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
        benchmark_curve=benchmark_curve,
        drawdown_breaker_triggered=breaker_triggered,
        oos_total_return=round(oos_result["total_return"], 2) if oos_result else None,
        oos_sharpe_ratio=round(oos_result["sharpe"], 2) if oos_result else None,
        oos_max_drawdown=round(oos_result["max_dd"], 2) if oos_result else None,
        oos_trades=oos_result["num_trades"] if oos_result else None,
    )


def _calc_benchmark_curve(signals_df: pd.DataFrame, initial_capital: float) -> list:
    """买入持有基准曲线: 第一天全仓买入, 持有到最后"""
    first_price = signals_df.iloc[0]["close"]
    benchmark = []
    for i in range(len(signals_df)):
        ts = int(signals_df.index[i].timestamp())
        val = initial_capital * signals_df.iloc[i]["close"] / first_price
        benchmark.append({"time": ts, "value": round(val, 2)})
    return benchmark


def _run_backtest_core(
    signals_df, strategy_fn, initial_capital,
    commission, stamp_tax, min_commission, slippage_bps,
    max_drawdown_limit, stop_config, position_config,
) -> dict:
    """
    核心回测循环 (修复前视偏差 + T+1 + 真实成本模型)
    
    关键逻辑:
    - 信号在 bar[i] 产生, 成交在 bar[i+1] 的开盘价(+滑点)
    - T+1: 卖出日必须严格晚于买入日 (entry_bar_idx < current bar)
    - 印花税只在卖出时收取
    - 佣金不低于min_commission
    """
    buy_mask, sell_mask = strategy_fn(signals_df)

    capital = initial_capital
    position = 0
    entry_price = 0.0
    entry_date = None
    entry_bar_idx = -1  # 买入时的bar索引, 用于T+1判断
    max_price_since_entry = 0.0
    trades = []
    eq_curve = []
    breaker_triggered = False
    breaker_active = False  # 回撤熔断激活后不再开新仓
    peak_equity = initial_capital  # 增量维护峰值

    # 待执行队列: 信号在bar[i], 执行在bar[i+1] (止损不走此队列, 盘中即时成交)
    pending_action = None  # ("buy", i) or ("sell", i, exit_type)

    n = len(signals_df)
    for i in range(n):
        row = signals_df.iloc[i]
        price = row["close"]
        bar_open = row["open"]
        bar_high = row["high"]
        bar_low = row["low"]
        atr_val = float(row.get("atr", 0)) if not pd.isna(row.get("atr", 0)) else 0
        date = signals_df.index[i]

        # ========== 第1步: 执行上一bar的待执行订单 ==========
        if pending_action is not None:
            action_type = pending_action[0]

            if action_type == "buy" and position == 0 and not breaker_active:
                fill_price = _apply_slippage(bar_open, "buy", slippage_bps)
                # Kelly模式
                hist_wr, hist_ratio = 0.5, 1.0
                if position_config and position_config.mode == "kelly" and len(trades) >= 10:
                    wins = sum(1 for t in trades if t["pnl"] > 0)
                    losses = len(trades) - wins
                    hist_wr = wins / len(trades) if trades else 0.5
                    avg_win = sum(t["return_pct"] for t in trades if t["pnl"] > 0) / max(wins, 1)
                    avg_loss = abs(sum(t["return_pct"] for t in trades if t["pnl"] <= 0) / max(losses, 1))
                    hist_ratio = avg_win / avg_loss if avg_loss > 0 else 1.0

                # calc_position_size 需要含佣金的等效价格
                eff_price = fill_price * (1 + commission)  # 近似含佣金价格
                shares = calc_position_size(capital, eff_price, position_config,
                                            win_rate=hist_wr, avg_win_loss_ratio=hist_ratio)
                if shares > 0:
                    cost = _calc_buy_cost(shares, fill_price, commission, min_commission)
                    if cost <= capital:
                        capital -= cost
                        position = shares
                        entry_price = fill_price
                        entry_date = date
                        entry_bar_idx = i
                        max_price_since_entry = bar_high

            elif action_type == "sell" and position > 0:
                fill_price = _apply_slippage(bar_open, "sell", slippage_bps)
                revenue = _calc_sell_revenue(position, fill_price, commission, stamp_tax, min_commission)
                # PnL = 价差收益 - 买入佣金 - 卖出佣金 - 卖出印花税
                gross_pnl = (fill_price - entry_price) * position
                buy_comm = _calc_commission(position * entry_price, commission, min_commission)
                sell_comm = _calc_commission(position * fill_price, commission, min_commission)
                sell_stamp = position * fill_price * stamp_tax
                pnl = gross_pnl - buy_comm - sell_comm - sell_stamp

                capital += revenue
                exit_type = pending_action[2] if len(pending_action) > 2 else "signal"
                trades.append({
                    "buy_date": str(entry_date.date()) if hasattr(entry_date, 'date') else str(entry_date)[:10],
                    "sell_date": str(date.date()) if hasattr(date, 'date') else str(date)[:10],
                    "entry_price": round(entry_price, 2),
                    "exit_price": round(fill_price, 2),
                    "shares": position,
                    "pnl": round(pnl, 2),
                    "return_pct": round((fill_price / entry_price - 1) * 100, 2),
                    "exit_type": exit_type,
                })
                position = 0
                max_price_since_entry = 0.0

            pending_action = None

        # ========== 第2步: 记录当前净值 ==========
        eq_curve.append(capital + position * price)

        # ========== 第3步: 回撤熔断检查 ==========
        if max_drawdown_limit > 0 and not breaker_active:
            current_value = eq_curve[-1]
            peak_equity = max(peak_equity, current_value)
            current_dd = (peak_equity - current_value) / peak_equity
            if current_dd >= max_drawdown_limit:
                breaker_active = True
                breaker_triggered = True

        # ========== 第4步: 产生新信号(下一bar执行) ==========
        is_buy = bool(buy_mask.iloc[i]) if hasattr(buy_mask, 'iloc') else bool(buy_mask[i])
        is_sell = bool(sell_mask.iloc[i]) if hasattr(sell_mask, 'iloc') else bool(sell_mask[i])

        # 止盈止损检查(盘中即时触发, 当日当价成交, 不走next-bar)
        if position > 0 and stop_config:
            max_price_since_entry = max(max_price_since_entry, bar_high)
            stop_type, stop_fill_price = apply_stop_rules(
                price, bar_low, bar_high, entry_price,
                max_price_since_entry, atr_val, stop_config)
            if stop_type:
                # T+1: 卖出执行日(今天i) > 买入执行日(entry_bar_idx)
                if i > entry_bar_idx:
                    fill_price = _apply_slippage(stop_fill_price, "sell", slippage_bps)
                    revenue = _calc_sell_revenue(position, fill_price, commission, stamp_tax, min_commission)
                    gross_pnl = (fill_price - entry_price) * position
                    buy_comm = _calc_commission(position * entry_price, commission, min_commission)
                    sell_comm = _calc_commission(position * fill_price, commission, min_commission)
                    sell_stamp = position * fill_price * stamp_tax
                    pnl = gross_pnl - buy_comm - sell_comm - sell_stamp

                    capital += revenue
                    trades.append({
                        "buy_date": str(entry_date.date()) if hasattr(entry_date, 'date') else str(entry_date)[:10],
                        "sell_date": str(date.date()) if hasattr(date, 'date') else str(date)[:10],
                        "entry_price": round(entry_price, 2),
                        "exit_price": round(fill_price, 2),
                        "shares": position,
                        "pnl": round(pnl, 2),
                        "return_pct": round((fill_price / entry_price - 1) * 100, 2),
                        "exit_type": stop_type,
                    })
                    position = 0
                    max_price_since_entry = 0.0
                continue  # 止损优先, 不看其他信号

        if is_buy and position == 0 and not breaker_active:
            pending_action = ("buy", i)
        elif is_sell and position > 0:
            # T+1: 买入执行在entry_bar_idx, 卖出执行在i+1
            # 要求 i+1 > entry_bar_idx → i >= entry_bar_idx
            if i >= entry_bar_idx:
                pending_action = ("sell", i, "signal")

    # ---- 未平仓持仓 ----
    if position > 0:
        final_price = signals_df.iloc[-1]["close"]
        final_date = signals_df.index[-1]
        market_value = position * final_price
        gross_pnl = (final_price - entry_price) * position
        buy_comm = _calc_commission(position * entry_price, commission, min_commission)
        pnl = gross_pnl - buy_comm  # 未卖出不扣卖出费用

        trades.append({
            "buy_date": str(entry_date.date()) if hasattr(entry_date, 'date') else str(entry_date)[:10],
            "sell_date": str(final_date.date()) + "(未平仓)" if hasattr(final_date, 'date') else str(final_date)[:10] + "(未平仓)",
            "entry_price": round(entry_price, 2),
            "exit_price": round(final_price, 2),
            "shares": position,
            "pnl": round(pnl, 2),
            "return_pct": round((final_price / entry_price - 1) * 100, 2),
            "exit_type": "holding",
        })
        # eq_curve最后一个值已经包含了position * close

    # 返回核心数据
    final_value = eq_curve[-1] if eq_curve else initial_capital
    total_ret = (final_value / initial_capital - 1) * 100
    eq_s = pd.Series(eq_curve)
    peak = eq_s.cummax()
    max_dd = ((eq_s - peak) / peak * 100).min()
    daily_ret = eq_s.pct_change().dropna()
    sharpe_val = (daily_ret.mean() / daily_ret.std() * np.sqrt(252)) if daily_ret.std() > 0 else 0

    return {
        "trades": trades,
        "eq_curve": eq_curve,
        "breaker_triggered": breaker_triggered,
        "total_return": total_ret,
        "sharpe": sharpe_val,
        "max_dd": max_dd,
        "num_trades": len(trades),
    }


def backtest_compare(
    df: pd.DataFrame,
    params: SignalParams = None,
    initial_capital: float = 1000000.0,
    commission: float = 0.00025,
    stamp_tax: float = 0.0005,
    strategy_ids: list = None,
    stop_config=None,
    position_config=None,
    weekly_signals_df=None,
    slippage_bps: float = 0.0,
    min_commission: float = 5.0,
    max_drawdown_limit: float = 0.0,
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
        result = backtest(df, params=params, initial_capital=initial_capital,
                         commission=commission, stamp_tax=stamp_tax, strategy=sid,
                         stop_config=stop_config, position_config=position_config,
                         weekly_signals_df=weekly_signals_df,
                         _precomputed_signals=signals_df,
                         slippage_bps=slippage_bps,
                         min_commission=min_commission,
                         max_drawdown_limit=max_drawdown_limit)
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
            "benchmark_curve": result.benchmark_curve,
        })

    results.sort(key=lambda x: x["total_return"], reverse=True)
    return results
