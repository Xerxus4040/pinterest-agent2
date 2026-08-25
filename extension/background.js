// This extension intentionally does not read Pinterest session cookies.
// Authentication is handled by the web app through Pinterest OAuth.
// The service worker only provides a tiny message bridge for the dashboard.

const BACKEND = "https://YOUR-DOMAIN.vercel.app";

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type === "backend-health") {
    fetch(BACKEND + "/api/health", {credentials:"include"})
      .then(async r => sendResponse({ok:r.ok, status:r.status, body:await r.text()}))
      .catch(() => sendResponse({ok:false, error:"Backend Server Unreachable"}));
    return true;
  }
});
