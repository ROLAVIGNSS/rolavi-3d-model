"""HTTP(S) requests for the base map tiles and ROLAVI Viewer.

Only ``http://`` and ``https://`` addresses are opened: the opener has no handlers for
``file://``, ``ftp://`` or ``data:``, so neither a typed address (for example a custom XYZ
server) nor a redirect can make the plugin read local files.
"""
from __future__ import annotations

import ssl
import urllib.request
from urllib.parse import urlsplit

SCHEMES = ("http", "https")


class UrlError(ValueError):
    """The address is not http:// or https://."""


def check_url(url: str) -> str:
    """The URL itself if it is http(s); UrlError otherwise."""
    if urlsplit(str(url)).scheme.lower() not in SCHEMES:
        raise UrlError(f"Only http:// and https:// addresses can be opened: {url}")
    return url


def http_opener(context: ssl.SSLContext | None = None) -> urllib.request.OpenerDirector:
    """An opener that speaks HTTP and HTTPS (certificates verified) and nothing else."""
    opener = urllib.request.OpenerDirector()
    for handler in (
        urllib.request.ProxyHandler(),
        urllib.request.UnknownHandler(),
        urllib.request.HTTPHandler(),
        urllib.request.HTTPSHandler(context=context or ssl.create_default_context()),
        urllib.request.HTTPDefaultErrorHandler(),
        urllib.request.HTTPRedirectHandler(),
        urllib.request.HTTPErrorProcessor(),
    ):
        opener.add_handler(handler)
    return opener


def open_url(request: urllib.request.Request | str, timeout: float,
             opener: urllib.request.OpenerDirector | None = None):
    """Open an http(s) request; the response works as a context manager like ``urlopen``'s."""
    url = request.full_url if isinstance(request, urllib.request.Request) else request
    check_url(url)
    return (opener or http_opener()).open(request, timeout=timeout)
