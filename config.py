"""集中配置：读 config.json，环境变量优先。

新用户只需复制 config.example.json 为 config.json 再改几项，
不需要动代码里的任何常量。
"""
import json
import os

DEFAULTS = {
    "panel": {"host": "127.0.0.1", "port": 8787, "debug": False},
    "gateway": {"host": "127.0.0.1", "port": 3426},
    "registration": {"concurrency": 1, "target": 1, "headless": True,
                     "task_timeout_s": 600, "retries_per_task": 1},
    "mail": {"provider": "cloudflare", "domain": "", "worker_url": "",
             "api_token": "", "custom_url": "", "manual_mails": []},
    "proxy": {
        "registration": {"enabled": False, "list": []},
        "gateway": {"enabled": False, "list": []},
    },
    "quota": {"stop_percent": 95, "warn_percent": 60},
    "pool_import": {"headful": True},
    "gateway_behavior": {"default_model": "fast", "max_tokens": 4096,
                         "allow_stream": True, "cors": True},
}

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def _merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load():
    cfg = dict(DEFAULTS)
    try:
        with open(_PATH, encoding="utf-8") as f:
            cfg = _merge(cfg, json.load(f))
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[config] 读取失败，使用默认值：{e}")
    # 环境变量覆盖
    env = os.environ
    if env.get("PANEL_PORT"):
        cfg["panel"]["port"] = int(env["PANEL_PORT"])
    if env.get("GW_PORT"):
        cfg["gateway"]["port"] = int(env["GW_PORT"])
    if env.get("MAIL_DOMAIN"):
        cfg["mail"]["domain"] = env["MAIL_DOMAIN"]
    if env.get("WORKER_URL"):
        cfg["mail"]["worker_url"] = env["WORKER_URL"]
    if env.get("WORKER_TOKEN"):
        cfg["mail"]["api_token"] = env["WORKER_TOKEN"]
    return cfg


CFG = load()


def gateway_base():
    g = CFG["gateway"]
    return f"http://{g['host']}:{g['port']}"


def proxy_block(scope):
    """scope: registration | gateway"""
    blk = (CFG.get("proxy") or {}).get(scope) or {}
    return {"enabled": bool(blk.get("enabled")), "list": list(blk.get("list") or [])}


def proxies(scope="registration", enabled_only=True):
    blk = proxy_block(scope)
    if not blk["enabled"]:
        return []
    lst = [p for p in blk["list"] if p.get("url")]
    if enabled_only:
        lst = [p for p in lst if p.get("enabled", True)]
    return lst
