"""
多资产组合回测引擎
输入: 资产代码列表 + 配置策略 + 再平衡频率
输出: 组合权益曲线 + 持仓变化 + 统计指标
"""
import pandas as pd
import numpy as np
from dataclasses import dataclass
from typing import List, Dict, Optional
from .data import fetch_stock_daily
from .allocation import compute_weights, ALLOCATION_STRATEGIES


# ============================================================
# 数据对齐
# ============================================================

def fetch_multi_asset_data(symbols: List[str], start: str, end: str) -> tuple:
    """
    拉取多只标的日线收盘价, 对齐日期(inner join)
    返回: (prices_df, actual_symbols)
      prices_df: DataFrame, index=DatetimeIndex, columns=实际获取成功的symbols
      actual_symbols: 实际获取成功的symbol列表(顺序与columns一致)
    """
    all_close = {}
    errors = []
    for sym in symbols:
        try:
            df = fetch_stock_daily(sym, start, end)
            if len(df) > 0:
                all_close[sym] = df["close"]
        except Exception as e:
            errors.append(f"{sym}: {e}")

    if not all_close:
        raise ConnectionError(f"所有标的数据获取失败: {'; '.join(errors)}")

    prices = pd.DataFrame(all_close)
    rows_before = len(prices)
    prices = prices.dropna()  # inner join: 只保留所有标的都有数据的日期
    actual_symbols = list(prices.columns)

    if len(prices) < 10:
        raise ValueError(f"对齐后数据不足: {len(prices)}行(至少需要10行)")

    if len(actual_symbols) < len(symbols):
        failed = set(symbols) - set(actual_symbols)
        print(f"  [数据获取] {len(failed)}个标的获取失败已跳过: {', '.join(failed)}")

    if rows_before - len(prices) > 10:
        print(f"  [数据对齐] 丢弃了 {rows_before - len(prices)} 行(部分标的数据缺失), "
              f"实际范围: {prices.index[0].strftime('%Y-%m-%d')} ~ {prices.index[-1].strftime('%Y-%m-%d')}")

    return prices, actual_symbols


# ============================================================
# 再平衡日期生成
# ============================================================

