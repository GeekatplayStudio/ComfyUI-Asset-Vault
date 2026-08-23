"""The download allowlist (SECURITY_REVIEW R1) and redirect policy (R2).

Two rules, and they are the whole security boundary of the fetcher:

1. **No caller ever supplies a URL.**  Every URL this module sees came out of a
   workflow file on disk, a Civitai API response, or the ComfyUI-Manager
   registry.  No REST body and no MCP tool argument accepts one.
2. **Every URL is matched against a frozen host list after normalisation**, and
   every redirect hop is matched again *before* it is taken.  ``https`` is
   mandatory; credentials in the authority are refused outright, because
   ``https://huggingface.co@evil.test/x`` is a request to ``evil.test``.

The list is intentionally small.  Adding a host is a security decision and must
be a deliberate edit here, not a config value a hostile workflow could reach.
"""

from __future__ import annotations

import contextlib
import ipaddress
from dataclasses import dataclass
from urllib.parse import urlsplit

from ..core.errors import UpstreamUnavailable, ValidationError

#: Hosts that may serve model weights.  Exact matches.
MODEL_HOSTS_EXACT: frozenset[str] = frozenset({
    "civitai.com",
    "huggingface.co",
    "hf.co",
})

#: Hosts that may serve model weights.  A candidate matches when it *ends with*
#: ``"." + suffix``, which is what covers ``cdn-lfs-us-1.huggingface.co`` and the
#: rest of the Hugging Face / Civitai CDN fan-out without listing each one.
MODEL_HOSTS_SUFFIX: tuple[str, ...] = (
    ".civitai.com",
    ".huggingface.co",
    ".hf.co",
)

#: Hosts that may serve a git remote for a node package.  Registry-declared
#: repositories only - the ComfyUI-Manager registry is overwhelmingly GitHub.
GIT_HOSTS_EXACT: frozenset[str] = frozenset({
    "github.com",
    "gitlab.com",
    "codeberg.org",
})
GIT_HOSTS_SUFFIX: tuple[str, ...] = ()

KIND_MODEL = "model"
KIND_GIT = "git"

#: R2.  Five hops, each re-validated.  A chain longer than this is a redirect
#: loop or an attempt to walk somewhere allowlisting cannot see.
MAX_REDIRECTS = 5

_SCHEME = "https"


@dataclass(frozen=True)
class CheckedUrl:
    """A URL that has passed every rule in this module."""

    url: str
    host: str
    kind: str

    @property
    def display(self) -> str:
        return self.url


class HostNotAllowed(ValidationError):
    """A URL that is not on the allowlist.  Never retried, never followed."""

    def __init__(self, url: str, reason: str, host: str | None = None) -> None:
        super().__init__(
            f"Refused: {reason}",
            details={"url": _redact(url), "host": host, "reason": reason,
                     "allowed_model_hosts": sorted(MODEL_HOSTS_EXACT)
                     + list(MODEL_HOSTS_SUFFIX),
                     "allowed_git_hosts": sorted(GIT_HOSTS_EXACT)},
        )
        self.reason = reason


def _redact(url: str) -> str:
    """Never echo credentials back into a response body or a log line."""
    text = str(url or "")
    if "@" in text and "//" in text:
        head, _sep, tail = text.partition("//")
        _creds, _at, rest = tail.partition("@")
        return f"{head}//<redacted>@{rest}"
    return text[:400]


def normalize_host(raw: str | None) -> str | None:
    """Lower-cased, trailing-dot-stripped, IDNA-folded host, or ``None``.

    IDNA folding is what stops a Unicode homograph (a lookalike of ``huggingface.co``)
    from being compared as if it were the ASCII host it imitates.
    """
    if not raw:
        return None
    host = str(raw).strip().strip(".").lower()
    if not host:
        return None
    # Not representable as IDNA?  Then it cannot be one of our allowlisted
    # ASCII hosts, and the match below refuses it on its raw form.
    with contextlib.suppress(UnicodeError, UnicodeDecodeError):
        host = host.encode("idna").decode("ascii")
    return host


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


