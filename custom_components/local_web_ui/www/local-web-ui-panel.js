// Local Web UIs: the panel and the dashboard card. Plain web components, no build step.
//
// /local-web-ui               list of web UIs (devices, sites, hidden)
// /local-web-ui/<view_id>     one web UI in an iframe
// /local-web-ui-<view_id>     sidebar entry for one web UI (panel.config.view_id)
// custom:local-web-ui-card    one web UI on a dashboard (this module is loaded on every page)
//
// Admins see and open every web UI; other users the ones shared with them.

const SANDBOX =
  "allow-scripts allow-forms allow-popups allow-popups-to-escape-sandbox allow-modals allow-downloads";
const KEEPALIVE_MS = 60_000;
const PANEL_PATH = "/local-web-ui";

const STYLE = `
  /* Panels get no definite height from their container, so size to the viewport */
  :host { display: flex; flex-direction: column; height: 100vh; height: 100dvh;
          background: var(--primary-background-color); color: var(--primary-text-color);
          font-family: var(--ha-font-family-body, Roboto, sans-serif); }
  .toolbar { display: flex; align-items: center; gap: 4px; flex: none; box-sizing: border-box;
             position: relative;
             height: var(--header-height, 56px); padding: 0 8px;
             background: var(--app-header-background-color); color: var(--app-header-text-color, white);
             border-bottom: var(--app-header-border-bottom, none); }
  .title { flex: 1; min-width: 0; margin-left: 8px; }
  .title .main { font-size: 20px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .title .sub { font-size: 12px; opacity: .75; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  button, a.button { display: inline-flex; align-items: center; justify-content: center; gap: 6px;
           height: 40px; min-width: 40px; padding: 0 8px; border: 0; border-radius: 20px;
           background: none; color: inherit; font: inherit; font-size: 14px; cursor: pointer;
           text-decoration: none; }
  button:hover, a.button:hover { background: rgba(127,127,127,.18); }
  .menu-toggle { display: none; } :host([narrow]) .menu-toggle { display: inline-flex; }
  :host([narrow]) .badge span, :host([narrow]) .toolbar .label { display: none; }
  :host([narrow]) .toolbar .device,
  :host([narrow]) .toolbar .linked,
  :host([narrow]) .toolbar .forget,
  :host([narrow]) .toolbar .newtab { display: none; }
  .toolbar .more { display: none; }
  :host([narrow]) .toolbar .more { display: inline-flex; }
  .popup { position: absolute; right: 8px; top: 52px; z-index: 10; min-width: 220px; padding: 4px 0;
           background: var(--card-background-color); border-radius: 8px;
           box-shadow: 0 4px 16px rgba(0,0,0,.35); border: 1px solid var(--divider-color); }
  .popup button { width: 100%; justify-content: flex-start; border-radius: 0; padding: 10px 16px;
                  gap: 12px; color: var(--primary-text-color); }
  .badge { display: inline-flex; align-items: center; gap: 4px; padding: 2px 10px; border-radius: 12px;
           font-size: 12px; background: rgba(127,127,127,.2); }
  .badge.trusted { background: var(--warning-color, #ffa600); color: #000; }
  iframe { flex: 1 1 auto; min-height: 0; width: 100%; border: 0; background: white; }
  .content { flex: 1; overflow: auto; }
  .inner { max-width: 820px; margin: 0 auto; padding: 16px; box-sizing: border-box; }
  h2 { font-size: 14px; font-weight: 500; text-transform: uppercase; letter-spacing: .05em;
       color: var(--secondary-text-color); margin: 24px 4px 8px; }
  h2:first-child { margin-top: 4px; }
  .row { display: flex; align-items: center; gap: 12px; padding: 10px 8px 10px 16px; margin-bottom: 8px;
         border-radius: var(--ha-card-border-radius, 12px); background: var(--card-background-color);
         border: 1px solid var(--divider-color); position: relative; }
  .row.clickable { cursor: pointer; } .row.clickable:hover { border-color: var(--primary-color); }
  .row ha-icon { color: var(--primary-color); flex: none; }
  .row .text { flex: 1; min-width: 0; }
  .row .name { font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .row .desc { color: var(--secondary-text-color); font-size: 13px; white-space: nowrap;
               overflow: hidden; text-overflow: ellipsis; }
  .chips { display: flex; gap: 6px; flex: none; }
  :host([narrow]) .chips { display: none; }
  .row-actions { display: flex; flex: none; }
  .row-actions button { color: var(--secondary-text-color); }
  .row-actions button:hover { color: var(--primary-color); }
  .message { padding: 32px 16px; text-align: center; color: var(--secondary-text-color); line-height: 1.5; }
  .actions { display: flex; gap: 8px; justify-content: flex-end; flex-wrap: wrap; margin: 4px 0 8px; }
  .actions a.button { border: 1px solid var(--divider-color); color: var(--primary-color); }
  a.notice { display: flex; gap: 8px; align-items: center; padding: 10px 14px; margin: 0 0 8px;
             border-radius: 8px; background: rgba(3,169,244,.12); color: inherit; text-decoration: none; }
  .actions .hint { flex: 1; align-self: center; font-size: 13px; color: var(--secondary-text-color); }
  details summary { cursor: pointer; color: var(--secondary-text-color); margin: 24px 4px 8px; }
  code { background: rgba(127,127,127,.18); padding: 1px 4px; border-radius: 4px; }
`;

