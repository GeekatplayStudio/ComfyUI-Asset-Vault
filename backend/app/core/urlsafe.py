"""Where the app is allowed to send an outbound request it was *told* to send.

SECURITY_REVIEW S-08.  Ollama is the only service whose address the user
supplies, and ``POST /ai/describe`` sends an asset fact sheet - including a
workflow's ``positive_prompt`` - to whatever that address names.  An arbitrary
absolute URL there is both a host/port scanner and a data-exfiltration sink, so
the address is checked before it is stored and again before it is used.

**Deliberate scope.** Loopback and the RFC1918 private ranges are allowed with
no ceremony, because running Ollama on another machine on the home LAN is a real
setup this app must not break.  Everything else is refused:

* link-local (``169.254.0.0/16``, ``fe80::/10``) is not a LAN address anyone
  serves Ollama on - it is the cloud metadata endpoint;
* public addresses would send the owner's prompts off the machine, which
  ARCHITECTURE 8.4 rules out;
* **names other than ``localhost`` are refused**, so DNS is not in the trust
  path at all.  A resolve-then-check rule would still be defeated by rebinding
  between the check and the request, and a LAN Ollama is addressed by IP.  If a
  name is ever genuinely needed this is where an explicit, per-host opt-in
  belongs - never a silent widening.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

ALLOWED_SCHEMES = ("http", "https")
LOCAL_NAMES = frozenset({"localhost"})
MAX_URL_CHARS = 2048

#: Written out rather than taken from ``ip_address.is_private``, which also
#: answers True for 0.0.0.0/8, 192.0.0.0/24 and 240.0.0.0/4 - none of which is a
#: home LAN, and whose membership has changed between CPython releases.
LOCAL_NETWORKS = tuple(ipaddress.ip_network(cidr) for cidr in (
    "127.0.0.0/8",       # loopback
    "10.0.0.0/8",        # RFC1918
    "172.16.0.0/12",     # RFC1918
    "192.168.0.0/16",    # RFC1918
    "::1/128",           # IPv6 loopback
    "fc00::/7",          # IPv6 unique-local
))


class UrlRejected(ValueError):
    """The URL is well formed but does not name a local or private host."""


def _host_is_local(host: str) -> bool:
    name = host.strip().strip("[]").rstrip(".").lower()
    if not name:
        return False
    if name in LOCAL_NAMES:
        return True
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return False  # a name, not a literal - see the module docstring
    if address.is_link_local or address.is_multicast or address.is_unspecified:
        return False
    return any(address in network for network in LOCAL_NETWORKS
               if address.version == network.version)


def check_local_url(value: str | None, *, field: str = "url") -> str:
    """Return ``value`` normalised, or raise ``UrlRejected`` with a reason."""
    if value is None:
        raise UrlRejected(f"{field}: a URL is required.")
    text = str(value).strip()
    if not text:
        raise UrlRejected(f"{field}: a URL is required.")
    if len(text) > MAX_URL_CHARS:
        raise UrlRejected(f"{field}: the URL is too long.")
    parts = urlsplit(text)
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise UrlRejected(f"{field}: the URL must start with http:// or https://.")
    if parts.username or parts.password:
        raise UrlRejected(f"{field}: credentials in the URL are not accepted.")
    if parts.query or parts.fragment:
        raise UrlRejected(f"{field}: the URL must not carry a query or fragment.")
    if parts.path not in ("", "/"):
        raise UrlRejected(f"{field}: the URL must name a host and port only.")
    try:
        port = parts.port
    except ValueError as exc:
        raise UrlRejected(f"{field}: the port is not a number.") from exc
    if port is not None and not (1 <= port <= 65535):
        raise UrlRejected(f"{field}: the port is out of range.")
    host = parts.hostname or ""
    if not _host_is_local(host):
        raise UrlRejected(
            f"{field}: '{host or text}' is not a local or private-network "
            f"address. Ollama must run on this machine or on your own LAN - "
            f"give it as an IP address such as http://192.168.1.10:11434.")
    return text.rstrip("/")


def is_local_url(value: str | None) -> bool:
    try:
        check_local_url(value)
    except UrlRejected:
        return False
    return True
