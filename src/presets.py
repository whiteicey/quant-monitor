"""
资产池预设管理
定义预设方案 + 资产元数据 + 智能推荐逻辑
"""
from dataclasses import dataclass, field
from typing import List, Dict, Optional


# ============================================================
# 资产元数据
# ============================================================

@dataclass
class Asset:
    """单个资产"""
    symbol: str           # 股票/ETF代码
    name: str             # 中文名称
    category: str         # 资产类别: equity/bond/gold/commodity/cash/overseas
    sub_category: str     # 细分: broad_etf/sector_etf/cross_border/gov_bond/dividend_stock/...
    description: str = "" # 简短描述


# 全量资产库 — 所有可选的标的
ASSET_LIBRARY: Dict[str, Asset] = {
    # 宽基ETF
    "560610": Asset("560610", "A500 ETF", "equity", "broad_etf", "中证A500指数ETF"),
    "510300": Asset("510300", "沪深300ETF", "equity", "broad_etf", "大盘蓝筹"),
    "510500": Asset("510500", "中证500ETF", "equity", "broad_etf", "中盘成长"),
    "159915": Asset("159915", "创业板ETF", "equity", "broad_etf", "科技成长"),
    "159922": Asset("159922", "中证500ETF", "equity", "broad_etf", "中证500(深)"),
    "510050": Asset("510050", "上证50ETF", "equity", "broad_etf", "超大盘"),

    # 行业/主题ETF
    "510880": Asset("510880", "红利ETF", "equity", "sector_etf", "高股息策略"),
    "159928": Asset("159928", "消费ETF", "equity", "sector_etf", "大消费板块"),
    "512010": Asset("512010", "医药ETF", "equity", "sector_etf", "医药健康"),
    "512480": Asset("512480", "半导体ETF", "equity", "sector_etf", "芯片半导体"),
    "515790": Asset("515790", "光伏ETF", "equity", "sector_etf", "光伏新能源"),
    "512690": Asset("512690", "酒ETF", "equity", "sector_etf", "白酒板块"),
    "512660": Asset("512660", "军工ETF", "equity", "sector_etf", "国防军工"),

    # 跨境ETF
    "513100": Asset("513100", "纳指100ETF", "overseas", "cross_border", "美股科技龙头"),
    "513500": Asset("513500", "标普500ETF", "overseas", "cross_border", "美股大盘"),
    "513180": Asset("513180", "恒生科技ETF", "overseas", "cross_border", "港股科技"),
    "159866": Asset("159866", "日经ETF", "overseas", "cross_border", "日本股市"),

    # 债券ETF
    "511010": Asset("511010", "国债ETF", "bond", "gov_bond", "5年期国债"),
    "511210": Asset("511210", "企债ETF", "bond", "corp_bond", "企业债券"),
    "511260": Asset("511260", "十年国债ETF", "bond", "gov_bond", "10年国债(深)"),

    # 货币基金(现金等价)
    "511880": Asset("511880", "货币ETF", "cash", "money_market", "货币基金,接近无风险"),
    "511990": Asset("511990", "华宝货币", "cash", "money_market", "场内货币基金"),

    # 贵金属
    "518880": Asset("518880", "黄金ETF", "gold", "precious_metal", "实物黄金"),
    "161226": Asset("161226", "白银基金", "gold", "precious_metal", "白银投资"),

    # 商品
    "159985": Asset("159985", "豆粕ETF", "commodity", "agri_commodity", "农产品商品"),
    "512400": Asset("512400", "有色金属ETF", "commodity", "metal_commodity", "有色金属"),

    # 高股息个股
    "600900": Asset("600900", "长江电力", "equity", "dividend_stock", "水电龙头,高股息"),
    "601088": Asset("601088", "中国神华", "equity", "dividend_stock", "煤炭龙头,高股息"),
    "601006": Asset("601006", "大秦铁路", "equity", "dividend_stock", "铁路运输,高股息"),
    "600028": Asset("600028", "中国石化", "equity", "dividend_stock", "石化龙头,高股息"),
    "601288": Asset("601288", "农业银行", "equity", "dividend_stock", "大行高股息"),
}


