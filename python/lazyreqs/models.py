"""Response models: ``PreparedRequest``, ``Response`` and ``LazyResponse``.

``Response`` is the ``requests``/``pyureq`` compatible model wrapping the Rust
``RawResponse``.  ``LazyResponse`` subclasses it and resolves the underlying
response transparently on first attribute access — no ``await`` anywhere.
"""

import http
import json as _json
import threading
from datetime import timedelta

from .structures import CaseInsensitiveDict, LazyDict

try:  # pragma: no cover - optional dependency
    from . import _lazyreqs
except ImportError:  # pragma: no cover - source checkout without build
    _lazyreqs = None

from .exceptions import HTTPError, RequestException

__all__ = ["LazyResponse", "PreparedRequest", "Response"]


class PreparedRequest:
    """Metadata about a request that was sent.

    Mirrors the attributes carried by ``requests.PreparedRequest`` so
    ``exception.request.method`` and friends keep working.
    """

    def __init__(
        self,
        method=None,
        url=None,
        headers=None,
        data=None,
        json=None,
        params=None,
        cookies=None,
        timeout=None,
        auth=None,
        proxies=None,
        verify=True,
        allow_redirects=True,
        files=None,
    ):
        self.method = method
        self.url = url
        self.headers = headers if headers is not None else CaseInsensitiveDict()
        self.data = data
        self.json = json
        self.params = params or []
        self.cookies = cookies or {}
        self.timeout = timeout
        self.auth = auth
        self.proxies = proxies or {}
        self.verify = verify
        self.allow_redirects = allow_redirects
        self.files = files

    @property
    def body(self):
        """The request body as bytes (``None`` when there is no body)."""
        if isinstance(self.data, (bytes, bytearray)):
            return bytes(self.data)
        if isinstance(self.data, str):
            return self.data.encode("utf-8")
        return None

    def prepare(self, **kwargs):  # pragma: no cover - compatibility no-op
        return self

    def copy(self):
        return PreparedRequest(
            method=self.method,
            url=self.url,
            headers=self.headers,
            data=self.data,
            json=self.json,
            params=list(self.params),
            cookies=dict(self.cookies),
            timeout=self.timeout,
            auth=self.auth,
            proxies=dict(self.proxies),
            verify=self.verify,
            allow_redirects=self.allow_redirects,
            files=self.files,
        )

    def __repr__(self):
        return f"<PreparedRequest [{self.method} {self.url}]>"


