"""Shared utility helpers for the plugin sidecar."""

# Headers whose values must never appear in logs in clear text.
_SENSITIVE_HEADERS = frozenset({
    "authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "proxy-authorization",
})


def redact_headers(headers: dict, *, visible: int = 4) -> dict:
    """Return a copy of *headers* with sensitive values redacted.

    Handles both ``str`` and ``list[str]`` header values. Header name
    matching is case-insensitive.
    """
    out: dict = {}
    for key, value in headers.items():
        if key.lower() in _SENSITIVE_HEADERS:
            if isinstance(value, list):
                out[key] = [redact_token(v, visible=visible) for v in value]
            else:
                out[key] = redact_token(str(value), visible=visible)
        else:
            out[key] = value
    return out


def redact_token(value: str, *, visible: int = 4) -> str:
    """Return a safely-loggable representation of a sensitive token or secret.

    Shows the first *visible* characters followed by ``...[redacted]`` so logs
    remain useful for debugging without exposing the full secret.
    """
    if not value:
        return "<empty>"
    if len(value) <= visible or visible <= 0:
        return "[redacted]"
    return f"{value[:visible]}...[redacted]"
