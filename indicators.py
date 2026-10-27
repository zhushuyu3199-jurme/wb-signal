# -*- coding: utf-8 -*-
"""
指标引擎 v2 · 五个标的各用各的锚
================================
每项独立取数、独立失败：某一项挂了显示「—」，不影响其他指标。

数据源：
  行情/日线   腾讯 qt.gtimg.cn / web.ifzq.gtimg.cn
  指数估值    蛋卷 danjuanfunds.com（A500 / 沪深300 / 标普500）
  国债收益率  东财 datacenter-web.eastmoney.com
  基金净值    东财 api.fund.eastmoney.com
  实际利率    FRED fred.stlouisfed.org (DFII10)
  资金面      上交所 GC007（DR007 代理）
"""
import csv, io, json, os, time, urllib.request
from datetime import datetime, timedelta, timezone

import config as C

BASE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASE, "cache")
MANUAL_PATH = os.path.join(BASE, "manual.json")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
BJ = timezone(timedelta(hours=8))


def now_bj():
    return datetime.now(BJ)


def _get(url, referer=None, timeout=20, encoding="utf-8"):
    h = dict(UA); h["Accept"] = "*/*"
    if referer:
        h["Referer"] = referer
    with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
        return r.read().decode(encoding, "ignore")


def load_manual():
    if os.path.exists(MANUAL_PATH):
        try:
            with open(MANUAL_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_manual(d):
    with open(MANUAL_PATH, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)


# ══ 行情
def fetch_quotes(codes):
    out = {}
    if not codes:
        return out
    txt = _get("https://qt.gtimg.cn/q=" + ",".join(codes), encoding="gbk")
    for line in txt.strip().split(";"):
        if "~" not in line:
            continue
        f = line.split("~")
        if len(f) < 52:
            continue
        try:
            price, prev = float(f[3]), float(f[4])
        except (ValueError, IndexError):
            continue
        nav = None
        try:
            v = float(f[51])
            if v > 0:
                nav = v
        except (ValueError, IndexError):
            pass
        out[f[2].strip()] = {"name": f[1].strip(), "price": price, "prev": prev,
                             "nav": nav, "chg": (price / prev - 1) * 100 if prev else None}
    return out


def _get_retry(url, referer=None, timeout=20, encoding="utf-8", tries=3):
    last = None
    for i in range(tries):
        try:
            return _get(url, referer=referer, timeout=timeout, encoding=encoding)
        except Exception as ex:
            last = ex
            time.sleep(1.2)
    raise last


def fetch_kline(code, n=300):
    url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,day,,,%d,qfq"
           % (code, n))
    d = json.loads(_get_retry(url))
    node = d.get("data", {}).get(code, {})
    rows = node.get("qfqday") or node.get("day") or []
    out = []
    for r in rows:
        try:
            out.append({"d": r[0], "c": float(r[2]), "h": float(r[3]), "l": float(r[4])})
        except (ValueError, IndexError):
            continue
    return out


# ══ 估值
def fetch_index_eva(code):
    d = json.loads(_get("https://danjuanfunds.com/djapi/index_eva/detail/%s" % code,
                        referer="https://danjuanfunds.com/"))
    data = d.get("data")
    if not data or not data.get("pe"):
        return None
    try:
        return {"pe": float(data["pe"]), "pb": float(data.get("pb") or 0),
                "pe_pct": float(data.get("pe_percentile") or 0) * 100,
                "pb_pct": float(data.get("pb_percentile") or 0) * 100,
                "yield": float(data.get("yeild") or 0) * 100,
                "name": data.get("name", ""), "date": data.get("date", "")}
    except (TypeError, ValueError):
        return None


def fetch_bond_10y():
    if C.BOND_YIELD_OVERRIDE:
        return {"value": float(C.BOND_YIELD_OVERRIDE), "date": "手动"}
    url = ("https://datacenter-web.eastmoney.com/api/data/v1/get?"
           "reportName=RPTA_WEB_TREASURYYIELD&columns=ALL&pageSize=1"
           "&sortColumns=SOLAR_DATE&sortTypes=-1")
    d = json.loads(_get(url, referer="https://data.eastmoney.com/"))
    row = d["result"]["data"][0]
    v = row.get(C.BOND_YIELD_FIELD)
    if v is None:
        raise ValueError("字段 %s 无数据" % C.BOND_YIELD_FIELD)
    v = float(v)
    if not (0.3 < v < 8.0):
        raise ValueError("收益率 %.3f 异常，请检查字段" % v)
    return {"value": v, "date": str(row.get("SOLAR_DATE", ""))[:10]}


