# Safari (Phase 6): prepared, NOT built or tested

Safari extensions can only be built on a **Mac with Xcode**, and shipped through an **Apple Developer account** (paid, yearly). None
of that exists on the Windows development machine, so nothing here has been run. This file is the checklist for the day a Mac is available.

## Steps (on the Mac)
1. Install Xcode (from the App Store) and open it once so it finishes setup. `xcode-select -p` must print a path.
2. Copy the repository, then `python3 scripts/build_extension.py --target chromium` (the Chromium build is the starting point).
3. Convert it:
   ```
   xcrun safari-web-extension-converter dist/chromium --project-location dist/safari --app-name "bizfy Scraper" \
       --bundle-identifier com.bizfylabs.scraper --macos-only --no-open
   ```
   Add `--ios-only` or leave both out for iPhone and iPad (the converter makes an app wrapper for each).
4. Open the generated Xcode project, choose your team under Signing, run. In Safari: Settings > Extensions > turn bizfy Scraper on, and
   (during development) Develop > Allow Unsigned Extensions.
5. For release: archive in Xcode, upload to App Store Connect, submit for review.

## Things to check, because they are likely to differ from Chrome (unverified; I have not run any of this)
- **Windows API**: the scraper opens its own small window (`windows.create` type `popup`). If Safari refuses it, the code already falls back to opening a normal tab (`background.js`), so check that path.
- **Optional host permissions** and `permissions.request` for one site: Safari asks for site access in its own way (per-website "Always allow / Allow for one day"). Check that "Click next page" and "Scrape each URL" still get access, and that a refusal gives the plain message.
- **`scripting.executeScript`** with `files`, **`tabs.onUpdated`** status events and **`tabs.create` with `active: false`** (used by Scrape each URL).
- **Background**: Safari prefers a non-persistent background page; the manifest's Chromium form (`service_worker`) may need to become `"scripts": ["background.js"]` like the Firefox build. If so, add a `safari` target to `scripts/build_extension.py` with its own patch file rather than editing the shared manifest.
- **Storage**: `storage.local` for templates and settings.
- **Downloads**: CSV / JSON download from an extension page (a blob link with `download`) and the clipboard copy.
- **Minimum Safari version**: find the lowest version where all of the above work and put it in the listing.

## What must not change for Safari
The rules stay the same: no login, no CAPTCHA or bot-check bypass, no network calls of its own, no remote code. Apple's review also wants a
privacy policy URL (use `extension/PRIVACY.md`, published) and a privacy "nutrition label" that says **Data Not Collected**.
