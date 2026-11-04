"""Remember a quick-tunnel HTTPS origin after the hostname changes.

The file stores only the URL. Raw tunnel logs are not kept.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlparse

from ephy_discord.config import AppConfig


def state_path(config: AppConfig) -> Path:
    return Path(config.cache.path).expanduser().parent / "mcp-public-url.json"


def validate_public_url(url: str) -> str:
    parsed = urlparse(url.strip())
    host = parsed.hostname or ""
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.port
        or host in {"localhost", "127.0.0.1", "::1"}
        or _looks_like_ip(host)
    ):
        raise ValueError("mcp public url must be https without userinfo, port, query, or ip")
    return parsed.geturl().rstrip("/")


def write_public_url(config: AppConfig, url: str) -> str:
    cleaned = validate_public_url(url)
    path = state_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"url": cleaned}, ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return cleaned


def read_public_url(config: AppConfig) -> str:
    if config.mcp.public_base_url:
        return config.mcp.public_base_url
    path = state_path(config)
    if not path.is_file():
        return ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        url = payload.get("url") if isinstance(payload, dict) else ""
        return validate_public_url(url) if isinstance(url, str) else ""
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return ""


def _looks_like_ip(host: str) -> bool:
    if host.count(".") == 3 and all(part.isdigit() for part in host.split(".")):
        return True
    return ":" in host