def fred_last(sid, timeout=30, tries=2):
    txt = _get_retry("https://fred.stlouisfed.org/graph/fredgraph.csv?id=%s" % sid,
                     timeout=timeout, tries=tries)
    rows = []
    for r in csv.reader(io.StringIO(txt)):
        if len(r) < 2 or r[0] == "observation_date" or not r[1].strip():
            continue
        try:
            rows.append((r[0], float(r[1])))
        except ValueError:
            continue
    if not rows:
        raise ValueError("%s 无数据" % sid)
    return rows


def fetch_us10y():
    """美元10年期名义利率 —— 港股通红利的无风险基准（港币挂美元）"""
    if C.USD_10Y_OVERRIDE:
        return {"value": float(C.USD_10Y_OVERRIDE), "src": "手动", "date": ""}
    try:
        r = fred_last("DGS10")
        return {"value": r[-1][1], "src": "FRED DGS10", "date": r[-1][0]}
    except Exception:
        pass
    try:
        r = fred_last("DFII10")
        return {"value": r[-1][1] + C.USD_BREAKEVEN_ASSUMED,
                "src": "TIPS+%.1fpp估算" % C.USD_BREAKEVEN_ASSUMED, "date": r[-1][0]}
    except Exception:
        pass
    return {"value": C.USD_10Y_FALLBACK, "src": "兜底常量", "date": ""}


def fetch_tips10():
    rows = fred_last("DFII10")
    back = rows[-21] if len(rows) >= 21 else rows[0]
    return {"value": rows[-1][1], "date": rows[-1][0],
            "chg20": rows[-1][1] - back[1]}


def fetch_shiller_pe():
    """GuruFocus 席勒PE（页面 meta 描述里带当前值）"""
    import re, time
    t = ""
    for _ in range(3):
        try:
            t = _get(C.SHILLER_URL)
            if "席勒市盈率当前值为" in t:
                break
        except Exception:
            pass
        time.sleep(1.5)
    m = re.search(r"席勒市盈率当前值为\s*([\d.]+)", t)
    if not m:
        raise ValueError("未匹配到席勒PE")
    v = float(m.group(1))
    if not (5 < v < 100):
        raise ValueError("席勒PE %.1f 异常" % v)
    exp = re.search(r"未来8年年化回报率预期为\s*(-?[\d.]+)%", t)
    return {"value": v, "exp8": float(exp.group(1)) if exp else None}


def fetch_fx(code):
    t = _get("https://hq.sinajs.cn/list=%s" % code,
             referer="https://finance.sina.com.cn/", encoding="gbk")
    parts = t.split('"')[1].split(",")
    return float(parts[1])


def calc_dxy():
    """美元指数 = ICE 官方公式，由 6 个币对合成（新浪外汇实时）"""
    eur = fetch_fx("fx_seurusd")
    jpy = fetch_fx("fx_susdjpy")
    gbp = fetch_fx("fx_sgbpusd")
    cad = fetch_fx("fx_susdcad")
    sek = fetch_fx("fx_susdsek")
    chf = fetch_fx("fx_susdchf")
    v = (50.14348112 * eur ** -0.576 * jpy ** 0.136 * gbp ** -0.119
         * cad ** 0.091 * sek ** 0.042 * chf ** 0.036)
    if not (60 < v < 160):
        raise ValueError("美元指数 %.1f 异常" % v)
    if v >= 102:
        zone = "强势·压制金价"
    elif v >= 98:
        zone = "中性偏强"
    elif v >= 95:
        zone = "中性偏弱"
    else:
        zone = "弱势·利好金价"
    return {"value": v, "zone": zone}


def calc_southbound(days=5):
    """南向资金：东财沪深港通历史，MUTUAL_TYPE=006 为南向合计，单位百万元"""
    url = ("https://datacenter-web.eastmoney.com/api/data/v1/get?"
           "reportName=RPT_MUTUAL_DEAL_HISTORY&columns=ALL&pageSize=200"
           "&sortColumns=TRADE_DATE&sortTypes=-1")
    d = json.loads(_get(url, referer="https://data.eastmoney.com/"))
    rows = [r for r in d["result"]["data"]
            if r.get("MUTUAL_TYPE") == "006" and r.get("NET_DEAL_AMT") is not None]
    if not rows:
        raise ValueError("南向资金无数据")
    vals = [float(r["NET_DEAL_AMT"]) / 100.0 for r in rows[:days]]  # 百万→亿
    streak = 0
    for v in vals:
        if v > 0:
            streak += 1
        else:
            break
    return {"sum": sum(vals), "last": vals[0], "days": len(vals),
            "streak_buy": streak, "date": rows[0]["TRADE_DATE"][:10]}


