#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
combo_daily.py · 四标的合并信号日报（PushPlus 微信推送）
==============================================================================
一条消息覆盖四个标的：

  A) 红利低波100 (cs930955) —— 250日均线偏离度档位
     DCA<=-5% 定投 / BUY1<=-10% 加仓 / BUY2<=-15% 打满 / TRIM>=+20% / SELL>=+30% / HOLD

  B) 中证红利 (sh000922)   —— 与 A 同档位体系（均值回归型，低吸有效）

  C) 南方金利定开 (sz160128) —— 折价率主信号(>=+1%买 / >=+2%重点) + 20/40日新低、20日均线偏离次信号
     持有: >=1个月; 理想持有至 2027-05 开放期赎回

  D) 纳斯达克ETF华夏 (sh513300) —— 按专项分析结论（趋势型资产）:
     卖出: 净值口径偏离 >= +33% (q99 泡沫止盈线, 历史上250日后 -39%/胜率2%)
     买入: 距250日高点回撤 >= 10% 起分档 (浅-中回调为历史最优加仓区)
     过滤: 溢价率 > 8% -> 不加仓/评估换仓; < 0 (折价) -> 买点窗口
     注: 不使用"跌破均线就卖"（35年36次破位中位4天收复, 属假信号）

数据源（每次运行脚本自抓公开接口）：
  - 指数:   腾讯快照 qt.gtimg.cn (cs930955 / sh000922) + 本地历史库
  - 金利:   腾讯日线 sz160128 + 东财最新净值(160128)
  - 513300: 腾讯日线 sh513300(500根) + 东财净值库(每日更新) nav_513300.json

用法:
  python combo_daily.py                # 抓数->算信号->推送(仅新交易日推送, 节假日静默)
  python combo_daily.py --no-push      # 只算不发(测试)
  python combo_daily.py --force        # 忽略"非新交易日"抑制
  python combo_daily.py --index-close 11540.66 --index-date 2026-10-12   # 兜底: 手动传红利低波100数据

