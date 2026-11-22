"""AWS WAF captcha 打码接口（框架，未启用）。

AWS WAF captcha 的特点：
  · 页面内嵌 iframe：cdn.<region>.threat-mitigation.aws.amazon.com
  · 解题后拿到 aws-waf-token（写在 cookie 里），注入即可过
  · CapSolver 对应任务类型：AntiAwsWafTask（自带代理）/ AntiAwsWafTaskProxyLess

⚠️ 关键约束：打码用的出口 IP 必须与注册/授权时的 IP 一致，
   否则返回的 token 会被判无效。所以生产环境要用 AntiAwsWafTask + 同一住宅代理。

配置：环境变量 CAPSOLVER_KEY
"""
import json
import os
import time
import urllib.request

API = "https://api.capsolver.com"


class CaptchaUnavailable(RuntimeError):
    pass


def _post(path, payload):
    req = urllib.request.Request(API + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as f:
        return json.load(f)


def solve_aws_waf(captcha_script: str, page_url: str, proxy: str | None = None,
                  timeout: int = 120) -> str:
    """返回 aws-waf-token 字符串。

    captcha_script: iframe 的完整 src（含 ?origin=...）
    page_url:       触发验证的页面地址
    proxy:          http://user:pass@host:port —— 必须与注册出口一致
    """
    key = os.environ.get("CAPSOLVER_KEY")
    if not key:
        raise CaptchaUnavailable("未配置 CAPSOLVER_KEY")

    task = {
        "type": "AntiAwsWafTask" if proxy else "AntiAwsWafTaskProxyLess",
        "websiteURL": page_url,
        "captchaScript": captcha_script,
    }
    if proxy:
        # http://user:pass@host:port
        from urllib.parse import urlparse
        u = urlparse(proxy)
        task["proxyType"] = u.scheme or "http"
        task["proxyAddress"] = u.hostname
        task["proxyPort"] = u.port
        if u.username:
            task["proxyLogin"] = u.username
            task["proxyPassword"] = u.password

    created = _post("/createTask", {"clientKey": key, "task": task})
    if created.get("errorId"):
        raise CaptchaUnavailable(created.get("errorDescription") or "createTask 失败")
    tid = created["taskId"]

    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(3)
        r = _post("/getTaskResult", {"clientKey": key, "taskId": tid})
        if r.get("status") == "ready":
            sol = r.get("solution") or {}
            if sol.get("cookie"):
                return sol["cookie"]
            raise CaptchaUnavailable("返回里没有 cookie")
        if r.get("errorId"):
            raise CaptchaUnavailable(r.get("errorDescription") or "解题失败")
    raise CaptchaUnavailable("打码超时")
