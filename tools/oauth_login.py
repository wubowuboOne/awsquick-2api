"""完成 Quick 的 OAuth 登录：打开代理给出的授权地址，登录，等回调落到 127.0.0.1。

用法：
  python3 tools/oauth_login.py --url "<授权地址>" --email ... --password ...
"""
import argparse
import json
import time
import traceback
import urllib.parse
import urllib.request

from patchright.sync_api import sync_playwright

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36")
BRIDGE = json.load(open("data/mail_bridge.json"))


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


def main(url, email, pwd):
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        ctx = b.new_context(viewport={"width": 1280, "height": 860}, locale="en-US",
                            timezone_id="America/New_York", user_agent=UA)
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)
        print("起点:", page.url[:90])

        for i in range(1, 15):
            if page.url.startswith("http://127.0.0.1"):
                print("已回调:", page.url[:100])
                break
            page.screenshot(path=f"/tmp/oauth-{i}.png")
            if page.query_selector("input[placeholder='6-digit']"):
                code = wait_code(email, int((time.time() - 300) * 1000))
                print(f"[{i}] 验证码:", code)
                if not code:
                    break
                setval(page, "input[placeholder='6-digit']", code)
            elif page.query_selector("input[type=password]"):
                print(f"[{i}] 填密码")
                setval(page, "input[type=password]", pwd)
            elif page.query_selector("input[type=email]"):
                print(f"[{i}] 填邮箱")
                setval(page, "input[type=email]", email)
            else:
                print(f"[{i}] 无表单，等待…", page.url[:80])
                page.wait_for_timeout(9000)
                continue
            click_primary(page)
            page.wait_for_timeout(8000)

        print("最终:", page.url[:120])
        try:
            print("页面:", page.evaluate("document.body.textContent").replace("\n", " ")[:200])
        except Exception:
            pass
        page.screenshot(path="/tmp/oauth-final.png")
        b.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--email", required=True)
    ap.add_argument("--password", required=True)
    a = ap.parse_args()
    try:
        main(a.url, a.email, a.password)
    except Exception:
        traceback.print_exc()
