"""Behavioral tests for the browser session security boundary."""
from __future__ import annotations

import base64
import hashlib
import hmac

import pytest

import notesbot.web_auth as web_auth
from notesbot.web_auth import (
    SESSION_TTL_SECONDS,
    issue_session,
    require_same_origin,
    verify_session,
)


def _signed_token(secret: str, expiry: int, csrf: str, nonce: str) -> str:
    """Build a token independently so malformed-time tests need no internals."""
    body = f"v1.{expiry}.{csrf}.{nonce}"
    signature = base64.urlsafe_b64encode(
        hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
    ).rstrip(b"=").decode()
    return f"{body}.{signature}"


def test_session_round_trip_uses_the_documented_five_field_format():
    token, csrf = issue_session("test-secret", now=1_000)

    parts = token.split(".")
    session = verify_session(token, "test-secret", now=1_001)

    assert len(parts) == 5
    assert parts[0] == "v1"
    assert parts[1] == "44200"
    assert parts[2] == csrf
    assert len(csrf) == 32
    assert len(parts[3]) == 32
    assert session is not None
    assert session.csrf == csrf
    assert session.nonce == parts[3]
    assert session.expires_at == 44_200


def test_session_is_invalid_at_its_exact_expiry_boundary():
    token, _ = issue_session("test-secret", now=1_000)

    assert verify_session(token, "test-secret", now=1_000 + SESSION_TTL_SECONDS - 1)
    assert verify_session(token, "test-secret", now=1_000 + SESSION_TTL_SECONDS) is None


def test_tampering_with_a_signed_field_or_signature_is_rejected():
    token, _ = issue_session("test-secret", now=1_000)
    parts = token.split(".")
    changed_csrf = ("0" if parts[2][0] != "0" else "1") + parts[2][1:]
    tampered_field = ".".join([parts[0], parts[1], changed_csrf, parts[3], parts[4]])
    changed_signature = parts[4][:-1] + ("A" if parts[4][-1] != "A" else "B")

    assert verify_session(tampered_field, "test-secret", now=1_001) is None
    assert verify_session(".".join([*parts[:4], changed_signature]), "test-secret", now=1_001) is None


def test_non_ascii_session_mac_is_rejected_without_raising():
    token, _ = issue_session("test-secret", now=1_000)
    malformed = ".".join([*token.split(".")[:4], "\u00e9" * 43])

    assert verify_session(malformed, "test-secret", now=1_001) is None


def test_session_renewal_preserves_csrf_nonce_and_extends_from_current_time():
    token, _ = issue_session("test-secret", now=1_000)
    original = verify_session(token, "test-secret", now=1_001)

    renewed_token = web_auth.renew_session("test-secret", original, now=44_000)
    renewed = verify_session(renewed_token, "test-secret", now=44_000)

    assert renewed is not None
    assert renewed.csrf == original.csrf
    assert renewed.nonce == original.nonce
    assert renewed.expires_at == 44_000 + SESSION_TTL_SECONDS


@pytest.mark.parametrize(
    "token",
    [
        "",
        "v1.44200.aa.bb",
        "v2.44200.0123456789abcdef0123456789abcdef.0123456789abcdef0123456789abcdef.sig",
        "v1.nope.0123456789abcdef0123456789abcdef.0123456789abcdef0123456789abcdef.sig",
        "v1.44200.not-hex.0123456789abcdef0123456789abcdef.sig",
        "v1.44200.0123456789abcdef0123456789abcdef.not-hex.sig",
    ],
)
def test_malformed_or_wrong_version_sessions_are_rejected_without_an_exception(token):
    assert verify_session(token, "test-secret", now=1_000) is None


def test_empty_secret_and_an_implausibly_future_expiry_are_rejected():
    future = _signed_token(
        "test-secret",
        1_000 + SESSION_TTL_SECONDS + 1,
        "0123456789abcdef0123456789abcdef",
        "fedcba9876543210fedcba9876543210",
    )

    assert verify_session(future, "test-secret", now=1_000) is None
    assert verify_session(future, "", now=1_000) is None
    with pytest.raises(ValueError):
        issue_session("", now=1_000)


@pytest.mark.parametrize(
    "csrf",
    [
        "00 00 00 00 00 00 00 00 00 00 00",
        "0" * 15 + "\t" + "0" * 16,
        "０" * 32,
        "ABCDEF0123456789abcdef0123456789",
    ],
)
def test_signed_tokens_require_exactly_32_ascii_lowercase_hex_characters(csrf):
    token = _signed_token(
        "test-secret",
        44_200,
        csrf,
        "fedcba9876543210fedcba9876543210",
    )

    assert verify_session(token, "test-secret", now=1_000) is None


def test_session_issuance_rejects_pre_epoch_timestamps():
    with pytest.raises(ValueError):
        issue_session("test-secret", now=-1)


@pytest.mark.parametrize(
    ("origin", "host", "production_host"),
    [
        ("https://shortnotes-azure.vercel.app", "shortnotes-azure.vercel.app", ""),
        (
            "https://shortnotes-azure.vercel.app",
            "shortnotes-azure.vercel.app",
            "shortnotes-azure.vercel.app",
        ),
        ("http://localhost:3000", "localhost:3000", ""),
    ],
)
def test_same_origin_allows_exact_https_host_and_safe_localhost(origin, host, production_host):
    assert require_same_origin(origin, host, production_host)


@pytest.mark.parametrize(
    ("origin", "host", "production_host"),
    [
        ("https://evil.example", "shortnotes-azure.vercel.app", ""),
        ("http://shortnotes-azure.vercel.app", "shortnotes-azure.vercel.app", ""),
        ("https://shortnotes-azure.vercel.app", "evil.example", ""),
        (
            "https://preview-shortnotes.vercel.app",
            "preview-shortnotes.vercel.app",
            "shortnotes-azure.vercel.app",
        ),
        ("http://localhost:3000", "localhost:3001", ""),
        ("http://127.0.0.1:3000", "127.0.0.1:3000", ""),
    ],
)
def test_same_origin_rejects_cross_origin_non_https_and_unsafe_development_hosts(
    origin, host, production_host
):
    assert not require_same_origin(origin, host, production_host)


@pytest.mark.parametrize(
    ("origin", "host"),
    [
        ("https://example.com:0", "example.com"),
        ("https://example.com", "example.com:0"),
        ("https://example.com:", "example.com"),
        ("https://example.com", "example.com:"),
        ("https://example.com:8443", "example.com:9443"),
    ],
)
def test_same_origin_rejects_invalid_dangling_and_mismatched_ports(origin, host):
    assert not require_same_origin(origin, host)


@pytest.mark.parametrize(
    ("origin", "host"),
    [
        ("https://example.com", "example.com:443"),
        ("https://example.com:443", "example.com"),
        ("https://example.com:8443", "example.com:8443"),
        ("https://[2001:db8::1]:8443", "[2001:db8::1]:8443"),
    ],
)
def test_same_origin_handles_default_nondefault_and_bracketed_ipv6_ports(origin, host):
    assert require_same_origin(origin, host)


@pytest.mark.parametrize(
    ("origin", "host"),
    [
        ("https://good.example,evil.example", "good.example,evil.example"),
        ("https://good.example", "good.example,evil.example"),
        ("https://good.example", "good.example, evil.example"),
        ("https://good.example", " good.example,evil.example "),
    ],
)
def test_same_origin_rejects_comma_separated_forwarded_host_values(origin, host):
    assert not require_same_origin(origin, host)
