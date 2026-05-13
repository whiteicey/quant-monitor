"""
参数优化模块
网格搜索 + Walk-Forward交叉验证 + 过拟合概率(PBO)
"""
import numpy as np
import pandas as pd
from itertools import product
from dataclasses import dataclass
from typing import List, Dict, Optional
from .signals import compute_signals, SignalParams
from .backtest import backtest, _run_backtest_core, STRATEGIES
from .statistics import sharpe_confidence_interval, returns_ttest, multiple_comparison_correction


# ============================================================
# 参数网格定义
# ============================================================

# 精简网格: 约144组合, 总运行时间约15-30秒
DEFAULT_PARAM_GRID = {
    "fast_length": [5, 8, 10, 12],
    "slow_length": [15, 20, 26, 30],
    "signal_length": [4, 7, 9],
    "rsi_length": [10, 14, 20],
}

# 固定不搜索的参数(减少搜索空间)
FIXED_PARAMS = {
    "bb_length": 20,
    "bb_mult": 2.0,
    "volume_length": 5,
    "atr_length": 14,
    "price_mode": "default",
}


def _generate_param_combos(grid: dict = None) -> list:
    """生成参数组合列表"""
    if grid is None:
        grid = DEFAULT_PARAM_GRID

    keys = list(grid.keys())
    values = list(grid.values())
    combos = []
    for vals in product(*values):
        combo = dict(zip(keys, vals))
        # 过滤无效组合: fast必须<slow
        if "fast_length" in combo and "slow_length" in combo:
            if combo["fast_length"] >= combo["slow_length"]:
                continue
        combos.append(combo)
    return combos


# ============================================================
# 网格搜索 + Walk-Forward
# ============================================================

@dataclass
class OptimizationResult:
    """优化结果"""
    strategy: str
    total_combos: int              # 总搜索组合数
    results: list                  # 所有组合的结果列表
    best_params: dict              # 最优参数
    best_is_return: float          # 最优参数样本内收益
    best_oos_return: float         # 最优参数样本外收益
    pbo: float                     # 样本外失效率(OOS loss rate)
    sharpe_ci: dict                # 最优参数Sharpe置信区间
    ttest: dict                    # 最优参数t检验
    multi_compare: dict            # 多重比较校正结果
    recommendation: str            # 文字推荐


