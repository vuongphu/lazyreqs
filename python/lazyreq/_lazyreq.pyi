"""Type stubs for the compiled ``lazyreq._lazyreq`` extension module."""

from typing import Any

__version__: str

#: Number of worker threads in the shared Tokio runtime.
worker_threads: int


class RawResponse:
    """Immutable view over a completed response, produced by the Rust core."""

    status_code: int
    reason: str
    #: Raw ``(name, value)`` pairs in wire order.
    headers: list[tuple[str, str]]
    #: Response body, already decompressed.
    body: bytes
    url: str
    elapsed_secs: float
    #: Charset from ``Content-Type``, when advertised.
    encoding: str | None
    http_version: str
    #: Every ``Set-Cookie`` header, verbatim.
    set_cookies: list[str]
    #: Parsed ``(name, value)`` cookie pairs.
    cookies: list[tuple[str, str]]
    request_method: str
    request_url: str
    request_headers: list[tuple[str, str]]


class LazyResponse:
    """Handle for an in-flight request dispatched on the Tokio runtime."""

    def resolve(self) -> RawResponse:
        """Block until the request completes and return the raw response."""

    def wait(self) -> RawResponse:
        """Alias for :meth:`resolve`."""

    def is_ready(self) -> bool:
        """``True`` once the background request has completed."""

    @property
    def ready(self) -> bool: ...

    @property
    def request_method(self) -> str: ...

    @property
    def request_url(self) -> str: ...


class RustClient:
    """Owns the reqwest client cache and the concurrency semaphore."""

    def __init__(
        self,
        verify: Any = None,
        timeout: Any = None,
        no_proxy_all: bool = False,
        max_concurrency: int = 100,
    ) -> None: ...

    def request(
        self, method: str | None = None, url: str | None = None, **kwargs: Any
    ) -> RawResponse:
        """Send a request, releasing the GIL while waiting."""

    def request_lazy(
        self, method: str | None = None, url: str | None = None, **kwargs: Any
    ) -> LazyResponse:
        """Dispatch a request and return a handle without blocking."""

    def close(self) -> None:
        """Drop cached clients and their connection pools."""

    def in_flight(self) -> int:
        """Number of requests currently in flight."""