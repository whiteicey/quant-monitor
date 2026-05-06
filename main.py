"""
东芯股份(688110)多空监控系统 - Python版
用法:
  python main.py                    # 默认分析688110
  python main.py 600519             # 分析贵州茅台
  python main.py 688110 --backtest  # 回测
  python main.py 688110 --start 20220101  # 指定起始日期
"""
import sys
import argparse
from src import (
    fetch_stock_daily, fetch_stock_info,
    compute_signals, get_latest_signal, SignalParams,
    backtest,
    plot_signals, print_report, print_backtest,
)


def main():
    parser = argparse.ArgumentParser(description="A股多空监控系统")
    parser.add_argument("symbol", nargs="?", default="688110", help="股票代码 (默认688110)")
    parser.add_argument("--start", default="20230101", help="起始日期 YYYYMMDD")
    parser.add_argument("--end", default="", help="结束日期 YYYYMMDD")
    parser.add_argument("--backtest", action="store_true", help="运行回测")
    parser.add_argument("--strong-only", action="store_true", help="回测仅用强烈信号")
    parser.add_argument("--no-chart", action="store_true", help="不生成图表")
    parser.add_argument("--last-n", type=int, default=120, help="图表显示最近N根K线")
    args = parser.parse_args()

    print(f"\n正在获取 {args.symbol} 的行情数据...")
    try:
        stock_name = fetch_stock_info(args.symbol)
    except Exception:
        stock_name = args.symbol

    try:
        df = fetch_stock_daily(args.symbol, args.start, args.end)
    except Exception as e:
        print(f"获取数据失败: {e}")
        print("请确保已安装 akshare: pip install akshare")
        sys.exit(1)

    print(f"获取到 {len(df)} 条日线数据: {df.index[0].date()} ~ {df.index[-1].date()}")

    # 计算信号
    signals_df = compute_signals(df)
    latest = get_latest_signal(signals_df)

    # 打印报告
    print_report(latest, stock_name)

    # 生成图表
    if not args.no_chart:
        chart_path = f"{args.symbol}_chart.png"
        plot_signals(signals_df, title=f"{stock_name}({args.symbol}) 多空监控系统",
                     save_path=chart_path, last_n=args.last_n)
        print(f"\n图表已保存: {chart_path}")

    # 回测
    if args.backtest:
        print("\n正在运行回测...")
        result = backtest(df, use_strong_only=args.strong_only)
        print_backtest(result)


if __name__ == "__main__":
    main()