class Response:
    """The response object returned by ``lazyreqs.get``/``Session.get`` (and
    every other convenience function).

    Drop-in compatible with ``requests.Response`` and ``pyureq.Response``.
    """

    def __init__(self, raw, request=None, lazy=False, lazy_json=False):
        self._raw = raw
        self._content = bytes(raw.body)
        self._headers = CaseInsensitiveDict(raw.headers)
        self._elapsed = timedelta(seconds=raw.elapsed_secs)
        self._encoding_override = None
        self._request = request
        self.lazy = lazy
        self.lazy_json = lazy_json

    # ------------------------------------------------------------------
    # Core attributes
    # ------------------------------------------------------------------
    @property
    def status_code(self):
        """Integer status code (e.g. 200, 404)."""
        return self._raw.status_code

    @property
    def headers(self):
        """Case-insensitive dict of response headers."""
        return self._headers

    @property
    def content(self):
        """Content of the response in bytes."""
        return self._content

    @property
    def encoding(self):
        """Encoding used to decode :attr:`text`."""
        if self._encoding_override:
            return self._encoding_override
        if self._raw.encoding:
            return self._raw.encoding
        content_type = self._headers.get("content-type", "")
        for part in content_type.split(";"):
            part = part.strip()
            if part.lower().startswith("charset="):
                return part[len("charset="):].strip().strip('"')
        return "utf-8"

    @encoding.setter
    def encoding(self, value):
        self._encoding_override = value

    @property
    def text(self):
        """Content of the response, decoded with :attr:`encoding`."""
        return self.content.decode(self.encoding, errors="replace")

    @property
    def url(self):
        """Final URL of the response (after redirects)."""
        return self._raw.url

    @property
    def ok(self):
        """``True`` if ``status_code`` is below 400."""
        return self.status_code < 400

    @property
    def is_redirect(self):
        """``True`` for 301/302/303/307/308 responses."""
        return self.status_code in (301, 302, 303, 307, 308)

    @property
    def is_permanent_redirect(self):
        """``True`` for 301/308 responses."""
        return self.status_code in (301, 308)

    @property
    def elapsed(self):
        """Time between sending the request and finishing the response."""
        return self._elapsed

    @property
    def apparent_encoding(self):
        """Best-effort encoding detection (falls back to :attr:`encoding`)."""
        try:
            import chardet  # type: ignore

            detected = chardet.detect(self.content)
            return detected.get("encoding") or self.encoding
        except Exception:  # noqa: BLE001 - detection is best-effort
            try:
                import charset_normalizer  # type: ignore

                best = charset_normalizer.from_bytes(self.content).best()
                return str(best.encoding) if best else self.encoding
            except Exception:  # noqa: BLE001
                return self.encoding

    @property
    def reason(self):
        """Textual reason phrase (e.g. ``"Not Found"``)."""
        if self._raw.reason:
            return self._raw.reason
        try:
            return http.HTTPStatus(self.status_code).phrase
        except ValueError:
            return ""

    @property
    def request(self):
        """The :class:`PreparedRequest` that produced this response."""
        return self._request

    @property
    def raw(self):
        """The underlying Rust :class:`RawResponse`."""
        return self._raw

    @property
    def cookies(self):
        """Cookies sent back by the server, as a ``name -> value`` dict."""
        return dict(self._raw.cookies)

    @property
    def history(self):
        """Redirect history.  Always empty (redirects are followed inline)."""
        return []

    @property
    def links(self):
        """Parsed ``Link`` header(s), keyed by ``rel``."""
        header = self._headers.get("link", "")
        if not header:
            return {}
        result = {}
        for part in header.split(","):
            part = part.strip()
            segments = part.split(";")
            link = {"url": segments[0].strip().strip("<>")}
            for attribute in segments[1:]:
                if "=" in attribute:
                    key, _, value = attribute.strip().partition("=")
                    link[key.strip()] = value.strip().strip('"')
            result[link.get("rel", link["url"])] = link
        return result

    # ------------------------------------------------------------------
    # Body helpers
    # ------------------------------------------------------------------
    def iter_content(self, chunk_size=1, decode_unicode=False):
        """Iterate over the response data in ``chunk_size`` byte chunks."""
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        if decode_unicode:
            if self.encoding is None:
                raise ValueError("no encoding set; cannot decode response")
            content = self.content.decode(self.encoding, errors="replace")
            step = max(1, int(chunk_size))
            for index in range(0, len(content), step):
                yield content[index:index + step]
            return
        for index in range(0, len(self.content), chunk_size):
            yield self.content[index:index + chunk_size]

    def iter_lines(self, chunk_size=512, decode_unicode=False, delimiter=None):
        """Iterate over the response, yielding lines."""
        pending = None
        for chunk in self.iter_content(
            chunk_size=chunk_size, decode_unicode=decode_unicode
        ):
            if pending is not None:
                chunk = pending + chunk
            if delimiter:
                lines = chunk.split(delimiter)
            else:
                lines = chunk.splitlines()
            if lines and lines[-1] and chunk and lines[-1][-1] == chunk[-1]:
                pending = lines.pop()
            else:
                pending = None
            yield from lines
        if pending is not None:
            yield pending

    # ------------------------------------------------------------------
    # Methods
    # ------------------------------------------------------------------
    def json(self, **kwargs):
        """Return the JSON-decoded content.

        When ``lazy_json=True`` was requested, the JSON document is parsed
        only when the returned :class:`LazyDict` is first touched.
        """
        if self.lazy_json:
            return LazyDict(lambda: _json.loads(self.content, **kwargs))
        return _json.loads(self.content, **kwargs)

    def raise_for_status(self):
        """Raise :class:`HTTPError` for 4xx/5xx status codes."""
        if 400 <= self.status_code < 600:
            kind = "Client" if self.status_code < 500 else "Server"
            error = HTTPError(
                f"{self.status_code} {kind} Error: {self.url}",
                response=self,
                request=self._request,
            )
            raise error

    def close(self):
        """Release the connection.  A no-op: the Rust pool manages connections."""


    def __bool__(self):
        return self.ok

    def __repr__(self):
        return f"<Response [{self.status_code}]>"

    def __getstate__(self):
        return {
            "status_code": self.status_code,
            "content": self.content,
            "headers": dict(self._headers),
            "url": self.url,
            "elapsed": self.elapsed,
            "encoding": self._raw.encoding,
            "reason": self.reason,
            "request": self._request,
            "lazy": self.lazy,
            "lazy_json": self.lazy_json,
        }

    def __setstate__(self, state):
        # Rebuild from plain values; used by ``copy``/pickle helpers.
        class _StandIn:
            pass

        standin = _StandIn()
        standin.status_code = state["status_code"]
        standin.body = state["content"]
        standin.headers = state["headers"]
        standin.url = state["url"]
        standin.elapsed_secs = state["elapsed"].total_seconds()
        standin.encoding = state["encoding"]
        standin.reason = state["reason"]
        standin.cookies = []
        standin.request_method = ""
        standin.request_url = state["url"]
        standin.request_headers = []
        self._raw = standin
        self._content = state["content"]
        self._headers = CaseInsensitiveDict(state["headers"])
        self._elapsed = state["elapsed"]
        self._encoding_override = None
        self._request = state.get("request")
        self.lazy = state["lazy"]
        self.lazy_json = state["lazy_json"]


