"""Session handling and request dispatch.

The Python layer owns *configuration* (headers, cookies, proxies, timeouts,
auth, verify, redirect and lazy policy) while the Rust core owns *transport*
(connection pooling, TLS, HTTP/2, decompression and concurrency limits).
"""

import builtins
import json as _json
import threading
from urllib.parse import urlencode

from . import _lazyreq
from ._version import __version__
from .exceptions import (
    ConnectionError,
    InvalidHeader,
    RequestException,
    Timeout,
)
from .models import LazyResponse, PreparedRequest, Response
from .structures import CaseInsensitiveDict

# The Python built-in ConnectionError, saved before our import shadows it.
_builtin_ConnectionError = builtins.ConnectionError

_DEFAULT_HEADERS = {
    "User-Agent": f"lazyreq/{__version__}",
    # Only advertise encodings the Rust core can actually decode.
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Accept": "*/*",
}


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------
def _normalize_headers(headers):
    """Convert any header mapping to a plain ``{str: str}`` dict."""
    if headers is None:
        return {}

    def _coerce(value):
        # bytes must be decoded as latin-1 (the HTTP/1.1 header encoding),
        # never wrapped in ``str()`` which would produce ``"b'...'"``.
        if isinstance(value, (bytes, bytearray)):
            text = bytes(value).decode("latin-1")
        else:
            text = str(value)
        if "\r" in text or "\n" in text:
            raise InvalidHeader(f"Header value must not contain newlines: {text!r}")
        return text

    if isinstance(headers, CaseInsensitiveDict):
        return {str(key): _coerce(value) for key, value in headers.items()}
    if hasattr(headers, "items"):
        items = headers.items()
    else:
        items = headers
    return {str(key): _coerce(value) for key, value in items}


def _normalize_params(params):
    """Convert params (dict / list of tuples / None) to ``[(str, str)]``.

    Sequence values are expanded into repeated keys, matching
    ``urllib.parse.urlencode(..., doseq=True)`` / ``requests``.
    """
    if params is None:
        return []
    if hasattr(params, "items"):
        items = params.items()
    else:
        items = params
    out = []
    for key, value in items:
        key = str(key)
        if isinstance(value, (list, tuple)):
            for item in value:
                out.append((key, "" if item is None else str(item)))
        else:
            out.append((key, "" if value is None else str(value)))
    return out


def _normalize_cookies(cookies):
    if cookies is None:
        return {}
    if hasattr(cookies, "items"):
        return {str(key): str(value) for key, value in cookies.items()}
    return {str(key): str(value) for key, value in cookies}


def _normalize_auth(auth):
    if auth is None:
        return None
    if isinstance(auth, (tuple, list)):
        if len(auth) < 2:
            return None
        return str(auth[0]), str(auth[1])
    username = getattr(auth, "username", None)
    password = getattr(auth, "password", None)
    if username is not None:
        return str(username), "" if password is None else str(password)
    return None


def _normalize_files(files):
    """Convert requests-style ``files`` into 4-tuples for the Rust core."""
    if not files:
        return None
    if hasattr(files, "items"):
        entries = files.items()
    else:
        entries = files

    normalized = []
    for name, value in entries:
        content_type = None
        if isinstance(value, tuple):
            if len(value) == 2:
                filename, content = value
            elif len(value) >= 3:
                filename, content, content_type = value[0], value[1], value[2]
            else:
                raise ValueError("lazyreq: malformed file tuple")
        else:
            filename, content = None, value

        if hasattr(content, "read"):
            content = content.read()
        if isinstance(content, str):
            content = content.encode("utf-8")
        normalized.append(
            (
                str(name),
                None if filename is None else str(filename),
                None if content_type is None else str(content_type),
                bytes(content),
            )
        )
    return normalized


def _prepare_body(data=None, json=None, files=None):
    """Return ``(body_bytes, content_type, form_fields)`` for the Rust core."""
    if files:
        form_fields = []
        if data is None:
            pass
        elif hasattr(data, "items"):
            form_fields = [
                (str(key), "" if value is None else str(value))
                for key, value in data.items()
            ]
        elif isinstance(data, (list, tuple)):
            form_fields = [
                (str(key), "" if value is None else str(value)) for key, value in data
            ]
        return None, None, form_fields

    if json is not None:
        return (
            _json.dumps(json, separators=(",", ":")).encode("utf-8"),
            "application/json",
            None,
        )

    if data is not None:
        if isinstance(data, (bytes, bytearray)):
            return bytes(data), None, None
        if isinstance(data, str):
            return data.encode("utf-8"), None, None
        if isinstance(data, (dict,)) or hasattr(data, "items"):
            return (
                urlencode(data, doseq=True).encode("utf-8"),
                "application/x-www-form-urlencoded",
                None,
            )
        if isinstance(data, (list, tuple)):
            return (
                urlencode(data, doseq=True).encode("utf-8"),
                "application/x-www-form-urlencoded",
                None,
            )
        if hasattr(data, "read"):
            return data.read(), None, None

    return None, None, None


def _split_timeout(timeout):
    """Return a ``requests``-style timeout value, preserving tuples."""
    if timeout is None:
        return None
    if isinstance(timeout, (tuple, list)):
        if len(timeout) >= 2:
            return (timeout[0], timeout[1])
        if len(timeout) == 1:
            return timeout[0]
        return None
    return timeout


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
def _dispatch(client, prepared, options, session=None):
    """Convert Python arguments to a Rust call and wrap the result."""
    headers = _normalize_headers(prepared.headers)
    params = _normalize_params(prepared.params)
    cookies = _normalize_cookies(prepared.cookies)
    auth = _normalize_auth(prepared.auth)
    files = _normalize_files(prepared.files)
    body, content_type, form_fields = _prepare_body(
        prepared.data, prepared.json, prepared.files
    )
    timeout = _split_timeout(prepared.timeout)

    proxies = dict(prepared.proxies or {})
    # ``proxies`` is the requests spelling; ``no_proxy`` may also live there.
    no_proxy = options.get("no_proxy")
    if no_proxy is None:
        no_proxy = proxies.pop("no_proxy", None)

    lazy = bool(options.get("lazy"))
    lazy_json = bool(options.get("lazy_json"))
    call = {
        "headers": headers or None,
        "params": params or None,
        "body": body,
        "content_type": content_type,
        "auth": auth,
        "timeout": timeout,
        "verify": options.get("verify", True),
        "allow_redirects": bool(options.get("allow_redirects", True)),
        "cookies": cookies or None,
        "proxies": proxies or None,
        "no_proxy": no_proxy,
        "no_proxy_all": bool(options.get("no_proxy_all", False)),
        "max_concurrency": int(options.get("max_concurrency") or 100),
        "files": files,
        "form_fields": form_fields,
        "trust_env": bool(options.get("trust_env", True)),
    }

    try:
        if lazy:
            if session is not None:
                call["cookie_callback"] = session._store_cookies
            handle = client.request_lazy(prepared.method, prepared.url, **call)
            return LazyResponse(
                handle, request=prepared, lazy_json=lazy_json
            )
        raw = client.request(prepared.method, prepared.url, **call)
    except RequestException as exc:
        if getattr(exc, "request", None) is None:
            exc.request = prepared
        raise
    except builtins.TimeoutError as exc:
        error = Timeout(str(exc))
        error.request = prepared
        raise error from exc
    except (_builtin_ConnectionError, OSError) as exc:
        error = ConnectionError(str(exc))
        error.request = prepared
        raise error from exc
    except Exception as exc:
        error = RequestException(str(exc))
        error.request = prepared
        raise error from exc

    response = Response(raw, request=prepared, lazy=False, lazy_json=lazy_json)
    if session is not None:
        session._store_cookies(raw.set_cookies)
    return response


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------
class Session:
    """A session with persistent headers, cookies and connection pooling.

    Compatible with ``requests.Session``/``pyureq.Session``::

        with lazyreq.Session() as s:
            s.headers.update({"X-MyHeader": "value"})
            r = s.get("https://example.com")
    """

    def __init__(self, lazy=False):
        self.headers = CaseInsensitiveDict(_DEFAULT_HEADERS.copy())
        self.auth = None
        self.cookies = {}
        self.proxies = {}
        self._verify = True
        self.params = {}
        self.timeout = None
        self.allow_redirects = True
        self.lazy = bool(lazy)
        self.lazy_json = False
        self.max_concurrency = 100
        self.trust_env = True
        self._cookie_lock = threading.Lock()
        self._client = _lazyreq.RustClient(
            verify=True,
            timeout=None,
            no_proxy_all=False,
            max_concurrency=self.max_concurrency,
        )

    # ------------------------------------------------------------------
    # TLS verification
    # ------------------------------------------------------------------
    @property
    def verify(self):
        """TLS verification mode.

        Accepts the same values as ``requests``: ``True`` (verify against the
        platform trust store), ``False`` (skip verification), or a path to a
        PEM CA bundle.  Changing it recreates the connection pool.
        """
        return self._verify

    @verify.setter
    def verify(self, value):
        self._verify = value
        if self._client is not None:
            self._client.close()

    # ------------------------------------------------------------------
    # Cookies
    # ------------------------------------------------------------------
    def _store_cookies(self, set_cookies):
        """Merge ``Set-Cookie`` values into the session cookie jar."""
        with self._cookie_lock:
            for header in set_cookies or ():
                first = header.split(";", 1)[0].strip()
                if "=" in first:
                    name, _, value = first.partition("=")
                    name = name.strip()
                    if name:
                        self.cookies[name] = value.strip()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _merge(self, kwargs):
        """Merge session defaults with per-request overrides."""
        per_request_proxies = kwargs.get("proxies")
        proxies = dict(self.proxies or {})
        if isinstance(per_request_proxies, str):
            # pyureq-style bare proxy string ("use this proxy for everything").
            proxies = {"all": per_request_proxies}
        elif per_request_proxies:
            proxies.update(dict(per_request_proxies))

        # Explicit ``proxies={}`` disables system proxies (requests/pyureq
        # semantics); an empty dict with no explicit override keeps
        # ``trust_env`` behaviour.
        if per_request_proxies is not None and not per_request_proxies:
            no_proxy_all = True
        else:
            no_proxy_all = not self.trust_env
        no_proxy_all = no_proxy_all or (
            bool(proxies) and all(not value for value in proxies.values())
        )
        no_proxy_all = no_proxy_all or bool(kwargs.get("no_proxy_all"))

        return {
            "headers": {
                **_normalize_headers(self.headers),
                **_normalize_headers(kwargs.get("headers")),
            },
            "params": _normalize_params(self.params)
            + _normalize_params(kwargs.get("params")),
            "cookies": {
                **_normalize_cookies(self.cookies),
                **_normalize_cookies(kwargs.get("cookies")),
            },
            "auth": kwargs.get("auth", self.auth),
            "timeout": kwargs.get("timeout", self.timeout),
            "verify": kwargs.get("verify", self.verify),
            "allow_redirects": kwargs.get("allow_redirects", self.allow_redirects),
            "data": kwargs.get("data"),
            "json": kwargs.get("json"),
            "files": kwargs.get("files"),
            "proxies": proxies,
            "no_proxy": kwargs.get("no_proxy"),
            "no_proxy_all": no_proxy_all,
            "trust_env": kwargs.get("trust_env", self.trust_env),
            "lazy": kwargs.get("lazy", self.lazy),
            "lazy_json": kwargs.get("lazy_json", self.lazy_json),
            "max_concurrency": kwargs.get("max_concurrency", self.max_concurrency),
        }

    # ------------------------------------------------------------------
    # Public request methods
    # ------------------------------------------------------------------
    def request(self, method, url, **kwargs):
        """Construct and send a request."""
        merged = self._merge(kwargs)
        prepared = PreparedRequest(
            method=str(method).upper(),
            url=url,
            headers=merged["headers"],
            data=merged["data"],
            json=merged["json"],
            params=merged["params"],
            cookies=merged["cookies"],
            timeout=merged["timeout"],
            auth=merged["auth"],
            proxies=merged["proxies"],
            verify=merged["verify"],
            allow_redirects=merged["allow_redirects"],
            files=merged["files"],
        )
        return _dispatch(self._client, prepared, merged, session=self)

    def get(self, url, params=None, **kwargs):
        """Send a GET request."""
        kwargs.setdefault("allow_redirects", True)
        return self.request("GET", url, params=params, **kwargs)

    def post(self, url, data=None, json=None, **kwargs):
        """Send a POST request."""
        return self.request("POST", url, data=data, json=json, **kwargs)

    def put(self, url, data=None, **kwargs):
        """Send a PUT request."""
        return self.request("PUT", url, data=data, **kwargs)

    def patch(self, url, data=None, **kwargs):
        """Send a PATCH request."""
        return self.request("PATCH", url, data=data, **kwargs)

    def delete(self, url, **kwargs):
        """Send a DELETE request."""
        return self.request("DELETE", url, **kwargs)

    def head(self, url, **kwargs):
        """Send a HEAD request."""
        kwargs.setdefault("allow_redirects", False)
        return self.request("HEAD", url, **kwargs)

    def options(self, url, **kwargs):
        """Send an OPTIONS request."""
        kwargs.setdefault("allow_redirects", True)
        return self.request("OPTIONS", url, **kwargs)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        """Close the session (releases pooled connections)."""
        if self._client is not None:
            self._client.close()

    def __repr__(self):
        return "<lazyreq.Session>"


class LazySession(Session):
    """A :class:`Session` whose requests return :class:`LazyResponse` by
    default.

    Calling the session's HTTP methods returns immediately, firing all
    requests concurrently on the shared Tokio runtime::

        with LazySession() as s:
            pending = [s.get(url) for url in urls]   # no await, all in flight
            bodies = [r.text for r in pending]       # resolve on access
    """

    def __init__(self):
        super().__init__(lazy=True)

    def request(self, method, url, **kwargs):
        kwargs.setdefault("lazy", True)
        return super().request(method, url, **kwargs)

    def __repr__(self):
        return "<lazyreq.LazySession>"


def session():
    """Return a :class:`Session` for context-manager use."""
    return Session()