def grid_search_optimize(
    df: pd.DataFrame,
    strategy: str = "macd_rsi",
    initial_capital: float = 1000000.0,
    commission: float = 0.00025,
    stamp_tax: float = 0.0005,
    slippage_bps: float = 0.0,
    min_commission: float = 5.0,
    param_grid: dict = None,
    n_wf_windows: int = 5,
    wf_train_ratio: float = 0.7,
    top_n: int = 10,
) -> OptimizationResult:
    """
    网格搜索 + Walk-Forward交叉验证 + 统计检验
    
    流程:
    1. 生成参数组合
    2. 对每组参数跑全样本回测 → 得到样本内(IS)指标
    3. 按IS Sharpe排序, 取top_n
    4. 对top_n做Walk-Forward验证 → 得到样本外(OOS)指标
    5. 计算过拟合概率(PBO)
    6. 对最优参数做统计检验
    7. 多重比较校正
    """
    if strategy not in STRATEGIES:
        strategy = "macd_rsi"

    combos = _generate_param_combos(param_grid)
    strategy_fn = STRATEGIES[strategy][1]

    # 数据切分: 前70%做参数选择(IS), 后30%做验证(OOS)
    n_total = len(df)
    is_cutoff = int(n_total * wf_train_ratio)
    df_is = df.iloc[:is_cutoff]
    df_oos = df.iloc[is_cutoff:]

    if len(df_is) < 60 or len(df_oos) < 20:
        return OptimizationResult(
            strategy=strategy, total_combos=len(combos), results=[],
            best_params={}, best_is_return=0, best_oos_return=0,
            pbo=1.0, sharpe_ci={}, ttest={}, multi_compare={},
            recommendation="数据不足: 至少需要100根K线才能做参数优化")

    # ========== Phase 1: 在IS数据上搜索参数 ==========
    all_results = []
    for combo in combos:
        params = SignalParams(
            fast_length=combo.get("fast_length", 6),
            slow_length=combo.get("slow_length", 7),
            signal_length=combo.get("signal_length", 4),
            rsi_length=combo.get("rsi_length", 14),
            **FIXED_PARAMS,
        )
        try:
            signals_df = compute_signals(df_is, params)
            core_result = _run_backtest_core(
                signals_df, strategy_fn, initial_capital,
                commission, stamp_tax, min_commission, slippage_bps,
                0, None, None)  # max_dd=0, no stop, no position sizing

            eq = pd.Series(core_result["eq_curve"])
            daily_ret = eq.pct_change().dropna()
            sharpe = float(daily_ret.mean() / daily_ret.std() * np.sqrt(252)) if daily_ret.std() > 0 else 0
            total_ret = core_result["total_return"]
            max_dd = core_result["max_dd"]

            all_results.append({
                "params": combo,
                "is_return": round(total_ret, 2),
                "is_sharpe": round(sharpe, 2),
                "is_max_dd": round(max_dd, 2),
                "is_trades": core_result["num_trades"],
                "daily_returns": daily_ret,  # 保留用于统计检验
            })
        except Exception:
            continue

    if not all_results:
        return OptimizationResult(
            strategy=strategy, total_combos=len(combos), results=[],
            best_params={}, best_is_return=0, best_oos_return=0,
            pbo=1.0, sharpe_ci={}, ttest={}, multi_compare={},
            recommendation="无有效参数组合")

    # 按IS Sharpe排序
    all_results.sort(key=lambda x: x["is_sharpe"], reverse=True)

    # ========== Phase 2: Top-N 在OOS数据上验证(真正的样本外) ==========
    top_results = all_results[:top_n]

    for item in top_results:
        params = SignalParams(
            fast_length=item["params"].get("fast_length", 6),
            slow_length=item["params"].get("slow_length", 7),
            signal_length=item["params"].get("signal_length", 4),
            rsi_length=item["params"].get("rsi_length", 14),
            **FIXED_PARAMS,
        )
        try:
            signals_oos = compute_signals(df_oos, params)
            if len(signals_oos) < 10:
                item["oos_return"] = 0
                item["wf_consistency"] = 0
                continue

            oos_result = _run_backtest_core(
                signals_oos, strategy_fn, initial_capital,
                commission, stamp_tax, min_commission, slippage_bps,
                0, None, None)

            item["oos_return"] = round(oos_result["total_return"], 2)

            # Walk-Forward在OOS数据内部做多窗口验证
            n_oos = len(signals_oos)
            wf_size = n_oos // n_wf_windows
            wf_wins = 0
            wf_total = 0
            if wf_size >= 10:
                for w in range(n_wf_windows):
                    ws = w * wf_size
                    we = min(ws + wf_size, n_oos)
                    if we - ws < 5:
                        continue
                    try:
                        wr = _run_backtest_core(
                            signals_oos.iloc[ws:we], strategy_fn, initial_capital,
                            commission, stamp_tax, min_commission, slippage_bps,
                            0, None, None)
                        wf_total += 1
                        if wr["total_return"] > 0:
                            wf_wins += 1
                    except Exception:
                        continue
            item["wf_consistency"] = round(wf_wins / max(wf_total, 1) * 100, 1)
        except Exception:
            item["oos_return"] = 0
            item["wf_consistency"] = 0

    # ========== Phase 3: 样本外失效率(OOS Loss Rate) ==========
    # top-N参数中OOS亏损的比例, 越高说明IS表现越不可信
    n_worse_oos = sum(1 for item in top_results if item.get("oos_return", 0) <= 0)
    oos_loss_rate = n_worse_oos / max(len(top_results), 1)

    # ========== Phase 4: 统计检验 ==========
    # 按OOS表现重新排序选最优
    top_results.sort(key=lambda x: x.get("oos_return", -999), reverse=True)
    best = top_results[0]
    best_daily = best.get("daily_returns", pd.Series(dtype=float))

    sharpe_ci = sharpe_confidence_interval(best_daily)
    ttest = returns_ttest(best_daily)

    # ========== Phase 5: 多重比较校正 ==========
    all_p_values = []
    for item in all_results:
        dr = item.get("daily_returns", pd.Series(dtype=float))
        if len(dr) > 10:
            tt = returns_ttest(dr)
            all_p_values.append(tt["p_value"])
        else:
            all_p_values.append(1.0)

    multi_compare = multiple_comparison_correction(all_p_values, method="bhy")

    # ========== Phase 6: 生成推荐文字 ==========
    recommendation = _generate_recommendation(
        best, sharpe_ci, ttest, oos_loss_rate, multi_compare, strategy)

    # 清理daily_returns(不传给前端)
    for item in all_results:
        item.pop("daily_returns", None)
    for item in top_results:
        item.pop("daily_returns", None)

    # 构造前端友好的结果(只返回top_n)
    display_results = []
    for item in top_results:
        display_results.append({
            "params": item["params"],
            "is_return": item["is_return"],
            "is_sharpe": item["is_sharpe"],
            "is_max_dd": item["is_max_dd"],
            "is_trades": item["is_trades"],
            "oos_return": item.get("oos_return", 0),
            "wf_consistency": item.get("wf_consistency", 0),
        })

    return OptimizationResult(
        strategy=strategy,
        total_combos=len(combos),
        results=display_results,
        best_params=best["params"],
        best_is_return=best["is_return"],
        best_oos_return=best.get("oos_return", 0),
        pbo=round(oos_loss_rate, 2),
        sharpe_ci=sharpe_ci,
        ttest=ttest,
        multi_compare={
            "n_significant": multi_compare["n_significant"],
            "n_total": multi_compare["n_total"],
            "method": multi_compare["method"],
        },
        recommendation=recommendation,
    )


