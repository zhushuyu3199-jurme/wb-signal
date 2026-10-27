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
import argparse, json, os, sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config as C
import indicators as I
import push_notify as P


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
            r["ok"] = True
            return r
        except Exception as ex:
            return {"ok": False, "err": "%s: %s" % (type(ex).__name__, ex)}

    return {
        "erp": erp,
        "a500": I.calc_a500(),
        "div_low": I.calc_div_index(C.DIV_LOW, erp.get("bond") if erp.get("ok") else None),
        "div_922": I.calc_div_index(C.DIV_922, erp.get("bond") if erp.get("ok") else None),
        "us10y": us10y,
        "sp": I.calc_sp500(get("sp500")) if get("sp500") else {"ok": False, "err": "无行情"},
        "sb": I.calc_short_bond(),
        "gold": I.calc_gold(),
        "reb": I.calc_rebalance(),
        "dxy": safe(I.calc_dxy),
        "sbx": safe(I.calc_southbound),
    }


STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "send_state.json")
DEDUPE_MINUTES = 40


def _read_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_state(slot, when_iso):
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump({"slot": slot, "at": when_iso}, f, ensure_ascii=False)
    except Exception:
        pass


def legacy_blocks():
    """旧持仓：仅南方金利定开"""
    import combo_daily as L
    out = []
    p1 = C.LEGACY[0]["pit_short"]
    try:
        rows = L.fetch_kline_rows("sz160128", 150)
        try:
            nav_info = L.fetch_fund_nav("160128")
        except Exception:
            nav_info = dict(date=None, nav=None)
        if nav_info.get("nav"):
            f = L.fund_signal(rows, nav_info)
            disc = ("折价 **%+.2f%%**" % f["discount"]) if f["discount"] is not None else "折价 —"
        else:
            f = L.fund_signal(rows, dict(date="缺", nav=rows[-1]["close"]))
            f.update(nav=None, nav_date=None, discount=None, disc_gap=None)
            disc = "净值暂缺"
        out += ["", "**⑦ 南方金利定开（160128）· 折价率**",
                "%s ｜ 20日偏离 %+.2f%% ｜ 新低20/40：%s/%s" %
                (disc, f["dev20"], "是" if f["new20"] else "否", "是" if f["new40"] else "否"),
                "→ %s" % f["action"], "⚠ " + p1]
    except Exception as e:
        out += ["", "**⑦ 南方金利定开**：取数失败（%s）" % e]
    return out


def div_lines(tag, cfg, x, weighted):
    """红利指数：位置 + 估值 双轴"""
    L = []
    title = "**%s %s（%s）· %s**" % (
        tag, cfg["name"], cfg["code"],
        "锚：均线位置 + 估值分位" if weighted else "观察（不加权）")
    L.append(title)
    if not x.get("ok"):
        L.append("→ 取数失败：%s" % x.get("err"))
        L.append("⚠ " + cfg["pit_short"])
        return L
    L.append("位置 偏离 **%+.2f%%**（%s）｜ 距250日高 %+.1f%% ｜ 源：%s" %
             (x["pct"], x["zone"], x["dd"], x["src"]))
    L.append("估值 股息率 **%.2f%%** ｜ PE %.2f（分位%.0f%%）｜ PB %.2f（分位%.0f%%）" %
             (x["yld"], x["pe"], x["pe_pct"], x["pb"], x["pb_pct"]))
    tail = " ｜ 仅观察" if not weighted else ""
    L.append("→ 位置：%s ｜ 估值：**%s**%s" % (x["action"], x["val"], tail))
    L.append("⚠ " + cfg["pit_short"])
    return L


