"""AWS Quick 免费版注册流程：P0 侦察出的真实选择器与步骤。

真实执行时用 Playwright；这里只定义"每一步做什么"，与浏览器实现解耦。
所有选择器来自 2026-11-22 实测，见 P0-注册流程侦察.md。
"""

# 入口：必须从 aws.amazon.com/quick/pricing 的真实点击进入，
# workflowStateHandle 不可伪造（裸 URL 返回 400）。
ENTRY = "https://aws.amazon.com/quick/pricing/"
ENTRY_LINK_TEXT = "Create your Quick account"

SEL = {
    # 第 1 步 signin.aws/platform/<id>/login
    "email": "input[type=email]",
    "submit": "[data-testid=test-primary-button]",
    "social_google": "#signup-with-button-Google",
    "social_apple": "#signup-with-button-Apple",
    "social_github": "#signup-with-button-GitHub",
    "social_amazon": "#signup-with-button-Amazon",
    # 第 2 步 profile.aws.amazon.com#/signup/enter-email
    "name": "input[type=text]",          # 页面仅此一个 text input
    "next": "[data-testid=signup-next-button]",
    "change_email": "[data-testid=signup-email-change-button]",
    # 第 3 步 #/signup/verify-otp
    "otp": "input[placeholder='6-digit']",
    "verify": "[data-testid=email-verification-verify-button]",
    "resend": "[data-testid=email-verification-resend-code-button]",
    # 条件步骤 #/signup/captcha（AWS WAF Captcha）
    "captcha_answer": "input[type=text]",
    "captcha_submit": "[data-testid=test-primary-button]",
}

# 前端状态机原文：Start / EnterEmail / VerifyEmail / SetPassword
STEPS = [
    ("page_loaded",       "打开定价页并点击 Create your Quick account"),
    ("email_submitted",   f"填入邮箱并点 {SEL['submit']}"),
    ("identity_loaded",   "到达 profile.aws.amazon.com#/signup/enter-email"),
    ("name_submitted",    f"填入姓名并点 {SEL['next']} → POST /api/send-otp"),
    ("otp_pending",       "等待邮件验证码"),
    ("otp_ok",            f"填码并点 {SEL['verify']}"),
    ("password_set",      "SetPassword 步骤（选择器待补）"),
    ("account_active",    "账号可用"),
]

FAIL_REASONS = {
    "email_blocked":   "ERR-837 / send-otp 400：邮箱域名被拉黑",
    "captcha_stuck":   "AWS WAF Captcha 未通过",
    "pow_stuck":       "Proof-of-Work 未完成（按钮无响应）",
    "otp_timeout":     "验证码超时未收到",
    "fingerprint_flag":"设备指纹被标记",
    "unknown_error":   "未分类错误",
}


def pick_failure(rng, email_domain_trusted: bool):
    """模拟器用：按权重挑一个失败原因。真实执行器不用这个。"""
    weights = [
        ("otp_timeout", 0.30),
        ("captcha_stuck", 0.25),
        ("pow_stuck", 0.15),
        ("fingerprint_flag", 0.15),
        ("unknown_error", 0.15),
    ]
    if not email_domain_trusted:
        weights.insert(0, ("email_blocked", 0.55))
    r = rng.random() * sum(w for _, w in weights)
    acc = 0.0
    for name, w in weights:
        acc += w
        if r <= acc:
            return name
    return "unknown_error"
