"""部署邮件桥 Worker 到 Cloudflare，并把你的域名的 catch-all 指过去。

用法：
    python3 tools/deploy_mail_worker.py --token-file /path/to/cf_token \
        --subdomain quickmail-xxxx --api-token <取信口令>

需要的令牌权限（账户级 + 区域级）：
    Account → Workers Scripts → Edit
    Account → Workers KV Storage → Edit
    Zone    → Email Routing → Edit
    Zone    → Zone → Read
"""
import argparse
import json
import os
import secrets
import sys
import urllib.error
import urllib.request
import uuid

API = "https://api.cloudflare.com/client/v4"
def _zone_default():
    try:
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
        import config
        return config.CFG["mail"].get("domain") or "your-domain.com"
    except Exception:
        return "your-domain.com"


ZONE = _zone_default()
SCRIPT = "quick-mail-bridge"
HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "worker", "mail_bridge.js")


def auth_headers(token):
    """支持 Bearer 令牌，也支持 Global Key（email|key）。"""
    if "|" in token:
        em, key = token.split("|", 1)
        return {"X-Auth-Email": em.strip(), "X-Auth-Key": key.strip()}
    return {"Authorization": "Bearer " + token}


def call(token, path, method="GET", body=None, raw=None, ctype=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    h = auth_headers(token)
    if ctype:
        h["Content-Type"] = ctype
    elif data is not None:
        h["Content-Type"] = "application/json"
    r = urllib.request.Request(API + path, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(r, timeout=60) as f:
            return json.load(f)
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {"success": False, "errors": [{"message": f"HTTP {e.code}"}]}


def die(msg, r=None):
    print("✗", msg)
    if r:
        print(json.dumps(r, ensure_ascii=False)[:600])
    sys.exit(1)


def pick(opts, name):  # 兼容不同 API 返回字段名
    for k in opts:
        if k in name:
            return name[k]
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--token-file", required=True)
    ap.add_argument("--subdomain", default="quickmail-" + uuid.uuid4().hex[:8])
    ap.add_argument("--api-token", default=secrets.token_urlsafe(18))
    ap.add_argument("--zone-only", action="store_true", help="只把 catch-all 指向已存在的脚本")
    a = ap.parse_args()
    token = open(a.token_file).read().strip()

    z = call(token, f"/zones?name={ZONE}")
    if not z.get("success") or not z.get("result"):
        die("读不到 zone（令牌缺 Zone:Read）", z.get("errors"))
    zid = z["result"][0]["id"]
    aid = z["result"][0]["account"]["id"]
    print(f"zone {zid}  account {aid}")

    if a.zone_only:
        body = {"enabled": True, "name": f"catch-all -> {SCRIPT}",
                "matchers": [{"type": "all"}],
                "actions": [{"type": "worker", "value": [SCRIPT]}]}
        r = call(token, f"/zones/{zid}/email/routing/rules/catch_all", "PUT", body)
        print("catch-all:", "ok" if r.get("success") else json.dumps(r.get("errors"))[:300])
        return

    # 1. KV
    kv = call(token, f"/accounts/{aid}/storage/kv/namespaces", "POST", {"title": SCRIPT})
    if kv.get("success"):
        ns = kv["result"]["id"]
        print("KV 已建:", ns)
    else:
        lst = call(token, f"/accounts/{aid}/storage/kv/namespaces?per_page=100")
        found = [n for n in (lst.get("result") or []) if n.get("title") == SCRIPT]
        if not found:
            die("建 KV 失败（令牌缺 Workers KV Storage:Edit）", kv.get("errors"))
        ns = found[0]["id"]
        print("KV 复用:", ns)

    # 2. 上传脚本（multipart: metadata + module）
    src = open(SRC, "rb").read()
    boundary = "----qmb" + uuid.uuid4().hex
    meta = {"main_module": "mail_bridge.js",
            "bindings": [{"type": "kv_namespace", "name": "MAILBOX", "namespace_id": ns},
                         {"type": "plain_text", "name": "KEEP", "text": "200"}]}
    parts = []
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"metadata\"\r\n"
                 f"Content-Type: application/json\r\n\r\n{json.dumps(meta)}\r\n".encode())
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"mail_bridge.js\"; "
                 f"filename=\"mail_bridge.js\"\r\nContent-Type: application/javascript+module\r\n\r\n".encode()
                 + src + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    up = call(token, f"/accounts/{aid}/workers/scripts/{SCRIPT}", "PUT",
              raw=b"".join(parts), ctype=f"multipart/form-data; boundary={boundary}")
    if not up.get("success"):
        die("上传 Worker 失败（令牌缺 Workers Scripts:Edit）", up.get("errors"))
    print("Worker 已上传")

    # 3. 取信口令
    s = call(token, f"/accounts/{aid}/workers/scripts/{SCRIPT}/secrets", "PUT",
             {"name": "API_TOKEN", "text": a.api_token, "type": "secret_text"})
    print("密钥:", "ok" if s.get("success") else json.dumps(s.get("errors"))[:200])

    # 4. workers.dev 子域
    sd = call(token, f"/accounts/{aid}/workers/subdomain", "PUT", {"subdomain": a.subdomain})
    if not sd.get("success"):
        print("⚠ 设置 workers.dev 子域失败（可能已被占用）:", json.dumps(sd.get("errors"))[:200])
    en = call(token, f"/accounts/{aid}/workers/scripts/{SCRIPT}/subdomain", "POST", {"enabled": True})
    print("开启 workers.dev:", "ok" if en.get("success") else json.dumps(en.get("errors"))[:200])

    # 5. catch-all → Worker
    body = {"enabled": True, "name": f"catch-all -> {SCRIPT}",
            "matchers": [{"type": "all"}],
            "actions": [{"type": "worker", "value": [SCRIPT]}]}
    r = call(token, f"/zones/{zid}/email/routing/rules/catch_all", "PUT", body)
    print("catch-all:", "ok" if r.get("success") else json.dumps(r.get("errors"))[:300])

    os.makedirs("data", exist_ok=True)
    with open("data/mail_bridge.json", "w") as f:
        json.dump({"subdomain": a.subdomain,
                   "url": f"https://{SCRIPT}.{a.subdomain}.workers.dev",
                   "api_token": a.api_token}, f)
    print("\n完成：")
    print("  Worker  https://%s.%s.workers.dev" % (SCRIPT, a.subdomain))
    print("  口令已写入 data/mail_bridge.json（不回显）")


if __name__ == "__main__":
    main()
