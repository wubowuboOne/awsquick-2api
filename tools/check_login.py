"""登录验证：判断账号是否真的可用。

按页面元素逐步推进，而不是固定顺序：
  有 6 位验证码框 -> 从邮件桥取新码填入
  有密码框       -> 填密码
  有邮箱框       -> 填邮箱
每步截图，最后判断是否进入 Quick 应用。

用法：
  python3 tools/check_login.py --email you@your-domain.com --password 'YOUR-PASSWORD'
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
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.click()
                return sel
        except Exception:
            pass
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


def main(email, pwd, outdir="data/login"):
    os.makedirs(outdir, exist_ok=True)
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        ctx = b.new_context(viewport={"width": 1280, "height": 820}, locale="en-US",
                            timezone_id="America/New_York", user_agent=UA)
        pg = ctx.new_page()

        pg.goto("https://aws.amazon.com/quick/", wait_until="domcontentloaded", timeout=60000)
        pg.wait_for_timeout(2500)
        href = pg.locator('a:has-text("Sign in")').first.get_attribute("href")
        print("sign-in ->", (href or "")[:100])
        pg.goto(href, wait_until="domcontentloaded", timeout=60000)
        pg.wait_for_timeout(3000)

        for step in range(1, 7):
            n = step
            pg.screenshot(path=f"{outdir}/{n:02d}-step.png")
            url = pg.url
            six = pg.query_selector("input[placeholder='6-digit']")
            pwd_in = pg.query_selector("input[type=password]")
            mail_in = pg.query_selector("input[type=email]")
            body = ""
            try:
                body = pg.evaluate("document.body.textContent").replace("\n", " ")[:160]
            except Exception:
                pass
            print(f"[{n}] {url[:80]} | 6digit={bool(six)} pwd={bool(pwd_in)} email={bool(mail_in)}")
            if body.strip():
                print("    ", body)

            if six:
                since = int((time.time() - 300) * 1000)
                code = wait_code(email, since)
                print("     code:", code)
                if not code:
                    break
                setval(pg, "input[placeholder='6-digit']", code)
                click_primary(pg)
            elif pwd_in:
                setval(pg, "input[type=password]", pwd)
                click_primary(pg)
            elif mail_in:
                setval(pg, "input[type=email]", email)
                click_primary(pg)
            else:
                break
            pg.wait_for_timeout(8000)

        pg.screenshot(path=f"{outdir}/99-final.png")
        final = pg.url
        print("\n最终:", final[:140])
        ok = any(k in final for k in ("quick", "quicksight", "awsapps", "amazon.com/quick"))
        print("判定:", "进入应用" if ok else "未进入应用")
        b.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", required=True)
    ap.add_argument("--password", required=True)
    a = ap.parse_args()
    try:
        main(a.email, a.password)
    except Exception:
        traceback.print_exc()
