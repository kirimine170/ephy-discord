"""Resource-server checks for connector OAuth.

ChatGPT and Grok bring their own authorization server. This process verifies
a JWT with operator-pinned public keys and does not issue codes or tokens.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import jwt
from jwt import PyJWK

REQUIRED_SCOPE = "ephy:read"
ALGORITHMS = ["RS256", "ES256", "EdDSA"]


class OAuthConfigurationError(Exception):
    pass


@dataclass(frozen=True)
class OAuthSettings:
    issuer: str
    audience: str
    jwks: dict

    @property
    def configured(self) -> bool:
        return True


def load_oauth_settings(
    environ: dict[str, str],
    *,
    resource_url: str,
) -> OAuthSettings | None:
    issuer = environ.get("EPHY_MCP_OAUTH_ISSUER", "").strip()
    audience = environ.get("EPHY_MCP_OAUTH_AUDIENCE", "").strip()
    raw = environ.get("EPHY_MCP_OAUTH_JWKS", "").strip()
    path = environ.get("EPHY_MCP_OAUTH_JWKS_FILE", "").strip()
    if not issuer and not audience and not raw and not path:
        return None
    if not issuer or not audience or not (raw or path) or not _https_resource(resource_url):
        raise OAuthConfigurationError("OAUTH_NOT_CONFIGURED")
    if audience != resource_url:
        raise OAuthConfigurationError("OAUTH_AUDIENCE_MISMATCH")
    _exact_issuer(issuer)
    if path:
        raw = Path(path).read_text(encoding="utf-8")
    jwks = _public_jwks(raw)
    return OAuthSettings(issuer=issuer, audience=audience, jwks=jwks)


def protected_resource_metadata(settings: OAuthSettings) -> dict:
    return {
        "resource": settings.audience,
        "authorization_servers": [settings.issuer],
        "bearer_methods_supported": ["header"],
        "scopes_supported": [REQUIRED_SCOPE],
    }


def resource_metadata_url(resource: str) -> str:
    return resource.rstrip("/") + "/.well-known/oauth-protected-resource/mcp"


def verify_access_token(token: str, settings: OAuthSettings) -> bool:
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        return False
    kid = header.get("kid")
    key = _select_key(settings.jwks, kid if isinstance(kid, str) else None)
    if key is None:
        return False
    try:
        payload = jwt.decode(
            token,
            key,
            algorithms=ALGORITHMS,
            audience=settings.audience,
            issuer=settings.issuer,
            options={"require": ["exp", "iat", "sub"]},
        )
    except jwt.PyJWTError:
        return False
    scope = payload.get("scope")
    if not isinstance(scope, str) or REQUIRED_SCOPE not in scope.split(" "):
        return False
    client = payload.get("azp") or payload.get("client_id")
    return isinstance(client, str) and bool(client) and isinstance(payload.get("sub"), str)


def _exact_issuer(value: str) -> str:
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or any(char.isspace() for char in value)
    ):
        raise OAuthConfigurationError("INVALID_OAUTH_CONFIGURATION")
    return value


def _https_resource(value: str) -> bool:
    parsed = urlparse(value)
    return (
        parsed.scheme == "https"
        and bool(parsed.hostname)
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
    )


def _public_jwks(raw: str) -> dict:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise OAuthConfigurationError("INVALID_OAUTH_CONFIGURATION") from exc
    keys = parsed.get("keys") if isinstance(parsed, dict) else None
    if not isinstance(keys, list) or not keys:
        raise OAuthConfigurationError("INVALID_OAUTH_CONFIGURATION")
    for key in keys:
        if not isinstance(key, dict) or "d" in key or key.get("kty") == "oct":
            raise OAuthConfigurationError("INVALID_OAUTH_CONFIGURATION")
    return parsed


def _select_key(jwks: dict, kid: str | None):
    keys = jwks["keys"]
    chosen = None
    for key in keys:
        if kid is None or key.get("kid") == kid:
            chosen = key
            if kid is not None:
                break
    if chosen is None:
        return None
    try:
        return PyJWK.from_dict(chosen).key
    except jwt.PyJWTError:
        return None
