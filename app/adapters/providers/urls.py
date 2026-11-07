"""Offline URL validation. Loopback exceptions never use DNS resolution."""

from urllib.parse import unquote, urlsplit

from app.errors import LiveStartRefused


def validate_compatible_url(base_url: str | None, api_key: str | None = None) -> bool:
    """Return whether the host is allowlisted loopback, or refuse the URL."""
    if not base_url:
        raise LiveStartRefused("BASE_URL_NOT_CONFIGURED")
    if api_key and (api_key in base_url or api_key in unquote(base_url)):
        raise LiveStartRefused("BASE_URL_CREDENTIALS")
    try:
        url = urlsplit(base_url)
        # Reject normalization ambiguities, invalid ports, and empty delimiters too.
        port = url.port
        if (
            not url.hostname
            or url.username is not None
            or url.password is not None
            or "?" in base_url
            or "#" in base_url
            or "\\" in base_url
            or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in base_url)
            or (port is not None and port == 0)
        ):
            raise ValueError
    except ValueError:
        raise LiveStartRefused("BASE_URL_UNSUPPORTED") from None
    loopback = url.hostname.lower() in {"localhost", "127.0.0.1", "::1"}
    if url.scheme != "https" and not (url.scheme == "http" and loopback):
        raise LiveStartRefused("BASE_URL_INSECURE")
    return loopback
