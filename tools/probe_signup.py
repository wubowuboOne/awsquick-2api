"""注册流程探针：用 Playwright 跑一遍 Quick 免费版注册，记录每一步与网络结果。

用法：
    python3 tools/probe_signup.py --email you@your-domain.com --name "Ethan Caldwell"

输出：
    data/recon/<ts>/step-NN.png   每步截图
    data/recon/<ts>/log.json      步骤、URL、告警文本、/api/* 响应状态
不提交验证码，到 verify-otp 就停。
"""
import argparse
import json
import os
import time
import traceback

from playwright.sync_api import sync_playwright

ENTRY = "https://aws.amazon.com/quick/pricing/"


class Probe:
    def __init__(self, email, name):
        self.email = email
        self.name = name
        self.out = os.path.join("data", "recon", time.strftime("%H%M%S"))
        os.makedirs(self.out, exist_ok=True)
        self.rec = {"email": email, "steps": [], "api": [], "console": []}
        self.n = 0

    def snap(self, pg, label, note=""):
        self.n += 1
        path = os.path.join(self.out, f"step-{self.n:02d}-{label}.png")
        try:
            pg.screenshot(path=path, full_page=False)
        except Exception:
            path = None
        url = pg.url
        alerts = []
        try:
            alerts = [a.inner_text().replace("\n", " ")[:200]
                      for a in pg.query_selector_all("[role=alert]")]
        except Exception:
            pass
        self.rec["steps"].append({"n": self.n, "label": label, "note": note,
                                  "url": url, "alerts": alerts, "shot": path})
        print(f"[{self.n:02d}] {label:14s} {url[:100]}")
        if alerts:
            print("     ⚠", alerts[0][:160])
        return alerts

    def run(self):
        with sync_playwright() as p:
            b = p.firefox.launch()
            ctx = b.new_context(viewport={"width": 1280, "height": 860},
                                locale="en-US", timezone_id="America/New_York")
            pg = ctx.new_page()

            def on_resp(r):
                u = r.url
                if "send-otp" in u or "verify" in u:
                    item = {"status": r.status, "url": u[:160]}
                    try:
                        item["body"] = r.text()[:600]
                    except Exception:
                        item["body"] = "<no body>"
                    self.rec["api"].append(item)
                elif "/api/" in u:
                    self.rec["api"].append({"status": r.status, "url": u[:160]})
            pg.on("response", on_resp)

            pg.goto(ENTRY, wait_until="domcontentloaded", timeout=60000)
            self.snap(pg, "entry")

            link = pg.locator('a:has-text("Create your Quick account")').first
            link.scroll_into_view_if_needed()
            href = link.get_attribute("href") or ""
            self.rec["entry_href"] = href[:300]
            # 入口在点击时才补 ampDeviceId，且通常开新标签：必须真点 + 接弹窗
            page = pg
            try:
                with ctx.expect_page(timeout=12000) as pi:
                    link.click(timeout=30000)
                page = pi.value
                page.wait_for_load_state("domcontentloaded", timeout=60000)
            except Exception:
                page = pg
            page.on("response", on_resp)
            page.wait_for_timeout(2500)
            pg = page
            self.snap(pg, "identity")

            pg.wait_for_selector("input[type=email]", timeout=45000)
            pg.fill("input[type=email]", self.email)
            self.snap(pg, "email-filled")
            pg.click("[data-testid=test-primary-button]")
            pg.wait_for_timeout(6000)
            self.snap(pg, "after-email")

            # 姓名页
            try:
                pg.wait_for_selector("[data-testid=signup-next-button]", timeout=45000)
            except Exception:
                self.snap(pg, "no-name-page")
                self.finish(b)
                return
            pg.fill("input[type=text]", self.name)
            pg.click("[data-testid=signup-next-button]")
            pg.wait_for_timeout(7000)
            alerts = self.snap(pg, "after-name")

            ok = any("verify-otp" in s["url"] for s in self.rec["steps"])
            self.rec["verdict"] = "accepted" if ok else ("blocked" if alerts else "unknown")
            print("\n判定:", self.rec["verdict"])
            self.finish(b)

    def finish(self, b):
        with open(os.path.join(self.out, "log.json"), "w") as f:
            json.dump(self.rec, f, ensure_ascii=False, indent=1)
        print("记录:", os.path.join(self.out, "log.json"))
        b.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", required=True)
    ap.add_argument("--name", default="Ethan Caldwell")
    a = ap.parse_args()
    try:
        Probe(a.email, a.name).run()
    except Exception:
        traceback.print_exc()
