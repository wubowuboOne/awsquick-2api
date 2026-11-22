"""探针：验证「注册后的浏览器状态，能否直接复用去完成 OAuth 授权」。

用法：
    python3 tools/probe_oauth_reuse.py --mode login  --email you@your-domain.com --password 'PW' --state data/profiles/probe.json
    python3 tools/probe_oauth_reuse.py --mode reuse  --state data/profiles/probe.json
"""
import argparse, json, os, re, sys, time, urllib.request
from patchright.sync_api import sync_playwright

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36")


def new_login_url():
    req = urllib.request.Request("http://127.0.0.1:3426/login", method="POST")
    with urllib.request.urlopen(req, timeout=30) as f:
        return json.load(f)["url"]


def enter_kind(page, kind, text):
    """kind: 'email' | 'password'；AWS 用 JS 设 type 属性，[type=...] 选择器抓不到。"""
    return page.evaluate("""([kind,t]) => {
        const el = [...document.querySelectorAll('input')].find(i => i.type === kind && i.offsetParent !== null)
                 || [...document.querySelectorAll('input')].find(i => i.type === kind);
        if (!el) return false;
        const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;
        set.call(el, t);
        el.dispatchEvent(new Event('input',{bubbles:true,composed:true}));
        el.dispatchEvent(new Event('change',{bubbles:true,composed:true}));
        el.focus();
        return true;
    }""", [kind, text])


def enter(page, sel, text):
    page.evaluate("""([s,t]) => {
        const el = document.querySelector(s);
        const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;
        set.call(el, t);
        el.dispatchEvent(new Event('input',{bubbles:true,composed:true}));
        el.dispatchEvent(new Event('change',{bubbles:true,composed:true}));
    }""", [sel, text])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["login", "reuse"], required=True)
    ap.add_argument("--email", default="")
    ap.add_argument("--password", default="")
    ap.add_argument("--state", default="data/profiles/probe.json")
    ap.add_argument("--headless", action="store_true")
    a = ap.parse_args()

    os.makedirs(os.path.dirname(a.state), exist_ok=True)
    url = new_login_url()
    print("授权地址:", url[:100], "…")

    with sync_playwright() as p:
        b = p.chromium.launch(headless=a.headless,
                              args=["--disable-blink-features=AutomationControlled"])
        kw = {"viewport": {"width": 1366, "height": 880}, "user_agent": UA}
        if a.mode == "reuse" and os.path.exists(a.state):
            kw["storage_state"] = a.state
        ctx = b.new_context(**kw)
        pg = ctx.new_page()
        try:
            pg.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print("打开失败:", e)
        pg.wait_for_timeout(5000)

        # 1) 先处理 AWS 的 cookie 同意弹窗
        for label in ("Accept", "接受", "Accept all"):
            try:
                btn = pg.get_by_role("button", name=label)
                if btn.count():
                    btn.first.click()
                    print("已通过 cookie 弹窗:", label)
                    pg.wait_for_timeout(1500)
                    break
            except Exception:
                pass
        pg.wait_for_timeout(1000)

        def counts():
            return pg.evaluate("""() => ({
                all: document.querySelectorAll('input').length,
                email: [...document.querySelectorAll('input')].filter(i=>i.type==='email').length,
                pwd: [...document.querySelectorAll('input')].filter(i=>i.type==='password').length,
                cap: !!document.querySelector("iframe[src*='threat-mitigation'],iframe[src*='captcha']"),
                txt: (document.body.innerText || '').slice(0, 120).replace(/\\n/g, ' ')
            })""")

        def probe(tag):
            c = counts()
            print(f"[{tag}] url={pg.url[:100]}")
            print(f"       输入框={c['all']} 邮箱={c['email']} 密码={c['pwd']} captcha={c['cap']} | {c['txt']}")
            return {"url": pg.url, "pwd": bool(c["pwd"]), "email": bool(c["email"]),
                    "captcha": bool(c["cap"]), "inputs": c["all"]}

        # 等输入框出现（用 evaluate 判定，避免 locator 差异）
        for _ in range(20):
            if counts()["email"] or counts()["pwd"]:
                break
            pg.wait_for_timeout(1000)

        # cookie 同意弹窗：用 evaluate 直接点，避免 role/locator 差异
        for _ in range(4):
            clicked = pg.evaluate("""() => {
                const b = [...document.querySelectorAll('button')]
                    .find(x => (x.innerText || '').trim().toLowerCase() === 'accept');
                if (b) { b.click(); return true; }
                return false;
            }""")
            if clicked:
                print("已通过 cookie 弹窗")
                pg.wait_for_timeout(2000)
                break
            pg.wait_for_timeout(1500)

        st = probe("到达")

        def submit():
            for sel in ("[data-testid=test-primary-button]", "button[type=submit]",
                        "button:has-text('Continue')", "button:has-text('Next')"):
                try:
                    loc = pg.locator(sel)
                    if loc.count():
                        loc.first.click(); return True
                except Exception:
                    continue
            try:
                pg.keyboard.press("Enter"); return True
            except Exception:
                return False

        if st["email"] and not st["pwd"] and a.email:
            ok_fill = enter_kind(pg, "email", a.email)
            print("       填入邮箱:", ok_fill)
            pg.wait_for_timeout(500)
            submit()
            for _ in range(15):
                if counts()["pwd"] or counts()["cap"]:
                    break
                pg.wait_for_timeout(1000)
            st = probe("提交邮箱后")

        if st["pwd"] and a.password:
            ok_fill = enter_kind(pg, "password", a.password)
            print("       填入密码:", ok_fill)
            pg.wait_for_timeout(500)
            submit()
            pg.wait_for_timeout(9000)
            st = probe("提交密码后")

        shot = a.state.replace(".json", f"-{a.mode}.png")
        try:
            pg.screenshot(path=shot); print("截图:", shot)
        except Exception:
            pass

        cookies = ctx.cookies()
        domains = sorted({c["domain"] for c in cookies})
        print("cookie 域:", domains[:10])
        ctx.storage_state(path=a.state)
        print("状态已保存:", a.state, f"({len(cookies)} cookies)")

        ok = ("/?code=" in pg.url) or ("code=" in pg.url and "error" not in pg.url)
        print("RESULT:", json.dumps({"mode": a.mode, "callback": ok, **st}, ensure_ascii=False))
        b.close()


if __name__ == "__main__":
    main()