def fetch_fund_navs(fund_code, n=15):
    url = ("https://api.fund.eastmoney.com/f10/lsjz?fundCode=%s&pageIndex=1&pageSize=%d"
           % (fund_code, n))
    d = json.loads(_get(url, referer="https://fundf10.eastmoney.com/"))
    out = []
    for it in d["Data"]["LSJZList"]:
        try:
            out.append({"d": it["FSRQ"], "nav": float(it["DWJZ"])})
        except (KeyError, ValueError):
            continue
    return out


# ══ 分档工具
def band_lower(v, bands, idx=1):
    """取「阈值 <= 值」中最大的一档（不要求 bands 已排序）"""
    for row in sorted(bands, key=lambda r: r[0], reverse=True):
        if v >= row[0]:
            return row[idx]
    return sorted(bands, key=lambda r: r[0])[0][idx]


def band_upper(v, bands, idx=1):
    """取「阈值 > 值」中最小的一档"""
    for row in sorted(bands, key=lambda r: r[0]):
        if v < row[0]:
            return row[idx]
    return sorted(bands, key=lambda r: r[0])[-1][idx]


def fetch_kline_sina(symbol, n=2000):
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           "CN_MarketData.getKLineData?symbol=%s&scale=240&ma=no&datalen=%d" % (symbol, n))
    t = _get_retry(url, referer="https://finance.sina.com.cn/")
    d = json.loads(t)
    if not isinstance(d, list) or len(d) < 60:
        raise ValueError("新浪日线不足")
    out = sorted([{"d": x["day"], "c": float(x["close"]),
                   "h": float(x["high"]), "l": float(x["low"])} for x in d],
                 key=lambda r: r["d"])
    # 新鲜度校验：最后一条必须是近期的，否则该源返回的是最老的一段
    last = out[-1]["d"]
    today = now_bj().strftime("%Y-%m-%d")
    try:
        gap = (datetime.strptime(today, "%Y-%m-%d") - datetime.strptime(last, "%Y-%m-%d")).days
    except Exception:
        gap = 999
    if gap > 15:
        raise ValueError("新浪数据过旧(末条 %s)，改用其他源" % last)
    return out


def fetch_kline_any(symbols, n=1200):
    """新浪优先（历史更长），腾讯兜底"""
    for s in symbols:
        try:
            return fetch_kline_sina(s, n), "新浪"
        except Exception:
            pass
    for s in symbols:
        try:
            rows = fetch_kline(s, min(n, 800))
            if len(rows) >= 60:
                return rows, "腾讯"
        except Exception:
            pass
    raise ValueError("日线取不到: %s" % ",".join(symbols))


def calc_div_index(cfg, bond_cn=None):
    """红利指数：位置（均线偏离，沿用 ma250_monitor）+ 估值（PE/PB分位）"""
    r = {"ok": False, "err": None}
    try:
        rows, src = fetch_kline_any(cfg["px"])
        closes = [x["c"] for x in rows]
        dates = [x["d"] for x in rows]
        from ma250_monitor import signal_for
        sig = signal_for(closes, dates, label=cfg["code"])
        eva = fetch_index_eva(cfg["eva"])
        if not eva:
            raise ValueError("估值取不到")
        pe_pct = eva["pe_pct"]
        val = band_upper(pe_pct, C.DIV_VAL_BANDS, 1)
        peak = max(closes[-250:])
        r.update(ok=True, close=closes[-1], ma=sig["ma"], pct=sig["pct"],
                 zone=sig["zone"], action=sig["action"], level=sig["level"],
                 dd=(closes[-1] / peak - 1) * 100, src=src, n=len(rows),
                 start=dates[0],
                 yld=eva["yield"], pe=eva["pe"], pb=eva["pb"],
                 pe_pct=pe_pct, pb_pct=eva["pb_pct"], val=val,
                 spread_cn=(eva["yield"] - bond_cn) if bond_cn else None)
    except Exception as e:
        r["err"] = "%s: %s" % (type(e).__name__, e)
    return r


