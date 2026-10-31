# Manual test checklist (the parts a script cannot do)

Automated tests cover the logic and Chromium in headless mode (`pytest tests/test_extension.py`). These need a person: real clicks,
real permission prompts, and browsers that are not on the build machine. Tick each box; note anything odd with a screenshot.

Build first: `python scripts/build_extension.py` (folders `dist/chromium` and `dist/firefox`).

## A. Chrome (and Edge: same folder)
Load: `chrome://extensions` (Edge: `edge://extensions`) > Developer mode > Load unpacked > `D:\AI_autonomous_browser\dist\chromium`.
Turn other scraper extensions off while testing.
- [ ] Open https://books.toscrape.com/ and click the icon: a window opens with 20 rows and 7 columns.
- [ ] **Click next page**, Pages 3, Load more rows. A permission prompt names only books.toscrape.com. **Allow**: 60 rows, 3 pages.
- [ ] Repeat after removing the site access (extension Details > Site access). **Deny**: a plain message, nothing broken.
- [ ] Rescan, Max URLs 4, **Scrape each URL**: tabs open and close by themselves; `page upc`, `page price...` columns fill; no tab is left open.
- [ ] https://quotes.toscrape.com/scroll with **Scroll down**, Pages 4: more rows, no repeats.
- [ ] **Page details** shows one row; **Rescan** goes back to the list.
- [ ] Hide a column, rename one, **Copy** and paste into a spreadsheet; **Download CSV** opens in Excel with the £ sign right; **Download JSON**.
- [ ] **Save as template** (name "Books"), close the window, click the icon again: the template is applied and a note says so. **None** detects again. **Export templates**, then **Delete**, then **Import templates** with that file: it is back.
- [ ] A page you are logged into (your own account): the list appears. Do not share that data.
- [ ] A browser page (`chrome://extensions`) or the Web Store: the window says the page cannot be read, with no error screen.
- [ ] Toolbar: the icon and tooltip look right at 16 / 32 / 48 px; the name everywhere is "bizfy Scraper".

## B. Firefox desktop (140 or newer)
Load: `about:debugging` > This Firefox > Load Temporary Add-on > `dist\firefox\manifest.json`.
- [ ] Everything in section A that does not depend on Chrome menus. Pay attention to: the **permission prompt** for next page / scrape each URL (Firefox asks on click), the window opening, downloads, and Copy.
- [ ] `npx web-ext lint --source-dir dist/firefox` shows 0 errors (already true when this file was written).

## C. Firefox for Android (142 or newer)
Load: install Firefox Nightly or Beta, enable the debug menu, use `web-ext run --target=firefox-android` from a PC with USB debugging, or install a signed build.
- [ ] The icon opens the scraper **as a tab** (Android has no popup windows).
- [ ] The layout fits a phone: nothing runs off the side, buttons are easy to tap, the table scrolls sideways.
- [ ] Table, Load more and Download work. Scrape each URL opens background tabs (may behave differently on Android: note it).

## D. Safari
Not built: needs a Mac. See `extension/SAFARI.md`.

## E. Before a store submission
- [ ] The items under "Before you submit" in `extension/store/LISTING.md` are done.
- [ ] Read `extension/PRIVACY.md` once and check that every sentence is still true.
