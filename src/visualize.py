"""
可视化模块 - K线图 + 指标 + 信号标注
"""
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.patches import FancyBboxPatch

# 中文支持
plt.rcParams["font.sans-serif"] = ["SimHei", "Microsoft YaHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False


def plot_signals(df: pd.DataFrame, title: str = "多空监控系统", save_path: str = "chart.png", last_n: int = 120):
    """
    绘制完整的K线+指标+信号图
    df: compute_signals() 返回的 DataFrame
    """
    df = df.tail(last_n).copy()
    dates = df.index

    fig, axes = plt.subplots(4, 1, figsize=(18, 14), height_ratios=[4, 1.2, 1, 1],
                              gridspec_kw={"hspace": 0.08})
    fig.suptitle(title, fontsize=16, fontweight="bold", y=0.98)

    ax_price, ax_vol, ax_macd, ax_rsi = axes

    # ===== 价格 + 布林带 + EMA =====
    ax_price.plot(dates, df["close"], color="#333", linewidth=1, label="收盘价")
    ax_price.plot(dates, df["fast_ema"], color="blue", linewidth=0.8, alpha=0.7, label=f"快速EMA")
    ax_price.plot(dates, df["slow_ema"], color="red", linewidth=0.8, alpha=0.7, label=f"慢速EMA")
    ax_price.plot(dates, df["bb_upper"], color="gray", linewidth=0.5, linestyle="--")
    ax_price.plot(dates, df["bb_lower"], color="gray", linewidth=0.5, linestyle="--")
    ax_price.fill_between(dates, df["bb_upper"], df["bb_lower"], alpha=0.05, color="blue")
    ax_price.plot(dates, df["bb_basis"], color="gold", linewidth=0.5)

    # 标注信号
    strong_buy = df[df["strong_buy"]]
    strong_sell = df[df["strong_sell"]]
    weak_buy = df[df["weak_buy"] & ~df["strong_buy"]]
    weak_sell = df[df["weak_sell"] & ~df["strong_sell"]]

    ax_price.scatter(strong_buy.index, strong_buy["low"] * 0.99, marker="^", c="red", s=120, zorder=5, label="强烈买入")
    ax_price.scatter(strong_sell.index, strong_sell["high"] * 1.01, marker="v", c="green", s=120, zorder=5, label="强烈卖出")
    ax_price.scatter(weak_buy.index, weak_buy["low"] * 0.99, marker="^", c="#ff6b6b", s=60, zorder=4, label="弱势买入")
    ax_price.scatter(weak_sell.index, weak_sell["high"] * 1.01, marker="v", c="#51cf66", s=60, zorder=4, label="弱势卖出")

    ax_price.legend(loc="upper left", fontsize=8, ncol=4)
    ax_price.set_ylabel("价格")
    ax_price.grid(True, alpha=0.3)
    ax_price.tick_params(labelbottom=False)

    # ===== 成交量 =====
    colors = ["red" if c > o else "green" for c, o in zip(df["close"], df["open"])]
    ax_vol.bar(dates, df["volume"], color=colors, alpha=0.6, width=0.8)
    ax_vol.plot(dates, df["volume_avg"], color="blue", linewidth=0.8, label="均量线")
    ax_vol.set_ylabel("成交量")
    ax_vol.tick_params(labelbottom=False)
    ax_vol.grid(True, alpha=0.3)

    # ===== MACD =====
    macd_colors = ["red" if v >= 0 else "green" for v in df["macd_hist"]]
    ax_macd.bar(dates, df["macd_hist"], color=macd_colors, alpha=0.5, width=0.8)
    ax_macd.plot(dates, df["macd_line"], color="blue", linewidth=0.8, label="MACD")
    ax_macd.plot(dates, df["signal_line"], color="orange", linewidth=0.8, label="Signal")
    ax_macd.axhline(0, color="gray", linewidth=0.5)
    ax_macd.set_ylabel("MACD")
    ax_macd.legend(fontsize=7, loc="upper left")
    ax_macd.tick_params(labelbottom=False)
    ax_macd.grid(True, alpha=0.3)

    # ===== RSI =====
    ax_rsi.plot(dates, df["rsi"], color="purple", linewidth=1)
    ax_rsi.axhline(70, color="red", linewidth=0.5, linestyle="--")
    ax_rsi.axhline(30, color="green", linewidth=0.5, linestyle="--")
    ax_rsi.axhline(50, color="gray", linewidth=0.3, linestyle=":")
    ax_rsi.fill_between(dates, 70, 100, alpha=0.05, color="red")
    ax_rsi.fill_between(dates, 0, 30, alpha=0.05, color="green")
    ax_rsi.set_ylabel("RSI")
    ax_rsi.set_ylim(0, 100)
    ax_rsi.grid(True, alpha=0.3)

    # X轴日期格式
    ax_rsi.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
    ax_rsi.xaxis.set_major_locator(mdates.WeekdayLocator(interval=2))
    plt.xticks(rotation=45)

    plt.savefig(save_path, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close()
    return save_path


def print_report(signal_result, stock_name: str = ""):
    """终端打印信号报告"""
    s = signal_result
    header = f"{'='*50}"
    print(header)
    print(f"  {stock_name} 多空监控报告")
    print(header)
    print(f"  做多信号: {s.bullish_signals}/6  |  做空信号: {s.bearish_signals}/6")
    print(f"  趋势: {s.trend}")
    print(f"  RSI: {s.rsi_value}  |  MACD: {s.macd_value}")
    print(f"  价格位置: {s.price_position}%")
    print(f"  支撑: {s.support}  |  阻力: {s.resistance}")
    print(f"  推荐买入: {s.recommended_buy}  |  推荐卖出: {s.recommended_sell}")
    print(f"  止损价位: {s.stop_loss}")
    print()

    if s.strong_buy:
        print("  >>> 强烈买入信号 - 建议分批建仓 <<<")
    elif s.strong_sell:
        print("  >>> 强烈卖出信号 - 建议减仓或离场 <<<")
    elif s.weak_buy:
        print("  >>> 弱势买入信号 - 谨慎试探 <<<")
    elif s.weak_sell:
        print("  >>> 弱势卖出信号 - 注意风险 <<<")
    else:
        print("  >>> 观望状态 - 等待明确信号 <<<")
    print(header)


def print_backtest(result):
    """终端打印回测结果"""
    r = result
    print(f"\n{'='*50}")
    print(f"  回测报告")
    print(f"{'='*50}")
    print(f"  总收益率:   {r.total_return:>8.2f}%")
    print(f"  年化收益率: {r.annual_return:>8.2f}%")
    print(f"  最大回撤:   {r.max_drawdown:>8.2f}%")
    print(f"  夏普比率:   {r.sharpe_ratio:>8.2f}")
    print(f"  总交易次数: {r.total_trades:>8d}")
    print(f"  盈利次数:   {r.profit_trades:>8d}")
    print(f"  亏损次数:   {r.loss_trades:>8d}")
    print(f"  胜率:       {r.win_rate:>8.2f}%")
    print(f"{'='*50}")
    if not r.trades.empty:
        print("\n  最近交易:")
        print(r.trades.tail(10).to_string(index=False))