# ══ 指标
def calc_erp():
    r = {"ok": False, "err": None}
    try:
        eva, src, proxy = None, "中证A500", False
        for code in (C.ERP_PE_INDEX, C.ERP_PE_PROXY):
            eva = fetch_index_eva(code)
            if eva:
                src = "中证A500" if code == C.ERP_PE_INDEX else "沪深300"
                proxy = code != C.ERP_PE_INDEX
                break
        if not eva:
            raise ValueError("宽基PE取不到")
        bond = fetch_bond_10y()
        erp = 100.0 / eva["pe"] - bond["value"]
        r.update(ok=True, erp=erp, pe=eva["pe"], pe_pct=eva["pe_pct"],
                 pb_pct=eva["pb_pct"], pe_src=src, proxy=proxy,
                 bond=bond["value"], bond_date=bond["date"],
                 target=band_lower(erp, C.ERP_BANDS, 1),
                 label=band_lower(erp, C.ERP_BANDS, 2))
    except Exception as e:
        r["err"] = "%s: %s" % (type(e).__name__, e)
    return r


def calc_a500():
    r = {"ok": False, "err": None}
    try:
        eva, src, proxy = None, "中证A500", False
        for code in C.A500["indices"]:
            eva = fetch_index_eva(code)
            if eva:
                src = "中证A500" if code == C.A500["indices"][0] else "沪深300"
                proxy = code != C.A500["indices"][0]
                break
        if not eva:
            raise ValueError("A500估值取不到")
        pct_pe, pct_pb = eva["pe_pct"], eva["pb_pct"]
        div = abs(pct_pe - pct_pb)
        judge = C.A500.get("judge", "auto")
        low_src = "PE" if pct_pe <= pct_pb else "PB"
        high_src = "PB" if low_src == "PE" else "PE"
        warn = None
        if judge == "pe":
            pct, basis = pct_pe, "PE"
        elif judge == "min":
            pct, basis = min(pct_pe, pct_pb), low_src
        else:  # auto
            if div >= C.A500["div_warn"]:
                pct, basis = max(pct_pe, pct_pb), high_src + "取高"
                warn = "PE/PB背离 %.0fpp，盈利质量预警，已改取高" % div
            elif div >= C.A500["div_soft"]:
                pct = min(100.0, min(pct_pe, pct_pb) + C.A500["div_penalty"])
                basis = low_src + "降一档"
                warn = "PE/PB背离 %.0fpp，已降一档" % div
            else:
                pct, basis = min(pct_pe, pct_pb), low_src
        mult, act = band_upper(pct, C.A500["pe_bands"], 1), band_upper(pct, C.A500["pe_bands"], 2)
        r.update(ok=True, pct=pct, pct_pe=pct_pe, pe=eva["pe"], pb=eva["pb"],
                 pb_pct=pct_pb, src=src, proxy=proxy, basis=basis,
                 div=div, warn=warn,
                 mult=mult, action=act,
                 amount=(C.DCA_BASE_AMOUNT * mult) if C.DCA_BASE_AMOUNT else None,
                 date=eva.get("date"))
    except Exception as e:
        r["err"] = "%s: %s" % (type(e).__name__, e)
    return r


def calc_hk_div(bond_cn=None, bond_us=None):
    r = {"ok": False, "err": None}
    m = load_manual().get("hk_yield", {})
    v = m.get("value") or C.HK_YIELD_MANUAL.get("value")
    asof = m.get("asof") or C.HK_YIELD_MANUAL.get("asof")
    if not v:
        r["err"] = "未录入"
        return r
    age = None
    if asof:
        try:
            age = (now_bj().date() - datetime.strptime(asof, "%Y-%m-%d").date()).days
        except ValueError:
            pass
    v = float(v)
    frozen = (age is not None and age > C.HK_YIELD_FREEZE_DAYS)
    zone = "数据过期，冻结判定" if frozen else band_lower(v, C.HK_DIV["yield_bands"], 1)
    r.update(ok=True, value=v, asof=asof, age=age, zone=zone,
             spread_us=(v - bond_us) if bond_us else None,
             spread_cn=(v - bond_cn) if bond_cn else None,
             frozen=frozen, net=v * 0.8,
             stale=(age is not None and age > C.HK_YIELD_STALE_DAYS))
    return r