// For text and for quoted attribute values: names come from devices, not only admins
const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const SETTINGS_PATH = "/config/integrations/integration/local_web_ui";
const esc = (text) => String(text ?? "").replace(/[&<>"']/g, (c) => ESCAPES[c]);
const icon = (name, fallback = "") =>
  customElements.get("ha-icon") ? `<ha-icon icon="${esc(name)}"></ha-icon>` : fallback;

class LocalWebUiPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._key = null;
    this._session = null;
    this._timer = null;
    this._onDocClick = () => this._closePopups();
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) this._update();
  }
  set narrow(narrow) { this.toggleAttribute("narrow", !!narrow); }
  set route(route) { this._route = route; if (this._hass) this._update(); }
  set panel(panel) { this._panel = panel; if (this._hass) this._update(); }

  connectedCallback() {
    document.addEventListener("click", this._onDocClick);
    if (this._hass) this._update();
  }
  disconnectedCallback() {
    document.removeEventListener("click", this._onDocClick);
    clearInterval(this._timer);
    this._timer = null;
    this._key = null;
  }

  get _singleView() { return this._panel?.config?.view_id || null; }

  _closePopups() {
    this.shadowRoot?.querySelectorAll(".popup").forEach((p) => p.remove());
  }

  _update() {
    this._closePopups();
    const viewId = this._singleView || (this._route?.path || "").split("/").filter(Boolean)[0] || null;
    const key = viewId ? `view:${viewId}` : "list";
    if (key === this._key) return;
    this._key = key;
    clearInterval(this._timer);
    this._timer = null;
    this._session = null;
    if (viewId) this._showView(viewId);
    else this._showList();
  }

  _navigate(path) {
    history.pushState(null, "", path);
    window.dispatchEvent(new CustomEvent("location-changed"));
  }

  _toolbar(title, subtitle, actions = "", back = false) {
    return `<style>${STYLE}</style>
      <div class="toolbar">
        <button class="menu-toggle" title="Menu">${icon("mdi:menu", "☰")}</button>
        ${back ? `<button class="back" title="Back">${icon("mdi:arrow-left", "←")}</button>` : ""}
        <div class="title"><div class="main">${esc(title)}</div>${subtitle ? `<div class="sub">${esc(subtitle)}</div>` : ""}</div>
        ${actions}
      </div>`;
  }

  _bindToolbar() {
    const root = this.shadowRoot;
    root.querySelector(".menu-toggle").addEventListener("click", () =>
      this.dispatchEvent(new Event("hass-toggle-menu", { bubbles: true, composed: true })));
    root.querySelector(".back")?.addEventListener("click", () => this._navigate(this._backTarget || PANEL_PATH));
  }

  // ---- list ----------------------------------------------------------------

  get _admin() { return !!this._hass?.user?.is_admin; }

  async _showList() {
    const key = this._key;
    const settings = this._admin
      ? `<a class="button" href="${SETTINGS_PATH}" title="Add web UIs and change options">${icon("mdi:cog", "⚙")}<span class="label">Settings</span></a>`
      : "";
    this.shadowRoot.innerHTML =
      this._toolbar("Local Web UIs", "", settings) +
      `<div class="content"><div class="inner"><div class="message">Loading…</div></div></div>`;
    this._bindToolbar();
    let result;
    try {
      result = await this._hass.callWS({ type: "local_web_ui/views" });
    } catch (err) {
      if (this._key === key) this._inner().innerHTML = `<div class="message">${esc(err.message || err)}</div>`;
      return;
    }
    if (this._key !== key) return;
    this._views = result;
    this._renderList();
  }

  _inner() { return this.shadowRoot.querySelector(".inner"); }

  _renderList() {
    const { views, discovery, discovered } = this._views;
    const section = (title, list) =>
      list.length ? `<h2>${esc(title)}</h2>${list.map((v) => this._row(v)).join("")}` : "";
    if (!this._admin) {
      this._inner().innerHTML = views.length
        ? section("Web UIs", views)
        : `<div class="message">No web UIs are shared with you yet. An admin can share one in its settings.</div>`;
      this._bindRows(views);
      return;
    }
    const devices = views.filter((v) => v.device_id);
    const sites = views.filter((v) => !v.device_id);
    let html = `<div class="actions"><span class="hint">Each web UI is an entry of the integration:
      its settings are behind the ⚙ button on its row.</span>
      <a class="button" href="${SETTINGS_PATH}">${icon("mdi:plus", "+")} Add a web UI</a></div>`;
    if (discovered) {
      html += `<a class="notice" href="${SETTINGS_PATH}">${icon("mdi:magnify", "🔍")}
        <span>${discovered === 1 ? "1 discovered web UI is" : `${discovered} discovered web UIs are`}
        waiting to be added or ignored.</span></a>`;
    }
    html += section("Devices", devices) + section("Sites", sites);
    if (!views.length) {
      html += `<div class="message">No web UIs yet.<br>${
        discovery
          ? "Devices whose integration links to a local web page (for example ESPHome with <code>web_server</code>) are offered under Discovered, on the integration's page."
          : "Discovery is off in the integration options."
      }<br>You can also add any local site with "Add a web UI".</div>`;
    }
    this._inner().innerHTML = html;
    this._bindRows(views);
  }

  _bindRows(views) {
    this._inner().querySelectorAll(".row").forEach((row) => {
      const view = views.find((v) => v.view_id === row.dataset.id);
      row.addEventListener("click", (ev) => {
        if (ev.target.closest("button, a")) return;
        this._fromList = true;
        this._navigate(`${PANEL_PATH}/${view.view_id}`);
      });
      row.querySelectorAll(".row-actions button").forEach((button) =>
        button.addEventListener("click", (ev) => {
          ev.stopPropagation();
          this._rowAction(button.dataset.act, view);
        }));
    });
  }

  _row(v) {
    const desc = [v.area, v.subtitle].filter(Boolean).join(" · ");
    const chips = v.mode === "trusted" ? `<span class="badge trusted">Trusted</span>` : "";
    const glyph = v.icon || (v.device_id ? "mdi:devices" : "mdi:web");
    return `<div class="row clickable" data-id="${esc(v.view_id)}">
      ${icon(glyph)}
      <div class="text"><div class="name">${esc(v.name)}</div><div class="desc">${esc(desc)}</div></div>
      <div class="chips">${chips}</div>
      <div class="row-actions">
        <button data-act="open-tab" title="Open in a new tab">${icon("mdi:open-in-new", "↗")}</button>
        ${v.device_id ? `<button data-act="device" title="Device page">${icon("mdi:devices", "▣")}</button>` : ""}
        ${this._admin ? `<button data-act="settings" title="Settings (mode, login, who can open it…)">${icon("mdi:cog", "⚙")}</button>` : ""}
      </div>
    </div>`;
  }

  async _rowAction(act, view) {
    const ws = (msg) => this._hass.callWS(msg);
    try {
      if (act === "open-tab") {
        const tab = window.open("about:blank", "_blank");
        try {
          const s = await ws({ type: "local_web_ui/session", view_id: view.view_id });
          if (tab) tab.location = s.url;
        } catch (err) {
          tab?.close();
          throw err;
        }
        return;
      }
      if (act === "settings") return this._navigate(`${SETTINGS_PATH}#config_entry=${view.entry_id}`);
      if (act === "device") return this._navigate(`/config/devices/device/${view.device_id}`);
    } catch (err) {
      alert(err.message || err);
    }
  }

  // ---- one view ------------------------------------------------------------

  async _openSession(viewId, token) {
    return this._hass.callWS({ type: "local_web_ui/session", view_id: viewId, ...(token ? { token } : {}) });
  }

  async _showView(viewId) {
    const key = this._key;
    const back = !this._singleView;
    // Back leads to where the view was opened from: the list, or else the device
    // page whose "Visit" button leads here
    const fromList = this._fromList;
    this._fromList = false;
    this._backTarget = null;
    this.shadowRoot.innerHTML = this._toolbar("Local Web UIs", "", "", back) +
      `<div class="content"><div class="message">Connecting…</div></div>`;
    this._bindToolbar();
    let session;
    try {
      session = await this._openSession(viewId);
    } catch (err) {
      if (this._key === key) this.shadowRoot.querySelector(".message").textContent = err.message || String(err);
      return;
    }
    if (this._key !== key) return;
    this._session = session;
    const view = session.view;
    if (!fromList && view.device_id) this._backTarget = `/config/devices/device/${view.device_id}`;
    const isolated = view.mode !== "trusted";
    const badge = isolated
      ? `<span class="badge" title="Isolated: this page cannot access Home Assistant">${icon("mdi:shield-lock", "🔒")}<span>Isolated</span></span>`
      : `<span class="badge trusted" title="Trusted: this page runs with Home Assistant's origin and can act as you">${icon("mdi:shield-alert", "⚠")}<span>Trusted</span></span>`;
    this.shadowRoot.innerHTML =
      this._toolbar(view.name, [view.area, view.subtitle].filter(Boolean).join(" · "),
        `${badge}
         ${view.device_id ? `<button class="device" title="Device page">${icon("mdi:devices", "▣")}</button>` : ""}
         ${view.linked_device_id ? `<button class="linked" title="Web UI device page">${icon("mdi:link-variant", "⛓")}</button>` : ""}
         <button class="forget" title="Forget my saved logins and data">${icon("mdi:cookie-remove", "✕")}</button>
         <button class="reload" title="Reload">${icon("mdi:refresh", "⟳")}</button>
         <button class="newtab" title="Open in a new tab">${icon("mdi:open-in-new", "↗")}</button>
         <button class="more" title="More">${icon("mdi:dots-vertical", "⋮")}</button>`,
        back) +
      `<iframe title="${esc(view.name)}" src="${esc(session.url)}" ${isolated ? `sandbox="${SANDBOX}"` : ""}
               allow="fullscreen; clipboard-write" referrerpolicy="same-origin"></iframe>`;
    this._bindToolbar();
    const root = this.shadowRoot;
    root.querySelector(".more")?.addEventListener("click", (ev) => {
      ev.stopPropagation();
      const existing = root.querySelector(".popup");
      if (existing) { existing.remove(); return; }
      const popup = document.createElement("div");
      popup.className = "popup";
      let items = `<button data-act="newtab">${icon("mdi:open-in-new", "↗")} Open in a new tab</button>`;
      if (view.device_id) items += `<button data-act="device">${icon("mdi:devices", "▣")} Device page</button>`;
      if (view.linked_device_id) items += `<button data-act="linked">${icon("mdi:link-variant", "⛓")} Web UI device page</button>`;
      items += `<button data-act="forget">${icon("mdi:cookie-remove", "✕")} Forget my saved data</button>`;
      popup.innerHTML = items;
      popup.addEventListener("click", (e) => {
        e.stopPropagation();
        const act = e.target.closest("button")?.dataset.act;
        popup.remove();
        if (act === "newtab") root.querySelector(".newtab")?.click();
        else if (act === "device") root.querySelector(".device")?.click();
        else if (act === "linked") root.querySelector(".linked")?.click();
        else if (act === "forget") root.querySelector(".forget")?.click();
      });
      root.querySelector(".toolbar").appendChild(popup);
    });
    root.querySelector(".device")?.addEventListener("click", () =>
      this._navigate(`/config/devices/device/${view.device_id}`));
    root.querySelector(".linked")?.addEventListener("click", () =>
      this._navigate(`/config/devices/device/${view.linked_device_id}`));
    root.querySelector(".reload").addEventListener("click", () => {
      root.querySelector("iframe").src = this._session.url;
    });
    root.querySelector(".forget").addEventListener("click", async () => {
      if (!confirm(`Forget the cookies and stored data ${view.name} keeps for you? You may have to log in to it again.`)) return;
      try {
        await this._hass.callWS({ type: "local_web_ui/clear_site_data", view_id: view.view_id });
        root.querySelector("iframe").src = this._session.url;
      } catch (err) {
        alert(err.message || err);
      }
    });
    // A tab of its own gets its own session, separate from the iframe's
    root.querySelector(".newtab").addEventListener("click", async () => {
      const tab = window.open("about:blank", "_blank");
      try {
        const own = await this._openSession(viewId);
        if (tab) tab.location = own.url;
      } catch (err) {
        tab?.close();
        alert(err.message || err);
      }
    });
    // Keep the session alive while the view is open, even if the page goes quiet
    this._timer = setInterval(async () => {
      try {
        const next = await this._openSession(viewId, this._session.token);
        if (next.token !== this._session.token) {
          this._session = next;
          root.querySelector("iframe").src = next.url;
        }
      } catch {
        /* HA unreachable or the view is gone; the iframe shows the proxy's error */
      }
    }, KEEPALIVE_MS);
  }
}

