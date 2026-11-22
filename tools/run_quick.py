"""Quick 注册执行器（抗检测版探针）。

差异点：
  · 默认用 patchright（打过补丁的 Playwright，抹掉 CDP 痕迹）
  · 支持 --proxy，代理出口与 locale/timezone 成套绑定
  · 捕获 /api/send-otp 的响应体，区分 ERR-837 与 BLOCKED by TES

用法：
  python3 tools/run_quick.py --email you@your-domain.com --proxy http://user:pass@host:port
  python3 tools/run_quick.py --email you@your-domain.com --engine firefox --headless
"""
import argparse
import json
import os
import time
import traceback
import urllib.request

ENTRY = "https://aws.amazon.com/quick/pricing/"

GEO = {  # 代理国别 -> (locale, timezone)
    "us": ("en-US", "America/New_York"),
    "de": ("de-DE", "Europe/Berlin"),
    "jp": ("ja-JP", "Asia/Tokyo"),
    "sg": ("en-SG", "Asia/Singapore"),
    "hk": ("zh-HK", "Asia/Hong_Kong"),
}


def make_browser(p, engine):
    if engine == "patchright":
        from patchright.sync_api import sync_playwright  # noqa: F401  (同一套 API)
        return p.chromium.launch(headless=True)
    return p.firefox.launch(headless=True)


def enter_text(page, selector, text):
    """AWS 这个受控输入对 fill / 键盘都不一定买账，三级回退。"""
    el = page.locator(selector).first
    el.click()
    page.wait_for_timeout(120)

    def val():
        try:
            return el.input_value()
        except Exception:
            return ""

    page.keyboard.type(text, delay=70)
    page.wait_for_timeout(200)
    if val() == text:
        return val()

    # 原生 setter + 事件（真实浏览器里验证过有效）
    el.evaluate(
        """(e, t) => {
            const s = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
            s.call(e, t);
            e.dispatchEvent(new Event('input', {bubbles: true, composed: true}));
            e.dispatchEvent(new Event('change', {bubbles: true, composed: true}));
            e.focus();
        }""", text)
    page.wait_for_timeout(250)
    if val() == text:
        return val()

    el.press_sequentially(text, delay=70)
    page.wait_for_timeout(250)
    return val()


