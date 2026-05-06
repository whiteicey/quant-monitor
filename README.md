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

## 原始 Pine Script

本项目的信号系统移植自以下 TradingView Pine Script 指标：

<details>
<summary>点击展开 Pine Script 源码</summary>

```pine
//@version=5
indicator("东芯股份(688110)多空监控系统", shorttitle="688110 Monitor", overlay=true)

// 输入参数 - 针对A股特性优化
fastLength = input.int(6, "快速EMA周期")
slowLength = input.int(7, "慢速EMA周期")
signalLength = input.int(4, "信号线周期")
rsiLength = input.int(14, "RSI周期")
bbLength = input.int(20, "布林带周期")
bbMult = input.float(2.0, "布林带标准差倍数")
volumeLength = input.int(5, "成交量平均周期")
atrLength = input.int(14, "ATR周期")

// 移动平均线
fastEMA = ta.ema(close, fastLength)
slowEMA = ta.ema(close, slowLength)
emaBullish = fastEMA > slowEMA and close > fastEMA
emaBearish = fastEMA < slowEMA and close < fastEMA

// 金叉死叉判断
goldenCross = ta.crossover(fastEMA, slowEMA)
deathCross = ta.crossunder(fastEMA, slowEMA)

// MACD
[macdLine, signalLine, _] = ta.macd(close, fastLength, slowLength, signalLength)
macdHistogram = macdLine - signalLine
macdBullish = macdLine > signalLine and macdLine > 0
macdBearish = macdLine < signalLine and macdLine < 0
macdGoldenCross = ta.crossover(macdLine, signalLine)
macdDeathCross = ta.crossunder(macdLine, signalLine)

// RSI
rsi = ta.rsi(close, rsiLength)
rsiOverbought = rsi > 70
rsiOversold = rsi < 30
rsiBullish = rsi > 45 and rsi < 70 and rsi > rsi[1]
rsiBearish = rsi < 55 and rsi > 30 and rsi < rsi[1]

// 布林带
bbBasis = ta.sma(close, bbLength)
bbDev = bbMult * ta.stdev(close, bbLength)
bbUpper = bbBasis + bbDev
bbLower = bbBasis - bbDev
bbBullish = close > bbBasis and close < bbUpper
bbBearish = close < bbBasis and close > bbLower
nearBBLower = close <= bbLower * 1.02
nearBBUpper = close >= bbUpper * 0.98

// 成交量分析
volumeAvg = ta.sma(volume, volumeLength)
volumeHigh = volume > volumeAvg * 1.5
volumeBullish = volumeHigh and close > open and close > close[1]
volumeBearish = volumeHigh and close < open and close < close[1]

// ATR波动率
atr = ta.atr(atrLength)
highVolatility = atr > ta.sma(atr, 20) * 1.2

// 支撑阻力识别
resistance = ta.highest(high, 50)
support = ta.lowest(low, 50)
nearResistance = close >= resistance * 0.985
nearSupport = close <= support * 1.015

// 价格位置分析
pricePosition = (close - support) / (resistance - support) * 100
lowPriceZone = pricePosition < 30
highPriceZone = pricePosition > 70

// 多空信号计算
bullishSignals = 0
bearishSignals = 0

// EMA信号
bullishSignals += emaBullish ? 1 : 0
bearishSignals += emaBearish ? 1 : 0

// MACD信号
bullishSignals += macdBullish ? 1 : 0
bearishSignals += macdBearish ? 1 : 0

// RSI信号
bullishSignals += (rsiBullish and not rsiOverbought) ? 1 : 0
bearishSignals += (rsiBearish and not rsiOversold) ? 1 : 0

// 布林带信号
bullishSignals += (bbBullish or nearBBLower) ? 1 : 0
bearishSignals += (bbBearish or nearBBUpper) ? 1 : 0

// 成交量确认
bullishSignals += volumeBullish ? 1 : 0
bearishSignals += volumeBearish ? 1 : 0

// 位置信号
bullishSignals += (nearSupport or lowPriceZone) ? 1 : 0
bearishSignals += (nearResistance or highPriceZone) ? 1 : 0

// 生成交易信号
strongBuy = bullishSignals >= 5 and bearishSignals <= 1 and (goldenCross or macdGoldenCross)
strongSell = bearishSignals >= 5 and bullishSignals <= 1 and (deathCross or macdDeathCross)
weakBuy = bullishSignals >= 4
weakSell = bearishSignals >= 4

// 绘制信号
plotshape(strongBuy, title="强烈买入", location=location.belowbar, color=color.green, style=shape.triangleup, size=size.normal)
plotshape(strongSell, title="强烈卖出", location=location.abovebar, color=color.red, style=shape.triangledown, size=size.normal)
plotshape(weakBuy, title="弱势买入", location=location.belowbar, color=color.lime, style=shape.triangleup, size=size.small)
plotshape(weakSell, title="弱势卖出", location=location.abovebar, color=color.orange, style=shape.triangledown, size=size.small)

// 绘制指标线
plot(fastEMA, "快速EMA", color=color.blue, linewidth=1)
plot(slowEMA, "慢速EMA", color=color.red, linewidth=1)
plot(bbUpper, "布林带上轨", color=color.gray, linewidth=1)
plot(bbLower, "布林带下轨", color=color.gray, linewidth=1)
plot(bbBasis, "布林带中轨", color=color.yellow, linewidth=1)

// 修复：alertcondition只能使用常量字符串，不能拼接变量
alertcondition(strongBuy, title="东芯股份强烈买入信号", message="东芯股份出现强烈买入信号，请查看图表获取推荐价格")
alertcondition(strongSell, title="东芯股份强烈卖出信号", message="东芯股份出现强烈卖出信号，请查看图表获取推荐价格")

// 在图表上显示信号强度
var table infoTable = table.new(position.top_right, 2, 10, bgcolor=color.white, border_width=1)
if barstate.islast
    currentBuyPrice = ta.lowest(low, 20) * 0.98
    currentSellPrice = ta.highest(high, 20) * 1.02
    currentStopLoss = close * 0.95
    
    table.cell(infoTable, 0, 0, "东芯股份监控", bgcolor=color.blue, text_color=color.white)
    table.cell(infoTable, 1, 0, "数值", bgcolor=color.blue, text_color=color.white)
    table.cell(infoTable, 0, 1, "做多信号", bgcolor=color.green)
    table.cell(infoTable, 1, 1, str.tostring(bullishSignals), bgcolor=color.green)
    table.cell(infoTable, 0, 2, "做空信号", bgcolor=color.red)
    table.cell(infoTable, 1, 2, str.tostring(bearishSignals), bgcolor=color.red)
    table.cell(infoTable, 0, 3, "当前RSI", bgcolor=color.blue)
    table.cell(infoTable, 1, 3, str.tostring(math.round(rsi, 2)), bgcolor=color.blue)
    table.cell(infoTable, 0, 4, "MACD", bgcolor=color.orange)
    table.cell(infoTable, 1, 4, str.tostring(math.round(macdLine, 4)), bgcolor=color.orange)
    table.cell(infoTable, 0, 5, "推荐买入价", bgcolor=color.green)
    table.cell(infoTable, 1, 5, str.tostring(math.round(currentBuyPrice, 2)), bgcolor=color.green)
    table.cell(infoTable, 0, 6, "推荐卖出价", bgcolor=color.red)
    table.cell(infoTable, 1, 6, str.tostring(math.round(currentSellPrice, 2)), bgcolor=color.red)
    table.cell(infoTable, 0, 7, "止损价位", bgcolor=color.orange)
    table.cell(infoTable, 1, 7, str.tostring(math.round(currentStopLoss, 2)), bgcolor=color.orange)
    table.cell(infoTable, 0, 8, "价格位置%", bgcolor=color.purple)
    table.cell(infoTable, 1, 8, str.tostring(math.round(pricePosition, 1)), bgcolor=color.purple)
    table.cell(infoTable, 0, 9, "趋势", bgcolor=color.blue)
    table.cell(infoTable, 1, 9, emaBullish ? "上涨" : emaBearish ? "下跌" : "震荡", bgcolor=color.blue)

// 背景色表示市场状态
bgcolor(strongBuy ? color.new(color.green, 95) : strongSell ? color.new(color.red, 95) : na)

// 输出交易建议
if barstate.islast
    var string recommendation = ""
    var string priceAdvice = ""
    
    currentBuyPrice = ta.lowest(low, 20) * 0.98
    currentSellPrice = ta.highest(high, 20) * 1.02
    currentStopLoss = close * 0.95
    
    if strongBuy
        recommendation := "🚀 强烈买入信号 - 建议分批建仓"
        priceAdvice := "买入区域: " + str.tostring(math.round(currentBuyPrice, 2)) + 
                      " | 止损: " + str.tostring(math.round(currentStopLoss, 2))
    else if strongSell
        recommendation := "🔻 强烈卖出信号 - 建议减仓或离场"
        priceAdvice := "目标价位: " + str.tostring(math.round(currentSellPrice, 2))
    else if weakBuy
        recommendation := "📈 弱势买入信号 - 谨慎试探"
        priceAdvice := "参考买入: " + str.tostring(math.round(currentBuyPrice, 2))
    else if weakSell
        recommendation := "📉 弱势卖出信号 - 注意风险"
        priceAdvice := "参考卖出: " + str.tostring(math.round(currentSellPrice, 2))
    else
        recommendation := "⚪ 观望状态 - 等待明确信号"
        priceAdvice := "支撑: " + str.tostring(math.round(support, 2)) + 
                      " | 阻力: " + str.tostring(math.round(resistance, 2))
    
    label.new(bar_index, high, 
              recommendation + "\n" + priceAdvice + 
              "\n做多强度: " + str.tostring(bullishSignals) + "/7 | 做空强度: " + str.tostring(bearishSignals) + "/7", 
              color=strongBuy ? color.green : strongSell ? color.red : color.gray, 
              style=label.style_label_down, yloc=yloc.abovebar)
```

