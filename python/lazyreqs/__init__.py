"""``lazyreqs`` — a requests/pyureq drop-in replacement powered by Rust.

The package is a 100% API-compatible replacement for both ``pyureq`` and
``requests``::

    import lazyreqs as requests
    r = requests.get("https://example.com")
    print(r.status_code, r.text)

It adds *lazy concurrency*: with ``lazy=True`` (or a :class:`LazySession`)
every call returns a :class:`LazyResponse` immediately while the request runs
on a shared Tokio runtime.  Nothing ever needs ``await``::

    import lazyreqs

    pending = [lazyreqs.get(url, lazy=True) for url in urls]
    bodies = [r.text for r in pending]   # resolves on access, all concurrent

Or defer even JSON parsing::

    data = lazyreqs.get(url, lazy_json=True).json()   # a LazyDict
    print(data["results"])                           # parsed right here
"""

from . import _lazyreqs as _lazyreqs
from . import exceptions, models, sessions, structures
from ._version import __version__
from .exceptions import (
    ChunkedEncodingError,
    ConnectionError,
    ConnectTimeout,
    ContentDecodingError,
    HTTPError,
    InvalidHeader,
    InvalidSchema,
    InvalidURL,
    MissingSchema,
    ProxyError,
    ReadTimeout,
    RequestException,
    RetryError,
    SSLError,
    Timeout,
    TooManyRedirects,
    URLRequired,
)
from .models import LazyResponse, PreparedRequest, Response
from .sessions import LazySession, Session
from .structures import CaseInsensitiveDict, LazyDict

__author__ = "lazyreqs contributors"

__all__ = [
    "CaseInsensitiveDict",
    "ChunkedEncodingError",
    "ConnectTimeout",
    "ConnectionError",
    "ContentDecodingError",
    "HTTPError",
    "InvalidHeader",
    "InvalidSchema",
    "InvalidURL",
    "LazyDict",
    "LazyResponse",
    "LazySession",
    "MissingSchema",
    "PreparedRequest",
    "ProxyError",
    "ReadTimeout",
    # exceptions
    "RequestException",
    "Response",
    "RetryError",
    "SSLError",
    "Session",
    "Timeout",
    "TooManyRedirects",
    "URLRequired",
    "__version__",
    "default_lazy",
    "delete",
    "exceptions",
    "get",
    "head",
    "lazy_session",
    "models",
    "options",
    "patch",
    "post",
    "put",
    "request",
    "session",
    "sessions",
    "set_default_lazy",
    "structures",
]


# ---------------------------------------------------------------------------
# Top-level convenience functions (mirrors ``requests.*`` / ``pyureq.*``)
# ---------------------------------------------------------------------------
_DEFAULT_LAZY = False


def set_default_lazy(value):
    """Make the top-level helpers lazy by default.

    Set this once, at startup::

        lazyreqs.set_default_lazy(True)
        lazyreqs.get(url)   # now returns a LazyResponse

    Sessions are unaffected; use ``Session(lazy=True)`` or
    ``LazySession()`` for the same effect on a single object.
    """
    global _DEFAULT_LAZY
    _DEFAULT_LAZY = bool(value)


def default_lazy():
    """Return the current default for the top-level helpers."""
    return _DEFAULT_LAZY


def request(method, url, **kwargs):
    """Construct and send a request.

    Parameters
    ----------
    method:
        HTTP method, e.g. ``"GET"``, ``"POST"``.
    url:
        URL for the request.
    params:
        Dictionary, list of tuples or string to send in the query string.
    data:
        Dictionary, list of tuples, bytes or file-like object to send in the
        body.
    json:
        A JSON-serialisable Python object to send in the body.
    headers:
        Dictionary of HTTP headers to send.
    cookies:
        Dictionary of cookies to send.
    auth:
        ``(username, password)`` tuple for Basic auth.
    timeout:
        Seconds to wait for the server (float or ``(connect, read)`` tuple).
    allow_redirects:
        Follow 3xx redirects (default ``True``).
    verify:
        ``True``/``False`` or a path to a CA bundle.
    proxies:
        Requests-style proxy mapping (``http``, ``https``, ``all``).
    lazy:
        Return a :class:`LazyResponse` immediately and resolve on access.
    lazy_json:
        Defer JSON parsing by returning a :class:`LazyDict` from ``.json()``.
    """
    return Session(lazy=_DEFAULT_LAZY).request(method.upper(), url, **kwargs)


def get(url, params=None, **kwargs):
    """Send a GET request."""
    kwargs.setdefault("allow_redirects", True)
    return request("GET", url, params=params, **kwargs)


def post(url, data=None, json=None, **kwargs):
    """Send a POST request."""
    return request("POST", url, data=data, json=json, **kwargs)


def put(url, data=None, **kwargs):
    """Send a PUT request."""
    return request("PUT", url, data=data, **kwargs)


def patch(url, data=None, **kwargs):
    """Send a PATCH request."""
    return request("PATCH", url, data=data, **kwargs)


def delete(url, **kwargs):
    """Send a DELETE request."""
    return request("DELETE", url, **kwargs)


def head(url, **kwargs):
    """Send a HEAD request."""
    kwargs.setdefault("allow_redirects", False)
    return request("HEAD", url, **kwargs)


def options(url, **kwargs):
    """Send an OPTIONS request."""
    kwargs.setdefault("allow_redirects", True)
    return request("OPTIONS", url, **kwargs)


def session():
    """Return a :class:`Session` for context-manager use."""
    return Session(lazy=_DEFAULT_LAZY)


def lazy_session():
    """Return a :class:`LazySession` for concurrent, zero-await request bursts."""
    return LazySession()