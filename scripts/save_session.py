"""One-time login helper: opens a real browser, you sign in by hand, the session is saved for the crawler.

  python scripts/save_session.py https://example.app/login session.json

Then upload session.json in the UI ('Logged-in site'), or set CRAWL_STORAGE_STATE_FILE=session.json.
The file holds live credentials: keep it out of git and delete it when done.
"""
import sys

from playwright.sync_api import sync_playwright


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    url, out = sys.argv[1:]
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        context.new_page().goto(url)
        input("Sign in in the opened browser window, then press Enter here to save the session... ")
        context.storage_state(path=out)
        browser.close()
    print(f"Saved session to {out}")


if __name__ == "__main__":
    main()
