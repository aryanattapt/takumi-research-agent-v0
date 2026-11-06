from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit


class UnsafeSourceUrl(ValueError):
    pass


class PublicSourceUrlPolicy:
    def __init__(self, allowed_domains: tuple[str, ...], *, resolve_dns: bool = True):
        self.allowed_domains = tuple(domain.lower().strip(".") for domain in allowed_domains if domain.strip())
        self.resolve_dns = resolve_dns

    def _check_host_syntax(self, url: str) -> str:
        parsed = urlsplit(url)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
            raise UnsafeSourceUrl("Only absolute HTTP(S) source URLs without embedded credentials are allowed")
        host = parsed.hostname.lower().rstrip(".")
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            ip = None
        if ip and (not ip.is_global or ip.is_multicast or ip.is_unspecified):
            raise UnsafeSourceUrl("Private, loopback, link-local, and non-routable IP destinations are blocked")
        if host in {"localhost", "metadata.google.internal"} or host.endswith(".localhost") or host.endswith(".local"):
            raise UnsafeSourceUrl("Local and metadata service hosts are blocked")
        if self.allowed_domains and not any(host == domain or host.endswith("." + domain) for domain in self.allowed_domains):
            raise UnsafeSourceUrl("Source host is not on the retrieval allowlist")
        return host

    async def validate(self, url: str) -> None:
        host = self._check_host_syntax(url)
        if not self.resolve_dns:
            return
        try:
            answers = await asyncio.to_thread(socket.getaddrinfo, host, None, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise UnsafeSourceUrl("Source hostname could not be resolved") from exc
        addresses = {answer[4][0].split("%", 1)[0] for answer in answers}
        if not addresses:
            raise UnsafeSourceUrl("Source hostname has no addresses")
        for value in addresses:
            try:
                parsed_ip = ipaddress.ip_address(value)
            except ValueError as exc:
                raise UnsafeSourceUrl("Source hostname resolved to an invalid address") from exc
            if not parsed_ip.is_global:
                raise UnsafeSourceUrl("Source hostname resolves to a private or reserved address")
