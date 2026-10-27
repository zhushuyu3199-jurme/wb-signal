# -*- coding: utf-8 -*-
"""
定盘 · 配置文件（每个标的一把锚）
================================
组合（2026-10-27 调整后）：
  A500      锚：估值分位
  红利低波   锚：均线位置 + 估值分位（H30269，10% 权重）
  中证红利   锚：均线位置 + 估值分位（000922，仅观察不加权）
  标普500    锚：溢价率
  固收桶     锚：资金面（不择时）
  黄金       锚：美国实际利率
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

# 美元 10 年期名义利率 —— 港币挂美元，港股类资产的无风险基准
USD_10Y_OVERRIDE = None
USD_BREAKEVEN_ASSUMED = 2.30
USD_10Y_FALLBACK = 4.10

EQUITY_BASE = 0.55          # A500 + 红利低波 + 标普500
EQUITY_KEYS = ("a500", "div_low", "sp500")

# ── ① 中证A500 ｜ 锚：估值分位
A500 = {
    "key": "a500", "name": "中证A500",
    "etf": "sh512050", "etf_name": "A500ETF华夏",
    "anchor": "估值分位", "weight": 0.30, "weighted": True,
    "indices": ["SH000510", "SH000300"],
    "judge": "auto",
    "div_warn": 25.0,
    "div_soft": 15.0,
    "div_penalty": 10.0,
    "pe_bands": [
        (20.0, 3.0, "全力加"),
        (30.0, 2.0, "定投加倍"),
        (70.0, 1.0, "正常定投"),
        (80.0, 0.5, "定投减半"),
        (101.0, 0.0, "停投并减仓"),
    ],
    "paint": "PE-TTM十年分位（主）· PB十年分位（辅）· 股债性价比ERP",
    "pit_short": "分位低能更低→分批 · 期短用300代理 · 熊末PE失真看PB · 高估可持续一年",
}

# ── ② 红利低波 ｜ 锚：均线位置 + 估值分位（10% 权重）
DIV_LOW = {
    "key": "div_low", "name": "红利低波", "code": "H30269",
    "etf": "sh512890", "etf_name": "红利低波ETF（华泰柏瑞）",
    "anchor": "均线位置 + 估值分位", "weight": 0.10, "weighted": True,
    "eva": "CSIH30269",
    # 价格序列：512890 跟踪 H30269，且从未分红 → 无除息跳空，可作干净代理
    "px": ["sh512890"],
    "px_note": "ETF代理（512890 无分红）",
    "pit_short": "与中证红利高度重叠 · PE分位已80% · ETF有跟踪误差",
}

# ── ③ 中证红利 ｜ 仅观察，不加权
DIV_922 = {
    "key": "div_922", "name": "中证红利", "code": "000922",
    "anchor": "均线位置 + 估值分位", "weight": 0.0, "weighted": False,
    "eva": "SH000922",
    "px": ["sh000922"],
    "px_note": "指数原始序列",
    "pit_short": "与红利低波重叠 · 高息可能来自股价跌 · 分红看现金流",
}

# ── ④ 标普500 ｜ 锚：溢价率
SP500 = {
    "key": "sp500", "name": "标普500",
    "etf": "sh513500", "etf_name": "标普500ETF博时",
    "anchor": "溢价率", "weight": 0.15, "weighted": True, "otc_fund": "050025",
    "prem_bands": [
        (8.0, "溢价极高，暂缓一切操作（个人无套利通道）"),
        (3.0, "暂停买入"),
        (1.5, "改场外申购 050025"),
        (-99.0, "场内可买"),
    ],
    "paint": "IOPV溢价率（每天）· 席勒PE · 美元人民币",
    "pit_short": "溢价天天变必看IOPV · 场外常限购 · 别用分位判死 · 汇率双刃",
}

# ── ⑤ 固收桶 ｜ 锚：资金面，不择时（40%）
SHORT_BOND = {
    "key": "short_bond", "name": "固收桶",
    "etf": "sh511360", "etf_name": "短融ETF海富通",
    "fund_code": "511360",
    "anchor": "资金面（不择时）", "weight": 0.40, "weighted": True,
    "lock_yield": 2.5,
    "components": "短融ETF + 南方金利定开 + 易方达增强回报",
    "has_lock": True,
    "paint": "7日年化 · DR007（用GC007代理）· 月末季末年末",
    "pit_short": "含定开有流动性锁 · 一级债基含转债敞口 · 弹药可能取不出",
}

# ── ⑥ 黄金 ｜ 锚：美国实际利率（5%）
GOLD = {
    "key": "gold", "name": "黄金",
    "etf": "sh518880", "etf_name": "黄金ETF华安",
    "anchor": "美国实际利率", "weight": 0.05, "weighted": True,
    "tips_bands": [
        (-99.0, "TIPS<0%：大牛市环境"),
        (0.0, "TIPS 0–1%：温和利好"),
        (1.0, "TIPS 1–2%：中性"),
        (2.0, "TIPS>2%：压制金价"),
    ],
    "spike_cut": 30.0,
    "paint": "美国10Y TIPS（核心）· 美元指数",
    "pit_short": "抗实际利率非通胀 · 无估值锚 · 5%是保险 · 涨30%要减",
}

ASSETS = [A500, DIV_LOW, DIV_922, SP500, SHORT_BOND, GOLD]
ASSET_BY_KEY = {a["key"]: a for a in ASSETS}

# ── 手动数据
HK_YIELD_MANUAL = {"value": None, "asof": None}
HK_YIELD_STALE_DAYS = 45
HK_YIELD_FREEZE_DAYS = 90
SHILLER_PE_MANUAL = {"value": None, "asof": None}
SHILLER_URL = "https://www.gurufocus.cn/indicator/shiller_pe"
TOTAL_CAPITAL = None
DCA_BASE_AMOUNT = None

# ── 红利指数：位置档来自 ma250_monitor，估值档按 PE 分位
DIV_VAL_BANDS = [
    (30.0, "便宜"),
    (70.0, "中性"),
    (85.0, "偏贵"),
    (101.0, "贵"),
]

# ── 旧持仓（已缩减为仅南方金利）
LEGACY = [
    {"key": "fund", "name": "南方金利定开", "code": "sz160128",
     "pit_short": "定开有流动性锁 · 折价可能长期不收敛 · 债基有信用风险"},
]

# ── 再平衡
REBALANCE_BAND = 0.05
REBALANCE_MONTH = 12
REBALANCE_LAST_WEEK = True
HOLDINGS = {"a500": None, "div_low": None, "sp500": None, "short_bond": None, "gold": None}

PUSH_TITLE_PREFIX = "定盘"
PUSH_HOURS = [12.0, 22.0]
SEND_WINDOW_NOON = (11.0, 14.5)
SEND_WINDOW_NIGHT = (21.0, 23.99)