def host_allowed(host: str | None, kind: str = KIND_MODEL) -> bool:
    """Pure predicate: is this host on the list for this kind of fetch?"""
    normalized = normalize_host(host)
    if not normalized or _is_ip_literal(normalized):
        return False
    if kind == KIND_GIT:
        exact, suffixes = GIT_HOSTS_EXACT, GIT_HOSTS_SUFFIX
    else:
        exact, suffixes = MODEL_HOSTS_EXACT, MODEL_HOSTS_SUFFIX
    if normalized in exact:
        return True
    return any(normalized.endswith(suffix) for suffix in suffixes)


def check(url: str | None, *, kind: str = KIND_MODEL) -> CheckedUrl:
    """Validate one URL.  Raises :class:`HostNotAllowed` with a reason."""
    text = str(url or "").strip()
    if not text:
        raise HostNotAllowed(text, "no URL")
    if len(text) > 2048:
        raise HostNotAllowed(text, "URL is longer than 2048 characters")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
        raise HostNotAllowed(text, "URL contains control characters")
    try:
        parts = urlsplit(text)
    except ValueError as exc:
        raise HostNotAllowed(text, f"unparsable URL ({exc})") from exc
    scheme = (parts.scheme or "").lower()
    if scheme != _SCHEME:
        raise HostNotAllowed(text, f"scheme {scheme or '(none)'!r} is not https")
    if parts.username or parts.password or "@" in (parts.netloc or ""):
        raise HostNotAllowed(text, "credentials in the URL authority")
    try:
        raw_host = parts.hostname
    except ValueError as exc:
        raise HostNotAllowed(text, f"unparsable host ({exc})") from exc
    host = normalize_host(raw_host)
    if not host:
        raise HostNotAllowed(text, "no host")
    if _is_ip_literal(host):
        raise HostNotAllowed(text, "a bare IP address is never allowlisted", host)
    if not host_allowed(host, kind):
        raise HostNotAllowed(text, f"host {host!r} is not on the {kind} allowlist", host)
    return CheckedUrl(url=text, host=host, kind=kind)


def check_redirect(location: str | None, *, current: CheckedUrl, hop: int) -> CheckedUrl:
    """Validate one redirect hop *before* the request that follows it is made.

    Raises :class:`UpstreamUnavailable` rather than :class:`HostNotAllowed` when
    a hop leaves the allowlist: the download itself is what failed, and the
    error the API reports for that is ``UPSTREAM_UNAVAILABLE`` (R2).
    """
    if hop >= MAX_REDIRECTS:
        raise UpstreamUnavailable(
            f"The source redirected more than {MAX_REDIRECTS} times.",
            details={"host": current.host, "hops": hop})
    target = _absolutize(location, current.url)
    try:
        return check(target, kind=current.kind)
    except HostNotAllowed as exc:
        raise UpstreamUnavailable(
            "The source redirected to a host that is not allowlisted; "
            "the redirect was not followed.",
            details={"from_host": current.host, "reason": exc.reason,
                     "url": _redact(target), "hop": hop + 1},
        ) from exc


def _absolutize(location: str | None, base: str) -> str:
    """Resolve a relative ``Location`` against the hop it came from.

    ``urljoin`` is deliberately not used for cross-host cases: a relative
    redirect stays on the current host, and an absolute one is checked as it
    stands.  Anything else (``//host/path``) is expanded explicitly.
    """
    raw = str(location or "").strip()
    if not raw:
        return ""
    if raw.startswith("//"):
        return f"https:{raw}"
    if "://" in raw[:12]:
        return raw
    base_parts = urlsplit(base)
    if raw.startswith("/"):
        return f"{base_parts.scheme}://{base_parts.netloc}{raw}"
    prefix = base_parts.path.rsplit("/", 1)[0]
    return f"{base_parts.scheme}://{base_parts.netloc}{prefix}/{raw}"


def strip_auth_on_host_change(headers: dict, previous: CheckedUrl,
                              nxt: CheckedUrl) -> dict:
    """R2: no ``Authorization`` header survives a host change."""
    if previous.host == nxt.host:
        return dict(headers)
    return {k: v for k, v in headers.items() if k.lower() != "authorization"}


def describe() -> dict:
    """What the UI shows when it explains where a file may come from."""
    return {
        "model_hosts": sorted(MODEL_HOSTS_EXACT) + list(MODEL_HOSTS_SUFFIX),
        "git_hosts": sorted(GIT_HOSTS_EXACT),
        "scheme": _SCHEME,
        "max_redirects": MAX_REDIRECTS,
    }
