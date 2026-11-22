"""执行引擎：真实注册（半自动）+ 全自动框架（占位）。

设计说明
--------
· 半自动（LiveRunner）：每个任务由人过一次 AWS WAF captcha 拿授权，
  注册本身由 tools/run_quick.py 驱动（它已实测能走通邮箱→姓名→验证码→密码）。
· 全自动（AutoRunner）：注册 + 授权全自动，依赖打码服务；框架已就位，
  打码与授权回填在 flows/captcha.py / flows/auto_oauth.py 里留了接口。
"""
import os
import queue
import subprocess
import threading
import time
import traceback

import config
import db
from flows import quick_signup as qs

TERMINAL = {"registered", "pooled", "blocked", "verify_failed", "dead"}
ROOT = os.path.dirname(os.path.abspath(__file__))


class Engine:
    def __init__(self):
        self.q = queue.Queue()
        self.workers = []
        self.running = False
        self.paused = False
        self.concurrency = 2
        self.target = 0
        self.produced = 0
        self.lock = threading.Lock()
        self.mode = "manual"          # manual | auto

    # ---------- 控制 ----------
    def _workers_alive(self):
        return any(t.is_alive() for t in self.workers)

    def ensure_running(self):
        """只保证有工作线程，不生成新任务（重试用）。"""
        with self.lock:
            if self.running and self._workers_alive():
                return True
            self.running = True
            self.paused = False
            self.workers = [t for t in self.workers if t.is_alive()]
            for i in range(self.concurrency):
                t = threading.Thread(target=self._worker, name=f"w{i}", daemon=True)
                t.start()
                self.workers.append(t)
        return True

    def start(self, concurrency=2, target=20, mode="manual"):
        with self.lock:
            self.concurrency = max(1, min(8, int(concurrency)))
            self.target = int(target)
            self.mode = mode if mode in ("manual", "auto") else "manual"
            self.paused = False
            if self.running and self._workers_alive():
                # 线程还活着：只补任务，不重启
                self._spawn_tasks()
                return {"ok": True, "note": "已在运行，参数已更新"}
            # 状态卡住（线程已死）：自愈重启
            self.running = False
            self.workers = [t for t in self.workers if t.is_alive()]
            self.running = True
            self.produced = 0
            for i in range(self.concurrency):
                t = threading.Thread(target=self._worker, name=f"w{i}", daemon=True)
                t.start()
                self.workers.append(t)
        try:
            self._spawn_tasks()
        except RuntimeError as e:
            self.running = False
            db.log(str(e), "error")
            return {"ok": False, "error": str(e)}
        db.log(f"引擎启动：并发 {self.concurrency}，目标 {self.target}，模式 {self.mode}", "system")
        return {"ok": True}

    def pause(self):
        self.paused = not self.paused
        db.log("引擎已暂停" if self.paused else "引擎已恢复", "system")
        return {"ok": True, "paused": self.paused}

    def stop(self):
        self.running = False
        self.paused = False
        self.workers.clear()
        db.log("引擎已停止", "system")
        return {"ok": True}

    def clear(self):
        with db.conn() as c:
            c.execute("DELETE FROM tasks")
            c.execute("DELETE FROM logs")
        db.log("已清空任务与日志", "system")
        return {"ok": True}

    # ---------- 生产任务 ----------
    def _spawn_tasks(self):
        """按目标数量生成待注册任务。没有真实域名就直接报错，绝不回退到假域名。"""
        import random
        rng = random.Random()
        m = config.CFG.get("mail") or {}
        manual = [x for x in (m.get("manual_mails") or []) if (x.get("email") or "").strip()] \
            if m.get("provider") == "manual" else []
        doms = [d for d in (db.list_resources("domain") or []) if "." in (d["value"] or "")]
        if not manual and not doms:
            raise RuntimeError("没有可用邮箱：手动模式请先在设置里添加邮箱，其它模式请先配置域名")
        used = {r["email"].lower() for r in db_recent_emails()}
        while self.produced < self.target:
            cpl = config.proxies("registration")
            prx = {"value": cpl[self.produced % len(cpl)]["url"]} if cpl else db.pick_resource("proxy")
            prf = db.pick_resource("profile")
            n = self.produced + 1
            if manual:
                pick = next((x for x in manual if (x.get("email") or "").lower() not in used), None)
                if not pick:
                    db.log("手动邮箱池已用完，停止生成新任务", "system")
                    break
                email = pick["email"].strip()
                pwd = (pick.get("password") or "").strip() or ("Qr#" + "".join(
                    rng.choice("abcdefghjkmnpqrstuvwxyz23456789") for _ in range(10)))
                name = rng.choice(["Daniel Whitmore", "Marcus Feld", "Alice Chen",
                                   "Tomas Ruder", "Nina Petrova", "Kenji Mori"])
                used.add(email.lower())
            else:
                dom = doms[self.produced % len(doms)]["value"]
                email = f"q{n:04d}{rng.randrange(1000, 9999)}@{dom}"
                name = rng.choice(["Daniel Whitmore", "Marcus Feld", "Alice Chen",
                                   "Tomas Ruder", "Nina Petrova", "Kenji Mori"])
                pwd = "Qr#" + "".join(rng.choice("abcdefghjkmnpqrstuvwxyz23456789") for _ in range(10))
            tid = db.create_task(email, pwd, name,
                                 proxy=prx["value"] if prx else None,
                                 profile_id=prf["value"] if prf else None)
            self.q.put(tid)
            self.produced += 1

    # ---------- 消费 ----------
    def _worker(self):
        while self.running:
            try:
                tid = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            if self.paused:
                self.q.put(tid)
                time.sleep(0.5)
                continue
            try:
                runner = LiveRunner(tid) if self.mode == "manual" else AutoRunner(tid)
                runner.run()
            except Exception:
                db.log(traceback.format_exc(limit=2), "error", tid)
            finally:
                self.q.task_done()


