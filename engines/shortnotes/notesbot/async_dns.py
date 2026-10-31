"""Native-async, no-executor DNS support for bounded HTTP clients."""
from __future__ import annotations

import asyncio
import ipaddress
import socket
import sys
import time
from typing import Callable

import aiohttp
import dns.asyncresolver
import dns.exception


class AsyncDnsResolver(aiohttp.abc.AbstractResolver):
    """Resolve A/AAAA records with dnspython's asyncio socket backend.

    The resolver owns no worker, thread, executor, or persistent socket.  Each
    dnspython query owns its UDP/TCP transport and cancellation closes that
    transport before the query coroutine exits.
    """

    def __init__(
        self,
        deadline_at: float,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if (
            not isinstance(deadline_at, (int, float))
            or isinstance(deadline_at, bool)
        ):
            raise ValueError("Invalid DNS deadline")
        self._deadline_at = float(deadline_at)
        self._monotonic = monotonic
        self._closed = False
        if sys.platform == "win32":
            # dnspython prefers optional WMI when it is importable, and its WMI
            # adapter deliberately starts a thread.  Select the registry path
            # before Resolver reads system configuration.
            from dns import win32util

            win32util.set_config_method(win32util.ConfigMethod.Registry)
        self._resolver = dns.asyncresolver.Resolver()

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_UNSPEC,
    ) -> list[dict[str, object]]:
        if self._closed:
            raise RuntimeError("DNS resolver is closed")
        if (
            not isinstance(host, str)
            or not host
            or not isinstance(port, int)
            or isinstance(port, bool)
            or not 0 <= port <= 65535
            or family not in {socket.AF_UNSPEC, socket.AF_INET, socket.AF_INET6}
        ):
            raise ValueError("Invalid DNS query")
        remaining = self._deadline_at - self._monotonic()
        if remaining <= 0:
            raise TimeoutError("DNS deadline exceeded")
        try:
            async with asyncio.timeout(remaining):
                answers = await self._resolver.resolve_name(
                    host,
                    family=family,
                    lifetime=remaining,
                )
        except dns.exception.DNSException:
            raise socket.gaierror(socket.EAI_AGAIN, "DNS lookup failed") from None
        rows: list[dict[str, object]] = []
        for address, answer_family in answers.addresses_and_families(family):
            parsed = ipaddress.ip_address(address)
            expected_family = (
                socket.AF_INET if parsed.version == 4 else socket.AF_INET6
            )
            if answer_family != expected_family:
                raise ValueError("DNS address family mismatch")
            rows.append({
                "hostname": host,
                "host": str(parsed),
                "port": port,
                "family": expected_family,
                "proto": socket.IPPROTO_TCP,
                "flags": socket.AI_NUMERICHOST | socket.AI_NUMERICSERV,
            })
        return rows

    async def close(self) -> None:
        # dnspython has no persistent query transport to close.  Marking the
        # adapter unusable keeps ownership explicit and makes close idempotent.
        self._closed = True
