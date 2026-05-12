"""
资产配置策略实现
6种策略: 等权/动量轮动/均线过滤/风险平价/均值方差/自适应
"""
import numpy as np
import pandas as pd
from typing import Dict, List, Optional


# ============================================================
# 策略注册表
# ============================================================

ALLOCATION_STRATEGIES = {
    "equal_weight":   ("等权配置",     "每个资产等权分配，定期再平衡"),
    "momentum":       ("动量轮动",     "选近N天涨幅最大的资产集中持有"),
    "ma_filter":      ("均线过滤",     "仅持有价格在均线上方的资产，其余转现金"),
    "risk_parity":    ("风险平价",     "按波动率的倒数分配权重，让每类资产贡献相同风险"),
    "mean_variance":  ("均值方差",     "马科维茨最优化，最大化夏普比率"),
    "adaptive":       ("自适应配置",   "动量+风险平价混合，兼顾趋势和风险"),
}


def get_strategy_list() -> list:
    """返回策略列表供前端使用"""
    return [{"id": k, "name": v[0], "desc": v[1]} for k, v in ALLOCATION_STRATEGIES.items()]


# ============================================================
# 各策略实现 — 输入收益率矩阵, 输出权重向量
# ============================================================

def equal_weight(returns: pd.DataFrame, **kwargs) -> np.ndarray:
    """等权: 1/N"""
    n = returns.shape[1]
    return np.ones(n) / n


def momentum(returns: pd.DataFrame, lookback: int = 20, top_k: int = 0, **kwargs) -> np.ndarray:
    """
    动量轮动: 选近lookback天累计收益最高的top_k个资产等权持有
    top_k=0 表示持有前一半
    """
    n = returns.shape[1]
    if top_k <= 0:
        top_k = max(1, n // 2)
    top_k = min(top_k, n)

    # 近lookback天累计收益
    if len(returns) < lookback:
        cum = (1 + returns).prod() - 1
    else:
        cum = (1 + returns.iloc[-lookback:]).prod() - 1

    cum = cum.fillna(-np.inf)  # NaN资产排在最后
    ranks = cum.values.argsort()[::-1]  # 降序排列
    weights = np.zeros(n)
    for i in range(top_k):
        weights[ranks[i]] = 1.0 / top_k
    return weights


def ma_filter(returns: pd.DataFrame, prices: pd.DataFrame = None,
              ma_period: int = 60, **kwargs) -> np.ndarray:
    """
    均线过滤: 价格在MA上方的资产等权持有, 全部在下方则100%现金(权重全0)
    prices: 收盘价DataFrame, 必须提供
    """
    n = returns.shape[1]
    if prices is None or prices.empty:
        return np.ones(n) / n  # fallback to equal weight

    # 最后一行价格 vs MA
    above_ma = np.zeros(n, dtype=bool)
    for j in range(n):
        col = prices.iloc[:, j].dropna()
        if len(col) < ma_period:
            ma = col.mean()
        else:
            ma = col.iloc[-ma_period:].mean()
        above_ma[j] = col.iloc[-1] > ma if len(col) > 0 else False

    n_above = above_ma.sum()
    if n_above == 0:
        return np.zeros(n)  # 全部在均线下方 → 空仓/现金
    weights = np.where(above_ma, 1.0 / n_above, 0.0)
    return weights


def risk_parity(returns: pd.DataFrame, lookback: int = 60, **kwargs) -> np.ndarray:
    """
    风险平价: 权重 ∝ 1/σ (波动率的倒数)
    使每个资产对组合波动率的贡献大致相等
    """
    n = returns.shape[1]
    if len(returns) < 5:
        return np.ones(n) / n

    # 用近lookback天的波动率
    window = returns.iloc[-lookback:] if len(returns) >= lookback else returns
    vols = window.std(ddof=0).values
    vols = np.where((vols < 1e-10) | np.isnan(vols), 1e-10, vols)  # 防除零+NaN

    inv_vol = 1.0 / vols
    weights = inv_vol / inv_vol.sum()
    return weights


def mean_variance(returns: pd.DataFrame, lookback: int = 120,
                  risk_free: float = 0.0, **kwargs) -> np.ndarray:
    """
    均值方差(马科维茨): 最大化Sharpe比率的最优权重
    使用解析解(仅做多约束用截断)
    """
    n = returns.shape[1]
    if len(returns) < 20 or n < 2:
        return np.ones(n) / n

    window = returns.iloc[-lookback:] if len(returns) >= lookback else returns
    mu = window.mean().values * 252  # 年化收益
    cov = window.cov().values * 252  # 年化协方差

    # 正则化协方差矩阵(防奇异)
    cov += np.eye(n) * 1e-6

    try:
        cov_inv = np.linalg.inv(cov)
    except np.linalg.LinAlgError:
        return np.ones(n) / n

    # 最大Sharpe解析解: w ∝ Σ^{-1} (μ - rf)
    excess = mu - risk_free
    raw_weights = cov_inv @ excess

    # 截断负权重(不允许做空), 重新归一化
    raw_weights = np.maximum(raw_weights, 0)
    total = raw_weights.sum()
    if total < 1e-10:
        return np.ones(n) / n
    return raw_weights / total


def adaptive(returns: pd.DataFrame, prices: pd.DataFrame = None,
             lookback: int = 60, **kwargs) -> np.ndarray:
    """
    自适应: 动量权重和风险平价权重各50%混合
    动量部分: 近期收益排名加权
    风险部分: 波动率倒数加权
    """
    w_mom = momentum(returns, lookback=lookback, **kwargs)
    w_rp = risk_parity(returns, lookback=lookback, **kwargs)

    # 50/50 混合
    w = 0.5 * w_mom + 0.5 * w_rp

    # 归一化
    total = w.sum()
    if total < 1e-10:
        n = returns.shape[1]
        return np.ones(n) / n
    return w / total


# ============================================================
# 策略调度
# ============================================================

_STRATEGY_FN = {
    "equal_weight": equal_weight,
    "momentum": momentum,
    "ma_filter": ma_filter,
    "risk_parity": risk_parity,
    "mean_variance": mean_variance,
    "adaptive": adaptive,
}


def compute_weights(strategy: str, returns: pd.DataFrame,
                    prices: pd.DataFrame = None, **kwargs) -> np.ndarray:
    """
    根据策略ID计算权重
    returns: 日收益率 DataFrame, 列=各资产
    prices: 收盘价 DataFrame (均线过滤等策略需要)
    返回: numpy array, shape=(n_assets,), 求和=1 (或0表示空仓)
    """
    fn = _STRATEGY_FN.get(strategy, equal_weight)
    weights = fn(returns, prices=prices, **kwargs)

    # 安全检查
    weights = np.nan_to_num(weights, nan=0.0, posinf=0.0, neginf=0.0)
    weights = np.maximum(weights, 0)  # 不允许负权重
    total = weights.sum()
    if total > 1e-10:
        weights = weights / total
    return weights
