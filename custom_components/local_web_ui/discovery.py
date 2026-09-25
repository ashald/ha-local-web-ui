"""Which device configuration URLs count as local web UIs."""

from __future__ import annotations

import ipaddress

from yarl import URL

# Names that point at the Home Assistant host itself or its internal services
# (Supervisor, apps). Proxying to them from a URL a device or integration chose
# would turn discovery into a way to reach services that are not device UIs.
_BLOCKED_HOSTS = frozenset(
    {"localhost", "supervisor", "homeassistant", "hassio", "host.docker.internal"}
)
_LOCAL_SUFFIXES = (".local", ".lan", ".home", ".home.arpa", ".internal", ".localdomain")
# Supervisor's internal network on HA OS / Supervised installs
_SUPERVISOR_NETWORK = ipaddress.ip_network("172.30.32.0/23")
# Carrier-grade NAT space, which Tailscale uses for devices on a tailnet
_SHARED_NETWORK = ipaddress.ip_network("100.64.0.0/10")
_METADATA_ADDRESSES = frozenset(
    {ipaddress.ip_address("169.254.169.254"), ipaddress.ip_address("fd00:ec2::254")}
)


def parse_http_url(value: str | None) -> URL | None:
    """Return value as an absolute http(s) URL, or None."""
    if not value:
        return None
    try:
        url = URL(value)
    except ValueError:
        return None
    if url.scheme not in ("http", "https") or not url.host:
        return None
    return url


def is_local_ui_url(url: URL, own_hosts: frozenset[tuple[str, int]] = frozenset()) -> bool:
    """Return True if a discovered URL points at a device on the local network.

    Deliberately conservative: loopback, link-local, cloud metadata, Supervisor's
    network and Home Assistant itself are excluded. Static views are configured
    by an admin and are not subject to this filter.
    """
    host = (url.host or "").rstrip(".").lower()
    if not host or (host, url.port or 0) in own_hosts:
        return False
    try:
        address = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        if host in _BLOCKED_HOSTS or host.startswith(("a0d7b954-", "core-", "local-")):
            return False  # App hostnames on the Supervisor network
        return "." not in host or host.endswith(_LOCAL_SUFFIXES)
    if (
        address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
        or address in _METADATA_ADDRESSES
    ):
        return False
    if address.version == 4 and address in _SUPERVISOR_NETWORK:
        return False
    return address.is_private or (address.version == 4 and address in _SHARED_NETWORK)
