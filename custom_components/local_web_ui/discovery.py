"""Which device configuration URLs count as local web UIs."""

from __future__ import annotations

import ipaddress
import socket

from aiohttp.abc import AbstractResolver, ResolveResult
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


def is_lan_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True for addresses of devices on the local network."""
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
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


def is_local_ui_url(url: URL, own_hosts: frozenset[tuple[str, int]] = frozenset()) -> bool:
    """Return True if a discovered URL points at a device on the local network.

    Deliberately conservative: loopback, link-local, cloud metadata, Supervisor's
    network and Home Assistant itself are excluded. Names are checked again once
    resolved (LanOnlyResolver), since a name can resolve anywhere. Static views
    are configured by an admin and are not subject to this filter.
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
    return is_lan_address(address)


class LanOnlyResolver(AbstractResolver):
    """Resolve through another resolver, keeping only local network addresses.

    Used for discovered web UIs, whose URLs come from devices and integrations:
    the check applies to the very addresses the connection is made to, so a name
    cannot be pointed at Home Assistant itself or its internal network.
    """

    def __init__(self, resolver: AbstractResolver) -> None:
        self._resolver = resolver

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[ResolveResult]:
        results = await self._resolver.resolve(host, port, family)
        allowed = []
        for result in results:
            try:
                address = ipaddress.ip_address(result["host"].split("%", 1)[0])
            except ValueError:
                continue
            if is_lan_address(address):
                allowed.append(result)
        if not allowed:
            raise OSError(f"{host} does not resolve to a local network address")
        return allowed

    async def close(self) -> None:
        """The wrapped resolver is shared; it is not ours to close."""
