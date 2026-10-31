"""SSRF-safe URL validation for every outbound QC fetch.

The verifier downloads pages and PDFs from approved official domains
(config/upsc_qc_sources.yaml), but the concrete URLs originate from AI output
and config files, so every fetch target must be treated as attacker-influenced:

- https only — plain http is never fetched, so nothing on the network path can
  feed the fact-checker fake evidence in transit;
- the hostname must be a syntactically valid DNS name, and the raw URL must
  contain no tab/newline, so a host that urlsplit and the HTTP client read
  differently is refused rather than approved for a request that lands somewhere
  else. Both directions of that divergence matter: urlsplit keeps a backslash
  inside the host ("evil.com\\.pib.gov.in") where requests cuts the authority
  and reaches evil.com, and urlsplit silently strips tabs ("pib.gov\\t.in" ->
  the approved "pib.gov.in") where requests keeps them;
- the hostname must match the approved-domain list, and the suffix match is
  anchored on a dot: "x.pib.gov.in" passes for "pib.gov.in" while
  "notpib.gov.in" and "pib.gov.in.attacker.com" do not;
- the hostname is resolved BEFORE any fetch and every resolved address must be
  public, so a DNS record pointing at 127.0.0.1 / 10.x / 169.254.x (cloud
  metadata) is rejected here.

Note what this pre-fetch resolution does NOT do: the HTTP client resolves the
name again when it connects, so a TTL-0 record can answer differently to the
two lookups. Closing that check-then-connect window needs the address actually
connected to, which only the caller can see — page_fetcher._peer_address_is_public
inspects the live socket for exactly that reason. Do not treat this module alone
as rebinding-proof.

Approval is checked before DNS, so hostnames that are not on the approved list
are never resolved at all. Nothing in this module raises: callers get an
(ok, reason) pair and turn failures into warnings, because a bad URL must
degrade one claim's verification, never crash the pipeline.
"""
from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlsplit

HTTPS_PORT = 443

# A syntactically valid DNS hostname (or a bare IP literal, handled separately).
# Anything outside this alphabet is refused rather than interpreted, because
# urlsplit and the HTTP client do not agree on where the authority ends:
# urlsplit reads "evil.com\.pib.gov.in" as one host, while requests/urllib3
# follow WHATWG, cut at the backslash, and connect to evil.com. A host that two
# parsers read differently must never reach an approval test.
_HOSTNAME_RE = re.compile(
    r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$"
)


def _normalise_host(host: str) -> str:
    """Lower-case and drop the trailing root dot so 'PIB.gov.in.' == 'pib.gov.in'."""
    return host.strip().lower().rstrip(".")


def _is_valid_hostname(host: str) -> bool:
    """True for a plain DNS name or an IP literal; False for anything a URL
    parser might disagree about (backslashes, %-escapes, spaces)."""
    if not host or len(host) > 253:
        return False
    if _HOSTNAME_RE.match(host):
        return True
    try:  # IPv6 literals arrive from urlsplit already stripped of [] brackets.
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _has_url_control_chars(url: str) -> bool:
    """True when the raw URL contains a tab/newline/CR.

    These must be checked on the raw string, never on the parsed host: urlsplit
    follows WHATWG and SILENTLY STRIPS them, so "https://pib.gov\\t.in/" parses
    to the approved host "pib.gov.in", while urllib3 keeps the character and
    requests connects to "pib.gov\\t.in". That divergence hands out approval for
    a host the HTTP client will not use — the more dangerous direction, because
    the stripped form is the one that looks legitimate.
    """
    return any(c in url for c in ("\t", "\n", "\r"))


def is_approved_url(url: str, approved_domains: list[str]) -> bool:
    """True when the URL's hostname is an approved domain or a subdomain of one.

    The '.' anchor in the suffix test is the whole point: a bare endswith
    would let "evilpib.gov.in.attacker.com"-style lookalikes through. The
    hostname must also be syntactically valid (see _is_valid_hostname), so a
    parser-divergent host can never be approved for a request that would
    actually reach a different server.
    """
    if not isinstance(url, str) or _has_url_control_chars(url):
        return False
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return False
    if not host:
        return False
    host = _normalise_host(host)
    if not _is_valid_hostname(host):
        return False
    for domain in approved_domains or []:
        d = _normalise_host(str(domain))
        if not d:
            continue
        if host == d or host.endswith("." + d):
            return True
    return False


def _is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Public means internet-routable; everything else is internal attack
    surface (private ranges, loopback, link-local incl. 169.254.169.254
    metadata endpoints, reserved, multicast, 0.0.0.0)."""
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def validate_url_for_fetch(url: str, approved_domains: list[str]) -> tuple[bool, str]:
    """Full pre-fetch gate; call it on the original URL and on every redirect hop.

    Returns (ok, reason). The reason string is always safe to log verbatim —
    URLs carrying userinfo (a credential-shaped construct) are rejected before
    anything else could echo them.
    """
    if not isinstance(url, str):
        return False, "url is not a string"
    if _has_url_control_chars(url):
        return False, "url contains a tab or newline character"

    try:
        parts = urlsplit(url)
    except ValueError as e:
        return False, f"unparseable url: {e}"

    scheme = (parts.scheme or "").lower()
    if scheme != "https":
        return False, f"scheme {scheme or '(none)'!r} not allowed: https only"

    if "@" in parts.netloc:
        return False, "userinfo (user@host) in url not allowed"

    host = parts.hostname
    if not host:
        return False, "url has no hostname"
    host = _normalise_host(host)
    # Refuse hosts whose parse is ambiguous before doing anything else with
    # them: "evil.com\.pib.gov.in" passes a naive suffix test but connects to
    # evil.com, and a mangled IP literal ("169.254.169.254\.pib.gov.in") slips
    # past the raw-IP branch below because ip_address() cannot parse it.
    if not _is_valid_hostname(host):
        return False, "hostname contains characters that are not valid in a dns name"

    try:
        port = parts.port
    except ValueError:
        return False, "invalid port in url"
    if port is not None and port != HTTPS_PORT:
        return False, f"non-standard port {port} not allowed: 443 only"

    # Raw-IP hostnames are judged literally, never resolved. A private literal
    # is rejected even if someone whitelisted it: the approved list holds
    # domains, and an IP must never bypass the public-address rule.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not _is_public(literal):
            return False, f"hostname is a non-public ip address: {host}"
        if not is_approved_url(url, approved_domains):
            return False, f"host {host!r} is not on the approved domain list"
        return True, "ok"

    # Approval before DNS: unapproved hostnames are never resolved, so this
    # validator cannot itself be used to leak lookups for arbitrary names.
    if not is_approved_url(url, approved_domains):
        return False, f"host {host!r} is not on the approved domain list"

    try:
        infos = socket.getaddrinfo(host, HTTPS_PORT, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError) as e:
        # socket.gaierror is an OSError; UnicodeError covers idna failures on
        # hostile hostnames. Never raise — the caller records a warning.
        return False, f"dns lookup failed for {host!r}: {e}"
    if not infos:
        return False, f"dns lookup for {host!r} returned no addresses"

    for info in infos:
        addr = str(info[4][0]).split("%", 1)[0]  # strip IPv6 zone id
        try:
            resolved = ipaddress.ip_address(addr)
        except ValueError:
            return False, f"dns returned an unparseable address for {host!r}: {addr}"
        if not _is_public(resolved):
            # EVERY address must be public: a single private A record among
            # public ones is exactly how DNS-rebinding pivots inside.
            return False, f"host {host!r} resolves to non-public address {addr}"
    return True, "ok"