退出码: 0=成功/静默跳过; 1=四标的全失败(不推送、不伪造)
"""
import argparse
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

from ma250_monitor import signal_for              # noqa: E402
from push_notify import load_config, send_pushplus  # noqa: E402

HIST_IDX = os.path.join(BASE, "hist_cs930955.json")   # 红利低波100历史库
HIST_922 = os.path.join(BASE, "hist_000922.json")     # 中证红利历史库
NAV_513300 = os.path.join(BASE, "nav_513300.json")    # 513300净值库
STATE_PATH = os.path.join(BASE, "signal_state.json")
ERR_LOG = os.path.join(BASE, "push_error.log")

# ===== 南方金利定开 · 阈值 =====
FUND_TH = dict(
    disc_buy1=1.0, disc_buy2=2.0,
    dev20_watch=-0.3, dev20_buy=-0.5,
    N20=20, N40=40,
)

# ===== 纳斯达克ETF 513300 · 阈值（按专项分析结论） =====
ETF_TH = dict(
    bubble=33.0,    # 卖出: 净值口径偏离 >= +33% (q99泡沫线)
    warn=25.0,      # 偏高观察
    dip1=10.0,      # 买入档1: 距250日高点回撤 >= 10%
    dip2=20.0,      # 买入档2: >= 20%
    dip3=30.0,      # 买入档3(深熊): >= 30%
    prem_hi=8.0,    # 溢价过高 -> 不加仓/换仓
    prem_warn=2.0,  # 溢价偏高 -> 不宜追买
)

UA = {"User-Agent": "Mozilla/5.0"}


def http_get(url, headers=None, timeout=20):
    req = urllib.request.Request(url, headers=headers or UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# ---------- 数据抓取 ----------
def fetch_index_snapshot(code="cs930955"):
    """腾讯快照 -> dict(date, close, name)。收盘后 close 即当日收盘。"""
    raw = http_get("https://qt.gtimg.cn/q=%s" % code).decode("gbk", "ignore")
    m = re.search(r'="([^"]+)"', raw)
    if not m:
        raise RuntimeError("腾讯快照无数据: %s" % raw[:80])
    f = m.group(1).split("~")
    price = float(f[3])
    ts = f[30]
    date = "%s-%s-%s" % (ts[0:4], ts[4:6], ts[6:8])
    if price <= 0:
        raise RuntimeError("腾讯快照价格异常: %s" % price)
    return dict(date=date, close=price, name=f[1])


def fetch_kline_rows(code, n=500):
    """腾讯日线 -> list[dict(date,open,close,high,low)]"""
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,day,,,%d,qfq" % (code, n)
    obj = json.loads(http_get(url).decode("utf-8"))
    node = obj["data"][code]
    rows = node.get("qfqday") or node.get("day") or []
    out = []
    for r in rows:
        out.append(dict(date=r[0], open=float(r[1]), close=float(r[2]),
                        high=float(r[3]), low=float(r[4])))
    if len(out) < 60:
        raise RuntimeError("腾讯日线数据过少(%s): %d 条" % (code, len(out)))
    return out


def fetch_fund_nav(fund="160128"):
    """东财最新单位净值 -> dict(date, nav)"""
    url = ("https://api.fund.eastmoney.com/f10/lsjz?fundCode=%s&pageIndex=1&pageSize=3" % fund)
    h = dict(UA)
    h["Referer"] = "http://fundf10.eastmoney.com/"
    j = json.loads(http_get(url, h).decode("utf-8"))
    lst = j["Data"]["LSJZList"]
    if not lst:
        raise RuntimeError("东财净值列表为空")
    return dict(date=lst[0]["FSRQ"], nav=float(lst[0]["DWJZ"]))


def update_nav_lib(path, fund):
    """拉最新净值 merge 进本地净值库 -> (dates, closes, added)"""
    hist = json.load(open(path, encoding="utf-8"))
    m = dict(zip(hist["dates"], hist["closes"]))
    url = ("https://api.fund.eastmoney.com/f10/lsjz?fundCode=%s&pageIndex=1&pageSize=6" % fund)
    h = dict(UA)
    h["Referer"] = "http://fundf10.eastmoney.com/"
    j = json.loads(http_get(url, h).decode("utf-8"))
    added = 0
    for it in j["Data"]["LSJZList"]:
        d, v = it["FSRQ"], float(it["DWJZ"])
        if d not in m:
            m[d] = v
            added += 1
    ds = sorted(m)
    json.dump({"code": hist.get("code"), "name": hist.get("name"),
               "dates": ds, "closes": [m[d] for d in ds]},
              open(path, "w", encoding="utf-8"), ensure_ascii=False)
    return ds, [m[d] for d in ds], added


def merge_hist(hist, d, c):
    if not hist["dates"] or hist["dates"][-1] < d:
        hist["dates"].append(d)
        hist["closes"].append(c)
    elif hist["dates"][-1] == d:
        hist["closes"][-1] = c


def ma_series(cl, N=250):
    out = [None] * len(cl)
    s = 0.0
    for i, c in enumerate(cl):
        s += c
        if i >= N:
            s -= cl[i - N]
        if i >= N - 1:
            out[i] = s / N
    return out


# ---------- 信号计算 ----------
def fund_signal(rows, nav_info, th=FUND_TH):
    """南方金利定开信号。折价率= (净值-市价)/净值 *100, 正数=折价、负数=溢价。"""
    dates = [r["date"] for r in rows]
    closes = [r["close"] for r in rows]
    i = len(closes) - 1
    close, date = closes[i], dates[i]
    nav, nav_date = nav_info["nav"], nav_info["date"]

    ma20 = sum(closes[i - 19:i + 1]) / 20.0
    dev20 = (close - ma20) / ma20 * 100.0

    prev20 = closes[max(0, i - th["N20"]):i]
    prev40 = closes[max(0, i - th["N40"]):i]
    low20 = min(prev20) if len(prev20) >= th["N20"] else None
    low40 = min(prev40) if len(prev40) >= th["N40"] else None
    new20 = (low20 is not None) and close < low20
    new40 = (low40 is not None) and close < low40

    disc = None
    disc_gap = None
    if nav and nav > 0:
        disc = (nav - close) / nav * 100.0
        disc_gap = max(0.0, th["disc_buy1"] - disc)

    if (disc is not None and disc >= th["disc_buy2"]) or new40:
        level = "STRONG"
        action = "主信号(强): 折价≥2% 或创40日新低 → 可买入(重点档)"
    elif (disc is not None and disc >= th["disc_buy1"]) or new20 or dev20 <= th["dev20_buy"]:
        level = "BUY"
        action = "买点触发: 折价≥1% / 20日新低 / 低于20MA 0.5% → 可买入"
    elif dev20 <= th["dev20_watch"]:
        level = "WATCH"
        action = "接近买点: 已低于20MA 0.3%, 留意折价与价格进一步走弱"
    else:
        level = "NONE"
        action = "等待: 折价率与价格均未触发买入条件"

    return dict(code="sz160128", name="南方金利定开", date=date,
                close=close, nav=nav, nav_date=nav_date,
                discount=(round(disc, 2) if disc is not None else None),
                disc_gap=(round(disc_gap, 2) if disc_gap is not None else None),
                ma20=round(ma20, 4), dev20=round(dev20, 2),
                new20=new20, new40=new40,
                low20=(round(low20, 4) if low20 else None),
                low40=(round(low40, 4) if low40 else None),
                level=level, action=action)


def etf513300_signal(price_rows, nav_dates, nav_closes, th=ETF_TH):
    """纳斯达克ETF 513300 信号（趋势型资产专属框架）。"""
    p = [r["close"] for r in price_rows]
    p_ma = ma_series(p)
    p_last, p_ma_last = p[-1], p_ma[-1]
    p_dev = (p_last / p_ma_last - 1) * 100.0
    hh_p = max(p[-250:])
    dd_p = (p_last / hh_p - 1) * 100.0

    n_cl = nav_closes
    n_ma = ma_series(n_cl)
    n_last, n_ma_last = n_cl[-1], n_ma[-1]
    n_dev = (n_last / n_ma_last - 1) * 100.0
    hh_n = max(n_cl[-250:])
    dd_n = (n_last / hh_n - 1) * 100.0

    prem = (p_last - n_last) / n_last * 100.0

    if n_dev >= th["bubble"]:
        level = "BUBBLE"
        action = "泡沫区(净值偏离≥%d%%)：考虑大比例减仓" % th["bubble"]
    elif dd_n <= -th["dip3"]:
        level = "DIP3"
        action = "深熊区(回撤≥30%)：分批加仓、不一次梭哈"
    elif dd_n <= -th["dip2"]:
        level = "DIP2"
        action = "深度回调区(回撤≥20%)：分批加仓"
    elif dd_n <= -th["dip1"]:
        level = "DIP1"
        action = "回调加仓区(回撤≥10%)：可分批加仓"
    elif n_dev >= th["warn"]:
        level = "WARN"
        action = "偏高区(净值偏离≥%d%%)：不追高" % th["warn"]
    else:
        level = "NONE"
        action = "无信号：持有/等待（未达泡沫线+33%，回撤未达加仓线-10%）"

    if prem >= th["prem_hi"]:
        prem_lv, prem_act = "HI", "溢价过高(>8%)：不加仓，评估换仓（换低溢价通道）"
    elif prem >= th["prem_warn"]:
        prem_lv, prem_act = "MID", "溢价偏高(2~8%)：不宜追买"
    elif prem < 0:
        prem_lv, prem_act = "DISC", "折价：买点窗口较好"
    else:
        prem_lv, prem_act = "OK", "溢价正常(0~2%)"

    return dict(code="sh513300", name="纳斯达克ETF华夏",
                date=price_rows[-1]["date"], price=p_last,
                nav=n_last, nav_date=nav_dates[-1],
                premium=round(prem, 2),
                nav_dev=round(n_dev, 2), nav_dd=round(dd_n, 2),
                price_dev=round(p_dev, 2), price_dd=round(dd_p, 2),
                level=level, action=action, prem_lv=prem_lv, prem_act=prem_act)


# ---------- 消息组装 ----------
def idx_block(title, idx):
    L = ["**【%s】**" % title]
    if idx is None:
        L.append("- 数据暂缺（本次取数失败）")
        return L
    if idx.get("ma") is None:
        L.append("- 数据不足，无法计算250日均线")
        return L
    L.append("- 收盘 **%.2f** ｜ 250日均线 %.2f" % (idx["close"], idx["ma"]))
    L.append("- 偏离250日线 **%+.2f%%**（%s）" % (idx["pct"], idx["zone"]))
    L.append("- 动作: %s" % idx["action"])
    g = (idx.get("dist") or {}).get("dca")
    if g is not None:
        L.append("- 距 -5%% 定投档还需下跌 **%.2f** 个百分点" % g)
    return L


def fund_block(fund):
    L = ["**【南方金利定开 · 160128】**"]
    if fund is None:
        L.append("- 数据暂缺（本次取数失败）")
        return L
    if fund["discount"] is not None:
        L.append("- 收盘 **%.3f** ｜ 净值 **%.4f**（%s）"
                 % (fund["close"], fund["nav"], fund["nav_date"]))
        if fund["discount"] > 0:
            L.append("- 折价率 **%+.2f%%**（市价低于净值）" % fund["discount"])
        elif fund["discount"] == 0:
            L.append("- 折价率 **0.00%**（市价≈净值，无折溢价）")
        else:
            L.append("- 折价率 **%+.2f%%**（市价高于净值，属溢价）" % fund["discount"])
        if fund["disc_gap"] and fund["disc_gap"] > 0:
            L.append("- 主信号(折价≥+1%%): 未触发，还需折价扩大 **%.2f** 个百分点" % fund["disc_gap"])
        else:
            L.append("- 主信号(折价≥+1%): **已触发**")
    else:
        L.append("- 收盘 **%.3f** ｜ 净值: 暂缺（稍后公布）" % fund["close"])
    L.append("- 20日均线 %.4f（偏离 **%+.2f%%**）" % (fund["ma20"], fund["dev20"]))
    L.append("- 20日新低: %s ｜ 40日新低: %s"
             % ("✅触发" if fund["new20"] else "未触发",
                "✅触发" if fund["new40"] else "未触发"))
    L.append("- 动作: %s" % fund["action"])
    L.append("- 持有计划: 买入后持有≥1个月; 理想持有至 **2027-05 开放期**赎回")
    return L


def etf_block(etf):
    L = ["**【纳斯达克ETF华夏 · 513300】**（趋势型：泡沫止盈+回撤加仓+溢价过滤）"]
    if etf is None:
        L.append("- 数据暂缺（本次取数失败）")
        return L
    L.append("- 价格 **%.3f** ｜ 净值 %.4f（%s）｜ 溢价 **%+.2f%%**"
             % (etf["price"], etf["nav"], etf["nav_date"], etf["premium"]))
    L.append("- 净值口径偏离250日线 **%+.2f%%**（价格口径 %+.2f%%）"
             % (etf["nav_dev"], etf["price_dev"]))
    L.append("- 距250日高点回撤 **%+.2f%%**（净值口径）" % etf["nav_dd"])
    L.append("- 位置信号: %s" % etf["action"])
    if etf["prem_lv"] == "HI":
        L.append("- 溢价信号: **%s**" % etf["prem_act"])
    else:
        L.append("- 溢价信号: %s" % etf["prem_act"])
    return L


def build_message(idx955, idx922, fund, etf, today):
    sell_alert = ((idx955 and idx955.get("level") == "SELL")
                  or (idx922 and idx922.get("level") == "SELL")
                  or (etf and etf["level"] == "BUBBLE"))
    buy_alert = ((idx955 and idx955.get("level") in ("BUY1", "BUY2"))
                 or (idx922 and idx922.get("level") in ("BUY1", "BUY2"))
                 or (fund and fund["level"] in ("STRONG", "BUY"))
                 or (etf and etf["level"] in ("DIP1", "DIP2", "DIP3"))
                 or (etf and etf["prem_lv"] == "DISC"))
    hint = ((idx955 and idx955.get("level") == "DCA")
            or (idx922 and idx922.get("level") == "DCA")
            or (etf and (etf["level"] == "WARN" or etf["prem_lv"] == "HI")))
    if sell_alert:
        prefix = "⚠️卖出警示"
    elif buy_alert:
        prefix = "⚠️买点信号"
    elif hint:
        prefix = "📌提示"
    else:
        prefix = "📊信号日报"
    title = "%s｜红利低波100/中证红利/金利/纳指ETF %s" % (prefix, today[5:])

    L = []
    L += idx_block("红利低波100 · cs930955", idx955)
    L.append("")
    L += idx_block("中证红利 · 000922", idx922)
    L.append("")
    L += fund_block(fund)
    L.append("")
    L += etf_block(etf)
    L.append("")
    L.append("—— 系统自动推送 · 历史统计不构成投资建议")
    return title, "\n".join(L)


# ---------- 推送与状态 ----------
def push(title, content, dry=False):
    cfg = load_config()
    # token 顺序: 环境变量(云端 GitHub Actions 用 Secret 注入) > push_config.json(本地)
    token = os.environ.get("PUSHPLUS_TOKEN") or cfg.get("pushplus_token", "")
    if dry:
        return dict(ok=None, skipped=True, reason="dry-run")
    if not token:
        return dict(ok=False, error="push_config.json 未配置 pushplus_token")
    try:
        resp = send_pushplus(token, title, content)
        j = json.loads(resp)
        ok = (j.get("code") == 200)
        if not ok:
            with open(ERR_LOG, "a", encoding="utf-8") as f:
                f.write("%s | %s | %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), title, resp))
        return dict(ok=ok, resp=j)
    except Exception as e:
        with open(ERR_LOG, "a", encoding="utf-8") as f:
            f.write("%s | %s | EXC: %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), title, e))
        return dict(ok=False, error=str(e))


def load_state():
    if os.path.exists(STATE_PATH):
        try:
            return json.load(open(STATE_PATH, encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(today, idx955, idx922, fund, etf, pushed, prev):
    st = dict(prev)
    st.update({
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "date": today,
        "index": ({k: idx955.get(k) for k in ("date", "close", "ma", "pct", "level", "zone", "action")} if idx955 else None),
        "index922": ({k: idx922.get(k) for k in ("date", "close", "ma", "pct", "level", "zone", "action")} if idx922 else None),
        "fund": ({k: fund.get(k) for k in ("date", "close", "nav", "nav_date", "discount", "dev20", "level", "action")} if fund else None),
        "etf513300": ({k: etf.get(k) for k in ("date", "price", "nav", "nav_date", "premium", "nav_dev", "nav_dd", "level", "prem_lv")} if etf else None),
        "pushed": pushed,
    })
    json.dump(st, open(STATE_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


# ================== 盘中巡检模式（v2） ==================
BJ_TZ = timezone(timedelta(hours=8))
INTRADAY_LEVELS_IDX = ("DCA", "BUY1", "BUY2", "SELL")        # 红利类盘中预警档位
INTRADAY_LEVELS_FUND = ("STRONG", "BUY")                     # 金利盘中预警档位
INTRADAY_LEVELS_ETF = ("BUBBLE", "DIP1", "DIP2", "DIP3")     # 纳指ETF盘中预警档位


def bj_now():
    return datetime.now(BJ_TZ)


def is_trading_time(t):
    if t.weekday() >= 5:
        return False
    hm = t.hour * 100 + t.minute
    return (930 <= hm <= 1135) or (1255 <= hm <= 1505)


def mode_of_now():
    """auto 分流: 交易时段->intraday; 盘中窗口边界->silent; 其余->evening"""
    t = bj_now()
    if is_trading_time(t):
        return "intraday"
    hm = t.hour * 100 + t.minute
    if t.weekday() < 5 and 830 <= hm <= 1600:
        return "silent"
    return "evening"


def _intraday_snapshot_signal(hist_path, code, label):
    """以「历史库 + 今日盘中价」在内存中重算指数信号（不落盘）。"""
    snap = fetch_index_snapshot(code)
    h = json.load(open(hist_path, encoding="utf-8"))
    ds, cs = h["dates"][:], h["closes"][:]
    if ds and ds[-1] == snap["date"]:
        cs[-1] = snap["close"]
    else:
        ds.append(snap["date"])
        cs.append(snap["close"])
    return signal_for(cs, ds, label=label), snap["date"]


def intraday_run(args):
    """盘中巡检：仅在触发档位时推送（同日同档位去重）；不写任何历史数据文件。"""
    errors, trig = [], []
    today = None

    try:
        sig, d = _intraday_snapshot_signal(HIST_IDX, "cs930955", "cs930955")
        if sig.get("level") in INTRADAY_LEVELS_IDX:
            trig.append(("955:%s" % sig["level"], "红利低波100",
                         "- 红利低波100 最新 %.2f，偏离250日线 %+.2f%%（%s）→ %s"
                         % (sig["close"], sig["pct"], sig["zone"], sig["action"])))
        today = d
    except Exception as e:
        errors.append("红利低波100: %s" % e)

    try:
        sig, d = _intraday_snapshot_signal(HIST_922, "sh000922", "sh000922")
        if sig.get("level") in INTRADAY_LEVELS_IDX:
            trig.append(("922:%s" % sig["level"], "中证红利",
                         "- 中证红利 最新 %.2f，偏离250日线 %+.2f%%（%s）→ %s"
                         % (sig["close"], sig["pct"], sig["zone"], sig["action"])))
        today = today or d
    except Exception as e:
        errors.append("中证红利: %s" % e)

    try:
        rows = fetch_kline_rows("sz160128", 150)
        try:
            nav_info = fetch_fund_nav("160128")
        except Exception:
            nav_info = dict(date=None, nav=None)
        if nav_info.get("nav"):
            fs = fund_signal(rows, nav_info)
            if fs["level"] in INTRADAY_LEVELS_FUND:
                if fs["discount"] is not None and fs["discount"] > 0:
                    dtxt = "折价率 %+.2f%%（市价低于净值）" % fs["discount"]
                else:
                    dtxt = "价格类信号触发"
                trig.append(("160128:%s" % fs["level"], "南方金利定开",
                             "- 南方金利定开 %s → %s" % (dtxt, fs["action"])))
        today = today or rows[-1]["date"]
    except Exception as e:
        errors.append("南方金利定开: %s" % e)

    try:
        prows = fetch_kline_rows("sh513300", 500)
        navobj = json.load(open(NAV_513300, encoding="utf-8"))  # 只读，不更新
        et = etf513300_signal(prows, navobj["dates"], navobj["closes"])
        if et["level"] in INTRADAY_LEVELS_ETF:
            trig.append(("513300:%s" % et["level"], "纳指ETF",
                         "- 纳指ETF 价格 %.3f，净值偏离 %+.2f%%，距高点回撤 %+.2f%% → %s"
                         % (et["price"], et["nav_dev"], et["nav_dd"], et["action"])))
        today = today or et["date"]
    except Exception as e:
        errors.append("纳斯达克ETF: %s" % e)

    if today is None:
        print(json.dumps({"ok": True, "mode": "intraday", "note": "全部数据源暂不可用，静默", "errors": errors},
                         ensure_ascii=False))
        return

    prev = load_state()
    it = prev.get("intraday") or {}
    pushed = set(it.get("levels") or []) if it.get("date") == today else set()
    newt = [t for t in trig if t[0] not in pushed]
    if not newt:
        print(json.dumps({"ok": True, "mode": "intraday", "date": today,
                          "skipped": "无新触发（或已推过）", "errors": errors}, ensure_ascii=False))
        return

    names = "、".join(t[1] for t in newt)
    title = "⚠️盘中预警｜%s %s" % (names, today[5:])
    L = ["**【盘中快照 · 非收盘确认】北京时间 %s**" % bj_now().strftime("%H:%M"),
         "（盘中数据触发，收盘可能回落失效；最终以当晚日报为确认）", ""]
    L += [t[2] for t in newt]
    L += ["", "—— 系统自动推送 · 不构成投资建议"]
    content = "\n".join(L)

    res = push(title, content, dry=args.no_push)
    if res.get("ok") and not args.no_push:
        if (prev.get("intraday") or {}).get("date") != today:
            prev["intraday"] = {"date": today, "levels": []}
        prev["intraday"]["levels"] = sorted(pushed | {t[0] for t in newt})
        prev["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        json.dump(prev, open(STATE_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(json.dumps({"ok": bool(res.get("ok")) or args.no_push, "mode": "intraday", "title": title,
                      "triggered": [t[0] for t in newt], "push_res": res, "errors": errors},
                     ensure_ascii=False, indent=2))


# ---------- 主流程 ----------
def main():
    ap = argparse.ArgumentParser(description="四标的合并信号日报（晚间/盘中双模式）")
    ap.add_argument("--no-push", action="store_true", help="只算不发")
    ap.add_argument("--force", action="store_true", help="忽略非新交易日抑制")
    ap.add_argument("--mode", choices=["auto", "intraday", "evening"], default="evening",
                    help="auto=按北京时间自动分流(云端定时用); intraday=盘中巡检(触发才推); evening=晚间日报(默认)")
    ap.add_argument("--index-close", type=float, default=None, help="兜底: 手动传红利低波100收盘")
    ap.add_argument("--index-date", default=None, help="兜底: 手动传红利低波100日期")
    args = ap.parse_args()

    mode = args.mode
    if mode == "auto":
        mode = mode_of_now()
    if mode == "silent":
        print(json.dumps({"ok": True, "mode": "silent", "reason": "盘中窗口边界时段，静默"}, ensure_ascii=False))
        return
    if mode == "intraday":
        intraday_run(args)
        return
    evening_run(args)


def evening_run(args):
    errors = []

    # 1) 红利低波100
    hist955 = json.load(open(HIST_IDX, encoding="utf-8"))
    idx955 = None
    try:
        snap = fetch_index_snapshot("cs930955")
        merge_hist(hist955, snap["date"], snap["close"])
        json.dump(hist955, open(HIST_IDX, "w", encoding="utf-8"), ensure_ascii=False)
        idx955 = signal_for(hist955["closes"], hist955["dates"], label="cs930955")
    except Exception as e:
        errors.append("红利低波100快照失败: %s" % e)
        if args.index_close and args.index_date:
            merge_hist(hist955, args.index_date, args.index_close)
            json.dump(hist955, open(HIST_IDX, "w", encoding="utf-8"), ensure_ascii=False)
            idx955 = signal_for(hist955["closes"], hist955["dates"], label="cs930955")
            errors.append("已用 --index-close/--index-date 兜底")

    # 2) 中证红利 000922
    hist922 = json.load(open(HIST_922, encoding="utf-8"))
    idx922 = None
    try:
        snap2 = fetch_index_snapshot("sh000922")
        merge_hist(hist922, snap2["date"], snap2["close"])
        json.dump(hist922, open(HIST_922, "w", encoding="utf-8"), ensure_ascii=False)
        idx922 = signal_for(hist922["closes"], hist922["dates"], label="sh000922")
    except Exception as e:
        errors.append("中证红利快照失败: %s" % e)

    # 3) 南方金利定开 160128
    fund = None
    try:
        rows = fetch_kline_rows("sz160128", 150)
        try:
            nav_info = fetch_fund_nav("160128")
        except Exception as e:
            errors.append("金利净值抓取失败: %s" % e)
            nav_info = dict(date=None, nav=None)
        if nav_info.get("nav"):
            fund = fund_signal(rows, nav_info)
        else:
            fund = fund_signal(rows, dict(date="缺", nav=rows[-1]["close"]))
            fund.update(nav=None, nav_date=None, discount=None, disc_gap=None)
            if fund["new40"]:
                fund["level"], fund["action"] = "STRONG", "主信号(强): 创40日新低（净值暂缺）"
            elif fund["new20"] or fund["dev20"] <= FUND_TH["dev20_buy"]:
                fund["level"], fund["action"] = "BUY", "买点触发: 价格类信号（净值暂缺）"
            elif fund["dev20"] <= FUND_TH["dev20_watch"]:
                fund["level"], fund["action"] = "WATCH", "接近买点（净值暂缺）"
            else:
                fund["level"], fund["action"] = "NONE", "等待（净值暂缺）"
    except Exception as e:
        errors.append("金利数据抓取失败: %s" % e)

    # 4) 纳斯达克ETF 513300
    etf = None
    try:
        prows = fetch_kline_rows("sh513300", 500)
        nds, ncs, added = update_nav_lib(NAV_513300, "513300")
        etf = etf513300_signal(prows, nds, ncs)
        if added:
            errors.append("513300净值库更新 %d 条" % added)  # 信息性
    except Exception as e:
        errors.append("513300数据失败: %s" % e)

    # 5) 全失败则退出
    if idx955 is None and idx922 is None and fund is None and etf is None:
        print(json.dumps({"ok": False, "errors": errors}, ensure_ascii=False))
        sys.exit(1)

    today = ((idx955 or {}).get("date") or (idx922 or {}).get("date")
             or (etf or {}).get("date") or (fund or {}).get("date"))

    # 6) 非新交易日抑制
    prev = load_state()
    if not args.force and prev.get("date") == today and prev.get("pushed"):
        print(json.dumps({"ok": True, "skipped": "非新交易日（数据日期 %s 与上次一致），静默退出" % today},
                         ensure_ascii=False))
        return

    # 7) 组装 + 推送 + 状态
    title, content = build_message(idx955, idx922, fund, etf, today)
    res = push(title, content, dry=args.no_push)
    pushed = bool(res.get("ok"))
    if not args.no_push:
        save_state(today, idx955, idx922, fund, etf, pushed, prev)

    out = dict(ok=(pushed or args.no_push), title=title, pushed=pushed, push_res=res,
               index955=({k: idx955.get(k) for k in ("date", "close", "ma", "pct", "level", "zone", "action")} if idx955 else None),
               index922=({k: idx922.get(k) for k in ("date", "close", "ma", "pct", "level", "zone", "action")} if idx922 else None),
               fund=fund, etf513300=etf, errors=errors)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
