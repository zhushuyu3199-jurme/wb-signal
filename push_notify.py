#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
微信推送脚本（程序 -> 用户微信）
支持两个通道（任选其一，推荐 pushplus：微信扫码注册，免费额度足够个人使用）：
  - pushplus  : POST https://www.pushplus.plus/send        （微信扫码登录即得 token）
  - serverchan: POST https://sctapi.ftqq.com/<key>.send    （sct.ftqq.com 微信登录得 SendKey）

用法：
  python push_notify.py --title "标题" --content "正文"
  python push_notify.py --title "标题" --content "正文" --channel serverchan --token SCTxxxx
  python push_notify.py --self-test        # 自检：不发消息，仅检查配置与接口可达性

配置：同目录 push_config.json（token 到手后填写，脚本和自动化都从这里读）
  {
    "channel": "pushplus",
    "pushplus_token": "你的token",
    "serverchan_key": ""
  }
"""
import argparse
import json
import os
import socket
import sys
import time
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "push_config.json")

# PushPlus 更名后仅保留新域名；旧域名 push.plus 已下线（SSL 直接断连）
PUSHPLUS_HOST = "www.pushplus.plus"
PUSHPLUS_SEND = "https://%s/send" % PUSHPLUS_HOST

CODE_CN = {
    200: "受理成功",
    903: "token 无效（过期/填错/换了账号）",
    999: "token 为空",
    900: "请求过于频繁",
    901: "参数错误",
    902: "配额已用尽",
}



def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(json.dumps({"ok": False, "error": "push_config.json 解析失败: %s" % e}, ensure_ascii=False))
            sys.exit(2)
    return {}


def post_json(url, payload, timeout=15):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def post_form(url, payload, timeout=15):
    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def send_pushplus(token, title, content, channel=None):
    payload = {"token": token, "title": title, "content": content, "template": "markdown"}
    if channel:
        payload["channel"] = channel
    last = None
    for i in range(3):
        try:
            return post_json(PUSHPLUS_SEND, payload, timeout=20)
        except Exception as e:
            last = e
            time.sleep(1.5)
    raise last


def send_serverchan(key, title, content):
    return post_form("https://sctapi.ftqq.com/%s.send" % key,
                     {"title": title, "desp": content})


def self_test():
    """不发消息，仅验证：配置文件状态 + 两个接口连通性。"""
    cfg = load_config()
    result = {"config_exists": os.path.exists(CONFIG_PATH), "config": cfg}
    for name, url in (("pushplus", PUSHPLUS_SEND),
                      ("serverchan", "https://sctapi.ftqq.com")):
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=12) as resp:
                result[name] = "HTTP %s" % resp.status
        except Exception as e:
            result[name] = "ERR: %s" % e
    print(json.dumps(result, ensure_ascii=False, indent=2))


def diagnose():
    """真发一条测试消息，并把返回码翻译成人话。定位「工作流绿了但没收到」。"""
    cfg = load_config()
    token = os.environ.get("PUSHPLUS_TOKEN") or cfg.get("pushplus_token", "")
    out = {"host": PUSHPLUS_HOST}
    try:
        out["dns"] = socket.gethostbyname(PUSHPLUS_HOST)
    except Exception as e:
        out["dns"] = "解析失败: %s" % e
    out["token_source"] = "环境变量 PUSHPLUS_TOKEN" if os.environ.get("PUSHPLUS_TOKEN") else "push_config.json"
    out["token_tail"] = ("..." + token[-4:]) if token else None
    out["token_len"] = len(token)
    if not token:
        out["result"] = "token 为空，无法发送"
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return
    try:
        resp = send_pushplus(token, "定盘 · 通道诊断",
                             "如果你在微信看到这条，说明 token 与接口都正常，问题在别处。",
                             channel=cfg.get("pushplus_channel"))
        out["raw"] = resp
        try:
            j = json.loads(resp)
            out["code"] = j.get("code")
            out["meaning"] = CODE_CN.get(j.get("code"), j.get("msg"))
        except Exception:
            pass
    except Exception as e:
        out["result"] = "请求失败: %s: %s" % (type(e).__name__, e)
    print(json.dumps(out, ensure_ascii=False, indent=2))



def main():
    ap = argparse.ArgumentParser(description="微信推送（PushPlus / ServerChan）")
    ap.add_argument("--title", help="消息标题")
    ap.add_argument("--content", help="消息正文（支持 markdown）")
    ap.add_argument("--channel", choices=["pushplus", "serverchan"], default=None)
    ap.add_argument("--token", default=None, help="临时指定 token/SendKey，优先于配置文件")
    ap.add_argument("--self-test", action="store_true", help="自检：不发送，仅检查配置与接口")
    ap.add_argument("--diagnose", action="store_true", help="诊断：真发一条测试消息并翻译返回码")
    args = ap.parse_args()

    if args.self_test:
        self_test()
        return

    if args.diagnose:
        diagnose()
        return

    if not args.title or not args.content:
        print(json.dumps({"ok": False, "error": "缺少 --title 或 --content"}, ensure_ascii=False))
        sys.exit(2)

    cfg = load_config()
    channel = args.channel or cfg.get("channel") or "pushplus"

    try:
        if channel == "pushplus":
            token = args.token or cfg.get("pushplus_token", "")
            if not token:
                print(json.dumps({"ok": False, "error": "缺少 pushplus_token（在 push_config.json 填写，或 --token 传入）"}, ensure_ascii=False))
                sys.exit(2)
            resp = send_pushplus(token, args.title, args.content,
                                 channel=cfg.get("pushplus_channel"))
        else:
            token = args.token or cfg.get("serverchan_key", "")
            if not token:
                print(json.dumps({"ok": False, "error": "缺少 serverchan_key（在 push_config.json 填写，或 --token 传入）"}, ensure_ascii=False))
                sys.exit(2)
            resp = send_serverchan(token, args.title, args.content)
    except Exception as e:
        print(json.dumps({"ok": False, "channel": channel, "error": str(e)}, ensure_ascii=False))
        sys.exit(1)

    print(json.dumps({"ok": True, "channel": channel, "resp": resp}, ensure_ascii=False))


if __name__ == "__main__":
    main()