def db_recent_emails():
    with db.conn() as c:
        return [dict(r) for r in c.execute("SELECT email FROM tasks")]


def _otp_endpoint(tid):
    """按邮箱来源返回 (url, token)。手动模式指向面板自身的取码接口。"""
    m = config.CFG.get("mail") or {}
    prov = m.get("provider", "cloudflare")
    if prov == "manual":
        host = config.CFG["panel"].get("host", "127.0.0.1")
        port = config.CFG["panel"].get("port", 8787)
        return f"http://{host}:{port}/api/otp?task={tid}", "manual"
    if prov == "custom" and m.get("custom_url"):
        u = m["custom_url"].replace("{token}", m.get("api_token", "") or "")
        return u.replace("{to}", ""), m.get("api_token", "")
    # Cloudflare / 默认：用 Worker 的 /latest 接口
    creds = _mail_creds()
    if creds:
        return creds["url"].rstrip("/"), creds["token"]   # run_quick 自己会拼 /latest
    return None


def _mail_creds():
    """取信凭据：config.json 优先，退回 data/mail_bridge.json。"""
    import json
    cfg = config.CFG.get("mail") or {}
    if cfg.get("worker_url") and cfg.get("api_token"):
        return {"url": cfg["worker_url"], "token": cfg["api_token"]}
    try:
        mb = json.load(open(os.path.join(ROOT, "data", "mail_bridge.json")))
        if mb.get("url") and mb.get("api_token"):
            return {"url": mb["url"], "token": mb["api_token"]}
    except Exception:
        pass
    return None


class BaseRunner:
    def __init__(self, tid):
        self.tid = tid
        self.t0 = time.time()

    def step(self, state, note=""):
        db.update_task(self.tid, state=state, step=state)
        if note:
            db.log(note, "step", self.tid)

    def finish(self, state, reason=None):
        db.update_task(self.tid, state=state, fail_reason=reason,
                       duration_ms=int((time.time() - self.t0) * 1000))
        t = db.get_task(self.tid) or {}
        ok = state in ("registered", "pooled", "account_active")
        if "@" in (t.get("email") or ""):
            db.mark_resource("domain", t["email"].split("@")[1], ok)
        for kind, val in (("proxy", t.get("proxy")), ("profile", t.get("profile_id"))):
            if val:
                db.mark_resource(kind, val, ok)
        lvl = "ok" if state == "account_active" else "fail"
        db.log(f"结束：{state}" + (f"（{qs.FAIL_REASONS.get(reason, reason)}）" if reason else ""),
               lvl, self.tid)


