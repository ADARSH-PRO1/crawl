"""Make the store screenshots (1280x800) and the small promo tile from the real extension, on local sample pages.

    python scripts/make_store_assets.py            # writes extension/store/screenshots/*.png

Needs Playwright with a Chromium (the same one the tests use). The sample shop is made up here, so the images contain no
third-party content or branding.
"""
import base64
import functools
import http.server
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import build_extension  # noqa: E402

OUT = ROOT / "extension" / "store" / "screenshots"
NAMES = ["Walnut desk lamp", "Linen throw blanket", "Ceramic pour-over set", "Oak serving board", "Brass bookends",
         "Wool floor cushion", "Glass storage jars", "Cotton bath towels", "Stoneware mug pair", "Rattan wall mirror",
         "Beeswax candle trio", "Slate coasters", "Hand-woven basket", "Enamel kettle", "Bamboo shelf riser", "Terracotta planter",
         "Marble pestle set", "Felt desk mat", "Copper watering can", "Paper lantern", "Teak soap dish", "Jute runner rug",
         "Cast iron trivet", "Porcelain vase"]
PER_PAGE = 8


def product(i: int) -> dict:
    return {"name": NAMES[i - 1], "price": f"£{12 + (i * 7) % 48}.{(i * 13) % 100:02d}", "stars": 3 + i % 3,
            "stock": ("In stock" if i % 4 else "Only 2 left")}


def shop_page(n: int) -> str:
    rows = "".join(
        f'<li class="product"><a class="title" href="/p/{i}.html">{product(i)["name"]}</a><span class="price">{product(i)["price"]}</span>'
        f'<span class="rating">{"★" * product(i)["stars"]}</span></li>' for i in range(PER_PAGE * (n - 1) + 1, PER_PAGE * n + 1))
    nxt = f'<a class="next" href="/shop{n + 1}.html">Next &rarr;</a>' if n < 3 else ""
    return ('<html><head><meta charset="utf-8"><title>Casa Sample: Home goods</title><style>body{font:16px system-ui;margin:40px}'
            'li{list-style:none;padding:10px 0;border-bottom:1px solid #ddd;display:flex;gap:20px}</style></head>'
            f"<body><h1>Home goods</h1><ul>{rows}</ul>{nxt}</body></html>")


def product_page(i: int) -> str:
    p = product(i)
    return (f'<html><head><meta charset="utf-8"><title>{p["name"]} | Casa Sample</title>'
            f'<meta name="description" content="A well made {p["name"].lower()} for everyday use."></head><body><main>'
            f"<h1>{p['name']}</h1><dl><dt>Availability</dt><dd>{p['stock']}</dd><dt>SKU</dt><dd>CS-{1000 + i}</dd>"
            f"<dt>Material</dt><dd>{['Walnut', 'Linen', 'Ceramic', 'Oak', 'Brass', 'Wool'][i % 6]}</dd></dl>"
            f"<p>Ships in two working days.</p></main></body></html>")


def tile_html(icon_uri: str) -> str:
    return (f'<html><body style="margin:0;width:440px;height:280px;background:linear-gradient(135deg,#ecfdf5,#a7f3d0);'
            f'font-family:system-ui,Segoe UI,sans-serif;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:14px">'
            f'<img src="{icon_uri}" width="72" height="72"><div style="font-size:34px;font-weight:800;color:#064e3b">bizfy Scraper</div>'
            f'<div style="font-size:17px;color:#065f46">Any list on the page, as a table. One click.</div></body></html>')


def main() -> int:
    from playwright.sync_api import sync_playwright

    OUT.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp())
    site = work / "site"
    (site / "p").mkdir(parents=True)
    for n in (1, 2, 3):
        (site / f"shop{n}.html").write_text(shop_page(n), encoding="utf-8")
    for i in range(1, PER_PAGE * 3 + 1):
        (site / "p" / f"{i}.html").write_text(product_page(i), encoding="utf-8")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(site))
    handler.log_message = lambda *a, **k: None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    ext = build_extension.build(work / "ext", targets=("chromium",), test=True)["chromium"]

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(str(work / "profile"), channel="chromium", headless=True, viewport={"width": 1280, "height": 800},
                                                    args=[f"--disable-extensions-except={ext}", f"--load-extension={ext}"])
        worker = ctx.service_workers[0] if ctx.service_workers else ctx.wait_for_event("serviceworker", timeout=15000)
        ext_id = worker.url.split("/")[2]

        def open_window(path: str):
            page = ctx.new_page()
            page.goto(f"{base}/{path}")
            tab = worker.evaluate("async (u) => (await chrome.tabs.query({})).find((t) => t.url === u).id", page.url)
            ui = ctx.new_page()
            ui.set_viewport_size({"width": 1280, "height": 800})
            ui.goto(f"chrome-extension://{ext_id}/ui/ui.html?tabId={tab}")
            ui.wait_for_selector("#grid tbody tr")
            return page, ui

        def finished(ui, prefix):
            ui.wait_for_function(f"() => document.getElementById('load-status').textContent.startsWith('{prefix}')", timeout=90000)

        page, ui = open_window("shop1.html")
        ui.screenshot(path=str(OUT / "1-table.png"))
        ui.select_option("#action", "next")
        ui.fill("#pages", "3")
        ui.click("#btn-load")
        finished(ui, "Stopped")
        ui.screenshot(path=str(OUT / "2-load-more.png"))
        ui.fill("#max-urls", "24")  # every row, so the picture has no empty cells
        ui.click("#btn-details")
        finished(ui, "Done")
        ui.evaluate("() => { document.getElementById('grid-wrap').scrollLeft = 330; }")
        ui.screenshot(path=str(OUT / "3-details.png"))
        icon = "data:image/png;base64," + base64.b64encode((ROOT / "extension" / "icons" / "128.png").read_bytes()).decode()
        tile = ctx.new_page()
        tile.set_viewport_size({"width": 440, "height": 280})
        tile.set_content(tile_html(icon))
        tile.screenshot(path=str(OUT / "tile-440x280.png"))
        ctx.close()
    server.shutdown()
    for f in sorted(OUT.glob("*.png")):
        print(f"[assets] {f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
