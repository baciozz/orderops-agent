"""Browser request boundary for the unauthenticated, loopback-only demo.

Not authentication: non-browser clients can supply or omit these headers.
Never trust forwarded host/proto to expand the accepted origin set.
"""

import re
from urllib.parse import urlsplit

from fastapi import Request

from app.errors import AppError

_AUTHORITY = re.compile(r"(localhost|127\.0\.0\.1|\[::1\])(?::([0-9]{1,5}))?", re.IGNORECASE)
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _authority(value: str, scheme: str) -> tuple[str, int] | None:
    match = _AUTHORITY.fullmatch(value)
    if not match or scheme not in {"http", "https"}:
        return None
    port = int(match[2]) if match[2] is not None else (443 if scheme == "https" else 80)
    if not 1 <= port <= 65535:
        return None
    return match[1].lower(), port


def _matches(value: str, scheme: str, authority: tuple[str, int], *, referer=False) -> bool:
    # urlsplit otherwise silently strips some control characters.
    if "#" in value or (not referer and "?" in value):
        return False
    if any(ord(char) <= 32 or ord(char) == 127 for char in value):
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return (
        parsed.scheme == scheme
        and _authority(parsed.netloc, parsed.scheme) == authority
        and not parsed.fragment
        and (referer or not (parsed.path or parsed.query))
    )


def browser_request_error(request: Request) -> AppError | None:
    """Reject before body consumption or dispatch; absent browser evidence permits CLI."""
    hosts = request.headers.getlist("host")
    scheme = request.scope["scheme"]
    authority = _authority(hosts[0], scheme) if len(hosts) == 1 else None
    if authority is None:
        return AppError("仅允许本机演示主机", code="UNTRUSTED_HOST", http_status=400)
    if request.method in _SAFE_METHODS:
        return None

    origins = request.headers.getlist("origin")
    sites = request.headers.getlist("sec-fetch-site")
    referers = request.headers.getlist("referer")
    denied = any(len(values) > 1 for values in (origins, sites, referers))
    if sites and sites[0] not in {"same-origin", "none"}:
        denied = True
    if origins:
        denied |= not _matches(origins[0], scheme, authority)
    if referers:
        denied |= not _matches(referers[0], scheme, authority, referer=True)
    fetch_metadata = any(name.startswith("sec-fetch-") for name in request.headers)
    if not origins and not referers and fetch_metadata and sites != ["same-origin"]:
        denied = True
    if denied:
        return AppError("拒绝非同源浏览器请求", code="CROSS_ORIGIN_REQUEST", http_status=403)
    return None
