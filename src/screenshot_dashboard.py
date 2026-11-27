"""Render each dashboard page to a PNG for the README (headless Chromium via Playwright).

    python src/screenshot_dashboard.py      -> images/11_dashboard_overview.png ... 14_dashboard_settlements.png

The CDN request for plotly.js is served from the copy bundled with the plotly Python package,
so this works offline and always matches the version the dashboard was built with.
"""
from pathlib import Path

import plotly
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "dashboard" / "reconciliation_dashboard.html"
LOCAL_PLOTLY = Path(plotly.__file__).parent / "package_data" / "plotly.min.js"
PAGES = [("p1", "11_dashboard_overview"), ("p2", "12_dashboard_exceptions"),
         ("p3", "13_dashboard_fees"), ("p4", "14_dashboard_settlements")]

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=1)
    page.route("https://cdn.plot.ly/**", lambda r: r.fulfill(path=str(LOCAL_PLOTLY),
                                                             content_type="application/javascript"))
    page.goto(HTML.as_uri())
    page.wait_for_function("window.Plotly !== undefined")
    for pid, name in PAGES:
        page.evaluate(f"show('{pid}')")
        page.wait_for_timeout(700)
        out = ROOT / "images" / f"{name}.png"
        page.locator(f"#{pid}").screenshot(path=str(out))
        print(f"saved {out.relative_to(ROOT)}")
    browser.close()
