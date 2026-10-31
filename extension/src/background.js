// Toolbar click: put the scraper into the page the user is looking at, then open our window next to it.
// Chrome and Firefox share this file; `browser` (Firefox) and `chrome` (Chromium) both give promises in MV3.
const api = globalThis.browser ?? globalThis.chrome;

const WINDOW = { type: "popup", width: 1040, height: 740 };

api.action.onClicked.addListener(async (tab) => {
  if (tab?.id == null) return;
  try {
    // activeTab grants access to this tab for this click only; nothing else is read.
    await api.scripting.executeScript({ target: { tabId: tab.id }, files: ["detect.js", "pagedetails.js", "content.js"] });
  } catch (e) {
    // Browser pages and stores cannot be scripted. The window below explains it to the user.
  }
  const url = api.runtime.getURL(`ui/ui.html?tabId=${tab.id}`);
  try {
    await api.windows.create({ url, ...WINDOW });
  } catch (e) {
    await api.tabs.create({ url }); // no windows API (Firefox for Android)
  }
});