// ---- dashboard card ----------------------------------------------------------
//
//   type: custom:local-web-ui-card
//   view_id: 01ABC…          the web UI (its entry id; the card editor offers a list)
//   title: Printer            optional card header
//   aspect_ratio: 75%         height as a share of the width (or 16:9); in a sections or
//                             panel view the card fills the space it is given instead
//
// It opens a session of its own, like the panel, keeps it alive while it is shown and
// renews it when it runs out. Isolated web UIs get the same sandbox as in the panel.

const CARD_TYPE = "local-web-ui-card";
const DEFAULT_ASPECT_RATIO = "75%";

// "75%", "16:9", "16x9" → padding-top percentage, or null if invalid
function aspectPercent(value) {
  const text = String(value ?? DEFAULT_ASPECT_RATIO).trim();
  let m = text.match(/^(\d+(?:\.\d+)?)\s*%$/);
  if (m) return Number(m[1]) > 0 ? Number(m[1]) : null;
  m = text.match(/^(\d+(?:\.\d+)?)\s*[:x]\s*(\d+(?:\.\d+)?)$/i);
  if (m && Number(m[1]) > 0 && Number(m[2]) > 0) return (Number(m[2]) / Number(m[1])) * 100;
  return null;
}

const CARD_STYLE = `
  :host { display: block; height: 100%; }
  ha-card, .card { overflow: hidden; height: 100%; width: 100%; display: flex; flex-direction: column;
                   box-sizing: border-box; }
  .frame { position: relative; width: 100%; flex: none; }
  /* Sections (grid) and panel views give the card its height: fill it */
  :host([layout="grid"]) .frame, :host([layout="panel"]) .frame {
    flex: 1 1 auto; padding-top: 0 !important; min-height: 0; }
  iframe { position: absolute; inset: 0; width: 100%; height: 100%; border: 0; background: white; }
  .message { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
             padding: 16px; text-align: center; color: var(--secondary-text-color); }
`;

