"""登录 Quick 并侦察应用内部：页面能力 + 网络接口。

用法：
  python3 tools/recon_app.py --email you@your-domain.com --password 'YOUR-PASSWORD'
产出：
  data/recon-app/<ts>/step-*.png
  data/recon-app/<ts>/requests.json   （抓到的 XHR / fetch）
  data/recon-app/<ts>/summary.json
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


def main(email, pwd):
    out = os.path.join("data", "recon-app", time.strftime("%H%M%S"))
    os.makedirs(out, exist_ok=True)
    reqs = []
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        ctx = b.new_context(viewport={"width": 1440, "height": 900}, locale="en-US",
                            timezone_id="America/New_York", user_agent=UA)
        pg = ctx.new_page()
        pg.on("request", lambda r: reqs.append({"m": r.method, "u": r.url[:180],
                                                "t": r.resource_type})
              if r.resource_type in ("xhr", "fetch") else None)

        # ---- 登录 ----
        pg.goto("https://aws.amazon.com/quick/", wait_until="domcontentloaded", timeout=60000)
        pg.wait_for_timeout(2000)
        href = pg.locator('a:has-text("Sign in")').first.get_attribute("href")
        pg.goto(href, wait_until="domcontentloaded", timeout=60000)
        pg.wait_for_timeout(2500)

        for i in range(1, 7):
            pg.screenshot(path=f"{out}/login-{i}.png")
            if pg.query_selector("input[placeholder='6-digit']"):
                code = wait_code(email, int((time.time() - 300) * 1000))
                print("code:", code)
                if not code:
                    break
                setval(pg, "input[placeholder='6-digit']", code)
            elif pg.query_selector("input[type=password]"):
                setval(pg, "input[type=password]", pwd)
            elif pg.query_selector("input[type=email]"):
                setval(pg, "input[type=email]", email)
            else:
                break
            click_primary(pg)
            pg.wait_for_timeout(8000)
            if "quick.aws.com" in pg.url:
                break

        print("登录后:", pg.url[:110])

        # ---- 侦察应用 ----
        for i in range(14):
            pg.wait_for_timeout(4000)
            try:
                n = pg.evaluate("document.querySelectorAll('textarea,[contenteditable=true]').length")
            except Exception:
                n = 0
            if n:
                print("输入框出现，等待", i * 4 + 4, "秒")
                break
        pg.screenshot(path=f"{out}/app-01-home.png", full_page=False)
        try:
            txt = pg.inner_text("body")[:1500]
        except Exception:
            txt = ""
        with open(f"{out}/home.txt", "w") as f:
            f.write(txt)

        # 发一条消息，抓聊天接口
        box = pg.query_selector("textarea") or pg.query_selector("[contenteditable=true]")
        if box:
            try:
                box.click()
                pg.wait_for_timeout(400)
                pg.keyboard.type("hello, reply with one word", delay=45)
                pg.wait_for_timeout(500)
                pg.keyboard.press("Enter")
                print("已发送测试消息")
                pg.wait_for_timeout(25000)
                pg.screenshot(path=f"{out}/app-chat.png")
                print("回复片段:", pg.evaluate("document.body.textContent").replace("\n", " ")[-500:])
            except Exception as e:
                print("发送失败:", str(e)[:100])
        else:
            print("没找到输入框")

        # 探测常见入口
        for name, url in [("chat", "https://quick.aws.com/"),
                          ("spaces", "https://quick.aws.com/spaces"),
                          ("research", "https://quick.aws.com/research")]:
            try:
                pg.goto(url, wait_until="domcontentloaded", timeout=45000)
                pg.wait_for_timeout(6000)
                pg.screenshot(path=f"{out}/app-{name}.png")
                print(name, "->", pg.url[:90])
            except Exception as e:
                print(name, "ERR", str(e)[:80])

        with open(f"{out}/requests.json", "w") as f:
            json.dump(reqs, f, ensure_ascii=False, indent=1)
        hosts = {}
        for r in reqs:
            h = urllib.parse.urlparse(r["u"]).netloc
            hosts[h] = hosts.get(h, 0) + 1
        summary = {"final_url": pg.url, "hosts": sorted(hosts.items(), key=lambda x: -x[1])[:15],
                   "n_requests": len(reqs), "home_text": txt[:800]}
        with open(f"{out}/summary.json", "w") as f:
            json.dump(summary, f, ensure_ascii=False, indent=1)
        print("\n主机分布:")
        for h, n in summary["hosts"]:
            print(f"  {n:4d}  {h}")
        print("输出:", out)
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
