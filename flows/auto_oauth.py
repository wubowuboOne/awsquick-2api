"""全自动授权流程（框架，未启用、未测试）。

流程：
  1. POST /login 拿授权地址
  2. patchright 打开 → 填邮箱/密码/验证码（复用 run_quick 里的 helpers）
  3. 遇到 security check → 调 flows.captcha.solve_aws_waf，注入 aws-waf-token cookie
  4. 捕获回调 URL → POST /login/submit
  5. 会话落盘，账号进池

当前状态：骨架。缺三块 —— 打码 key、与注册一致的住宅代理、captcha 与登录页的时序联调。
"""
import json
import urllib.request


def run_auto_oauth(tid, solve, log=print, proxy=None):
    from patchright.sync_api import sync_playwright  # noqa: F401  (依赖未在使用时也可导入)

    log(f"#{tid} 自动授权：请求授权地址")
    req = urllib.request.Request("http://127.0.0.1:3426/login", method="POST")
    with urllib.request.urlopen(req, timeout=30) as f:
        url = json.load(f)["url"]

    log(f"#{tid} 打开 {url[:80]}")
    # ---- 以下为待联调部分 ----
    # with sync_playwright() as p:
    #     b = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
    #     ctx = b.new_context(...); page = ctx.new_page(); page.goto(url)
    #     ... 邮箱/密码/验证码（复用 run_quick.enter_text）...
    #     iframe = page.query_selector("iframe[src*='threat-mitigation']")
    #     if iframe:
    #         token = solve(iframe.get_attribute("src"), page.url, proxy)
    #         ctx.add_cookies([{"name": "aws-waf-token", "value": token,
    #                           "domain": page.url.split('/')[2], "path": "/"}])
    #         page.reload()
    #     callback = page.url
    #     POST /login/submit {"url": callback}
    raise NotImplementedError("自动授权待联调：需要打码 key 与固定住宅代理")
