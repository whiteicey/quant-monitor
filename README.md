# A股多空信号监控系统

实时A股技术分析、多空信号监控与策略回测系统。基于 Pine Script 指标逻辑移植到 Python，提供 Web GUI 界面，支持 Windows 一键运行。

## 快速开始

### Windows 用户（推荐）

1. 从 [Releases](https://github.com/whiteicey/quant-monitor/releases) 下载 `A股信号监控-Windows-x64.zip`
2. 解压后双击 `A股信号监控.exe`
3. 浏览器自动打开，即可使用

### 手机用户

1. 电脑运行程序后，查看终端显示的局域网地址（如 `http://192.168.x.x:5000`）
2. 手机连接同一 WiFi，浏览器打开该地址
3. 点击浏览器菜单「添加到主屏幕」，获得类似 APP 的体验

### 从源码运行

```bash
git clone https://github.com/whiteicey/quant-monitor.git
cd quant-monitor
pip install -r requirements.txt
python app.py
```

## 功能

### 实时监控
- 实时行情每 5 秒自动刷新（新浪财经数据源）
- K线图、MACD、RSI 实时更新
- 多空评分、信号状态、关键价位实时重算
- 交易时段/收盘状态自动识别

### 技术指标
- **EMA** 快慢线 + 金叉/死叉
- **MACD** 双线 + 柱状图
- **RSI** 超买超卖
- **布林带** 上中下轨
- **成交量** 放量分析
- **ATR** 波动率
- **支撑/阻力** 位识别

### K线周期
5分钟 / 15分钟 / 30分钟 / 1小时 / 日线 / 周线 / 月线

### 12 种回测策略

| 策略 | 说明 |
|------|------|
| MACD金叉+RSI超买 | 实测胜率最高，默认策略 |
| 稳健保守 | 多条件确认，适合稳健投资者 |
| 趋势跟踪 | EMA金叉+MACD确认，跟随趋势 |
| 多重共振 | 3个以上维度同时看多才买入 |
| 超跌反弹 | RSI超卖区抄底 |
| 布林带反弹 | 触下轨买入触上轨卖出 |
| 放量突破 | 放量上涨入场 |
| 支撑阻力 | 近支撑买入近阻力卖出 |
| 综合评分(标准) | 多空评分>=4触发 |
| 综合评分(严格) | 评分>=5且需金叉确认 |
| 纯MACD交叉 | 经典MACD金叉死叉 |
| 纯EMA交叉 | 经典EMA金叉死叉 |

### 13 套参数预设

每套参数基于多只股票实测数据优化，切换回测策略时自动匹配推荐预设：

- 默认参数（原始 Pine Script）
- MACD+RSI 优化（EMA 10/22，胜率 89%）
- 经典 MACD（12/26/9）
- 灵敏 MACD（8/17/9）
- 布林带窄幅/宽幅
- 趋势慢速/快速
- 稳健优化、超跌反弹优化、放量突破优化
- RSI 灵敏/平滑

所有参数均可手动调整，点击「分析」或「回测」按钮后立即生效。

### 股票搜索
支持代码和名称模糊搜索，输入「茅台」或「600519」均可。

### 多数据源
东方财富 → 新浪财经 → 腾讯财经，自动切换，确保数据可用。

## 项目结构

```
quant-monitor/
├── app.py              # Web GUI 主程序
├── main.py             # 命令行版本
├── requirements.txt    # Python 依赖
└── src/
    ├── indicators.py   # 技术指标计算（EMA/MACD/RSI/BB/ATR）
    ├── signals.py      # 多空信号评分系统
    ├── data.py         # 数据获取（多数据源+股票搜索+实时行情）
    ├── backtest.py     # 回测引擎（12策略+13预设）
    └── visualize.py    # matplotlib 图表生成
```

## 命令行用法

```bash
python main.py 688110                    # 分析东芯股份
python main.py 600519 --backtest         # 贵州茅台回测
python main.py 688110 --start 20220101   # 指定起始日期
python main.py 688110 --strong-only      # 仅强烈信号回测
```

## 打包

```bash
pip install pyinstaller
pyinstaller --onefile --name "A股信号监控" --add-data "src;src" --hidden-import=akshare --hidden-import=flask --collect-all akshare app.py
```

生成的 exe 在 `dist/` 目录。

## 免责声明

本工具仅供学习和研究用途，不构成任何投资建议。股市有风险，投资需谨慎。作者不对使用本工具产生的任何损失负责。

## License

MIT
