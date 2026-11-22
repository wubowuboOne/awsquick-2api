"""持久化会话的 Quick 侦察：渲染应用 → 找聊天入口 → 发一条消息 → 抓接口。

首次需要验证码；之后复用 data/profile 里的登录态。

用法：
  python3 tools/recon_chat.py --email you@your-domain.com --password 'YOUR-PASSWORD'
  python3 tools/recon_chat.py --email ... --password ... --say "hello"
"""
import argparse
import json
import os
import time
import traceback
import urllib.parse
import urllib.request

from patchright.sync_api import sync_playwright

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36")
BRIDGE = json.load(open("data/mail_bridge.json"))
PROFILE = "data/profile"


def setval(page, sel, text):
    el = page.locator(sel).first
    el.click()
    page.wait_for_timeout(120)
    el.evaluate(
        """(e, t) => {
            const s = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
            s.call(e, t);
            e.dispatchEvent(new Event('input', {bubbles: true, composed: true}));
            e.dispatchEvent(new Event('change', {bubbles: true, composed: true}));
        }""", text)
    page.wait_for_timeout(200)


def click_primary(page):
    for sel in ("[data-testid=email-verification-verify-button]",
                "[data-testid=test-primary-button]",
                "button[type=submit]"):
        el = page.query_selector(sel)
        if el and el.is_visible():
            el.click()
            return sel
    return None


def wait_code(to, since_ms, timeout=110):
    end = time.time() + timeout
    while time.time() < end:
        q = urllib.parse.urlencode({"to": to, "token": BRIDGE["api_token"]})
        try:
            req = urllib.request.Request(f"{BRIDGE['url']}/latest?{q}",
                                         headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as f:
                d = json.load(f)
            m = d.get("message")
            if m and m.get("code") and m.get("received_at", 0) >= since_ms:
                return m["code"]
        except Exception:
            pass
        time.sleep(4)
    return None


def ensure_login(ctx, pg, email, pwd, out):
    pg.goto("https://quick.aws.com/", wait_until="domcontentloaded", timeout=60000)
    pg.wait_for_timeout(4000)
    if "signin.aws" not in pg.url and "signup.aws" not in pg.url:
        print("已有会话:", pg.url[:90])
        return True

    print("需要登录:", pg.url[:80])
    for i in range(1, 7):
        pg.screenshot(path=f"{out}/login-{i}.png")
        if pg.query_selector("input[placeholder='6-digit']"):
            code = wait_code(email, int((time.time() - 300) * 1000))
            print("code:", code)
            if not code:
                return False
            setval(pg, "input[placeholder='6-digit']", code)
        elif pg.query_selector("input[type=password]"):
            setval(pg, "input[type=password]", pwd)
        elif pg.query_selector("input[type=email]"):
            setval(pg, "input[type=email]", email)
        else:
            break
        click_primary(pg)
        pg.wait_for_timeout(8000)
        if "quick.aws.com" in pg.url and "signin" not in pg.url:
            return True
    return "quick.aws.com" in pg.url


def main(email, pwd, say):
    out = os.path.join("data", "recon-chat", time.strftime("%H%M%S"))
    os.makedirs(out, exist_ok=True)
    os.makedirs(PROFILE, exist_ok=True)
    reqs = []

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            PROFILE, headless=True, viewport={"width": 1440, "height": 900},
            locale="en-US", timezone_id="America/New_York", user_agent=UA,
            args=["--disable-blink-features=AutomationControlled"])
        pg = ctx.pages[0] if ctx.pages else ctx.new_page()
        pg.on("request", lambda r: reqs.append({"m": r.method, "u": r.url[:300]})
              if r.resource_type in ("xhr", "fetch") else None)

        if not ensure_login(ctx, pg, email, pwd, out):
            print("登录失败")
            ctx.close()
            return

        if "/sn/account/" not in pg.url:
            pg.goto("https://quick.aws.com/", wait_until="domcontentloaded", timeout=60000)
            pg.wait_for_timeout(3000)
            for label in ("Open Quick", "Go to Quick", "Launch", "Get started"):
                el = pg.query_selector(f'button:has-text("{label}")')
                if el and el.is_visible():
                    el.click()
                    pg.wait_for_timeout(6000)
                    break
        for i in range(8):
            pg.wait_for_timeout(5000)
            try:
                body = pg.evaluate("document.body.textContent").replace("\n", " ")[:120]
            except Exception:
                body = ""
            if body and "Loading" not in body:
                break
        pg.screenshot(path=f"{out}/app.png", full_page=False)
        print("应用页面:", pg.url[:90])

        info = pg.evaluate("""() => {
            const q = s => [...document.querySelectorAll(s)];
            return {
              textareas: q("textarea").map(e => ({ph: e.placeholder, id: e.id, cls: e.className.slice(0,40)})),
              editables: q("[contenteditable=true]").map(e => ({role: e.getAttribute('role'), ph: e.getAttribute('data-placeholder') || e.getAttribute('aria-label')})),
              buttons: q("button").filter(b => b.offsetParent).slice(0, 25).map(b => (b.getAttribute('aria-label') || b.textContent || '').trim().slice(0, 30)),
              text: document.body.textContent.replace(/\\s+/g, ' ').slice(0, 400)
            };
        }""")
        print("textarea:", info["textareas"])
        print("editable:", info["editables"])
        print("buttons:", info["buttons"][:15])
        print("text:", info["text"][:250])

        if say:
            box = None
            if pg.query_selector("textarea"):
                box = pg.query_selector("textarea")
            elif pg.query_selector("[contenteditable=true]"):
                box = pg.query_selector("[contenteditable=true]")
            if box:
                box.click()
                pg.wait_for_timeout(300)
                pg.keyboard.type(say, delay=45)
                pg.wait_for_timeout(400)
                pg.keyboard.press("Enter")
                print("已发送:", say)
                pg.wait_for_timeout(25000)
                pg.screenshot(path=f"{out}/chat.png", full_page=False)
                try:
                    print("回复:", pg.evaluate("document.body.textContent").replace("\n", " ")[-600:])
                except Exception:
                    pass
            else:
                print("没找到输入框")

        with open(f"{out}/requests.json", "w") as f:
            json.dump(reqs, f, ensure_ascii=False, indent=1)
        seen = set()
        print("\n--- 应用接口 ---")
        for r in reqs:
            u = r["u"]
            if "quick.aws.com" in u and "/api/" in u:
                k = (r["m"], u.split("?")[0])
                if k in seen:
                    continue
                seen.add(k)
                print(f'{r["m"]:5s} {u.split("?")[0][:140]}')
        print("输出:", out)
        ctx.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--say", default=None)
    a = ap.parse_args()
    try:
        main(a.email, a.password, a.say)
    except Exception:
        traceback.print_exc()
