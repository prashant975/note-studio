"""Small, dependency-free security primitives shared by web endpoints."""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import re
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlsplit


SESSION_TTL_SECONDS = 12 * 60 * 60
_TOKEN_VERSION = "v1"
_HEX_BYTES = 16
_MAX_EXPIRY_DIGITS = 11
_LOWER_HEX_TOKEN = re.compile(r"[0-9a-f]{32}\Z", re.ASCII)
_B64_MAC = re.compile(r"[A-Za-z0-9_-]{43}\Z", re.ASCII)
_DNS_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z", re.ASCII)


@dataclass(frozen=True)
class Session:
    """The authenticated data safely carried by a verified session token."""

    expires_at: int
    csrf: str
    nonce: str


def _now(value: int | None) -> int:
    if value is None:
        return int(time.time())
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("now must be a non-negative integer timestamp")
    return value


def _signature(secret: str, body: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _session_token(secret: str, expiry: int, csrf: str, nonce: str) -> str:
    body = f"{_TOKEN_VERSION}.{expiry}.{csrf}.{nonce}"
    return f"{body}.{_signature(secret, body)}"


def issue_session(secret: str, now: int | None = None) -> tuple[str, str]:
    """Return a signed 12-hour cookie value and its double-submit CSRF value."""
    if not isinstance(secret, str) or not secret:
        raise ValueError("a non-empty session secret is required")

    issued_at = _now(now)
    expiry = issued_at + SESSION_TTL_SECONDS
    csrf = secrets.token_hex(_HEX_BYTES)
    nonce = secrets.token_hex(_HEX_BYTES)
    return _session_token(secret, expiry, csrf, nonce), csrf


def renew_session(secret: str, session: Session, now: int | None = None) -> str:
    """Extend a verified live session without changing its authority or CSRF."""
    if not isinstance(secret, str) or not secret:
        raise ValueError("a non-empty session secret is required")
    if (
        not isinstance(session, Session)
        or not _is_hex_token(session.csrf)
        or not _is_hex_token(session.nonce)
    ):
        raise ValueError("a valid session is required")
    current_time = _now(now)
    if (
        isinstance(session.expires_at, bool)
        or not isinstance(session.expires_at, int)
        or session.expires_at <= current_time
        or session.expires_at - current_time > SESSION_TTL_SECONDS
    ):
        raise ValueError("a live session is required")
    return _session_token(
        secret,
        current_time + SESSION_TTL_SECONDS,
        session.csrf,
        session.nonce,
    )


def _is_hex_token(value: str) -> bool:
    return _LOWER_HEX_TOKEN.fullmatch(value) is not None


def verify_session(token: str, secret: str, now: int | None = None) -> Session | None:
    """Verify a session without revealing whether its structure or MAC was wrong."""
    if not isinstance(token, str) or not isinstance(secret, str) or not secret:
        return None

    try:
        version, expiry_text, csrf, nonce, received_signature = token.split(".")
    except ValueError:
        return None
    if (
        version != _TOKEN_VERSION
        or not expiry_text.isascii()
        or not expiry_text.isdigit()
        or len(expiry_text) > _MAX_EXPIRY_DIGITS
        or not _is_hex_token(csrf)
        or not _is_hex_token(nonce)
        or _B64_MAC.fullmatch(received_signature) is None
    ):
        return None

    try:
        expiry = int(expiry_text)
        current_time = _now(now)
    except (ValueError, OverflowError):
        return None
    if expiry <= current_time or expiry - current_time > SESSION_TTL_SECONDS:
        return None

    body = f"{version}.{expiry_text}.{csrf}.{nonce}"
    expected_signature = _signature(secret, body)
    if not hmac.compare_digest(received_signature, expected_signature):
        return None
    return Session(expires_at=expiry, csrf=csrf, nonce=nonce)


def _port(value: str) -> int | None:
    if not value or not value.isascii() or not value.isdigit():
        return None
    port = int(value)
    return port if 1 <= port <= 65_535 else None


def _dns_host(value: str) -> str | None:
    if not value.isascii() or len(value) > 253 or value.endswith("."):
        return None
    if all(character in "0123456789." for character in value):
        try:
            return str(ipaddress.IPv4Address(value))
        except ipaddress.AddressValueError:
            return None
    if not value or any(_DNS_LABEL.fullmatch(label) is None for label in value.split(".")):
        return None
    return value.lower()


def _host_parts(value: str, default_port: int) -> tuple[str, int] | None:
    """Parse exactly one Host authority with an optional valid, non-zero port."""
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or "," in value
        or "@" in value
        or any(character.isspace() for character in value)
    ):
        return None

    if value.startswith("["):
        closing_bracket = value.find("]")
        if closing_bracket < 2:
            return None
        literal = value[1:closing_bracket]
        suffix = value[closing_bracket + 1 :]
        if "%" in literal or (suffix and not suffix.startswith(":")):
            return None
        port = default_port if not suffix else _port(suffix[1:])
        if port is None:
            return None
        try:
            host = ipaddress.IPv6Address(literal).compressed.lower()
        except ipaddress.AddressValueError:
            return None
        return host, port

    if "[" in value or "]" in value or value.count(":") > 1:
        return None
    if ":" in value:
        raw_host, raw_port = value.rsplit(":", 1)
        port = _port(raw_port)
        if port is None:
            return None
    else:
        raw_host = value
        port = default_port

    host = _dns_host(raw_host)
    if host is None:
        return None
    return host, port


def require_same_origin(origin: str, host: str, production_host: str = "") -> bool:
    """Allow an exact HTTPS same-origin request, plus exact localhost HTTP in dev."""
    if (
        not isinstance(origin, str)
        or not origin
        or origin.strip() != origin
        or any(character.isspace() for character in origin)
    ):
        return False
    try:
        parsed = urlsplit(origin)
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.netloc
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            return False
    except ValueError:
        return False

    default_port = 443 if parsed.scheme == "https" else 80
    origin_authority = _host_parts(parsed.netloc, default_port)
    expected_host = _host_parts(host, default_port)
    if origin_authority is None or expected_host != origin_authority:
        return False
    origin_host, _ = origin_authority

    if production_host:
        configured_host = _host_parts(production_host, 443)
        return parsed.scheme == "https" and configured_host == expected_host
    return parsed.scheme == "https" or (parsed.scheme == "http" and origin_host == "localhost")