def build(d, mode):
    now = I.now_bj()
    night = (mode == "night")
    L = []
    L.append("**%s · %s**\u3000%s" % (C.PUSH_TITLE_PREFIX,
                                  "夜间完整版" if night else "午间快照",
                                  now.strftime("%m-%d %H:%M")))

    # 总开关
    e = d["erp"]
    L.append("")
    L.append("**◆ 总开关｜ERP 决定总仓位**")
    if e.get("ok"):
        L.append("ERP **%s** → 权益 **%d%%**（%s）" %
                 (f2(e["erp"], "%"), round(e["target"] * 100), e["label"]))
        L.append("\u3000PE %s（%s）｜10年国债 %s" % (f2(e["pe"]), e["pe_src"], f2(e["bond"], "%")))
    else:
        L.append("取数失败：%s" % e.get("err"))

    w = target_weights(e.get("target") if e.get("ok") else None)
    L.append("")
    L.append("**◆ 本期目标权重**")
    L.append("\u3000" + " ｜ ".join(
        "%s %d%%" % (C.ASSET_BY_KEY[k]["name"], round(w[k] * 100))
        for k in ("a500", "div_low", "sp500", "short_bond", "gold")))

    # ① A500
    a = d["a500"]
    L.append("")
    L.append("**① 中证A500（512050）· 锚：估值分位**")
    if a.get("ok"):
        tag = "（%s代理）" % a["src"] if a["proxy"] else ""
        L.append("PE分位 **%s**%s ｜ PB分位 **%s** ｜ 取低 **%s**（%s）" %
                 (f1(a["pct_pe"], "%"), tag, f1(a["pb_pct"], "%"),
                  f1(a["pct"], "%"), a["basis"]))
        amt = "（%s元）" % format(int(a["amount"]), ",") if a.get("amount") else ""
        L.append("→ **%s %.1f×**%s" % (a["action"], a["mult"], amt))
        if a.get("warn"):
            L.append("\u3000※ " + a["warn"])
    else:
        L.append("→ 取数失败：%s" % a.get("err"))
    L.append("⚠ " + C.A500["pit_short"])

    # ② 红利低波（10%）
    L.append("")
    L.extend(div_lines("②", C.DIV_LOW, d["div_low"], True))

    # ③ 中证红利（观察）
    L.append("")
    L.extend(div_lines("③", C.DIV_922, d["div_922"], False))

    # ④ 标普500
    sp = d["sp"]
    L.append("")
    L.append("**④ 标普500（513500）· 锚：溢价率**")
    if sp.get("ok"):
        shv, exp8 = sp.get("shiller"), sp.get("exp8")
        sht = f1(shv)
        if exp8 is not None:
            sht += "（8年预期年化 %s）" % f1(exp8, "%")
        L.append("溢价 **%s**（T-1净值）｜ 席勒PE **%s**" % (sg(sp["prem"]), sht))
        L.append("PE %s（分位%s）%s" %
                 (f1(sp["pe"]), f1(sp["pe_pct"], "%"),
                  " ｜ 席勒PE>35 属警戒区" if (shv or 0) > 35 else ""))
        L.append("→ **%s**" % sp["action"])
        if sp["prem"] >= 1.5:
            L.append("\u3000※ 下单前必须核实实时 IOPV")
    else:
        L.append("→ 取数失败：%s" % sp.get("err"))
    L.append("⚠ " + C.SP500["pit_short"])

    # ⑤ 固收桶
    b = d["sb"]
    L.append("")
    L.append("**⑤ %s（511360 为主）· 锚：资金面，不择时**" % C.ASSET_BY_KEY["short_bond"]["name"])
    if b.get("ok"):
        fr = []
        if b.get("gc007"):
            fr.append("GC007 %s" % f2(b["gc007"], "%"))
        if b.get("shibor"):
            fr.append("Shibor1W %s" % f2(b["shibor"], "%"))
        L.append("7日年化 **%s** ｜ 资金面 %s ｜ 窗口：%s" %
                 (f2(b["ann"], "%"), " / ".join(fr) or "—", b.get("window") or "无"))
        if b.get("lock"):
            L.append("→ **7日年化≥%.1f%%，可锁定**" % C.SHORT_BOND["lock_yield"])
        elif b.get("window"):
            L.append("→ **%s资金面，闲钱可多买一点**" % b["window"])
        else:
            L.append("→ **正常持有，不动**")
        L.append("\u3000构成：%s" % C.ASSET_BY_KEY["short_bond"].get("components", ""))
    else:
        L.append("→ 取数失败：%s" % b.get("err"))
    L.append("⚠ " + C.SHORT_BOND["pit_short"])

    # ⑥ 黄金
    g = d["gold"]
    L.append("")
    L.append("**⑥ 黄金（518880）· 锚：美国实际利率**")
    if g.get("ok"):
        dx = d.get("dxy") or {}
        l1 = "TIPS **%s**（20日 %s）" % (f2(g["tips"], "%"), sg(g["tips_chg20"], "pp"))
        if dx.get("ok"):
            l1 += " ｜ 美元 **%s**（%s）" % (f2(dx["value"]), dx["zone"])
        L.append(l1)
        L.append("距250日高 **%s** ｜ 60日 %s ｜ TIPS日期 %s" %
                 (sg(g["dd"]), sg(g["chg60"]), g["tips_date"]))
        if g.get("spike"):
            L.append("→ **60日涨超%.0f%%，先减一部分**" % C.GOLD["spike_cut"])
        elif g.get("trigger"):
            L.append("→ **实际利率下行 + 回撤深，可加**")
        else:
            L.append("→ **%s**" % g["label"])
    else:
        L.append("→ 取数失败：%s" % g.get("err"))
    L.append("⚠ " + C.GOLD["pit_short"])

    # ⑦ 旧持仓
    try:
        lg = legacy_blocks()
    except Exception:
        lg = []
    if lg:
        L.append("")
        L.append("**◆ 旧持仓**")
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
    if dl.get("ok") and dl["level"] in ("DCA", "BUY1", "BUY2"):
        acts.append("红利低波：%s" % dl["action"])
    elif dl.get("ok") and dl["level"] in ("TRIM", "SELL"):
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
        if g.get("ok") and False:
            pass
        if sp.get("ok") and sp.get("shiller_src") == "手动":
            maint.append("标普500 席勒PE：GuruFocus 抓取失败，当前为手动值")
        if maint:
            L.append("")
            L.append("**◆ 待维护**")
            for x in maint:
                L.append("· " + x)

        bad = [n for k, n in (("erp", "ERP"), ("a500", "A500"), ("div_low", "红利低波"),
                              ("div_922", "中证红利"), ("sp", "标普500"), ("sb", "固收桶"),
                              ("gold", "黄金"), ("dxy", "美元指数"))
               if not d[k].get("ok")]
        if bad:
            L.append("")
            L.append("**◆ 数据异常**：%s（显示「—」，其余不受影响）" % "、".join(bad))
        L.append("")
        L.append("---")
        utc = (now - timedelta(hours=8)).strftime("%m-%d %H:%M")
        L.append("*执行 %s 北京 / %s UTC ｜ 触发：%s*" %
                 (now.strftime("%m-%d %H:%M"), utc,
                  os.environ.get("TRIGGER", "本地/未知")))
        L.append("*六个标的锚不同：估值分位 / 均线+估值 / 溢价率 / 资金面 / 实际利率*")

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
    if now.weekday() >= 5 and not args.force:
        print("周末休市，跳过（--force 强制）")
        return 0

    mode = args.mode
    if mode == "auto":
        hh = now.hour + now.minute / 60.0
        mode = "noon" if 11.0 <= hh < 15.0 else "night"

    hh = now.hour + now.minute / 60.0
    lo, hi = (C.SEND_WINDOW_NOON if mode == "noon" else C.SEND_WINDOW_NIGHT)
    if not args.force and not (lo <= hh < hi):
        print("当前北京 %.2f 点，不在 %s 推送窗口 %.1f–%.1f，跳过（--force 可强制）"
              % (hh, "午间" if mode == "noon" else "夜间", lo, hi))
        return 0

    slot = now.strftime("%Y-%m-%d") + "-" + mode
    prev = _read_state()
    if prev.get("slot") == slot and not args.force:
        try:
            gap = (now - datetime.fromisoformat(prev["at"])).total_seconds() / 60.0
            if gap < DEDUPE_MINUTES:
                print("同一时段 %.0f 分钟前已推送，跳过（防重复）" % gap)
                return 0
        except Exception:
            pass

    msg = build(gather(), mode)
    print(msg)
    if args.dry:
        return 0

    title = "%s %s %s" % (C.PUSH_TITLE_PREFIX, "午间" if mode == "noon" else "夜间",
                          now.strftime("%m-%d"))
    try:
        cfg = P.load_config()
        ch = cfg.get("channel", "pushplus")
        if ch == "pushplus":
            token = os.environ.get("PUSHPLUS_TOKEN") or cfg.get("pushplus_token", "")
            if not token:
                print('{"ok": false, "error": "缺少 PUSHPLUS_TOKEN"}')
                return 2
            resp = P.send_pushplus(token, title, msg)
        else:
            resp = P.send_serverchan(os.environ.get("SERVERCHAN_KEY") or cfg.get("serverchan_key", ""),
                                     title, msg)
        _write_state(slot, now.isoformat())
        print('{"ok": true, "resp": %s}' % resp)
    except Exception as ex:
        print('{"ok": false, "error": "%s"}' % ex)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
