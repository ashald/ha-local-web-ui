// Local Web UIs panel. Plain web component, no build step.
//
// /local-web-ui               list of web UIs (devices, sites, hidden)
// /local-web-ui/<view_id>     one web UI in an iframe
// /local-web-ui-<view_id>     sidebar entry for one web UI (panel.config.view_id)

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
    if (this._hass) this._update();
  }
  disconnectedCallback() {
    clearInterval(this._timer);
    this._timer = null;
    this._key = null;
  }

  get _singleView() { return this._panel?.config?.view_id || null; }

  _update() {
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

  async _showList() {
    const key = this._key;
    this.shadowRoot.innerHTML =
      this._toolbar("Local Web UIs", "", `<a class="button" href="/config/integrations/integration/local_web_ui"
        title="Add web UIs and change options">${icon("mdi:cog", "⚙")}<span class="label">Settings</span></a>`) +
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
    const devices = views.filter((v) => v.device_id);
    const sites = views.filter((v) => !v.device_id);
    const section = (title, list) =>
      list.length ? `<h2>${esc(title)}</h2>${list.map((v) => this._row(v)).join("")}` : "";
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
        <button data-act="settings" title="Settings (mode, login, Visit button…)">${icon("mdi:cog", "⚙")}</button>
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
         <button class="newtab" title="Open in a new tab">${icon("mdi:open-in-new", "↗")}</button>`,
        back) +
      `<iframe title="${esc(view.name)}" src="${esc(session.url)}" ${isolated ? `sandbox="${SANDBOX}"` : ""}
               allow="fullscreen; clipboard-write" referrerpolicy="same-origin"></iframe>`;
    this._bindToolbar();
    const root = this.shadowRoot;
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

customElements.define("local-web-ui-panel", LocalWebUiPanel);
