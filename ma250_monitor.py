#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
红利低波100 · 250日均线「罕见机会」信号引擎
============================================
输入：一段日线行情（收盘价序列）
输出：当前相对250日均线的偏离度 + 按操作手册给出的买卖信号

档位（偏离度 = 相对250日均线）:
  DCA  : ≤ -5%   定投参考(可考虑开始定投)
  BUY1 : ≤ -10%  分批加仓
  BUY2 : ≤ -15%  大笔打满
  TRIM : ≥ +20%  偏热(慎用)
  SELL : ≥ +30%  大笔减仓
  HOLD : 其余     常态持有

用法:
  python ma250_monitor.py <数据文件> [代码标签]
  数据文件可以是:
    - 上一轮报告 HTML (内含 var DATA=[...])
    - westock-mcp data_kline 返回的 JSON (obj['data']['nodes'][{'date','last'}])
    - 自定义 JSON: {"dates":[...],"closes":[...]}

  # 批量(监控一篮子): 传入 manifest JSON: {"代码": "kline_代码.json", ...}
  python ma250_monitor.py manifest.json --batch
"""
import json, re, sys

# ===== 操作手册阈值(可改) =====
TH = dict(
    dca=-5.0,     # 定投档(低于均线): 可考虑开始定投
    buy1=-10.0,   # 开始分批加仓线(低于均线)
    buy2=-15.0,   # 大笔打满线(低于均线)
    trim=20.0,    # 偏热,可小幅止盈(高于均线)
    sell=30.0,    # 极端泡沫,才考虑大笔减仓(高于均线)
    N=250,
)

def load_series(path):
    txt = open(path, encoding="utf-8").read()
    # 1) 报告 HTML
    m = re.search(r"var DATA = (\[.*?\]);", txt, re.S)
    if m:
        DATA = json.loads(m.group(1))
        DATA.sort(key=lambda r: r[0])
        return [r[0] for r in DATA], [float(r[1]) for r in DATA]
    # 2) westock kline JSON
    try:
        obj = json.loads(txt)
        if isinstance(obj, dict) and "data" in obj and "nodes" in obj["data"]:
            nodes = obj["data"]["nodes"]
            recs = sorted(((n["date"], float(n["last"])) for n in nodes), key=lambda x: x[0])
            return [d for d, _ in recs], [c for _, c in recs]
    except Exception:
        pass
    # 3) 自定义 {dates,closes}
    obj = json.loads(txt)
    if isinstance(obj, dict) and "dates" in obj:
        return obj["dates"], [float(x) for x in obj["closes"]]
    raise ValueError("无法识别的数据格式: " + path)

def ma250(closes, N=TH["N"]):
    n = len(closes); ma = [None]*n
    for i in range(n):
        if i >= N-1:
            ma[i] = sum(closes[i-N+1:i+1]) / N
    return ma

def signal_for(closes, dates, th=TH, label=""):
    n = len(closes); ma = ma250(closes)
    i = n-1
    pct = (closes[i]-ma[i])/ma[i]*100.0 if ma[i] else None
    if pct is None or i < th["N"]-1:
        return dict(label=label, date=dates[-1], close=closes[-1], ma=None, pct=None,
                    zone="数据不足", action="需至少250个交易日")
    if pct <= th["buy2"]:
        zone, action, level = "深度破位(极值区)", "大笔重仓抄底", "BUY2"
    elif pct <= th["buy1"]:
        zone, action, level = "深度破位(加仓区)", "开始分批加仓", "BUY1"
    elif pct <= th["dca"]:
        zone, action, level = "定投区(-5%~-10%)", "可考虑开始定投(小额定期买入)", "DCA"
    elif pct >= th["sell"]:
        zone, action, level = "极端泡沫区", "考虑大笔减仓(留核心仓)", "SELL"
    elif pct >= th["trim"]:
        zone, action, level = "偏热区", "可小幅止盈(慎用)", "TRIM"
    else:
        zone, action, level = "常态持有区", "持有不动,吃股息", "HOLD"
    # 距各买入档位还差多少个百分点(还需下跌多少)
    dist = {}
    for k in ("dca", "buy1", "buy2"):
        if pct > th[k]:
            dist[k] = round(pct - th[k], 2)
    return dict(label=label, date=dates[-1], close=round(closes[-1],2),
                ma=round(ma[i],2), pct=round(pct,2), zone=zone, action=action, level=level,
                dist=dist)

def main():
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(1)
    path = sys.argv[1]
    if len(sys.argv) > 2 and sys.argv[2] == "--batch":
        manifest = json.load(open(path, encoding="utf-8"))
        results = []
        for code, f in manifest.items():
            d, c = load_series(f)
            results.append(signal_for(c, d, label=code))
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return
    label = sys.argv[2] if len(sys.argv) > 2 else ""
    dates, closes = load_series(path)
    sig = signal_for(closes, dates, label=label)
    print(json.dumps(sig, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