# ============================================================
# 预设资产池
# ============================================================

@dataclass
class PortfolioPreset:
    """资产池预设方案"""
    id: str
    name: str
    description: str
    symbols: List[str]                       # 包含的资产代码
    default_strategy: str = "equal_weight"   # 默认推荐策略
    default_rebalance: str = "monthly"       # 默认再平衡频率
    risk_level: str = "medium"               # low/medium/high
    rebalance_reason: str = ""               # 推荐频率的原因
    strategy_reason: str = ""                # 推荐策略的原因


PRESETS: Dict[str, PortfolioPreset] = {
    "classic_three": PortfolioPreset(
        id="classic_three",
        name="经典三驾马车",
        description="股票+债券+黄金，最基础的资产配置，三者相关性低，风险分散效果好",
        symbols=["560610", "511010", "518880"],
        default_strategy="risk_parity",
        default_rebalance="quarterly",
        risk_level="low",
        strategy_reason="三类资产波动差异大，风险平价能让每类资产贡献相同风险，避免股票风险主导",
        rebalance_reason="三驾马车相关性稳定，季度再平衡足够捕捉均值回归，频繁调仓反而增加成本",
    ),
    "all_weather": PortfolioPreset(
        id="all_weather",
        name="全天候配置",
        description="覆盖国内大中盘+海外+债券+黄金+现金，类似桥水全天候策略",
        symbols=["510300", "510500", "513100", "511010", "518880", "511880"],
        default_strategy="risk_parity",
        default_rebalance="monthly",
        risk_level="medium",
        strategy_reason="资产类别丰富，风险平价让低波动资产(债券/货币)权重更高，整体波动小、回撤可控",
        rebalance_reason="6类资产月度再平衡兼顾响应速度和交易成本，跨境ETF价格波动较大需要较及时的调整",
    ),
    "high_dividend": PortfolioPreset(
        id="high_dividend",
        name="高息防守",
        description="高股息ETF+个股+债券+黄金，注重现金流和本金安全",
        symbols=["510880", "600900", "601088", "511010", "518880"],
        default_strategy="equal_weight",
        default_rebalance="quarterly",
        risk_level="low",
        strategy_reason="高股息资产波动率相近，等权配置简单有效，避免过度集中在单一标的",
        rebalance_reason="高股息策略换手率天然低，季度再平衡配合分红到账周期，降低交易成本",
    ),
    "aggressive_growth": PortfolioPreset(
        id="aggressive_growth",
        name="激进成长",
        description="高弹性科技成长ETF组合，追求高收益承受高波动",
        symbols=["159915", "512480", "513100", "513180"],
        default_strategy="momentum",
        default_rebalance="monthly",
        risk_level="high",
        strategy_reason="成长型资产趋势特征明显，动量策略可以追涨杀跌、及时切换到强势品种",
        rebalance_reason="科技板块轮动快，月度再平衡能捕捉板块切换；更频繁则交易成本侵蚀收益",
    ),
}


# ============================================================
# 智能推荐
# ============================================================