class LazyResponse(Response):
    """A response whose underlying request has already been dispatched.

    Creating a ``LazyResponse`` returns in a few microseconds.  The first
    attribute access blocks until the request finishes; from then on the
    object behaves exactly like :class:`Response`.
    """

    def __init__(self, handle, request=None, lazy_json=False):
        self._handle = handle
        self._request = request
        self.lazy = True
        self.lazy_json = lazy_json
        self._lazy_resolved = False
        self._lazy_lock = threading.Lock()

    def _resolve(self):
        """Materialise the response (exactly once)."""
        if object.__getattribute__(self, "_lazy_resolved"):
            return object.__getattribute__(self, "_raw")

        with object.__getattribute__(self, "_lazy_lock"):
            if object.__getattribute__(self, "_lazy_resolved"):
                return object.__getattribute__(self, "_raw")
            try:
                raw = object.__getattribute__(self, "_handle").resolve()
            except RequestException as exc:
                if getattr(exc, "request", None) is None:
                    exc.request = object.__getattribute__(self, "_request")
                raise
            object.__setattr__(self, "_raw", raw)
            object.__setattr__(self, "_content", bytes(raw.body))
            object.__setattr__(self, "_headers", CaseInsensitiveDict(raw.headers))
            object.__setattr__(self, "_elapsed", timedelta(seconds=raw.elapsed_secs))
            if "_encoding_override" not in object.__getattribute__(self, "__dict__"):
                object.__setattr__(self, "_encoding_override", None)
            object.__setattr__(self, "_lazy_resolved", True)
            return raw

    def __getattr__(self, name):
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        self._resolve()
        return object.__getattribute__(self, name)

    def resolve(self):
        """Block until the request completes and return ``self``."""
        self._resolve()
        return self

    def wait(self):
        """Alias for :meth:`resolve`."""
        return self.resolve()

    @property
    def response(self):
        """The resolved :class:`Response` (``self``)."""
        self._resolve()
        return self

    def is_ready(self):
        """``True`` once the background request has completed (non-blocking)."""
        try:
            handle = object.__getattribute__(self, "_handle")
        except AttributeError:
            # Unpickled instances carry the resolved payload, not the handle.
            return bool(object.__getattribute__(self, "_lazy_resolved"))
        return handle.is_ready()

    def __getstate__(self):
        # Pickling forces resolution so the copy is a plain, usable Response.
        self._resolve()
        state = super().__getstate__()
        state["_lazy_resolved"] = True
        return state

    def __setstate__(self, state):
        super().__setstate__(state)
        object.__setattr__(self, "_lazy_resolved", True)
        object.__setattr__(self, "_lazy_lock", threading.Lock())

    @property
    def ready(self):
        return self.is_ready()

    def __repr__(self):
        if object.__getattribute__(self, "_lazy_resolved"):
            return f"<LazyResponse [{object.__getattribute__(self, '_raw').status_code}]>"
        return "<LazyResponse [pending]>"