class LocalWebUiCard extends HTMLElement {
  static getConfigElement() {
    return document.createElement(`${CARD_TYPE}-editor`);
  }

  static async getStubConfig(hass) {
    try {
      const { views } = await hass.callWS({ type: "local_web_ui/views" });
      if (views.length) return { view_id: views[0].view_id };
    } catch {
      /* not set up: the editor says so */
    }
    return { view_id: "" };
  }

  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = null;
    this._session = null;
    this._timer = null;
    this._generation = 0;
  }

  setConfig(config) {
    if (!config || typeof config !== "object") throw new Error("Invalid configuration");
    if (config.aspect_ratio != null && aspectPercent(config.aspect_ratio) === null) {
      throw new Error(`Invalid aspect_ratio "${config.aspect_ratio}": use a percentage (75%) or a ratio (16:9)`);
    }
    const changed = !this._config || this._config.view_id !== config.view_id;
    this._config = { ...config };
    if (changed) this._restart();
    else this._render();
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) this._restart();
  }

  // Set by Home Assistant: "grid" in a sections view, "panel" in a panel view
  set layout(layout) {
    if (layout) this.setAttribute("layout", layout);
    else this.removeAttribute("layout");
  }

  getCardSize() {
    return Math.max(3, Math.round((aspectPercent(this._config?.aspect_ratio) ?? 75) / 12.5));
  }

  getGridOptions() {
    return { columns: "full", rows: 6, min_rows: 2, min_columns: 3 };
  }

  connectedCallback() { this._restart(); }

  disconnectedCallback() {
    clearInterval(this._timer);
    this._timer = null;
    this._generation++;
  }

  _render(message) {
    const config = this._config || {};
    const ratio = aspectPercent(config.aspect_ratio) ?? 75;
    const session = message ? null : this._session;
    const view = session?.view;
    const isolated = view?.mode !== "trusted";
    const card = customElements.get("ha-card") ? "ha-card" : "div";
    const body = session
      ? `<iframe title="${esc(view.name)}" src="${esc(session.url)}" ${isolated ? `sandbox="${SANDBOX}"` : ""}
                 allow="fullscreen; clipboard-write" referrerpolicy="same-origin"></iframe>`
      : `<div class="message">${esc(message || "Connecting…")}</div>`;
    this.shadowRoot.innerHTML = `<style>${CARD_STYLE}</style>
      <${card} class="card" ${config.title ? `header="${esc(config.title)}"` : ""}>
        <div class="frame" style="padding-top: ${ratio}%">${body}</div>
      </${card}>`;
  }

  async _restart() {
    clearInterval(this._timer);
    this._timer = null;
    const generation = ++this._generation;
    this._session = null;
    if (!this.isConnected || !this._hass || !this._config) return;
    const viewId = this._config.view_id;
    if (!viewId) {
      this._render("Choose a web UI in the card's settings.");
      return;
    }
    this._render();
    try {
      this._session = await this._open(viewId);
    } catch (err) {
      if (generation === this._generation) this._render(err?.message || String(err));
      return;
    }
    if (generation !== this._generation) return;
    this._render();
    this._timer = setInterval(async () => {
      try {
        const next = await this._open(viewId, this._session.token);
        if (generation !== this._generation) return;
        if (next.token !== this._session.token) {
          const previous = this._session;
          this._session = next;
          const frame = this.shadowRoot.querySelector("iframe");
          // Same sandbox: just point the frame at the new session
          if (frame && next.view.mode === previous.view.mode) frame.src = next.url;
          else this._render();
        }
      } catch {
        /* Home Assistant unreachable for now: try again on the next beat */
      }
    }, KEEPALIVE_MS);
  }

  _open(viewId, token) {
    return this._hass.callWS({ type: "local_web_ui/session", view_id: viewId, ...(token ? { token } : {}) });
  }
}

