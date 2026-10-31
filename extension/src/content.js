// Runs inside the page, after detect.js. Answers the scraper window; it never sends anything anywhere else.
(() => {
  const api = globalThis.browser ?? globalThis.chrome;
  if (globalThis.__scraperContentInstalled) return; // injected again after a reload of the window: keep one listener
  globalThis.__scraperContentInstalled = true;

  api.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (!msg || typeof msg.type !== "string") return undefined;
    if (msg.type === "ping") {
      sendResponse({ ok: true });
      return undefined;
    }
    if (msg.type === "findLists") {
      globalThis.__scrapeDetect(msg.args || {}).then(
        (result) => sendResponse(result),
        (e) => sendResponse({ ok: false, error: String((e && e.message) || e), lists: [] }),
      );
      return true; // the answer comes later
    }
    if (msg.type === "pageDetails") {
      try { sendResponse(globalThis.__pageDetails()); } catch (e) { sendResponse({ ok: false, error: String((e && e.message) || e) }); }
      return undefined;
    }
    return undefined;
  });
})();