def calc_sp500(quote):
    r = {"ok": False, "err": None}
    try:
        price = quote["price"]
        nav = quote.get("nav")
        if not nav:
            raise ValueError("净值取不到")
        prem = (price / nav - 1) * 100
        act = band_lower(prem, C.SP500["prem_bands"], 1)
        eva = None
        try:
            eva = fetch_index_eva("SP500")
        except Exception:
            pass
        r.update(ok=True, price=price, nav=nav, prem=prem, action=act,
                 pe=eva["pe"] if eva else None,
                 pe_pct=eva["pe_pct"] if eva else None,
                 pb_pct=eva["pb_pct"] if eva else None)
        sh = load_manual().get("shiller_pe", {})
        r["shiller"], r["shiller_src"], r["exp8"] = sh.get("value"), "手动", None
        try:
            sp = fetch_shiller_pe()
            r["shiller"], r["shiller_src"], r["exp8"] = sp["value"], "GuruFocus", sp.get("exp8")
        except Exception:
            pass
    except Exception as e:
        r["err"] = "%s: %s" % (type(e).__name__, e)
    return r


def calc_short_bond():
    r = {"ok": False, "err": None}
    try:
        navs = fetch_fund_navs(C.SHORT_BOND["fund_code"], 12)
        if len(navs) < 8:
            raise ValueError("净值记录不足")
        ann = ((navs[0]["nav"] / navs[7]["nav"]) ** (244.0 / 7) - 1) * 100
        d = now_bj().date()
        month_end = (d + timedelta(days=7)).month != d.month
        window = ("年末" if d.month == 12 else "季末" if d.month in (3, 6, 9) else "月末") \
            if month_end else None
        gc007 = None
        try:
            q = fetch_quotes(["sh204007"])
            gc007 = q.get("204007", {}).get("price")
        except Exception:
            pass
        shibor = None
        try:
            u = ("https://datacenter-web.eastmoney.com/api/data/v1/get?"
                 "reportName=RPT_IMP_INTRESTRATEN&columns=ALL&pageSize=60"
                 "&sortColumns=REPORT_DATE&sortTypes=-1")
            j = json.loads(_get(u, referer="https://data.eastmoney.com/"))
            for r in j["result"]["data"]:
                if (r.get("MARKET_CODE") == "001" and r.get("LATEST_RECORD") == 1
                        and "1周" in str(r.get("REPORT_PERIOD")) and r.get("IR_RATE")):
                    shibor = float(r["IR_RATE"])
                    break
        except Exception:
            pass
        r.update(ok=True, ann=ann, nav=navs[0]["nav"], date=navs[0]["d"],
                 window=window, gc007=gc007, shibor=shibor,
                 lock=ann >= C.SHORT_BOND["lock_yield"])
    except Exception as e:
        r["err"] = "%s: %s" % (type(e).__name__, e)
    return r


def calc_gold():
    r = {"ok": False, "err": None}
    try:
        tips = fetch_tips10()
        rows = fetch_kline(C.GOLD["etf"], 300)
        if len(rows) < 70:
            raise ValueError("金价日线不足")
        price = rows[-1]["c"]
        peak = max(x["h"] for x in rows[-250:])
        dd = (price / peak - 1) * 100
        chg60 = (price / rows[-61]["c"] - 1) * 100
        label = band_lower(tips["value"], C.GOLD["tips_bands"], 1)
        conflict = (tips["value"] >= 2.0 and dd <= -25.0)
        if conflict:
            label = "信号冲突：利率压制 vs 深度回撤（需人工判断）"
        trigger = (tips["chg20"] <= -0.20) and (dd <= -15.0)
        r.update(ok=True, tips=tips["value"], tips_date=tips["date"],
                 tips_chg20=tips["chg20"], label=label, price=price,
                 dd=dd, chg60=chg60, trigger=trigger, conflict=conflict,
                 spike=(chg60 >= C.GOLD["spike_cut"]))
    except Exception as e:
        r["err"] = "%s: %s" % (type(e).__name__, e)
    return r


def calc_rebalance():
    r = {"ok": False, "err": None, "in_window": False}
    d = now_bj().date()
    r["in_window"] = (d.month == C.REBALANCE_MONTH) and (
        not C.REBALANCE_LAST_WEEK or (d + timedelta(days=7)).month != d.month)
    holds = {k: v for k, v in C.HOLDINGS.items() if v}
    if len(holds) < len(C.ASSETS):
        r["err"] = "HOLDINGS 未填全"
        return r
    total = sum(holds.values())
    rows = []
    for a in C.ASSETS:
        cur = holds.get(a["key"], 0) / total
        dev = cur - a["weight"]
        rows.append({"name": a["name"], "cur": cur, "tgt": a["weight"],
                     "dev": dev, "alert": abs(dev) >= C.REBALANCE_BAND})
    r.update(ok=True, rows=rows, total=total, ready=any(x["alert"] for x in rows))
    return r
