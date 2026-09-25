// End-to-end browser test for Local Web UIs against a running Home Assistant.
// Usage: NODE_PATH=$(npm root -g) node browser_e2e.mjs <tokens.json> <screenshot dir>
// Expects: ha_setup.py applied; Porch Light (ESPHome, discovered) and Router (added by hand).
import fs from "node:fs";
import { createRequire } from "node:module";

const { chromium } = createRequire(`${process.env.NODE_PATH}/`)("playwright");
const [tokenFile, outDir] = process.argv.slice(2);
fs.mkdirSync(outDir, { recursive: true });
const saved = JSON.parse(fs.readFileSync(tokenFile, "utf8"));
const HA = saved.hassUrl;
const results = [];
const check = (name, ok, detail = "") => {
  results.push({ name, ok, detail });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? `  (${detail})` : ""}`);
};

const refreshed = await (await fetch(`${HA}/auth/token`, {
  method: "POST",
  body: new URLSearchParams({ grant_type: "refresh_token", refresh_token: saved.refresh_token, client_id: saved.clientId }),
})).json();
const hassTokens = { ...refreshed, refresh_token: saved.refresh_token, hassUrl: HA, clientId: saved.clientId,
                     expires: Date.now() + refreshed.expires_in * 1000 };

const browser = await chromium.launch();
async function newPage(viewport) {
  const context = await browser.newContext({ viewport, colorScheme: "dark" });
  await context.addInitScript((t) => localStorage.setItem("hassTokens", JSON.stringify(t)), hassTokens);
  return context.newPage();
}
const proxied = (page, viewId) => page.frames().find((f) => f.url().includes(`/api/local_web_ui/${viewId}/`));
async function waitFrame(page, viewId) {
  for (let i = 0; i < 75; i++) {
    const f = proxied(page, viewId);
    if (f) return f;
    await page.waitForTimeout(200);
  }
  throw new Error(`no iframe for ${viewId}`);
}
const ws = (page, msg) => page.evaluate((m) => document.querySelector("home-assistant").hass.callWS(m), msg);
const probe = () => {
  const out = {};
  try { out.origin = window.origin; } catch (e) { out.origin = e.name; }
  try { out.haTokens = window.parent.localStorage.getItem("hassTokens") ? "READABLE" : "absent"; }
  catch (e) { out.haTokens = "blocked"; }
  return out;
};

const page = await newPage({ width: 1280, height: 800 });
await page.goto(`${HA}/local-web-ui`);
await page.waitForFunction(() => document.querySelector("home-assistant")?.hass?.connection);
let { views } = await ws(page, { type: "local_web_ui/views" });
const porch = views.find((v) => v.name === "Porch Light");
const router = views.find((v) => v.name === "Router");
check("Porch Light discovered from ESPHome", porch?.source === "discovered", porch?.url);
check("Router added by hand", router?.source === "static", router?.url);
await page.waitForTimeout(1500);
await page.screenshot({ path: `${outDir}/1-list.png` });

// 1) Device page: Visit opens the discovered view
await page.goto(`${HA}/config/devices/device/${porch.device_id}`);
await page.waitForTimeout(2500);
await page.screenshot({ path: `${outDir}/2-device-page.png` });
const visit = page.locator(`a[href="/local-web-ui/${porch.view_id}"]`).first();
check("Device page Visit link points at the view", (await visit.count()) > 0);
await visit.click();
let frame = await waitFrame(page, porch.view_id);
await frame.waitForFunction(() => document.getElementById("uptime")?.textContent.startsWith("uptime"), null, { timeout: 15000 });
await frame.waitForFunction(() => document.getElementById("echo")?.textContent === "websocket open", null, { timeout: 15000 });
check("Porch Light UI loads with live SSE and WebSocket", true, new URL(page.url()).pathname);
const before = await frame.locator("#toggle").innerText();
await frame.locator("#toggle").click();
await frame.waitForFunction((b) => document.getElementById("toggle").textContent !== b, before);
check("POST through the proxy, state back over SSE", true, `${before} -> ${await frame.locator("#toggle").innerText()}`);
const isolation = await frame.evaluate(probe);
check("Isolated: page has an opaque origin and cannot read HA tokens",
      isolation.origin === "null" && isolation.haTokens === "blocked", JSON.stringify(isolation));
await page.waitForTimeout(1200);
await page.screenshot({ path: `${outDir}/3-porch-light.png` });

// 2) Router: sidebar entry, stored Basic credentials, cookie login, redirects,
//    root-relative links, runtime-built URLs, localStorage and document.cookie shims
// Start logged out: forget what the proxy keeps for this site (as the panel menu does)
await ws(page, { type: "local_web_ui/clear_site_data", view_id: router.view_id });
const sidebarPath = `/local-web-ui-${router.view_id.toLowerCase()}`;
await page.goto(`${HA}${sidebarPath}`);
frame = await waitFrame(page, router.view_id);
await frame.waitForSelector("#login", { timeout: 15000 });
check("Router: stored login used (no prompt), redirected to its login page", frame.url().endsWith("/login"), new URL(frame.url()).pathname.replace(/\/[^/]{43}\//, "/<token>/"));
await frame.locator("#login").click();
await page.waitForTimeout(500);
frame = await waitFrame(page, router.view_id);
await frame.waitForFunction(() => document.getElementById("server")?.textContent.startsWith("{"), null, { timeout: 15000 });
const state1 = await frame.evaluate(() => ({
  visits: document.getElementById("visits").textContent,
  theme: document.getElementById("theme").textContent,
  server: JSON.parse(document.getElementById("server").textContent),
}));
check("Router: form login kept in the server-side cookie jar", state1.server.session === true, JSON.stringify(state1.server));
check("Router: fetch('/api/whoami') built at runtime stays in the proxy", typeof state1.server.session === "boolean");
check("Router: document.cookie set by script reaches the device", state1.theme === "dark" && state1.server.theme === "dark");
check("Router: localStorage works in isolated mode", state1.visits === "1", `visits=${state1.visits}`);
await page.waitForTimeout(800); // storage write-back is debounced
await page.reload();
frame = await waitFrame(page, router.view_id);
await frame.waitForFunction(() => document.getElementById("server")?.textContent.startsWith("{"), null, { timeout: 15000 });
const visits2 = await frame.evaluate(() => document.getElementById("visits").textContent);
check("Router: localStorage and login survive a reload (stored server side)", visits2 === "2", `visits=${visits2}`);
await page.waitForTimeout(1500); // let HA's loading splash fade after the reload
await page.screenshot({ path: `${outDir}/4-router-sidebar.png` });

// Save, then reload at once: the next page must see the write, although the
// write can reach Home Assistant after the reloaded page was rendered
for (let round = 1; round <= 3; round++) {
  const value = `saved-${round}-${Date.now()}`;
  await frame.evaluate((v) => { localStorage.setItem("race", v); location.reload(); }, value).catch(() => {});
  await page.waitForTimeout(300);
  frame = await waitFrame(page, router.view_id);
  await frame.waitForFunction(() => document.getElementById("server")?.textContent.startsWith("{"), null, { timeout: 15000 });
  const seen = await frame.evaluate(() => localStorage.getItem("race"));
  check(`Router: localStorage write right before a reload is kept (round ${round})`, seen === value, `${seen}`);
}
const apis = await frame.evaluate(async () => ({
  indexedDB: typeof indexedDB,
  serviceWorker: "serviceWorker" in navigator,
  credentialed: (await fetch("api/whoami", { credentials: "include" })).status,
}));
check("Isolated: throwing APIs removed, credentialed fetch works",
      apis.indexedDB === "undefined" && !apis.serviceWorker && apis.credentialed === 200, JSON.stringify(apis));

// 3) Standalone: open in a new tab (full page, no HA chrome), still isolated
const session = await ws(page, { type: "local_web_ui/session", view_id: router.view_id });
const tab = await page.context().newPage();
await tab.goto(`${HA}${session.url}`);
await tab.waitForFunction(() => document.getElementById("server")?.textContent.startsWith("{"), null, { timeout: 15000 });
const tabState = await tab.evaluate(probe);
check("Standalone tab: logged in, still isolated", tabState.origin === "null", JSON.stringify(tabState));
await tab.screenshot({ path: `${outDir}/5-router-standalone.png` });
await tab.close();

// 4) Per-device link switch: off restores the original Visit URL, on re-links
const deviceUrl = async () => (await ws(page, { type: "config/device_registry/list" })).find((d) => d.id === porch.device_id).configuration_url;
await ws(page, { type: "local_web_ui/set_device_link", device_id: porch.device_id, enabled: false });
const restored = await deviceUrl();
check("Link switch off restores the original Visit URL", restored.replace(/\/$/, "") === porch.url.replace(/\/$/, ""), restored);
await ws(page, { type: "local_web_ui/set_device_link", device_id: porch.device_id, enabled: true });
const relinked = await deviceUrl();
check("Link switch on points Visit back at the view", relinked === `homeassistant://local-web-ui/${porch.view_id}`, relinked);