def wait_otp(url, token, to, timeout=100):
    """轮询 Worker，取发给 to 的最新验证码。"""
    import urllib.parse
    deadline = time.time() + timeout
    while time.time() < deadline:
        q = urllib.parse.urlencode({"to": to, "token": token})
        try:
            req = urllib.request.Request(f"{url}/latest?{q}",
                                         headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
            with urllib.request.urlopen(req, timeout=15) as f:
                d = json.load(f)
            m = d.get("message")
            if m and m.get("code"):
                return m["code"], m
        except Exception:
            pass
        time.sleep(4)
    return None, None


class Run:
    def __init__(self, a):
        self.a = a
        self.out = os.path.join("data", "runs", time.strftime("%m%d-%H%M%S"))
        os.makedirs(self.out, exist_ok=True)
        self.rec = {"email": a.email, "proxy": bool(a.proxy), "steps": [], "api": []}
        self.n = 0

    def snap(self, pg, label):
        self.n += 1
        path = os.path.join(self.out, f"{self.n:02d}-{label}.png")
        try:
            pg.screenshot(path=path)
        except Exception:
            path = None
        url = pg.url
        self.rec["steps"].append({"label": label, "url": url, "shot": path})
        print(f"[{self.n:02d}] {label:16s} {url[:96]}")
        return url

    def go(self):
        a = self.a
        if a.engine == "patchright":
            from patchright.sync_api import sync_playwright
        else:
            from playwright.sync_api import sync_playwright

        loc, tz = (a.locale, a.timezone) if a.locale else GEO.get(a.geo, GEO["us"])

        with sync_playwright() as p:
            kw = {"headless": a.headless,
                  "args": ["--disable-blink-features=AutomationControlled"]}
            if a.proxy:
                kw["proxy"] = {"server": a.proxy} if "@" in a.proxy else {"server": a.proxy}
            browser = (p.chromium.launch(**kw) if a.engine == "patchright"
                       else p.firefox.launch(**kw))
            ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36")
            ctx = browser.new_context(viewport={"width": 1366, "height": 860},
                                      locale=loc, timezone_id=tz, user_agent=ua)
            page = ctx.new_page()

            def on_resp(r):
                if "send-otp" in r.url or "api/start" in r.url:
                    item = {"status": r.status, "url": r.url[:120]}
                    try:
                        item["body"] = r.text()[:400]
                    except Exception:
                        pass
                    self.rec["api"].append(item)
            page.on("response", on_resp)

            page.goto(ENTRY, wait_until="domcontentloaded", timeout=60000)
            self.snap(page, "entry")

            link = page.locator('a:has-text("Create your Quick account")').first
            link.scroll_into_view_if_needed()
            href = link.get_attribute("href") or ""
            self.rec["entry_href"] = href[:250]
            page.goto(href, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2000)
            self.snap(page, "identity")

            page.wait_for_selector("input[type=email]", timeout=45000)
            got = enter_text(page, "input[type=email]", a.email)
            print("     邮箱写入:", got)
            if not got:
                self.rec["verdict"] = "input-failed"
                return self.finish(browser)
            page.click("[data-testid=test-primary-button]")
            page.wait_for_timeout(6000)
            self.snap(page, "after-email")

            try:
                page.wait_for_selector("[data-testid=signup-next-button]", timeout=40000)
            except Exception:
                self.rec["verdict"] = "no-name-form"
                return self.finish(browser)

            got = enter_text(page, "input[type=text]", a.name)
            print("     姓名写入:", got)
            if not got:
                self.rec["verdict"] = "input-failed"
                return self.finish(browser)
            page.click("[data-testid=signup-next-button]")
            page.wait_for_timeout(7000)
            url = self.snap(page, "after-name")

            otp = [x for x in self.rec["api"] if "send-otp" in x["url"]]
            code = ""
            if otp:
                try:
                    code = json.loads(otp[-1].get("body") or "{}").get("errorCode", "")
                except Exception:
                    code = ""
            if "verify-otp" in url:
                verdict = "accepted"
            elif code == "BLOCKED":
                verdict = "tes-blocked"
            elif otp and otp[-1]["status"] == 400:
                verdict = "err-837"
            else:
                verdict = "unknown"
            self.rec["verdict"] = verdict
            print("\n判定:", verdict, "| send-otp:", otp[-1] if otp else "none")

            if "verify-otp" in url and a.otp_url and a.otp_token:
                code, mail = wait_otp(a.otp_url, a.otp_token, a.email, 110)
                self.rec["otp"] = {"code": code, "subject": (mail or {}).get("subject")}
                if not code:
                    print("✗ 没取到验证码")
                    return self.finish(browser)
                print("验证码:", code)
                enter_text(page, "input[placeholder='6-digit']", code)
                page.click("[data-testid=email-verification-verify-button]")
                page.wait_for_timeout(7000)
                url2 = self.snap(page, "after-otp")
                self.rec["url_after_otp"] = url2

                # SetPassword：两个密码框 + 条款勾选
                pwd = a.password or "YOUR-PASSWORD"
                ploc = page.locator("input[type=password]")
                cnt = ploc.count()
                print("     密码框数量:", cnt)
                for i in range(cnt):
                    el = ploc.nth(i)
                    el.evaluate(
                        """(e, t) => {
                            const s = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
                            s.call(e, t);
                            e.dispatchEvent(new Event('input', {bubbles: true, composed: true}));
                            e.dispatchEvent(new Event('change', {bubbles: true, composed: true}));
                        }""", pwd)
                page.wait_for_timeout(300)
                # 条款勾选（只勾可见的）
                for cb in page.query_selector_all("input[type=checkbox]"):
                    try:
                        if cb.is_visible() and not cb.is_checked():
                            cb.check()
                            print("     已勾选条款")
                            break
                    except Exception:
                        pass
                page.wait_for_timeout(300)
                btn = page.query_selector("[data-testid=test-primary-button]") or page.query_selector("button[type=submit]")
                if btn:
                    btn.click()
                page.wait_for_timeout(9000)
                self.snap(page, "after-password")
                self.rec["password_set"] = pwd

                # 等待开通完成（"Setting up ... up to 1 minute"）
                deadline = time.time() + 150
                last = ""
                while time.time() < deadline:
                    page.wait_for_timeout(5000)
                    try:
                        body = page.inner_text("body")[:200]
                    except Exception:
                        body = ""
                    if body and body.strip() != last.strip():
                        last = body
                    if "Setting up" not in body and "setting up" not in body.lower():
                        break
                self.snap(page, "final")
                self.rec["final_url"] = page.url
                self.rec["final_text"] = last[:200]
                with open(os.path.join("data", "accounts.jsonl"), "a") as f:
                    f.write(json.dumps({"email": a.email, "password": pwd, "name": a.name,
                                        "final_url": page.url, "ts": int(time.time())},
                                       ensure_ascii=False) + "\n")
                self.rec["verdict"] = "provisioned"

            self.finish(browser)

    def finish(self, browser):
        with open(os.path.join(self.out, "log.json"), "w") as f:
            json.dump(self.rec, f, ensure_ascii=False, indent=1)
        print("记录:", os.path.join(self.out, "log.json"))
        browser.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", required=True)
    ap.add_argument("--name", default="Ethan Caldwell")
    ap.add_argument("--engine", choices=["patchright", "firefox"], default="patchright")
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--geo", default="us", choices=list(GEO))
    ap.add_argument("--locale", default=None)
    ap.add_argument("--timezone", default=None)
    ap.add_argument("--headless", action="store_true", default=True)
    ap.add_argument("--otp-url", default=None, help="Worker 取信地址")
    ap.add_argument("--otp-token", default=None, help="Worker 取信口令")
    ap.add_argument("--password", default=None, help="SetPassword 要设置的密码")
    a = ap.parse_args()
    try:
        Run(a).go()
    except Exception:
        traceback.print_exc()
