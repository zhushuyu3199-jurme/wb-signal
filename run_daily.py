# -*- coding: utf-8 -*-
"""
定盘 · 主程序
================================
一天两次：北京时间 12:30（午间快照）/ 22:00（完整版）

用法：
  python run_daily.py                     # 按当前时间自动判断
  python run_daily.py --mode night --dry  # 只打印不推送
  python run_daily.py --set-hk-yield 5.6  # 录入港股通红利股息率
  python run_daily.py --set-shiller 36.2  # 录入标普500 席勒PE
"""
import argparse, inspect, json, os, re, sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config as C
import indicators as I
import push_notify as P

# 仓库里若残留旧版 push_notify.py（send_pushplus 没有 template 参数），
# 直接传 template 会抛 TypeError，整次运行以退出码 1 失败、消息也发不出去。
# 这里先探测签名：旧版内部本来就写死 markdown，退化成不带该参数调用即可。
_TPL_OK = "template" in inspect.signature(P.send_pushplus).parameters


def f1(v, s=""):
    return "—" if v is None else "%.1f%s" % (v, s)


def f2(v, s=""):
    return "—" if v is None else "%.2f%s" % (v, s)


def sg(v, s="%"):
    return "—" if v is None else "%+.2f%s" % (v, s)


def target_weights(erp_target):
    if erp_target is None:
        erp_target = C.EQUITY_BASE
    scale = erp_target / C.EQUITY_BASE
    w = {k: C.ASSET_BY_KEY[k]["weight"] * scale for k in C.EQUITY_KEYS}
    w["gold"] = C.ASSET_BY_KEY["gold"]["weight"]
    w["short_bond"] = 1.0 - sum(w.values())
    if w["short_bond"] < 0.05:
        excess = 0.05 - w["short_bond"]
        eq = sum(w[k] for k in C.EQUITY_KEYS)
        for k in C.EQUITY_KEYS:
            w[k] -= excess * w[k] / eq
        w["short_bond"] = 0.05
    return w


def gather():
    codes = [a["etf"] for a in C.ASSETS if a.get("etf")]
    try:
        quotes = I.fetch_quotes(codes)
    except Exception as e:
        quotes = {}
        print("[warn] 行情失败: %s" % e)
    def get(k):
        code = C.ASSET_BY_KEY[k].get("etf")
        return quotes.get(code.lstrip("shsz")) if code else None
    erp = I.calc_erp()
    us10y = None
    try:
        us10y = I.fetch_us10y()
    except Exception:
        us10y = {"value": None, "src": "取数失败"}

    def safe(fn, *a):
        try:
            r = fn(*a)
        except Exception as ex:
            return {"ok": False, "err": "%s: %s" % (type(ex).__name__, ex)}
        if isinstance(r, dict):
            r.setdefault("ok", True)
            return r
        return {"ok": True, "value": r}

    sp_quote = get("sp500")
    return {
        "erp": erp,
        "a500": I.calc_a500(),
        "div_low": I.calc_div_layers(C.DIV_LOW),
        "div_922": I.calc_div_layers(C.DIV_922),
        "us10y": us10y,
        "sp": I.calc_sp500(sp_quote) if sp_quote else {"ok": False, "err": "无行情"},
        "sb": I.calc_short_bond(),
        "gold": I.calc_gold(),
        "gold_rmb": I.calc_gold_rmb(),
        "star50": I.calc_star50(),
        "reb": I.calc_rebalance(),
        "dxy": safe(I.calc_dxy),
    }


STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "send_state.json")


def _read_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


# PushPlus 返回码对照（用于日志判读）
PUSHPLUS_CODE_CN = {
    200: "受理成功",
    903: "用户token无效（Secret 里的 token 过期或填错）",
    999: "token 为空（Secret 未配置或变量名不是 PUSHPLUS_TOKEN）",
    900: "请求过于频繁",
    901: "参数错误",
    902: "配额已用尽",
}


