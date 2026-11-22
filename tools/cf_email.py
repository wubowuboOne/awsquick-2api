"""Cloudflare Email Routing 配置：把你的域名的收件全部转发到一个可读邮箱。

用法：
    python3 tools/cf_email.py status
    python3 tools/cf_email.py setup --dest you@example.com
    python3 tools/cf_email.py rules

令牌从环境变量 CF_TOKEN 读，或从 --token-file 指定的文件读。
需要的权限：Zone:Read + Email Routing:Edit，Zone Resources 覆盖你的域名。
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.cloudflare.com/client/v4"
def _zone_default():
    try:
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
        import config
        return config.CFG["mail"].get("domain") or "your-domain.com"
    except Exception:
        return "your-domain.com"


ZONE_NAME = _zone_default()


def call(token, path, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(API + path, data=data, method=method,
                               headers={"Authorization": "Bearer " + token,
                                        "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=30) as f:
            return json.load(f)
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {"success": False, "errors": [{"message": f"HTTP {e.code}"}]}


def fail(msg, payload=None):
    print("✗", msg)
    if payload:
        print(json.dumps(payload, ensure_ascii=False)[:800])
    sys.exit(1)


def get_zone(token):
    r = call(token, f"/zones?name={ZONE_NAME}")
    if not r.get("success"):
        fail("读取 zone 失败（令牌缺 Zone:Read 或区域不在资源范围内）", r.get("errors"))
    res = r.get("result") or []
    if not res:
        fail("令牌可见的 zone 为空：请把令牌的 Zone Resources 设为 Include → " + ZONE_NAME)
    z = res[0]
    return z["id"], z["account"]["id"], z


def status(token):
    zid, aid, z = get_zone(token)
    print(f"zone      {z['name']}  ({zid})  status={z['status']}")
    print(f"account   {aid}")

    r = call(token, f"/accounts/{aid}/email/routing/addresses")
    dests = r.get("result") or []
    print(f"\n目标地址 {len(dests)} 个")
    for d in dests:
        print(f"  - {d.get('email')}  verified={d.get('verified')}  id={d.get('tag') or d.get('id')}")

    r = call(token, f"/zones/{zid}/email/routing/rules")
    rules = r.get("result") or []
    print(f"\n路由规则 {len(rules)} 条")
    for x in rules:
        print(f"  - [{x.get('enabled')}] {x.get('name')}  {x.get('matchers')} -> {x.get('actions')}")

    r = call(token, f"/zones/{zid}/email/routing")
    print("\nrouting 设置:", json.dumps(r.get("result"), ensure_ascii=False)[:300])


def ensure_destination(token, aid, dest):
    r = call(token, f"/accounts/{aid}/email/routing/addresses")
    for d in r.get("result") or []:
        if d.get("email", "").lower() == dest.lower():
            print(f"目标地址已存在 verified={d.get('verified')}")
            return d
    r = call(token, f"/accounts/{aid}/email/routing/addresses", "POST", {"email": dest})
    if not r.get("success"):
        fail("创建目标地址失败", r.get("errors"))
    print("目标地址已创建，Cloudflare 会向它发验证信")
    return r["result"]


def ensure_catchall(token, zid, dest):
    body = {
        "name": "catch-all -> " + dest,
        "enabled": True,
        "matchers": [{"type": "all"}],
        "actions": [{"type": "forward", "value": [dest]}],
    }
    r = call(token, f"/zones/{zid}/email/routing/rules/catch_all", "PUT", body)
    if not r.get("success"):
        fail("写入 catch-all 失败", r.get("errors"))
    print("catch-all 已写入:", json.dumps(r.get("result"), ensure_ascii=False)[:300])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["status", "setup", "rules"])
    ap.add_argument("--dest")
    ap.add_argument("--token-file", default=os.environ.get("CF_TOKEN_FILE"))
    a = ap.parse_args()
    token = os.environ.get("CF_TOKEN")
    if not token and a.token_file:
        token = open(a.token_file).read().strip()
    if not token:
        sys.exit("缺少令牌：设置 CF_TOKEN 或 --token-file")

    if a.cmd == "status":
        status(token)
    elif a.cmd == "rules":
        zid, aid, _ = get_zone(token)
        print(json.dumps(call(token, f"/zones/{zid}/email/routing/rules"), ensure_ascii=False, indent=1)[:2000])
    else:
        if not a.dest:
            sys.exit("setup 需要 --dest")
        zid, aid, _ = get_zone(token)
        ensure_destination(token, aid, a.dest)
        ensure_catchall(token, zid, a.dest)


if __name__ == "__main__":
    main()