</details>

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

## TODO

### 功能增强
- [ ] 自选股列表 — 同时监控多只股票，信号变化时弹窗/声音提醒
- [ ] 收益曲线图 — 回测结果可视化为权益曲线，直观对比策略表现
- [ ] 仓位管理 — 支持分仓买入、金字塔加仓、固定比例止损等资金管理策略
- [ ] 止盈止损 — 回测引擎加入移动止损、固定止损、ATR动态止损
- [ ] 策略对比 — 一键同时跑多个策略，表格对比各项指标
- [ ] 信号推送 — 强烈买卖信号触发时推送到微信/钉钉/Telegram
- [ ] 港股/美股 — 扩展数据源支持港股和美股

### 体验优化
- [ ] 深色/浅色主题切换
- [ ] 图表绘图工具 — 手动画趋势线、标注
- [ ] 数据缓存 — 避免重复请求相同数据，加快加载速度
- [ ] 云端部署 — Docker 一键部署，手机随时随地访问
- [ ] Android APK — 打包成独立安装包

### 策略研究
- [ ] 更多技术指标 — KDJ、BOLL Width、OBV、VWAP 等
- [ ] 机器学习 — 用历史数据训练信号分类模型
- [ ] 多周期共振 — 日线+周线信号同时确认时才触发
- [ ] 板块联动 — 分析个股所属板块整体强弱

## 免责声明

本工具仅供学习和研究用途，不构成任何投资建议。股市有风险，投资需谨慎。作者不对使用本工具产生的任何损失负责。

## License

MIT

---

> 为了方便使用，有廉价的云服务器厂商推荐吗QAQ