class LiveRunner(BaseRunner):
    """半自动：调用 tools/run_quick.py 走真实注册，人工过一次 captcha。"""

    def run(self):
        t = db.get_task(self.tid)
        self.step("page_loaded", f"启动真实注册：{t['email']}")
        try:
            cmd = ["python3", os.path.join(ROOT, "tools", "run_quick.py"),
                   "--email", t["email"], "--name", t["name"] or "Ethan Caldwell"]
            if t.get("proxy"):
                cmd += ["--proxy", t["proxy"]]
            otp = _otp_endpoint(self.tid)
            if otp:
                url_tpl, tok = otp
                cmd += ["--otp-url", url_tpl.replace("{to}", t["email"]), "--otp-token", tok]
                if (config.CFG.get("mail") or {}).get("provider") == "manual":
                    self.step("waiting_otp", "手动模式：请在面板里填入验证码")
            else:
                db.log("未配置收信来源，验证码无法读取", "error", self.tid)
            r = subprocess.run(cmd,
                capture_output=True, text=True, timeout=int(config.CFG["registration"].get("task_timeout_s", 600)), cwd=ROOT)
            out = (r.stdout or "") + (r.stderr or "")
        except subprocess.TimeoutExpired:
            self.finish("verify_failed", "otp_timeout")
            return
        run_dir = os.path.join(ROOT, "data", "runs", time.strftime("%m%d-%H%M%S"))
        try:
            os.makedirs(run_dir, exist_ok=True)
            with open(os.path.join(run_dir, f"task-{self.tid}.log"), "w") as f:
                f.write(out)
        except Exception:
            pass
        for line in out.splitlines():
            if line.startswith("验证码"):
                self.step("otp_ok", line.strip())
            elif "判定: accepted" in line:
                self.step("identity_loaded", "已进入验证码页")
            elif "没取到验证码" in line:
                self.finish("verify_failed", "otp_timeout")
                return
            elif "BLOCKED" in line:
                self.finish("blocked", "fingerprint_flag")
                return
        verdict = ""
        for line in out.splitlines():
            if line.startswith("判定:"):
                verdict = line.split(":", 1)[1].split("|")[0].strip()
        if verdict in ("provisioned", "accepted"):
            self.step("password_set", "密码已设置，等待授权")
            self.finish("registered")
        elif verdict in ("verify-otp", "verify_failed"):
            self.finish("verify_failed", "otp_timeout")
        elif "BLOCKED" in out:
            self.finish("blocked", "fingerprint_flag")
        else:
            self.finish("verify_failed", "unknown_error")


class AutoRunner(BaseRunner):
    """全自动：注册 → 授权 → 入池，全流程无人值守（框架，未启用）。"""

    def run(self):
        self.step("page_loaded", "自动模式启动")
        try:
            from flows.captcha import solve_aws_waf, CaptchaUnavailable
            from flows.auto_oauth import run_auto_oauth
        except Exception as e:
            self.finish("blocked", "captcha_stuck")
            db.log(f"自动模式缺少组件：{e}", "error", self.tid)
            return
        try:
            run_auto_oauth(self.tid, solve=solve_aws_waf, log=lambda m: db.log(m, "step", self.tid))
            self.finish("account_active")
        except CaptchaUnavailable as e:
            self.finish("blocked", "captcha_stuck")
            db.log(f"打码不可用：{e}", "error", self.tid)
        except Exception as e:
            self.finish("verify_failed", "unknown_error")
            db.log(f"自动流程失败：{e}", "error", self.tid)


def reconcile_stuck_tasks():
    """面板重启会杀掉工作线程：启动时把悬挂任务对账回真实状态。"""
    import json
    done = set()
    p = os.path.join(ROOT, "data", "accounts.jsonl")
    if os.path.exists(p):
        for line in open(p):
            try:
                done.add(json.loads(line)["email"].lower())
            except Exception:
                pass
    fixed = 0
    with db.conn() as c:
        # 只要 accounts.jsonl 里有这个邮箱，就算注册成功（包含被误判为失败的）
        rows = [dict(r) for r in c.execute(
            "SELECT id,email,state FROM tasks WHERE state NOT IN ('registered','pooled')")]
    for r in rows:
        if (r["email"] or "").lower() in done:
            db.update_task(r["id"], state="registered", step="password_set", fail_reason=None)
        elif r["state"] not in ("verify_failed", "blocked", "dead"):
            db.update_task(r["id"], state="interrupted", fail_reason="panel_restart")
        fixed += 1
    if fixed:
        db.log(f"启动对账：修正 {fixed} 个悬挂任务", "system")
    return fixed


_engine = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = Engine()
    return _engine