class LocalWebUiCardEditor extends HTMLElement {
  setConfig(config) {
    this._config = { ...config };
    this._render();
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) this._load();
    if (this._form) this._form.hass = hass;
  }

  async _load() {
    try {
      this._views = (await this._hass.callWS({ type: "local_web_ui/views" })).views;
    } catch (err) {
      this._error = err?.message || String(err);
    }
    this._render();
  }

  _render() {
    if (!this._hass || !this._config) return;
    if (this._error || (this._views && !this._views.length)) {
      this.innerHTML = `<p>${esc(this._error || "There are no web UIs you can open yet.")}</p>`;
      this._form = null;
      return;
    }
    if (!this._views) return;
    if (!this._form) {
      this.innerHTML = "";
      this._form = document.createElement("ha-form");
      this._form.computeLabel = (field) =>
        ({ view_id: "Web UI", title: "Title", aspect_ratio: "Aspect ratio" })[field.name];
      this._form.computeHelper = (field) =>
        field.name === "aspect_ratio"
          ? `Height as a share of the width, such as 75% (the default) or 16:9. In a sections or panel view the card fills the space it is given.`
          : undefined;
      this._form.addEventListener("value-changed", (ev) => {
        ev.stopPropagation();
        const config = { ...ev.detail.value };
        for (const key of ["title", "aspect_ratio"]) if (!config[key]) delete config[key];
        this._config = config;
        this.dispatchEvent(new CustomEvent("config-changed", { detail: { config }, bubbles: true, composed: true }));
      });
      this.appendChild(this._form);
    }
    this._form.hass = this._hass;
    this._form.schema = [
      {
        name: "view_id",
        required: true,
        selector: {
          select: {
            mode: "dropdown",
            options: this._views.map((v) => ({ value: v.view_id, label: v.name })),
          },
        },
      },
      { name: "title", selector: { text: {} } },
      { name: "aspect_ratio", selector: { text: {} } },
    ];
    this._form.data = this._config;
  }
}

const ELEMENTS = [
  ["local-web-ui-panel", LocalWebUiPanel],
  [CARD_TYPE, LocalWebUiCard],
  [`${CARD_TYPE}-editor`, LocalWebUiCardEditor],
];
function defineElements() {
  const registry = window.customElements;
  for (const [name, cls] of ELEMENTS) if (!registry.get(name)) registry.define(name, cls);
}
// This module is loaded on every page as early as Home Assistant's own app (for the card),
// and the app replaces window.customElements with a scoped registry polyfill as it starts.
// Elements defined before that swap are lost to it, so define them once the app is up, in
// the registry it ends up with. Loaded later (the panel), this runs right away.
window.customElements.whenDefined("home-assistant").then(defineElements);

window.customCards = window.customCards || [];
if (!window.customCards.some((card) => card.type === CARD_TYPE)) {
  window.customCards.push({
    type: CARD_TYPE,
    name: "Local Web UI",
    description: "A web UI from Local Web UIs: a device's or a site's own page, isolated like in the sidebar.",
    preview: false,
    documentationURL: "https://github.com/ashald/ha-local-web-ui#dashboard-card",
  });
}
