"""Who is viewing: Home Assistant asserts it to web UIs, the OpenID way.

A web UI can opt in to receive, with every proxied request, a short-lived JWT
(RS256) that Home Assistant signs, naming the Home Assistant user who opened it.
Home Assistant publishes an OpenID discovery document and its public keys (JWKS)
under one issuer URL, so any OpenID or JWT library, or a gateway such as
oauth2-proxy (--extra-jwt-issuers), can verify the token without a shared secret.

Claims: iss, aud (the web UI's origin unless set otherwise), sub (the Home
Assistant user id), name, preferred_username (the login username), iat, nbf, exp,
plus any claims configured for the user in the options (email, groups...).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import time
from typing import TYPE_CHECKING, Any, Final

from aiohttp import web
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from homeassistant.components.http import KEY_HASS, HomeAssistantView
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.helpers.storage import Store
import jwt

from .const import (
    CONF_IDENTITY_CLAIMS,
    CONF_IDENTITY_URL,
    DOMAIN,
    IDENTITY_LIFETIME,
    IDENTITY_PATH,
    IDENTITY_REUSE_MARGIN,
    STORAGE_KEY_IDENTITY,
    STORAGE_VERSION,
)

if TYPE_CHECKING:
    from homeassistant.auth.models import User

    from .hub import LocalWebUiHub

_LOGGER = logging.getLogger(__name__)

ALGORITHM: Final = "RS256"
DISCOVERY_PATH: Final = f"{IDENTITY_PATH}/.well-known/openid-configuration"
# Where oauth2-proxy also looks when an issuer has no discovery document
JWKS_PATH: Final = f"{IDENTITY_PATH}/.well-known/jwks.json"
# Set by the proxy itself, never from the options
RESERVED_CLAIMS: Final = frozenset(
    {"iss", "sub", "aud", "exp", "nbf", "iat", "jti", "azp", "nonce", "auth_time", "at_hash"}
)


def base_url(hass: HomeAssistant, configured: str | None) -> str | None:
    """Home Assistant's address as the token's verifiers reach it."""
    if configured := (configured or "").strip().rstrip("/"):
        return configured
    try:
        return get_url(hass, prefer_external=False, allow_cloud=False).rstrip("/")
    except NoURLAvailableError:
        return None


def login_username(user: User) -> str | None:
    """The username the user logs in with (Home Assistant's own login), if any."""
    for credential in user.credentials:
        if credential.auth_provider_type == "homeassistant" and (
            username := credential.data.get("username")
        ):
            return str(username)
    return None


def validate_claims(value: Any, users: list[User]) -> str | None:
    """None if valid, else an error key for the options form."""
    if value in (None, ""):
        return None
    if not isinstance(value, dict):
        return "invalid_identity_claims"
    known = {user.id for user in users} | {name for user in users if (name := login_username(user))}
    for key, claims in value.items():
        if not isinstance(claims, dict) or not all(isinstance(k, str) for k in claims):
            return "invalid_identity_claims"
        if str(key) not in known:
            return "unknown_identity_user"
        if RESERVED_CLAIMS & set(claims):
            return "reserved_identity_claim"
    return None


def _b64(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class IdentityIssuer:
    """Signs identity tokens and publishes the keys to verify them."""

    def __init__(self, hass: HomeAssistant, hub: LocalWebUiHub) -> None:
        self.hass = hass
        self.hub = hub
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, STORAGE_KEY_IDENTITY, private=True, atomic_writes=True
        )
        self._key: rsa.RSAPrivateKey | None = None
        self._jwk: dict[str, str] = {}
        # (user id, audience) -> (token, expiry)
        self._tokens: dict[tuple[str, str], tuple[str, float]] = {}
        self._loading = asyncio.Lock()  # One key, even when the first requests come together

    # ---- configuration --------------------------------------------------------

    @property
    def issuer(self) -> str | None:
        base = base_url(self.hass, self.hub.hub_option(CONF_IDENTITY_URL, None))
        return None if base is None else base + IDENTITY_PATH

    def claims_for(self, user: User) -> dict[str, Any]:
        claims: dict[str, Any] = {"sub": user.id, "name": user.name}
        username = login_username(user)
        if username:
            claims["preferred_username"] = username
        configured: dict[str, Any] = self.hub.hub_option(CONF_IDENTITY_CLAIMS, None) or {}
        for key in (user.id, username):
            if key and isinstance(extra := configured.get(key), dict):
                claims.update({k: v for k, v in extra.items() if k not in RESERVED_CLAIMS})
        return claims

    @callback
    def async_forget_tokens(self) -> None:
        """Options changed: the next requests get tokens with the new claims."""
        self._tokens.clear()

    # ---- keys -----------------------------------------------------------------

    async def async_ensure_key(self) -> None:
        """Load the signing key, or make one the first time it is needed."""
        if self._key is not None:
            return
        async with self._loading:
            if self._key is None:
                await self._async_load_key()

    async def _async_load_key(self) -> None:
        data = await self._store.async_load() or {}
        pem: str | None = data.get("key")
        if pem is None:
            pem = await self.hass.async_add_executor_job(_new_key_pem)
            await self._store.async_save({"key": pem})
            _LOGGER.info("Local Web UIs: made a new key for signing identity tokens")
        key = serialization.load_pem_private_key(pem.encode(), password=None)
        assert isinstance(key, rsa.RSAPrivateKey)
        numbers = key.public_key().public_numbers()
        jwk = {"kty": "RSA", "n": _b64(numbers.n), "e": _b64(numbers.e)}
        # RFC 7638 thumbprint: stable, and changes with the key
        thumbprint = hashlib.sha256(
            json.dumps(jwk, separators=(",", ":"), sort_keys=True).encode()
        ).digest()
        kid = base64.urlsafe_b64encode(thumbprint).rstrip(b"=").decode()
        self._jwk = {**jwk, "kid": kid, "use": "sig", "alg": ALGORITHM}
        self._key = key

    async def async_remove(self) -> None:
        """The last entry was removed: forget the key."""
        self._key = None
        self._jwk = {}
        self._tokens.clear()
        await self._store.async_remove()

    def jwks(self) -> dict[str, Any]:
        return {"keys": [self._jwk] if self._jwk else []}

    def discovery(self, issuer: str) -> dict[str, Any]:
        return {
            "issuer": issuer,
            "jwks_uri": issuer + JWKS_PATH.removeprefix(IDENTITY_PATH),
            "id_token_signing_alg_values_supported": [ALGORITHM],
            "subject_types_supported": ["public"],
            # Tokens are only ever handed out by the proxy: no endpoints to log in
            "response_types_supported": ["id_token"],
            "claims_supported": [
                "iss",
                "sub",
                "aud",
                "exp",
                "iat",
                "nbf",
                "name",
                "preferred_username",
                "email",
                "email_verified",
                "groups",
            ],
        }

    # ---- tokens -----------------------------------------------------------------

    def token(self, user: User, audience: str) -> str | None:
        """A token for user to send to audience; None if Home Assistant's URL is unknown.

        Reused while it has a while left, so each page load does not sign anew.
        """
        assert self._key is not None, "async_ensure_key first"
        now = time.time()
        cached = self._tokens.get((user.id, audience))
        if cached is not None and cached[1] - now > IDENTITY_REUSE_MARGIN:
            return cached[0]
        if (issuer := self.issuer) is None:
            return None
        issued = int(now)
        payload = {
            **self.claims_for(user),
            "iss": issuer,
            "aud": audience,
            "iat": issued,
            "nbf": issued,
            "exp": issued + IDENTITY_LIFETIME,
        }
        token = jwt.encode(
            payload, self._key, algorithm=ALGORITHM, headers={"kid": self._jwk["kid"]}
        )
        if len(self._tokens) > 1000:  # Users x web UIs; never grows in practice
            self._tokens.clear()
        self._tokens[(user.id, audience)] = (token, issued + IDENTITY_LIFETIME)
        return token


def _new_key_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def _issuer_of(request: web.Request) -> tuple[IdentityIssuer, str] | None:
    hub: LocalWebUiHub | None = request.app[KEY_HASS].data.get(DOMAIN)
    if hub is None or not hub.active or (issuer := hub.identity.issuer) is None:
        return None
    return hub.identity, issuer


class _PublicView(HomeAssistantView):
    """Read by verifiers (servers): no login, readable from anywhere."""

    requires_auth = False
    cors_allowed = True


class DiscoveryView(_PublicView):
    url = DISCOVERY_PATH
    name = f"api:{DOMAIN}:identity:discovery"

    async def get(self, request: web.Request) -> web.Response:
        if (found := _issuer_of(request)) is None:
            return web.Response(status=404)
        identity, issuer = found
        return self.json(identity.discovery(issuer), headers={"Cache-Control": "max-age=300"})


class JwksView(_PublicView):
    url = JWKS_PATH
    name = f"api:{DOMAIN}:identity:jwks"

    async def get(self, request: web.Request) -> web.Response:
        if (found := _issuer_of(request)) is None:
            return web.Response(status=404)
        identity = found[0]
        await identity.async_ensure_key()
        return self.json(identity.jwks(), headers={"Cache-Control": "max-age=300"})


@callback
def async_register_identity_views(hass: HomeAssistant) -> None:
    hass.http.register_view(DiscoveryView())
    hass.http.register_view(JwksView())
