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
import sys
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "push_config.json")


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


def send_pushplus(token, title, content):
    return post_json("https://www.pushplus.plus/send",
                     {"token": token, "title": title, "content": content, "template": "markdown"})


def send_serverchan(key, title, content):
    return post_form("https://sctapi.ftqq.com/%s.send" % key,
                     {"title": title, "desp": content})


def self_test():
    """不发消息，仅验证：配置文件状态 + 两个接口连通性。"""
    cfg = load_config()
    result = {"config_exists": os.path.exists(CONFIG_PATH), "config": cfg}
    for name, url in (("pushplus", "https://www.pushplus.plus/send"),
                      ("serverchan", "https://sctapi.ftqq.com")):
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=12) as resp:
                result[name] = "HTTP %s" % resp.status
        except Exception as e:
            result[name] = "ERR: %s" % e
    print(json.dumps(result, ensure_ascii=False, indent=2))


def main():
    ap = argparse.ArgumentParser(description="微信推送（PushPlus / ServerChan）")
    ap.add_argument("--title", help="消息标题")
    ap.add_argument("--content", help="消息正文（支持 markdown）")
    ap.add_argument("--channel", choices=["pushplus", "serverchan"], default=None)
    ap.add_argument("--token", default=None, help="临时指定 token/SendKey，优先于配置文件")
    ap.add_argument("--self-test", action="store_true", help="自检：不发送，仅检查配置与接口")
    args = ap.parse_args()

    if args.self_test:
        self_test()
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
            resp = send_pushplus(token, args.title, args.content)
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