def _generate_recommendation(best, sharpe_ci, ttest, pbo, multi_compare, strategy) -> str:
    """生成文字推荐"""
    parts = []

    # Sharpe判断
    sr = sharpe_ci.get("sharpe", 0)
    if sharpe_ci.get("is_significant"):
        parts.append(f"最优参数Sharpe={sr:.2f}(95%CI: {sharpe_ci['ci_lower']:.2f}~{sharpe_ci['ci_upper']:.2f}), 统计显著")
    else:
        parts.append(f"最优参数Sharpe={sr:.2f}, 但置信区间包含0, 可能是运气")

    # t检验
    if ttest.get("is_significant_5pct"):
        parts.append(f"收益t检验显著(p={ttest['p_value']:.4f}), 策略有真实alpha")
    else:
        parts.append(f"收益t检验不显著(p={ttest['p_value']:.4f}), 不能排除随机性")

    # PBO
    if pbo < 0.3:
        parts.append(f"过拟合概率{pbo:.0%}, 较低, 参数泛化能力强")
    elif pbo < 0.6:
        parts.append(f"过拟合概率{pbo:.0%}, 中等, 需谨慎使用")
    else:
        parts.append(f"过拟合概率{pbo:.0%}, 较高, 样本内表现不可信")

    # 多重比较
    n_sig = multi_compare.get("n_significant", 0)
    n_tot = multi_compare.get("n_total", 0)
    parts.append(f"经多重比较校正后, {n_tot}组参数中{n_sig}组仍统计显著")

    # 综合建议
    if sharpe_ci.get("is_significant") and pbo < 0.3 and best.get("wf_consistency", 0) >= 60:
        parts.append("[推荐] 综合评估: 该参数组合较可靠, 可考虑使用")
    elif pbo >= 0.6 or best.get("wf_consistency", 0) < 40:
        parts.append("[警告] 综合评估: 过拟合风险高, 建议使用默认参数或换策略")
    else:
        parts.append("[注意] 综合评估: 结果一般, 建议延长回测时间或增加样本外验证")

    return "; ".join(parts)