def _check_resp(resp, channel="pushplus"):
    """PushPlus 用 code=200 表示受理；ServerChan 用 code=0。
    以前这里不校验，903 也会打印 ok:true，导致工作流全绿但没消息。"""
    try:
        j = json.loads(resp)
    except Exception:
        return False, None, "返回不是 JSON: %s" % str(resp)[:120]
    code = j.get("code")
    if channel == "serverchan":
        if code == 0:
            return True, code, ""
        return False, code, "ServerChan 拒绝(code=%s)：%s" % (
            code, j.get("message") or j.get("msg") or "未知错误")
    if code == 200:
        return True, code, ""
    msg = PUSHPLUS_CODE_CN.get(code) or j.get("msg") or j.get("data") or "未知错误"
    return False, code, "PushPlus 拒绝(code=%s)：%s" % (code, msg)


def _load_slots():
    """已发送的时段清单，形如 {"2026-11-03-noon": "iso时间"}"""
    st = _read_state()
    if isinstance(st.get("slots"), dict):
        return dict(st["slots"])
    if st.get("slot"):                      # 兼容旧格式
        return {st["slot"]: st.get("at", "")}
    return {}


def _save_slot(slot, when_iso):
    slots = _load_slots()
    slots[slot] = when_iso
    cutoff = (I.now_bj().date() - timedelta(days=10)).isoformat()
    slots = {k: v for k, v in slots.items() if k[:10] >= cutoff}
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump({"slots": slots}, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def _pick_slot(now):
    """挑出「已到点但还没发」的那一期，允许回看 14 小时。
    GitHub 定时任务常延迟数小时，这样即使晚到也能把漏掉的那期补上，
    而不是像旧逻辑那样整期静默丢弃。"""
    sent = _load_slots()
    cands = []
    for back in (0, 1):
        d = (now - timedelta(days=back)).date()
        for name, hour in (("noon", C.SLOT_NOON_HOUR), ("night", C.SLOT_NIGHT_HOUR)):
            t = now.replace(year=d.year, month=d.month, day=d.day,
                            hour=hour, minute=0, second=0, microsecond=0)
            if t <= now and (now - t) <= timedelta(hours=C.SLOT_LOOKBACK_H):
                cands.append((t, "%s-%s" % (d.isoformat(), name), name))
    # 取最新的那一期：若积压了两期（如昨夜被静默跳过），补最新的，陈旧的那期直接丢弃，
    # 避免出现「今天 12:00 却在发昨天的夜间版」这种一天接一天的错位。
    for t, slot, name in sorted(cands, reverse=True):
        if slot not in sent:
            return slot, name, t
    return None, None, None


def legacy_blocks():
    """旧持仓：仅南方金利定开"""
    import combo_daily as L
    out = []
    try:
        rows = L.fetch_kline_rows("sz160128", 150)
        try:
            nav_info = L.fetch_fund_nav("160128")
        except Exception:
            nav_info = dict(date=None, nav=None)
        if nav_info.get("nav"):
            f = L.fund_signal(rows, nav_info)
            dc = f["discount"]
            if dc is None:
                disc = "净值暂缺"
            elif dc > 0.01:
                disc = "市价低于净值 **%.2f%%**（折价）" % dc
            elif dc < -0.01:
                disc = "市价高于净值 **%.2f%%**（溢价）" % abs(dc)
            else:
                disc = "市价≈净值（平价）"
        else:
            f = L.fund_signal(rows, dict(date="缺", nav=rows[-1]["close"]))
            f.update(nav=None, nav_date=None, discount=None, disc_gap=None)
            disc = "净值暂缺"
        d2 = "上方" if f["dev20"] >= 0 else "下方"
        lo = []
        if f["new20"]:
            lo.append("20日新低")
        if f["new40"]:
            lo.append("40日新低")
        out += ["", "**⑨ 南方金利定开** 160128 · {{D:legacy}}",
                "折价　%s" % disc,
                "位置　20日线%s **%+.2f%%** ｜ 新低 %s" %
                (d2, f["dev20"], "、".join(lo) if lo else "无"),
                "→ **%s**" % ("可以买（折价或新低已触发）" if f["level"] in ("BUY", "STRONG")
                            else "等待：折价不够深、也没创新低，暂不买")]
    except Exception as e:
        out += ["", "**⑨ 南方金利定开**：取数失败（%s）" % e]
    return out


ZONE_CN = {
    "常态持有区": "正常区间，不用动",
    "偏热区": "高于年线较多",
    "极端泡沫区": "远高于年线（极值）",
    "定投区(-5%~-10%)": "低于年线，可定投",
    "深度破位(加仓区)": "远低于年线",
    "深度破位(极值区)": "远低于年线（极值）",
    "数据不足": "数据不足",
}
ACT_CN = {
    "持有不动,吃股息": "持有，拿分红",
    "可小幅止盈(慎用)": "可以小幅止盈",
    "考虑大笔减仓(留核心仓)": "大笔减仓，留底仓",
    "可考虑开始定投(小额定期买入)": "可以开始小额定投",
    "开始分批加仓": "开始分批加仓",
    "大笔重仓抄底": "大笔买入",
}


def div_lines(tag, cfg, x, weighted):
    """红利指数三层：赔率 / 位置 / 拥挤（一行一个指标）"""
    L = []
    L.append("**%s %s** %s · {{D:%s}}" % (tag, cfg["name"], cfg["code"], cfg["key"]))
    if not x.get("ok"):
        L.append("→ 取数失败：%s" % x.get("err"))
        return L

    o = x["odds"]
    L.append("赔率　**%.2fpp**（股息 %.2f%% − 国债 %.2f%%，分位 %.0f%%）→ %s" %
             (o["spread"], o["yld"], o["bond"], o["pct_all"], o["label"]))

    word = "上方" if x["dev"] >= 0 else "下方"
    L.append("位置　年线%s **%.2f%%**（%s）" % (word, abs(x["dev"]), x["trend"]))

    if x.get("crowd") is None:
        L.append("拥挤　—（成交额取不到）")
    else:
        L.append("拥挤　**%.2f%%**（分位 %.0f%%）→ %s" %
                 (x["crowd"] * 100, x["crowd_pct"], x["crowd_label"]))

    tail = "　只观察" if not weighted else ""
    L.append("→ **%s**%s" % (x["action"], tail))
    return L


TERM_RE = re.compile(r"\{\{T:([a-z]+)\|([^}]+)\}\}")
DOC_RE = re.compile(r"\{\{D:([a-z0-9_]+)\}\}")


def render_md(md):
    """占位符 → markdown 链接。
    不用 html 模板：实测 PushPlus 的 html 通道会把 <a> 剥掉，正文只剩黑字；
    markdown 模板里的 [文字](url) 反而能保留成可点的超链接。"""
    url = C.GLOSSARY_URL
    if url:
        s = TERM_RE.sub(lambda m: "[%s](%s#%s)" % (m.group(2), url, m.group(1)), md)
        s = DOC_RE.sub(lambda m: "[说明](%s#%s)" % (
            url, C.DOC_ANCHOR.get(m.group(1), "top")), s)
    else:
        s = TERM_RE.sub(lambda m: m.group(2), md)
        s = DOC_RE.sub("说明", s)
    # markdown 的单换行在部分渲染器里会被合并成空格，行尾补两个空格强制换行
    return "\n".join(x + "  " if x.strip() else x for x in s.split("\n"))


def plain(md):
    """去掉占位符，用于本地 --dry 预览"""
    s = TERM_RE.sub(lambda m: m.group(2), md)
    return DOC_RE.sub("说明", s)


def dist_txt(cur, level):
    """现价到某个位置还差多少：往前跌写「还需跌」，往上走写「还需涨」"""
    if cur is None or level is None:
        return "—"
    diff = level - cur
    pct = (level / cur - 1) * 100
    word = "还需跌" if diff < 0 else "还需涨"
    return "%s %.1f 点（%+.1f%%）" % (word, abs(diff), pct)



def build(d, mode, now=None):
    now = now or I.now_bj()
    night = (mode == "night")
    L = []
    L.append("**%s · %s**\u3000%s" % (C.PUSH_TITLE_PREFIX,
                                  "夜间完整版" if night else "午间快照",
                                  now.strftime("%m-%d %H:%M")))

    # 总开关
    e = d["erp"]
    L.append("")
    L.append("**◆ 总开关**")
    if e.get("ok"):
        L.append("股债性价比　**%s** → 股票 **%d%%**（%s）" %
                 (f2(e["erp"], "%"), round(e["target"] * 100), e["label"]))
        L.append("宽基 PE　%s（%s）｜ 10年国债 %s" %
                 (f2(e["pe"]), e["pe_src"], f2(e["bond"], "%")))
    else:
        L.append("取数失败：%s" % e.get("err"))

    w = target_weights(e.get("target") if e.get("ok") else None)
    L.append("")
    L.append("**◆ 目标权重**")
    L.append(" ｜ ".join(
        "%s %d%%" % (C.ASSET_BY_KEY[k]["name"], round(w[k] * 100))
        for k in ("a500", "div_low", "sp500", "short_bond", "gold")))

    # ① A500
    a = d["a500"]
    L.append("")
    L.append("**① 中证A500** 512050 · {{D:a500}}")
    if a.get("ok"):
        tag = "（借用%s）" % a["src"] if a["proxy"] else ""
        btxt = a["basis"].replace("PB", "市净率").replace("PE", "市盈率")
        btxt = btxt.replace("降一档", "已降一档").replace("取高", "改取较高")
        L.append("估值　PE分位 **%s**%s ｜ PB分位 **%s**" %
                 (f1(a["pct_pe"], "%"), tag, f1(a["pb_pct"], "%")))
        L.append("取用　**%s**（%s）" % (f1(a["pct"], "%"), btxt))
        amt = "，本月 %s 元" % format(int(a["amount"]), ",") if a.get("amount") else ""
        L.append("→ **%s** ×%.1f%s" % (a["action"], a["mult"], amt))
        if a.get("warn"):
            L.append("※ PE/PB 分位差 %.0f 个百分点，盈利在变差，已自动降一档" % a["div"])
    else:
        L.append("→ 取数失败：%s" % a.get("err"))

    # ② 红利低波（10%）
    L.append("")
    L.extend(div_lines("②", C.DIV_LOW, d["div_low"], True))

    # ③ 中证红利（观察）
    L.append("")
    L.extend(div_lines("③", C.DIV_922, d["div_922"], False))

    # ④ 标普500
    sp = d["sp"]
    L.append("")
    L.append("**④ 标普500** 513500 · {{D:sp500}}")
    if sp.get("ok"):
        shv, exp8 = sp.get("shiller"), sp.get("exp8")
        L.append("溢价　**%s**（按昨日净值算）" % sg(sp["prem"]))
        if shv is None:
            L.append("席勒　—（本次没取到）")
        else:
            src = sp.get("shiller_src") or ""
            tail = "" if src.startswith("Guru") else "（来源 %s）" % (src or "备用源")
            L.append("席勒　**%s**%s ｜ 普通PE %s（分位 %s）" %
                     (f1(shv), tail, f1(sp["pe"]), f1(sp["pe_pct"], "%")))
            if shv > 35:
                L.append("※ 已进警戒区（只提示风险，不据此减仓）")
            if exp8 is not None:
                L.append("※ 按席勒倒推未来8年年化约 %s" % f1(exp8, "%"))
        L.append("→ **%s**" % sp["action"])
        if sp["prem"] >= 1.5:
            L.append("※ 下单前核实实时 IOPV")
    else:
        L.append("→ 取数失败：%s" % sp.get("err"))

    # ⑤ 固收桶
    b = d["sb"]
    L.append("")
    L.append("**⑤ 固收桶** 511360 为主 · {{D:short_bond}}")
    if b.get("ok"):
        rate = b.get("gc007") or b.get("shibor")
        fr = "%s" % f2(rate, "%") if rate else "—"
        L.append("年化　近7日 **%s** ｜ 资金利率 %s" % (f2(b["ann"], "%"), fr))
        L.append("窗口　%s" % (b.get("window") or "无"))
        if b.get("lock"):
            L.append("→ **7日年化≥%.1f%%，可锁定**" % C.SHORT_BOND["lock_yield"])
        elif b.get("window"):
            L.append("→ **%s资金面，闲钱可多买一点**" % b["window"])
        else:
            L.append("→ **正常持有，不动**")
    else:
        L.append("→ 取数失败：%s" % b.get("err"))

    # ⑥ 黄金
    g = d["gold"]
    L.append("")
    L.append("**⑥ 黄金** 518880 · {{D:gold}}")
    if g.get("ok"):
        dx = d.get("dxy") or {}
        L.append("实际利率　TIPS **%s**（20天 %+.2fpp）" % (f2(g["tips"], "%"), g["tips_chg20"]))
        if dx.get("ok"):
            L.append("美元指数　**%s**（%s）" % (f2(dx["value"]), dx["zone"]))
        L.append("回撤　距一年高点 **%s** ｜ 近60天 %s" % (sg(g["dd"]), sg(g["chg60"])))
        if g.get("spike"):
            L.append("→ **60日涨超%.0f%%，先减一部分**" % C.GOLD["spike_cut"])
        elif g.get("trigger"):
            L.append("→ **实际利率下行 + 回撤深，可加**")
        else:
            L.append("→ **%s**" % g["label"])
    else:
        L.append("→ 取数失败：%s" % g.get("err"))

    # ⑦ 人民币金价（沪金主力）
    gr = d.get("gold_rmb") or {}
    L.append("")
    L.append("**⑦ 人民币金价** 沪金主力 · {{D:gold_rmb}}")
    if gr.get("ok"):
        L.append("现价　**%.2f** 元/克（%s）" % (gr["price"], sg(gr.get("chg"))))
        for item in gr.get("levels") or []:
            L.append("%d　%s" % (round(item["level"]), dist_txt(gr["price"], item["level"])))
    else:
        L.append("→ 取数失败：%s" % gr.get("err", "无数据"))

    # ⑧ 科创综指（000680）
    st = d.get("star50") or {}
    L.append("")
    L.append("**⑧ 科创综指** 000680 · {{D:star50}}")
    if st.get("ok"):
        L.append("点位　**%.2f**（%s）" % (st["price"], sg(st.get("chg"))))
        L.append("趋势　年线%s **%.2f%%**（%s）→ %s" %
                 ("上方" if st["dev"] >= 0 else "下方", abs(st["dev"]),
                  st["trend"], st["zone"]))
        L.append("%d　%s" % (round(st["level"]), dist_txt(st["price"], st["level"])))
    else:
        L.append("→ 取数失败：%s" % st.get("err", "无数据"))

    # ⑦ 旧持仓
    try:
        lg = legacy_blocks()
    except Exception:
        lg = []
    if lg:
        L.extend(lg)

    # 今日动作
    acts = []
    if a.get("ok"):
        if a["mult"] >= 2:
            acts.append("A500 %s，本月 %.1f×" % (a["action"], a["mult"]))
        elif a["mult"] == 0:
            acts.append("A500 停投，钱转固收桶/减仓")
        elif a["mult"] < 1:
            acts.append("A500 定投减半")
    dl = d["div_low"]
    if dl.get("ok") and dl["action"] != "持有，按计划定投":
        acts.append("红利低波：%s" % dl["action"])
    if sp.get("ok") and sp["prem"] >= 1.5:
        acts.append("标普500：%s" % sp["action"])
    if b.get("ok") and b.get("lock"):
        acts.append("固收桶：年化≥2.5%，可锁定")
    if g.get("ok") and (g.get("spike") or g.get("trigger")):
        acts.append("黄金：%s" % ("减一部分" if g["spike"] else "可加"))

    L.append("")
    L.append("**◆ 今日动作**")
    if acts:
        for x in acts:
            L.append("· " + x)
    else:
        L.append("· 无操作 —— 按计划执行即可")

    if C.GLOSSARY_URL:
        L.append("")
        L.append("各标的为什么这么看 → [说明与术语](%s)" % C.GLOSSARY_URL)

    if night:
        r = d["reb"]
        if r.get("in_window"):
            L.append("")
            L.append("**◆ 再平衡窗口（12月最后一周）**")
            if r.get("ok"):
                for row in r["rows"]:
                    L.append("· %s 现%.0f%% / 目标%.0f%%%s" %
                             (row["name"], row["cur"] * 100, row["tgt"] * 100,
                              " ⚠" if row["alert"] else ""))
                L.append("\u3000先用新钱补低配，尽量不卖高配")
            else:
                L.append("· " + (r.get("err") or "未配置"))

        maint = []
        if sp.get("ok") and sp.get("shiller_src") == "手动":
            maint.append("标普500 席勒PE：GuruFocus 抓取失败，当前为手动值")
        if maint:
            L.append("")
            L.append("**◆ 待维护**")
            for x in maint:
                L.append("· " + x)

        bad = [n for k, n in (("erp", "ERP"), ("a500", "A500"), ("div_low", "红利低波"),
                              ("div_922", "中证红利"), ("sp", "标普500"), ("sb", "固收桶"),
                              ("gold", "黄金"), ("gold_rmb", "人民币金价"),
                              ("star50", "科创综指"), ("dxy", "美元指数"))
               if not d.get(k, {}).get("ok")]
        if bad:
            L.append("")
            L.append("**◆ 数据异常**：%s（显示「—」，其余不受影响）" % "、".join(bad))
        L.append("")
        L.append("---")
        utc = (now - timedelta(hours=8)).strftime("%m-%d %H:%M")
        L.append("*执行 %s 北京 / %s UTC ｜ 触发：%s*" %
                 (now.strftime("%m-%d %H:%M"), utc,
                  os.environ.get("TRIGGER", "本地/未知")))

    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["auto", "noon", "night"], default="auto")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--set-hk-yield", type=float, default=None)
    ap.add_argument("--set-shiller", type=float, default=None)
    args = ap.parse_args()

    if args.set_hk_yield is not None:
        m = I.load_manual()
        m["hk_yield"] = {"value": args.set_hk_yield, "asof": I.now_bj().strftime("%Y-%m-%d")}
        I.save_manual(m)
        print("已更新港股通红利股息率：%.2f%%（%s）" % (args.set_hk_yield, m["hk_yield"]["asof"]))
        return 0
    if args.set_shiller is not None:
        m = I.load_manual()
        m["shiller_pe"] = {"value": args.set_shiller, "asof": I.now_bj().strftime("%Y-%m-%d")}
        I.save_manual(m)
        print("已更新标普500 席勒PE：%.1f" % args.set_shiller)
        return 0

    now = I.now_bj()
    hh = now.hour + now.minute / 60.0

    if now.weekday() >= 5 and not args.force:
        print("周末休市，跳过（--force 强制）")
        return 0

    late = False
    mark_slot = True
    if args.force:
        mode = args.mode
        if mode == "auto":
            # 手动测试时的版式：15:00 之前算午间，之后算夜间
            mode = "noon" if hh < 15.0 else "night"
        slot = now.strftime("%Y-%m-%d") + "-" + mode
        # 强制运行若正好踩在目标时点（±45 分钟），才占用该时段，用来挡掉随后迟到的那次定时；
        # 其他时间的手动测试不占用，否则上午 10 点跑一次会把「夜间」标记成已发，晚上 22:00 就被跳过。
        tgt_hour = C.SLOT_NOON_HOUR if mode == "noon" else C.SLOT_NIGHT_HOUR
        tgt = now.replace(hour=tgt_hour, minute=0, second=0, microsecond=0)
        mark_slot = abs((now - tgt).total_seconds()) <= C.LATE_TOLERANCE_MIN * 60
    else:
        slot, mode, target = _pick_slot(now)
        if slot is None:
            print("当前没有「已到点且未发送」的时段，跳过（--force 可强制再发）")
            return 0
        if hh >= C.QUIET_FROM or hh < C.QUIET_TO:
            print("当前北京 %.2f 点处于静默时段（%.0f:00–%.0f:00），"
                  "延迟到的运行不补发，跳过" % (hh, C.QUIET_FROM, C.QUIET_TO))
            return 0
        gap_min = (now - target).total_seconds() / 60.0
        late = gap_min > C.LATE_TOLERANCE_MIN
        if late:
            print("[补发] 目标 %s，实际 %s，晚 %.0f 分钟"
                  % (target.strftime("%m-%d %H:%M"), now.strftime("%m-%d %H:%M"), gap_min))

    msg = build(gather(), mode, now)
    print(plain(msg))
    if args.dry:
        return 0

    title = "%s %s %s%s" % (C.PUSH_TITLE_PREFIX, "午间" if mode == "noon" else "夜间",
                            now.strftime("%m-%d"), "（补发）" if late else "")
    try:
        cfg = P.load_config()
        ch = cfg.get("channel", "pushplus")
        if ch == "pushplus":
            token = os.environ.get("PUSHPLUS_TOKEN") or cfg.get("pushplus_token", "")
            src = "Secret" if os.environ.get("PUSHPLUS_TOKEN") else "本地配置"
            if not token:
                print('{"ok": false, "error": "缺少 PUSHPLUS_TOKEN（Secret 未配置）"}')
                return 2
            # 回显 token 后 4 位，便于和本地对比是否同一枚
            print("[push] channel=pushplus 来源=%s token=...%s 长度=%d 主机=www.pushplus.plus"
                  % (src, token[-4:], len(token)))
            # 用 markdown 模板：html 模板下 PushPlus 会把 <a> 剥掉，正文里的链接全部消失
            tpl = "markdown"
            body = render_md(msg)
            if _TPL_OK:
                resp = P.send_pushplus(token, title, body,
                                       channel=cfg.get("pushplus_channel"), template=tpl)
            else:
                print("[warn] push_notify.py 是旧版（send_pushplus 无 template 参数），"
                      "本次按它内置的 markdown 发送；建议把仓库里的 push_notify.py 换成本地新版")
                resp = P.send_pushplus(token, title, body,
                                       channel=cfg.get("pushplus_channel"))
        else:
            resp = P.send_serverchan(os.environ.get("SERVERCHAN_KEY") or cfg.get("serverchan_key", ""),
                                     title, msg)

        ok, code, detail = _check_resp(resp, ch)
        if not ok:
            print('{"ok": false, "code": %s, "error": "%s", "raw": %s}' % (code, detail, resp))
            return 1
        if mark_slot:
            _save_slot(slot, now.isoformat())
        print('{"ok": true, "resp": %s}' % resp)
    except Exception as ex:
        print('{"ok": false, "error": "%s: %s"}' % (type(ex).__name__, ex))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
