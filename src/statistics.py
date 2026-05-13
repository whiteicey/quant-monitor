"""
统计检验模块
Sharpe置信区间 / 收益t检验 / 多重比较校正
"""
import numpy as np
import pandas as pd
from typing import List, Tuple, Optional


def sharpe_confidence_interval(
    returns: pd.Series,
    confidence: float = 0.95,
    periods: int = 252,
) -> dict:
    """
    Sharpe比率的置信区间 (Lo 2002)
    
    使用Sharpe比率的渐近标准误:
      SE(SR) = sqrt((1 + 0.5*SR^2) / (N-1))
    其中 SR 是年化Sharpe, N 是观测数
    
    返回:
    {
        "sharpe": float,         # 点估计
        "se": float,             # 标准误
        "ci_lower": float,       # 置信区间下界
        "ci_upper": float,       # 置信区间上界
        "confidence": float,     # 置信水平
        "is_significant": bool,  # CI下界>0则显著
    }
    """
    returns = returns.dropna()
    n = len(returns)
    if n < 10:
        return {"sharpe": 0, "se": 0, "ci_lower": 0, "ci_upper": 0,
                "confidence": confidence, "is_significant": False}

    mean_r = returns.mean()
    std_r = returns.std(ddof=1)
    if std_r < 1e-10:
        return {"sharpe": 0, "se": 0, "ci_lower": 0, "ci_upper": 0,
                "confidence": confidence, "is_significant": False}

    # 年化Sharpe
    sr_daily = mean_r / std_r  # 日频Sharpe
    sr = sr_daily * np.sqrt(periods)  # 年化Sharpe

    # 标准误 (Lo 2002): SE用日频SR计算, 再年化
    se = np.sqrt((1 + 0.5 * sr_daily ** 2) / (n - 1)) * np.sqrt(periods)

    # z值
    from scipy.stats import norm
    z = norm.ppf((1 + confidence) / 2)

    ci_lower = sr - z * se
    ci_upper = sr + z * se

    return {
        "sharpe": round(float(sr), 4),
        "se": round(float(se), 4),
        "ci_lower": round(float(ci_lower), 4),
        "ci_upper": round(float(ci_upper), 4),
        "confidence": confidence,
        "is_significant": bool(ci_lower > 0),
    }


def returns_ttest(
    returns: pd.Series,
    null_mean: float = 0.0,
) -> dict:
    """
    单样本t检验: 策略日收益均值是否显著不等于null_mean
    
    返回:
    {
        "mean_return": float,    # 日均收益
        "annual_return": float,  # 年化收益
        "t_stat": float,         # t统计量
        "p_value": float,        # p值(双尾)
        "p_value_one": float,    # p值(单尾, 检验>0)
        "is_significant_5pct": bool,
        "is_significant_1pct": bool,
    }
    """
    returns = returns.dropna()
    n = len(returns)
    if n < 10:
        return {"mean_return": 0, "annual_return": 0, "t_stat": 0,
                "p_value": 1, "p_value_one": 1,
                "is_significant_5pct": False, "is_significant_1pct": False}

    mean_r = returns.mean()
    std_r = returns.std(ddof=1)
    if std_r < 1e-10:
        return {"mean_return": float(mean_r), "annual_return": float(mean_r * 252 * 100),
                "t_stat": 0, "p_value": 1, "p_value_one": 1,
                "is_significant_5pct": False, "is_significant_1pct": False}

    from scipy.stats import t as t_dist
    t_stat = (mean_r - null_mean) / (std_r / np.sqrt(n))
    p_value = 2 * (1 - t_dist.cdf(abs(t_stat), df=n - 1))
    p_value_one = 1 - t_dist.cdf(t_stat, df=n - 1)

    return {
        "mean_return": round(float(mean_r * 100), 6),  # 日均收益%
        "annual_return": round(float(mean_r * 252 * 100), 2),  # 年化%
        "t_stat": round(float(t_stat), 4),
        "p_value": round(float(p_value), 6),
        "p_value_one": round(float(p_value_one), 6),
        "is_significant_5pct": p_value < 0.05,
        "is_significant_1pct": p_value < 0.01,
    }


def multiple_comparison_correction(
    p_values: List[float],
    method: str = "bhy",
) -> dict:
    """
    多重比较校正: 测试了N个策略/参数组合, 校正p值
    
    方法:
    - "bonferroni": 最保守, p_adj = p * N
    - "bhy": Benjamini-Hochberg-Yekutieli, 控制FDR, 更宽松
    
    返回:
    {
        "original_p": list,
        "adjusted_p": list,
        "significant_5pct": list[bool],  # 校正后哪些仍显著
        "n_significant": int,
        "n_total": int,
        "method": str,
    }
    """
    n = len(p_values)
    if n == 0:
        return {"original_p": [], "adjusted_p": [], "significant_5pct": [],
                "n_significant": 0, "n_total": 0, "method": method}

    p_arr = np.array(p_values, dtype=float)

    if method == "bonferroni":
        adjusted = np.minimum(p_arr * n, 1.0)
    elif method == "bhy":
        # Benjamini-Hochberg-Yekutieli
        sorted_idx = np.argsort(p_arr)
        sorted_p = p_arr[sorted_idx]
        # c(m) = sum(1/k for k in 1..m)
        cm = sum(1.0 / k for k in range(1, n + 1))
        adjusted = np.ones(n)
        for i in range(n):
            rank = i + 1
            adjusted[sorted_idx[i]] = sorted_p[i] * n * cm / rank
        # 保持单调性: 从后往前取cummin
        adj_sorted = adjusted[sorted_idx]
        for i in range(n - 2, -1, -1):
            adj_sorted[i] = min(adj_sorted[i], adj_sorted[i + 1])
        adjusted[sorted_idx] = adj_sorted
        adjusted = np.minimum(adjusted, 1.0)
    else:
        adjusted = p_arr

    significant = adjusted < 0.05

    return {
        "original_p": [round(float(p), 6) for p in p_arr],
        "adjusted_p": [round(float(p), 6) for p in adjusted],
        "significant_5pct": significant.tolist(),
        "n_significant": int(significant.sum()),
        "n_total": n,
        "method": method,
    }
