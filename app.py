"""注册机控制面板 — Flask 服务。

启动：python3 app.py  →  http://127.0.0.1:8787
"""
import csv
import io
import json
import os
import time

from flask import Flask, Response, jsonify, request, send_from_directory

import db
import engine
import urllib.request
import config

PROXY = config.gateway_base()
CFG = config.CFG

app = Flask(__name__, static_folder="static", static_url_path="")
db.init()
db.seed_pools()


@app.get("/")
def index():
    return send_from_directory("static", "index.html")


def proxy_get(path):
    try:
        with urllib.request.urlopen(PROXY + path, timeout=8) as f:
            return json.loads(f.read().decode())
    except Exception as e:
        return {"error": str(e)}



def proxy_call(path, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(PROXY + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as f:
            return json.loads(f.read().decode())
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/proxy/keys")
def api_keys():
    return jsonify(proxy_call("/keys"))


@app.post("/api/proxy/keys")
def api_keys_new():
    body = request.get_json(force=True, silent=True) or {}
    return jsonify(proxy_call("/keys", "POST", {"name": body.get("name", "default")}))


@app.delete("/api/proxy/keys/<kid>")
def api_keys_del(kid):
    return jsonify(proxy_call("/keys/" + kid, "DELETE"))


@app.get("/api/proxy/calls")
def api_calls():
    return jsonify(proxy_call("/calls?limit=" + request.args.get("limit", "100")))


@app.post("/api/proxy/login")
def api_proxy_login():
    return jsonify(proxy_call("/login"))


@app.post("/api/proxy/submit")
def api_proxy_submit():
    body = request.get_json(force=True, silent=True) or {}
    return jsonify(proxy_call("/login/submit", "POST", {"url": body.get("url", "")}))


@app.delete("/api/proxy/accounts/<tail>")
def api_proxy_del(tail):
    return jsonify(proxy_call("/accounts/" + tail, "DELETE"))


@app.post("/api/proxy/test")
def api_proxy_test():
    body = request.get_json(force=True, silent=True) or {}
    model = body.get("model", "fast")
    t0 = time.time()
    r = proxy_call("/v1/chat/completions", "POST",
                   {"model": model, "messages": [{"role": "user", "content": "say OK"}]})
    ms = int((time.time() - t0) * 1000)
    if "choices" in r:
        return jsonify({"ok": True, "ms": ms, "reply": r["choices"][0]["message"]["content"][:200],
                        "usage": r.get("usage")})
    return jsonify({"ok": False, "ms": ms, "error": r})



def _save_cfg():
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"), "w", encoding="utf-8") as f:
        json.dump(CFG, f, ensure_ascii=False, indent=2)


@app.post("/api/settings")
def api_settings_save():
    body = request.get_json(force=True, silent=True) or {}
    for section, keys in {
        "panel": ("host", "port"),
        "gateway": ("host", "port"),
        "mail": ("provider", "domain", "worker_url", "api_token", "custom_url",
                 "manual_mails", "imap"),
        "registration": ("concurrency", "target", "headless", "task_timeout_s", "retries_per_task"),
        "quota": ("stop_percent", "warn_percent"),
        "gateway_behavior": ("default_model", "max_tokens", "allow_stream", "cors"),
        "pool_import": ("headful",),
    }.items():
        src = body.get(section) or {}
        for k in keys:
            if k in src:
                CFG.setdefault(section, {})[k] = src[k]
    _save_cfg()
    return jsonify({"ok": True, "restart_required": any(
        k in (body.get("panel") or {}) or k in (body.get("gateway") or {}) for k in ("port", "host"))})


@app.get("/api/config")
def api_config():
    return jsonify({
        "gateway": CFG["gateway"], "panel": CFG["panel"],
        "mail": CFG["mail"], "quota": CFG["quota"],
        "proxy": CFG["proxy"], "registration": CFG["registration"],
        "gateway_behavior": CFG.get("gateway_behavior", {}),
        "pool_import": CFG.get("pool_import", {}),
    })


@app.post("/api/proxy/reload")
def api_proxy_reload():
    return jsonify(proxy_call("/reload", "POST"))


@app.get("/api/proxies")
def api_proxies():
    scope = request.args.get("scope", "registration")
    return jsonify({"scope": scope, **config.proxy_block(scope)})


@app.post("/api/proxies")
def api_proxies_add():
    body = request.get_json(force=True, silent=True) or {}
    scope = body.get("scope", "registration")
    url = (body.get("url") or "").strip()
    if not url:
        return jsonify({"ok": False, "error": "代理地址不能为空"}), 400
    blk = CFG["proxy"].setdefault(scope, {"enabled": False, "list": []})
    if any(p.get("url") == url for p in blk["list"]):
        return jsonify({"ok": False, "error": "已存在"}), 400
    blk["list"].append({"url": url, "enabled": bool(body.get("enabled", True)), "note": body.get("note", "")})
    _save_cfg()
    return jsonify({"ok": True})


@app.delete("/api/proxies")
def api_proxies_del():
    scope = request.args.get("scope", "registration")
    url = request.args.get("url", "")
    blk = CFG["proxy"].setdefault(scope, {"enabled": False, "list": []})
    blk["list"] = [p for p in blk["list"] if p.get("url") != url]
    _save_cfg()
    return jsonify({"ok": True})


@app.post("/api/proxies/toggle")
def api_proxies_toggle():
    body = request.get_json(force=True, silent=True) or {}
    scope = body.get("scope", "registration")
    blk = CFG["proxy"].setdefault(scope, {"enabled": False, "list": []})
    if body.get("global"):
        blk["enabled"] = bool(body.get("value", not blk["enabled"]))
    else:
        for p in blk["list"]:
            if p.get("url") == body.get("url"):
                p["enabled"] = not p.get("enabled", True)
    _save_cfg()
    return jsonify({"ok": True, **config.proxy_block(scope)})



# ---------- 手动验证码 ----------
OTP_WAITING = {}


@app.post("/api/accounts/manual")
def api_accounts_manual():
    body = request.get_json(force=True, silent=True) or {}
    email = (body.get("email") or "").strip()
    if not email:
        return jsonify({"ok": False, "error": "邮箱为空"}), 400
    rec = {"email": email, "password": body.get("password", ""), "name": "",
           "ts": int(time.time()), "source": "manual", "status": "imported"}
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "accounts.jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    db.log(f"手动导入账号：{email}", "ok")
    return jsonify({"ok": True})


@app.get("/api/otp")
def api_otp_get():
    tid = request.args.get("task", "0")
    code = OTP_WAITING.pop(tid, None)
    if tid.isdigit():
        if code:
            db.pop_otp(int(tid))         # 已从内存取到，清掉库里的副本
        else:
            code = db.pop_otp(int(tid))  # 面板重启后仍能取到
    if code:
        return jsonify({"ok": True, "code": code})
    return jsonify({"ok": False})


@app.post("/api/otp")
def api_otp_put():
    body = request.get_json(force=True, silent=True) or {}
    tid = str(body.get("task", ""))
    code = (body.get("code") or "").strip()
    if not tid or not code:
        return jsonify({"ok": False, "error": "缺少任务号或验证码"}), 400
    OTP_WAITING[tid] = code
    if tid.isdigit():
        db.put_otp(int(tid), code)
    db.log(f"手动录入验证码：{code}", "step", int(tid) if tid.isdigit() else None)
    return jsonify({"ok": True})



def _gateway_key():
    """面板内部调用网关：优先用已签发的第一把 key，其次用环境变量。"""
    try:
        keys = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                           "data", "keys.json")))
        if keys:
            return keys[0]["key"]
    except Exception:
        pass
    return os.environ.get("PROXY_TOKEN", "")


