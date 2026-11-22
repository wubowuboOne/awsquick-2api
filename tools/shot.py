"""无头截图工具：内嵌字体，解决本机 Chromium 读不到系统字体的问题。

用法：python3 tools/shot.py http://127.0.0.1:8787/ out.png
"""
import base64
import sys

from playwright.sync_api import sync_playwright

FONT = "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf"


def build_css() -> str:
    with open(FONT, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    return (
        "@font-face{font-family:'Embed';"
        f"src:url(data:font/ttf;base64,{b64}) format('truetype');font-weight:100 900}}"
        "html,body,h1,h2,td,th,span,div,button,input,select{font-family:'Embed',sans-serif !important}"
    )


def main(url, out, width=1500, height=950, wait=2500):
    with sync_playwright() as p:
        b = p.firefox.launch()  # 本机 Chromium 字体后端不可用，用 Firefox
        pg = b.new_page(viewport={"width": width, "height": height}, device_scale_factor=2)
        pg.goto(url, wait_until="networkidle")
        pg.add_style_tag(content=build_css())
        pg.wait_for_timeout(wait)
        pg.screenshot(path=out, full_page=True)
        print("saved", out)
        b.close()


if __name__ == "__main__":
    u = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8787/"
    o = sys.argv[2] if len(sys.argv) > 2 else "/tmp/panel.png"
    main(u, o)
