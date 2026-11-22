"""账号入池助手（A−）：自动打开授权页、填邮箱密码，遇到人机验证时把画面交给面板。

设计：
· 后台线程跑 patchright，页面操作只在这个线程里做（sync API 不跨线程）
· 面板通过 /api/pool/shot 取实时截图，通过 /api/pool/click 回传点击坐标
· 验证通过后自动捕获 ?code= 并提交给网关，账号直接进池
"""
import os
import queue
import threading
import time

from patchright.sync_api import sync_playwright

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHOT_DIR = os.path.join(ROOT, "data", "pool")
SHOT = os.path.join(SHOT_DIR, "live.png")


class Importer:
    def __init__(self):
        self.cmds = queue.Queue()
        self.thread = None
        self.state = {"running": False, "stage": "idle", "url": "", "error": "",
                      "account": "", "done": False, "started": 0}
        self.stop_flag = False

    # ---------- 对外接口 ----------
    def start(self, email, password, auth_url):
        if self.state["running"]:
            return {"ok": False, "error": "已有一个导入在进行"}
        os.makedirs(SHOT_DIR, exist_ok=True)
        self.state = {"running": True, "stage": "启动浏览器", "url": auth_url,
                      "error": "", "account": email, "done": False, "started": time.time()}
        self.stop_flag = False
        self.thread = threading.Thread(target=self._run, args=(email, password, auth_url), daemon=True)
        self.thread.start()
        return {"ok": True}

    def click(self, x, y):
        self.cmds.put(("click", int(x), int(y)))
        return {"ok": True}

    def type_text(self, text):
        self.cmds.put(("type", text))
        return {"ok": True}

    def stop(self):
        self.stop_flag = True
        return {"ok": True}

    def status(self):
        return dict(self.state)

    # ---------- 内部 ----------
    def _drain(self, page):
        while True:
            try:
                c = self.cmds.get_nowait()
            except queue.Empty:
                return
            try:
                if c[0] == "click":
                    page.mouse.click(c[1], c[2])
                elif c[0] == "type":
                    page.keyboard.type(c[1], delay=30)
            except Exception:
                pass

    def _snap(self, page):
        try:
            page.screenshot(path=SHOT, timeout=8000)
        except Exception:
            pass

    def _run(self, email, password, auth_url):
        try:
            with sync_playwright() as p:
                headful = False
                try:
                    import config
                    headful = bool(config.CFG.get("pool_import", {}).get("headful"))
                except Exception:
                    pass
                try:
                    b = p.chromium.launch(headless=not headful,
                                          args=["--disable-blink-features=AutomationControlled"])
                except Exception as e:
                    # 没有显示器时自动退回无头，别让整个流程挂掉
                    self.state["note"] = "无显示器，已退回无头模式"
                    b = p.chromium.launch(headless=True,
                                          args=["--disable-blink-features=AutomationControlled"])
                ctx = b.new_context(viewport={"width": 1280, "height": 860}, user_agent=UA)
                pg = ctx.new_page()
                self.state.update(stage="打开授权页")
                pg.goto(auth_url, wait_until="domcontentloaded", timeout=60000)
                pg.wait_for_timeout(4000)
                self._drain(pg)

                # cookie 弹窗
                for _ in range(4):
                    if pg.evaluate("""() => {const b=[...document.querySelectorAll('button')]
                        .find(x=>(x.innerText||'').trim().toLowerCase()==='accept'); if(b){b.click();return true} return false}"""):
                        pg.wait_for_timeout(1500)
                        break
                    pg.wait_for_timeout(1000)
                self._snap(pg)

                def fill(kind, text):
                    return pg.evaluate("""([k,t]) => {
                        const el=[...document.querySelectorAll('input')].find(i=>i.type===k&&i.offsetParent!==null)
                              || [...document.querySelectorAll('input')].find(i=>i.type===k);
                        if(!el) return false;
                        const set=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;
                        set.call(el,t); el.dispatchEvent(new Event('input',{bubbles:true,composed:true}));
                        el.dispatchEvent(new Event('change',{bubbles:true,composed:true})); el.focus(); return true;
                    }""", [kind, text])

                def submit():
                    for sel in ("[data-testid=test-primary-button]", "button[type=submit]",
                                "button:has-text('Continue')", "button:has-text('Next')"):
                        try:
                            loc = pg.locator(sel)
                            if loc.count():
                                loc.first.click()
                                return True
                        except Exception:
                            continue
                    try:
                        pg.keyboard.press("Enter")
                        return True
                    except Exception:
                        return False

                # 等输入框
                for _ in range(20):
                    self._drain(pg)
                    if pg.evaluate("[...document.querySelectorAll('input')].some(i=>i.type==='email'||i.type==='password')"):
                        break
                    pg.wait_for_timeout(800)

                if pg.evaluate("[...document.querySelectorAll('input')].some(i=>i.type==='email')"):
                    self.state.update(stage="填邮箱")
                    fill("email", email); pg.wait_for_timeout(500); submit()
                    pg.wait_for_timeout(4000)

                if pg.evaluate("[...document.querySelectorAll('input')].some(i=>i.type==='password')"):
                    self.state.update(stage="填密码")
                    fill("password", password); pg.wait_for_timeout(500); submit()
                    pg.wait_for_timeout(5000)

                # 进入人工环节：轮询直到拿到回调
                self.state.update(stage="等待验证（请看画面并点击）")
                deadline = time.time() + 300
                while time.time() < deadline and not self.stop_flag:
                    self._drain(pg)
                    url = pg.url
                    self.state["url"] = url
                    self._snap(pg)
                    if "code=" in url or "error=" in url:
                        break
                    # 验证通过后页面会跳，若已停在同一页超过很久则继续等
                    pg.wait_for_timeout(700)

                url = pg.url
                if "code=" in url:
                    self.state.update(stage="提交回调", done=True)
                    try:
                        import json, urllib.request
                        body = json.dumps({"url": url}).encode()
                        req = urllib.request.Request(
                            "http://127.0.0.1:8787/api/proxy/submit", data=body, method="POST",
                            headers={"Content-Type": "application/json"})
                        with urllib.request.urlopen(req, timeout=30) as f:
                            self.state["submit"] = f.read().decode()[:200]
                        self.state.update(stage="已入池", done=True)
                    except Exception as e:
                        self.state.update(stage="提交失败", error=str(e)[:200], done=True)
                else:
                    self.state.update(stage="未拿到回调", error=url[:160], done=True)
                b.close()
        except Exception as e:
            self.state.update(stage="出错", error=str(e)[:300], done=True)
        finally:
            self.state["running"] = False


_imp = None


def get_importer():
    global _imp
    if _imp is None:
        _imp = Importer()
    return _imp