@app.post("/api/chat")
def api_chat():
    body = request.get_json(force=True, silent=True) or {}
    model = body.get("model") or "fast"
    messages = body.get("messages") or []
    if not messages:
        return jsonify({"ok": False, "error": "消息为空"}), 400
    body_out = {"model": model, "messages": messages}
    if body.get("tools"):
        body_out["tools"] = body["tools"]
    if body.get("tool_choice"):
        body_out["tool_choice"] = body["tool_choice"]
    payload = json.dumps(body_out).encode()
    key = _gateway_key()
    req = urllib.request.Request(
        PROXY + "/v1/chat/completions", data=payload, method="POST",
        headers={"Content-Type": "application/json",
                 **({"Authorization": "Bearer " + key} if key else {})})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=300) as f:
            raw = f.read().decode()
            acct = f.headers.get("x-quick-account", "")
        ms = int((time.time() - t0) * 1000)
        j = json.loads(raw)
        if "choices" not in j:
            return jsonify({"ok": False, "error": (j.get("error") or {}).get("message", "调用失败"),
                            "ms": ms}), 200
        u = j.get("usage") or {}
        msg = j["choices"][0]["message"]
        if msg.get("tool_calls"):
            return jsonify({"ok": True, "tool_calls": msg["tool_calls"], "finish": "tool_calls",
                            "reply": msg.get("content") or "", "model": j.get("model", model),
                            "tokens": u.get("total_tokens", 0), "cost": u.get("cost", 0),
                            "account": acct, "ms": ms})
        return jsonify({"ok": True, "reply": msg["content"],
                        "model": j.get("model", model), "resolved": u.get("resolved_model_id", ""),
                        "tokens": u.get("total_tokens", 0), "cost": u.get("cost", 0),
                        "account": acct, "ms": ms})
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:300]
        return jsonify({"ok": False, "error": f"HTTP {e.code}: {detail}",
                        "ms": int((time.time() - t0) * 1000)}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:300],
                        "ms": int((time.time() - t0) * 1000)}), 200



# ---------- 入池助手（A−） ----------
from flows.pool_import import get_importer, SHOT