// 5) Optional "<device> web UI" linked device on the device page
const api = async (path, body) => (await fetch(`${HA}${path}`, {
  method: "POST",
  headers: { Authorization: `Bearer ${hassTokens.access_token}`, "Content-Type": "application/json" },
  body: JSON.stringify(body),
})).json();
const setOptions = async (changes) => {
  const entry = (await ws(page, { type: "config_entries/get", domain: "local_web_ui" }))[0];
  const flow = await api("/api/config/config_entries/options/flow", { handler: entry.entry_id });
  const current = Object.fromEntries(flow.data_schema.map((f) => [f.name, f.default]));
  return api(`/api/config/config_entries/options/flow/${flow.flow_id}`, { ...current, ...changes });
};
await setOptions({ linked_devices: true });
await page.goto(`${HA}/config/devices/device/${porch.device_id}`);
await page.waitForTimeout(3000);
const linkedCard = await page.evaluate(() => {
  let found = false;
  const walk = (root) => root.querySelectorAll("*").forEach((el) => {
    if (el.shadowRoot) walk(el.shadowRoot);
    if (el.textContent?.includes("Porch Light web UI")) found = true;
  });
  walk(document);
  return found;
});
check("Linked devices card shows the Porch Light web UI device", linkedCard);
await page.screenshot({ path: `${outDir}/7-linked-device.png` });
await setOptions({ linked_devices: false });

// 6) Phone width list
const phone = await newPage({ width: 390, height: 844 });
await phone.goto(`${HA}/local-web-ui`);
await phone.waitForTimeout(2500);
await phone.screenshot({ path: `${outDir}/6-phone-list.png` });

await browser.close();
const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
process.exit(failed.length ? 1 : 0);
