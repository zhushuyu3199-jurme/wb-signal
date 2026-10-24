# -*- coding: utf-8 -*-
"""
ETF 组合监测 · 配置文件（v2 · 按「各自锚」重设计）
================================
五个标的赚钱的底层逻辑不同，锚也不同：
  A500      锚：估值分位
  港股通红利 锚：股息率 vs 无风险利率
  标普500    锚：溢价率
  短融       锚：资金面（不择时）
  黄金       锚：美国实际利率

指标只回答「该买多少」，不回答「什么时候涨」。
"""

# ══ 总开关：ERP = 1/PE − 10年期国债收益率
ERP_BANDS = [
    (6.0, 0.65, "历史级便宜，权益可加到65%"),
    (4.0, 0.55, "正常区间，维持55%"),
    (3.0, 0.45, "偏贵，降到45%"),
    (-99.0, 0.40, "很贵，降到40%"),
]
ERP_PE_INDEX = "SH000510"
ERP_PE_PROXY = "SH000300"
BOND_YIELD_FIELD = "EMM00166469"
BOND_YIELD_OVERRIDE = None
EQUITY_BASE = 0.55

# ── ① 中证A500 ｜ 锚：估值分位
A500 = {
    "key": "a500", "name": "中证A500",
    "etf": "sh512050", "etf_name": "A500ETF华夏",
    "anchor": "估值分位", "weight": 0.30,
    "indices": ["SH000510", "SH000300"],
    # 判据：min = 取 PE 分位与 PB 分位中较低者（更保守）；pe = 只看 PE
    "judge": "min",
    "pe_bands": [
        (20.0, 3.0, "全力加"),
        (30.0, 2.0, "定投加倍"),
        (70.0, 1.0, "正常定投"),
        (80.0, 0.5, "定投减半"),
        (101.0, 0.0, "停投并减仓"),
    ],
    "paint": "PE-TTM十年分位（主）· PB十年分位（辅）· 股债性价比ERP",
    "pit_short": "分位低能更低→分批 · 期短用300代理 · 熊末PE失真看PB · 高估可持续一年",
    "pitfalls": [
        "分位低可能更低（2018年到20%后又跌30%）→ 只能分批",
        "新指数历史短 → 用沪深300代理",
        "熊市末期PE虚高（盈利下滑）→ 改看PB",
        "高估可持续一年以上 → 别太早清仓",
    ],
}

# ── ② 港股通红利 ｜ 锚：股息率 vs 无风险利率
HK_DIV = {
    "key": "hk_div", "name": "港股通红利",
    "etf": "sz159569", "etf_name": "港股红利低波ETF（景顺）",
    "anchor": "股息率−无风险利率", "weight": 0.10,
    "yield_bands": [
        (6.5, "重仓加"),
        (5.0, "持有"),
        (4.5, "减半"),
        (4.0, "减到三成"),
        (-99.0, "清仓"),
    ],
    "paint": "股息率（核心）· 股息利差 · 南向资金（情绪）",
    "pit_short": "高息可能是假的 · 红利税20% · 汇率吃收益 · 热的时候别买",
    "pitfalls": [
        "股息率是过去12个月，盈利下滑时可能是假高息",
        "港股通红利税20%，5.6%实际到手≈4.5%",
        "汇率暗吃收益（港币挂美元）",
        "人人讨论红利时，往往是阶段高点",
    ],
}

# ── ③ 标普500 ｜ 锚：溢价率
SP500 = {
    "key": "sp500", "name": "标普500",
    "etf": "sh513500", "etf_name": "标普500ETF博时",
    "anchor": "溢价率", "weight": 0.15, "otc_fund": "050025",
    "prem_bands": [
        (8.0, "可卖出套利"),
        (3.0, "暂停买入"),
        (1.5, "改场外申购 050025"),
        (-99.0, "场内可买"),
    ],
    "paint": "IOPV溢价率（每天）· 席勒PE · 美元人民币",
    "pit_short": "溢价天天变必看IOPV · 场外常限购 · 别用分位判死 · 汇率双刃",
    "pitfalls": [
        "溢价每天变，下单前必看实时IOPV",
        "场外常限购，大额买不进",
        "估值中枢在抬升，别用历史分位判死",
        "汇率双刃剑，人民币升值侵蚀收益",
    ],
}