@app.post("/api/pool/start")
def api_pool_start():
    body = request.get_json(force=True, silent=True) or {}
    email = (body.get("email") or "").strip()
    password = body.get("password") or ""
    if not email:
        return jsonify({"ok": False, "error": "缺少邮箱"}), 400
    if not password:
        row = None
        with db.conn() as c:
            row = c.execute("SELECT password FROM tasks WHERE email=? ORDER BY id DESC LIMIT 1",
                            (email,)).fetchone()
        password = (row["password"] if row else "") or ""
    if not password:
        return jsonify({"ok": False, "error": "该邮箱没有记录密码，请手动填写"}), 400
    r = json.loads(urllib.request.urlopen(
        urllib.request.Request(PROXY + "/login", method="POST"), timeout=30).read().decode())
    res = get_importer().start(email, password, r["url"])
    return jsonify(res)


@app.get("/api/pool/status")
def api_pool_status():
    return jsonify(get_importer().status())


@app.get("/api/pool/shot")
def api_pool_shot():
    if not os.path.exists(SHOT):
        return ("", 404)
    from flask import send_file
    return send_file(SHOT, mimetype="image/png", max_age=0)


@app.post("/api/pool/click")
def api_pool_click():
    body = request.get_json(force=True, silent=True) or {}
    return jsonify(get_importer().click(body.get("x", 0), body.get("y", 0)))


@app.post("/api/pool/type")
def api_pool_type():
    body = request.get_json(force=True, silent=True) or {}
    return jsonify(get_importer().type_text(body.get("text", "")))


@app.post("/api/pool/stop")
def api_pool_stop():
    return jsonify(get_importer().stop())


@app.get("/api/proxy")
def api_proxy():
    return jsonify({
        "accounts": proxy_get("/accounts"),
        "usage": proxy_get("/usage"),
        "stats": proxy_get("/stats"),
        "health": proxy_get("/health"),
    })


@app.get("/api/stats")
def api_stats():
    s = db.stats()
    s["mode"] = engine.get_engine().mode
    s["running"] = engine.get_engine().running
    s["paused"] = engine.get_engine().paused
    s["concurrency"] = engine.get_engine().concurrency
    s["target"] = engine.get_engine().target
    s["produced"] = engine.get_engine().produced
    s["queue"] = engine.get_engine().q.qsize()
    return jsonify(s)


@app.get("/api/tasks")
def api_tasks():
    return jsonify(db.list_tasks(request.args.get("state"), int(request.args.get("limit", 200))))


@app.get("/api/pools")
def api_pools():
    return jsonify(db.pools())


@app.post("/api/control")
def api_control():
    body = request.get_json(force=True, silent=True) or {}
    act = body.get("action")
    e = engine.get_engine()
    if act == "start":
        return jsonify(e.start(int(body.get("concurrency", 2)), int(body.get("target", 20)),
                               body.get("mode", "sim")))
    if act == "pause":
        return jsonify(e.pause())
    if act == "stop":
        return jsonify(e.stop())
    if act == "clear":
        return jsonify(e.clear())
    return jsonify({"ok": False, "error": "unknown action"}), 400


@app.post("/api/retry/<int:tid>")
def api_retry(tid):
    t = db.get_task(tid)
    if not t:
        return jsonify({"ok": False, "error": "not found"}), 404
    db.update_task(tid, state="created", step="retrying", fail_reason=None,
                   retries=t["retries"] + 1, duration_ms=None)
    e = engine.get_engine()
    e.ensure_running()
    e.q.put(tid)
    db.log(f"手动重试 #{tid}", "system", tid)
    return jsonify({"ok": True})


@app.get("/api/export.csv")
def api_export():
    rows = db.list_tasks(limit=100000)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "email", "password", "name", "state", "fail_reason",
                "proxy", "profile_id", "retries", "duration_ms"])
    for r in rows:
        w.writerow([r["id"], r["email"], r["password"], r["name"], r["state"],
                    r["fail_reason"] or "", r["proxy"] or "", r["profile_id"] or "",
                    r["retries"], r["duration_ms"] or ""])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=accounts.csv"})


@app.get("/api/logs")
def api_logs():
    return jsonify(db.tail_logs(int(request.args.get("after", 0))))


@app.get("/api/logs/stream")
def api_stream():
    def gen():
        last = 0
        while True:
            rows = db.tail_logs(last, 100)
            for r in rows:
                last = max(last, r["id"])
                yield "data: " + json.dumps(r, ensure_ascii=False) + "\n\n"
            time.sleep(0.6)
            yield ": ping\n\n"
    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


if __name__ == "__main__":
    engine.reconcile_stuck_tasks()
    _p = CFG["panel"]
    db.log(f"面板启动 http://{_p['host']}:{_p['port']}", "system")
    app.run(host=_p["host"], port=_p["port"], threaded=True, debug=_p.get("debug", False))
