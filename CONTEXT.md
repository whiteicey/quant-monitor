# Quant-Monitor 项目上下文 — 新对话启动文件

## 项目信息
- **仓库**: https://github.com/whiteicey/quant-monitor
- **本地路径**: C:\Users\autumn\Desktop\chat\quant-monitor
- **当前版本**: v1.8.0
- **技术栈**: Python 3.12 + Flask + LightweightCharts + Chaquopy(Android)

## 项目所有者
- 25岁程序员，目标40岁前存1000万
- 项目给爸爸用（非技术用户）
- 同时有wealth-manager项目: https://whiteicey.github.io/wealth-manager/

## 当前功能（已完成16项）
- 自选股列表 + 实时行情5秒刷新
- 13种回测策略 + 13套参数预设 + 7种关键价位模式
- 收益曲线图 + 策略对比(多选多曲线)
- 止盈止损(4种模式: 固定止损/止盈/移动止损/ATR)
- 仓位管理(全仓/固定比例/凯利公式)
- 多周期共振(日线+周线确认)
- 信号提醒(toast+蜂鸣声+独立声音开关)
- 10个技术指标(EMA/MACD/RSI/BB/KDJ/OBV/VWAP/ATR/成交量/支撑阻力)
- 板块联动(行业归属+强弱TOP5)
- 图表绘图工具(水平线+趋势线+localStorage持久化)
- 深色/浅色主题 + A股红涨绿跌配色
- 数据缓存 + 多数据源fallback(东方财富/新浪/腾讯)
- Android APK(GitHub Actions自动构建)
- Windows exe(PyInstaller)

## 代码架构
```
app.py              — Flask路由 + 内联HTML/CSS/JS (~2300行)
src/
  indicators.py     — 技术指标(EMA/SMA/RSI/MACD/BB/ATR/KDJ/OBV/VWAP)
  signals.py        — 信号引擎(compute_signals + get_latest_signal + SignalParams)
  backtest.py       — 回测引擎(13策略 + 13预设 + backtest/backtest_compare)
  data.py           — 数据获取(多源fallback + 缓存 + 实时行情 + 板块)
  extensions.py     — 止损/仓位/MTF/提醒接口
  interfaces.py     — 量化交易Protocol接口(Strategy/ExecutionModel/DataProvider/RiskManager/MetricsCalculator)
  visualize.py      — matplotlib图表(CLI版)
android/            — Chaquopy Android项目
.github/workflows/  — APK自动构建
```

## 已确认的开发计划

### 目标
最终成为可部署的量化交易工具，程序出建议→手动执行→验证后自动化。

### 用户偏好
- ETF轮动 + 个股策略都做
- 过渡方案：先手动执行，验证半年到一年
- 默认最大回撤20%，用户可调

### Phase 1: 修回测引擎（最高优先级，当前任务）
必须修复——当前回测数据不可信：

1. **前视偏差**: 当前收盘价成交→改为下一bar开盘价
   - `src/interfaces.py`已有`NextOpenExecutionModel`存根
   - 需要改`src/backtest.py`主循环
   
2. **A股T+1**: 买入当日不能卖出
   - `NextOpenExecutionModel.can_execute()`已有存根
   
3. **印花税**: 当前0.1%双边→改为卖方单边0.05%
   - `src/backtest.py`的commission/stamp_tax参数
   
4. **最低佣金**: 大部分券商最低5元
   
5. **可配置滑点**: 固定bps或成交量百分比
   - `NextOpenExecutionModel`已有slippage_bps参数
   
6. **最大回撤熔断**: 默认20%，触发后停止交易
   
7. **基准对比**: 买入持有 / 沪深300指数
   
8. **样本外测试**: 前70%训练、后30%测试

### Phase 2: 策略研究平台
- 因子模型(IC/IR)
- 统计检验(Sharpe CI/t检验)
- 多资产组合回测
- ETF轮动策略(A500/红利/纳指100)
- 参数优化+过拟合检测

### Phase 3: 迈向实盘
- 模拟交易(Paper Trading)
- Broker API抽象接口
- 实时风控+回撤熔断
- 执行算法(TWAP/VWAP)

## 已知技术债
- app.py 2300行内联HTML/CSS/JS（暂不拆分，打包依赖复杂）
- _SafeEncoder用字符串替换NaN（可能误伤含NaN的字符串）
- 无T+1、收盘价成交（Phase 1修复）

## 工作流规则
每次代码修改都需要：
1. 先做技术评估和路线设计
2. 实现代码
3. 第一轮测试
4. 代码review
5. 修复review问题
6. 第二轮测试确认
7. 更新README
8. 全套同步: git push + pyinstaller exe + gh workflow APK + gh release

## GitHub认证
- gh CLI已安装并登录(whiteicey)
- git代理: http://127.0.0.1:7897 (Clash)
- 系统代理开启时需要trust_env=False绕过

## 颜色约定（重要！）
A股红涨绿跌：
- CSS `--green` = #ff4757 (红色，用于涨/多头/买入)
- CSS `--red` = #00d98b (绿色，用于跌/空头/卖出)
- K线: upColor=#ef5350(红), downColor=#26a69a(绿)