# ── ④ 短融 ｜ 锚：资金面，不择时
SHORT_BOND = {
    "key": "short_bond", "name": "短融",
    "etf": "sh511360", "etf_name": "短融ETF海富通",
    "fund_code": "511360",
    "anchor": "资金面（不择时）", "weight": 0.40,
    "lock_yield": 2.5,
    "paint": "7日年化 · DR007（用GC007代理）· 月末季末年末",
    "pit_short": "别为0.5%折腾 · 有信用风险 · 是弹药库不是收益源",
    "pitfalls": [
        "别为0.5%频繁交易，佣金吃掉收益",
        "有信用风险（2020永煤AAA也违约）",
        "它是权益的弹药库，不是收益来源",
    ],
}

# ── ⑤ 黄金 ｜ 锚：美国实际利率
GOLD = {
    "key": "gold", "name": "黄金",
    "etf": "sh518880", "etf_name": "黄金ETF华安",
    "anchor": "美国实际利率", "weight": 0.05,
    "tips_bands": [
        (-99.0, "TIPS<0%：大牛市环境"),
        (0.0, "TIPS 0–1%：温和利好"),
        (1.0, "TIPS 1–2%：中性"),
        (2.0, "TIPS>2%：压制金价"),
    ],
    "spike_cut": 30.0,
    "paint": "美国10Y TIPS（核心）· 美元指数",
    "pit_short": "抗实际利率非通胀 · 无估值锚 · 5%是保险 · 涨30%要减",
    "pitfalls": [
        "黄金抗的是实际利率，不是通胀（2022通胀9%金价反跌）",
        "没有估值锚，只能看相对利率吸引力",
        "5%是保险不是投资，用于股债双杀时对冲",
        "短期涨超30%要减，情绪上涨会回吐",
    ],
}

ASSETS = [A500, HK_DIV, SP500, SHORT_BOND, GOLD]
ASSET_BY_KEY = {a["key"]: a for a in ASSETS}

# ── 手动数据
HK_YIELD_MANUAL = {"value": None, "asof": None}
HK_YIELD_STALE_DAYS = 45
SHILLER_PE_MANUAL = {"value": None, "asof": None}
SHILLER_URL = "https://www.gurufocus.cn/indicator/shiller_pe"
TOTAL_CAPITAL = None
DCA_BASE_AMOUNT = None

# ── 再平衡
REBALANCE_BAND = 0.05
REBALANCE_MONTH = 12
REBALANCE_LAST_WEEK = True
HOLDINGS = {"a500": None, "hk_div": None, "sp500": None, "short_bond": None, "gold": None}

# ══ 旧持仓标的（原四标的日报，已并入本推送）
LEGACY = [
    {"key": "idx955", "name": "红利低波100", "code": "cs930955",
     "pit_short": "银行权重高易被绑架 · 均值回归非趋势 · 破位可长期不修复"},
    {"key": "idx922", "name": "中证红利", "code": "sh000922",
     "pit_short": "高息可能来自股价跌 · 分红可持续性看现金流 · 同质化高"},
    {"key": "fund", "name": "南方金利定开", "code": "sz160128",
     "pit_short": "定开有流动性锁 · 折价可能长期不收敛 · 债基有信用风险"},
    {"key": "etf513300", "name": "纳指ETF华夏", "code": "sh513300",
     "pit_short": "QDII溢价常高 · 汇率与时差双扰动 · 泡沫顶难判"},
]

PUSH_TITLE_PREFIX = "组合监测"
PUSH_HOURS = [12.0, 22.0]