def recommend_strategy(symbols: List[str]) -> dict:
    """
    根据资产池组成推荐配置策略和再平衡频率
    
    返回:
    {
        "strategy": str,
        "strategy_reason": str,
        "rebalance": str,
        "rebalance_reason": str,
    }
    """
    assets = [ASSET_LIBRARY.get(s) for s in symbols if s in ASSET_LIBRARY]
    if not assets:
        return {
            "strategy": "equal_weight",
            "strategy_reason": "未识别的资产，默认使用等权配置",
            "rebalance": "monthly",
            "rebalance_reason": "默认月度再平衡",
        }

    categories = set(a.category for a in assets)
    sub_cats = set(a.sub_category for a in assets)
    n = len(assets)

    # 分析资产组成特征
    has_equity = "equity" in categories
    has_bond = "bond" in categories
    has_gold = "gold" in categories
    has_cash = "cash" in categories
    has_overseas = "overseas" in categories
    has_commodity = "commodity" in categories

    n_categories = len(categories)
    has_dividend = "dividend_stock" in sub_cats
    has_sector = "sector_etf" in sub_cats
    has_cross_border = "cross_border" in sub_cats

    # 策略推荐逻辑
    strategy = "equal_weight"
    strategy_reason = ""
    rebalance = "monthly"
    rebalance_reason = ""

    if n_categories >= 4:
        # 资产类别丰富 → 风险平价
        strategy = "risk_parity"
        strategy_reason = f"您的组合覆盖{n_categories}大类资产，风险平价可以让每类资产贡献相同风险份额，避免高波动资产(如股票)主导组合风险"
    elif n_categories >= 3 and has_bond:
        strategy = "risk_parity"
        strategy_reason = "股债金组合适合风险平价，债券波动低但占比可以更高来平衡股票的高波动"
    elif has_sector or has_cross_border:
        # 板块/跨境ETF → 动量轮动
        strategy = "momentum"
        strategy_reason = "行业/跨境ETF之间轮动特征明显，动量策略可以集中持有当前最强的品种"
    elif has_dividend and not has_sector:
        # 纯高股息 → 等权
        strategy = "equal_weight"
        strategy_reason = "高股息资产波动率接近，等权配置简洁有效，分散个股风险"
    elif n == 2:
        strategy = "adaptive"
        strategy_reason = "仅2个资产，自适应策略可以在两者之间灵活切换，兼顾动量和风险"
    else:
        strategy = "equal_weight"
        strategy_reason = "通用场景下等权配置是稳健的默认选择"

    # 再平衡频率推荐
    if strategy == "momentum":
        rebalance = "monthly"
        rebalance_reason = "动量策略需要较及时地跟随趋势，月度再平衡是交易成本和响应速度的最佳平衡点"
    elif strategy == "risk_parity" and n_categories >= 4:
        rebalance = "monthly"
        rebalance_reason = "多类资产的波动率变化需要定期跟踪，月度频率确保权重不会偏离太远"
    elif has_dividend:
        rebalance = "quarterly"
        rebalance_reason = "高股息策略天然低换手，季度再平衡配合分红周期，降低不必要的交易成本"
    elif n <= 3 and not has_sector:
        rebalance = "quarterly"
        rebalance_reason = "少量稳定资产不需要频繁调整，季度再平衡减少交易成本"
    else:
        rebalance = "monthly"
        rebalance_reason = "月度再平衡是通用场景下的稳健选择"

    return {
        "strategy": strategy,
        "strategy_reason": strategy_reason,
        "rebalance": rebalance,
        "rebalance_reason": rebalance_reason,
    }


# ============================================================
# API 辅助
# ============================================================

def get_preset_list() -> list:
    """返回所有预设方案(供前端)"""
    result = []
    for p in PRESETS.values():
        assets_info = []
        for s in p.symbols:
            a = ASSET_LIBRARY.get(s)
            if a:
                assets_info.append({"symbol": a.symbol, "name": a.name,
                                    "category": a.category, "description": a.description})
        result.append({
            "id": p.id,
            "name": p.name,
            "description": p.description,
            "symbols": p.symbols,
            "assets": assets_info,
            "default_strategy": p.default_strategy,
            "default_rebalance": p.default_rebalance,
            "risk_level": p.risk_level,
            "strategy_reason": p.strategy_reason,
            "rebalance_reason": p.rebalance_reason,
        })
    return result


def get_asset_library() -> list:
    """返回全量资产库(供前端自定义选择)"""
    result = []
    for a in ASSET_LIBRARY.values():
        result.append({
            "symbol": a.symbol, "name": a.name,
            "category": a.category, "sub_category": a.sub_category,
            "description": a.description,
        })
    # 按类别排序
    cat_order = {"equity": 0, "overseas": 1, "bond": 2, "gold": 3, "commodity": 4, "cash": 5}
    result.sort(key=lambda x: (cat_order.get(x["category"], 9), x["symbol"]))
    return result