def _get_rebalance_dates(index: pd.DatetimeIndex, frequency: str) -> List[int]:
    """
    根据频率返回需要再平衡的bar索引列表
    frequency: weekly/biweekly/monthly/quarterly
    返回: bar索引列表 (0-based)
    """
    dates = index.to_series()

    if frequency == "weekly":
        iso = dates.dt.isocalendar()
        groups = dates.groupby(iso.week.values + iso.year.values * 100)
    elif frequency == "biweekly":
        iso = dates.dt.isocalendar()
        groups = dates.groupby(((iso.week.values - 1) // 2) + iso.year.values * 100)
    elif frequency == "monthly":
        groups = dates.groupby(dates.dt.to_period("M"))
    elif frequency == "quarterly":
        groups = dates.groupby(dates.dt.to_period("Q"))
    else:
        groups = dates.groupby(dates.dt.to_period("M"))

    rebal_dates = []
    for _, grp in groups:
        # 每组最后一个交易日 → 用位置索引避免get_loc的类型问题
        last_date = grp.index[-1]
        pos = index.searchsorted(last_date, side="right") - 1
        if 0 <= pos < len(index):
            rebal_dates.append(int(pos))

    return sorted(set(rebal_dates))


# ============================================================
# 组合回测
# ============================================================

@dataclass
class PortfolioResult:
    """组合回测结果"""
    total_return: float         # 总收益率 %
    annual_return: float        # 年化收益率 %
    max_drawdown: float         # 最大回撤 %
    sharpe_ratio: float         # Sharpe比率
    calmar_ratio: float         # Calmar比率 (年化/最大回撤)
    volatility: float           # 年化波动率 %
    total_rebalances: int       # 再平衡次数
    turnover: float             # 平均换手率 %
    equity_curve: list          # [{"time": int, "value": float}, ...]
    benchmark_curve: list       # 等权基准 [{"time": int, "value": float}, ...]
    weight_history: list        # [{"time": int, "weights": {symbol: float}}, ...]
    symbols: list               # 资产代码列表
    strategy_name: str          # 策略名称


def portfolio_backtest(
    symbols: List[str],
    start: str = "20200101",
    end: str = "",
    strategy: str = "equal_weight",
    rebalance_freq: str = "monthly",
    initial_capital: float = 1000000.0,
    commission: float = 0.00025,
    stamp_tax: float = 0.0005,
    slippage_bps: float = 0.0,
    min_commission: float = 5.0,
    # 策略参数
    momentum_lookback: int = 20,
    ma_period: int = 60,
) -> PortfolioResult:
    """
    多资产组合回测
    
    流程:
    1. 拉取所有标的日线数据, 对齐日期
    2. 在每个再平衡日计算目标权重
    3. 按目标权重调仓(买卖差额部分), 扣除交易成本
    4. 非再平衡日: 持仓不变, 按市价估值
    5. 返回权益曲线 + 持仓变化 + 统计指标
    """
    # 1. 数据
    prices, actual_symbols = fetch_multi_asset_data(symbols, start, end)
    symbols = actual_symbols  # 用实际获取成功的标的列表
    if len(symbols) < 2:
        raise ValueError(f"获取成功的标的不足2个(仅{len(symbols)}个), 无法进行组合回测")
    returns = prices.pct_change().fillna(0).replace([np.inf, -np.inf], 0)
    n_assets = len(symbols)
    n_days = len(prices)

    # 2. 再平衡日期
    rebal_indices = _get_rebalance_dates(prices.index, rebalance_freq)
    # 确保第一天也做一次初始配置
    if 0 not in rebal_indices:
        rebal_indices = [0] + rebal_indices

    # 3. 回测主循环
    capital = initial_capital
    # 持仓: 每个资产持有的"份数"(用金额表示, 不用股数, 因为ETF/股票单位不同)
    holdings = np.zeros(n_assets)  # 每个资产的市值
    eq_curve = []
    weight_history = []
    total_turnover = 0.0
    n_rebalances = 0

    # A股印花税豁免: ETF/基金全部免征, 仅个股征收
    # 识别逻辑: sub_category 为 dividend_stock 的是个股, 其余全部免征
    from .presets import ASSET_LIBRARY
    stamp_exempt = set()
    for j, sym in enumerate(symbols):
        a = ASSET_LIBRARY.get(sym)
        if a and a.sub_category != "dividend_stock":
            stamp_exempt.add(j)
        elif not a:
            # 未知资产: 按代码前缀判断(5/1/51开头=基金/ETF, 免征)
            if sym.startswith(("5", "1")):
                stamp_exempt.add(j)

    for i in range(n_days):
        date = prices.index[i]

        # 非第一天: 先应用今日收益率(不论是否再平衡日)
        if i > 0:
            day_returns = returns.iloc[i].values
            holdings = holdings * (1 + day_returns)
            holdings = np.maximum(holdings, 0)

        if i == 0:
            # 第一天: 按策略计算权重并建仓
            target_weights = compute_weights(
                strategy, returns.iloc[:1], prices=prices.iloc[:1],
                lookback=momentum_lookback, ma_period=ma_period)
            target_values = initial_capital * target_weights
            # 建仓成本
            total_cost = 0.0
            for j in range(n_assets):
                if target_values[j] > 0:
                    cost = max(target_values[j] * commission, min_commission)
                    slippage_cost = target_values[j] * slippage_bps / 10000
                    total_cost += cost + slippage_cost
            # 成本按比例扣除
            if initial_capital > total_cost:
                cost_ratio = (initial_capital - total_cost) / initial_capital
                holdings = target_values * cost_ratio
            else:
                holdings = target_values
            holdings = np.maximum(holdings, 0)
            n_rebalances += 1
            weight_history.append({
                "time": int(date.timestamp()),
                "weights": {symbols[j]: round(float(target_weights[j]), 4) for j in range(n_assets)},
            })
        elif i in rebal_indices:
            # 再平衡日: 今日收益已在上面应用, 现在重新计算权重并调仓
            portfolio_value = holdings.sum()
            if portfolio_value < 1:
                eq_curve.append({"time": int(date.timestamp()), "value": round(float(holdings.sum()), 2)})
                continue

            target_weights = compute_weights(
                strategy, returns.iloc[:i+1], prices=prices.iloc[:i+1],
                lookback=momentum_lookback, ma_period=ma_period)
            target_values = portfolio_value * target_weights
            delta = target_values - holdings  # 需要买入(正)或卖出(负)的金额

            # 计算换手率
            turnover = np.abs(delta).sum() / (2 * portfolio_value) if portfolio_value > 0 else 0
            total_turnover += turnover

            # 计算交易成本
            total_cost = 0.0
            for j in range(n_assets):
                trade_amount = abs(delta[j])
                if trade_amount < 1:
                    continue
                comm = max(trade_amount * commission, min_commission)
                slip = trade_amount * slippage_bps / 10000
                # 卖出才收印花税, 债券/货币ETF免征
                stamp = trade_amount * stamp_tax if (delta[j] < 0 and j not in stamp_exempt) else 0
                total_cost += comm + slip + stamp

            # 调仓后持仓(扣成本)
            holdings = target_values
            if portfolio_value > total_cost:
                cost_ratio = (portfolio_value - total_cost) / portfolio_value
                holdings = holdings * cost_ratio
            holdings = np.maximum(holdings, 0)
            n_rebalances += 1

            weight_history.append({
                "time": int(date.timestamp()),
                "weights": {symbols[j]: round(float(target_weights[j]), 4) for j in range(n_assets)},
            })
        # else: 非再平衡日, 收益已在循环开头应用

        # 记录当日净值
        portfolio_value = holdings.sum()
        eq_curve.append({"time": int(date.timestamp()), "value": round(float(portfolio_value), 2)})

    # 4. 等权基准曲线
    eq_returns = returns.mean(axis=1)  # 每日等权平均收益
    benchmark_values = initial_capital * (1 + eq_returns).cumprod()
    benchmark_curve = []
    for i in range(n_days):
        benchmark_curve.append({
            "time": int(prices.index[i].timestamp()),
            "value": round(float(benchmark_values.iloc[i]), 2),
        })

    # 5. 统计指标
    eq_values = pd.Series([e["value"] for e in eq_curve])
    if len(eq_values) < 2 or eq_values.iloc[0] == 0:
        return PortfolioResult(
            total_return=0, annual_return=0, max_drawdown=0, sharpe_ratio=0,
            calmar_ratio=0, volatility=0, total_rebalances=0, turnover=0,
            equity_curve=eq_curve, benchmark_curve=benchmark_curve,
            weight_history=weight_history, symbols=symbols,
            strategy_name=ALLOCATION_STRATEGIES.get(strategy, ("", ""))[0])

    final_value = eq_values.iloc[-1]
    total_return = (final_value / initial_capital - 1) * 100
    days = (prices.index[-1] - prices.index[0]).days
    annual_return = ((final_value / initial_capital) ** (365.0 / max(days, 1)) - 1) * 100 if days > 0 else 0

    # 最大回撤
    peak = eq_values.cummax()
    drawdown = ((eq_values - peak) / peak * 100).min()

    # Sharpe
    daily_returns = eq_values.pct_change().dropna()
    volatility_annual = daily_returns.std() * np.sqrt(252) * 100
    sharpe = (daily_returns.mean() / daily_returns.std() * np.sqrt(252)) if daily_returns.std() > 0 else 0

    # Calmar
    calmar = annual_return / abs(drawdown) if drawdown != 0 else 0

    # 换手率
    avg_turnover = (total_turnover / max(n_rebalances - 1, 1)) * 100 if n_rebalances > 1 else 0

    strategy_name = ALLOCATION_STRATEGIES.get(strategy, ("未知", ""))[0]

    return PortfolioResult(
        total_return=round(total_return, 2),
        annual_return=round(annual_return, 2),
        max_drawdown=round(drawdown, 2),
        sharpe_ratio=round(sharpe, 2),
        calmar_ratio=round(calmar, 2),
        volatility=round(volatility_annual, 2),
        total_rebalances=n_rebalances,
        turnover=round(avg_turnover, 2),
        equity_curve=eq_curve,
        benchmark_curve=benchmark_curve,
        weight_history=weight_history,
        symbols=symbols,
        strategy_name=strategy_name,
    )
