"""Proxy environment fixes applied before any HTTP client is created."""

from __future__ import annotations

import ipaddress
import os
import re

_PROXY_URL_VARS = {"all_proxy", "http_proxy", "https_proxy"}
_NO_PROXY_SEPARATOR = re.compile(r"[,;]")
_BRACKETED_HOST = re.compile(r"\[(?P<address>[^\]]+)\](?::\d+)?(?P<prefix>/\d+)?")


def _httpx_no_proxy_entry(entry: str) -> str | None:
    """Return the form of a NO_PROXY entry httpx can parse, or None to drop it."""
    host = entry.strip()
    if "://" not in host and not host.isascii():
        return None
    if match := _BRACKETED_HOST.fullmatch(host):
        host = match["address"] + (match["prefix"] or "")
    address, slash, prefix = host.partition("/")
    try:
        if ipaddress.ip_address(address).version != 6:
            return entry
    except ValueError:
        return entry
    if slash and prefix != "128":
        return None
    # Bare IPv6 addresses already work; keep them byte for byte.
    return address if match or slash else entry


def normalize_proxy_env() -> None:
    """Rewrite proxy environment values that httpx <= 0.28.1 cannot use.

    With a proxy set, any of these makes every client fail at construction:

    - ``socks://`` proxy URLs, an alias httpx rejects, become ``socks5://``.
    - Bracketed IPv6 NO_PROXY entries (``[::1]``, ``[fd00::1]:8080``) become
      the bare address, which httpx turns into a working bypass.
    - IPv6 CIDR NO_PROXY entries: ``/128`` is one address and becomes that
      address; wider ranges (``fd00::/8``) are dropped. httpx never matched
      NO_PROXY entries as CIDR ranges, so those hosts now use the proxy.
    - Non-ASCII host entries (``中国.cn``) are dropped.

    Variable names match case-insensitively, as in ``urllib``. NO_PROXY
    entries may be separated by ``,`` or ``;`` and are rejoined with ``,``
    without duplicates. IPv4 ranges, host names, wildcards and URL-form
    entries are kept unchanged.
    """
    for var, value in list(os.environ.items()):
        name = var.lower()
        if name in _PROXY_URL_VARS and value.lower().startswith("socks://"):
            os.environ[var] = "socks5://" + value[len("socks://"):]
        elif name == "no_proxy" and value:
            entries = (_httpx_no_proxy_entry(entry) for entry in _NO_PROXY_SEPARATOR.split(value))
            os.environ[var] = ",".join(dict.fromkeys(entry for entry in entries if entry is not None))
