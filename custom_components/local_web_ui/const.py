"""Constants for Local Web UIs."""

from typing import Final

DOMAIN: Final = "local_web_ui"
NAME: Final = "Local Web UIs"

# Proxied views live at <prefix>/<view_id>/<session token>/<path>
PROXY_URL_PREFIX: Final = "/api/local_web_ui"
PANEL_URL_PATH: Final = "local-web-ui"
STATIC_URL_PATH: Final = "/local_web_ui_static"
PANEL_COMPONENT: Final = "local-web-ui-panel"
# Device pages link here: homeassistant://local-web-ui/<view_id>
DEVICE_LINK_PREFIX: Final = f"homeassistant://{PANEL_URL_PATH}/"

# Paths under a view that the proxy answers itself instead of forwarding
INTERNAL_PATH_PREFIX: Final = "__lwu/"

SUBENTRY_TYPE_VIEW: Final = "view"
DISCOVERED_PREFIX: Final = "d_"

CONF_URL: Final = "url"
CONF_MODE: Final = "mode"
CONF_VERIFY_SSL: Final = "verify_ssl"
CONF_USERNAME: Final = "username"
CONF_PASSWORD: Final = "password"
CONF_SHOW_IN_SIDEBAR: Final = "show_in_sidebar"
CONF_ICON: Final = "icon"
CONF_DEVICE_ID: Final = "device_id"
CONF_TRUSTED_ACK: Final = "trusted_acknowledged"

CONF_DISCOVERY: Final = "discovery"
CONF_LINK_DEVICE_PAGES: Final = "link_device_pages"
DEFAULT_DISCOVERY: Final = True
DEFAULT_LINK_DEVICE_PAGES: Final = True

MODE_ISOLATED: Final = "isolated"
MODE_TRUSTED: Final = "trusted"
MODES: Final = (MODE_ISOLATED, MODE_TRUSTED)

# A session expires this long after its last proxied request or panel keepalive
SESSION_TTL: Final = 300.0

STORAGE_VERSION: Final = 1
STORAGE_KEY: Final = DOMAIN
STORAGE_KEY_JAR: Final = f"{DOMAIN}.jar"
# Per (user, view) cap for the emulated localStorage of isolated views
MAX_SHIM_STORAGE_BYTES: Final = 1024 * 1024
