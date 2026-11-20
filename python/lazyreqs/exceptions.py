"""Exception hierarchy matching ``requests.exceptions`` and
``pyureq.exceptions``.

Every exception accepts the ``response`` and ``request`` keyword arguments so
that callers can inspect the exchange that failed, exactly like ``requests``.
"""

__all__ = [
    "ChunkedEncodingError",
    "ConnectTimeout",
    "ConnectionError",
    "ContentDecodingError",
    "HTTPError",
    "InvalidHeader",
    "InvalidSchema",
    "InvalidURL",
    "MissingSchema",
    "ProxyError",
    "ReadTimeout",
    "RequestException",
    "RetryError",
    "SSLError",
    "Timeout",
    "TooManyRedirects",
    "URLRequired",
]


class RequestException(IOError):
    """Base class for all ``lazyreqs`` exceptions."""

    def __init__(self, *args, response=None, request=None, **kwargs):
        self.response = response
        self.request = request
        super().__init__(*args, **kwargs)


class HTTPError(RequestException):
    """An HTTP error occurred (4xx or 5xx status code)."""


class ConnectionError(RequestException):
    """A connection error occurred."""


class ProxyError(ConnectionError):
    """A proxy error occurred."""


class SSLError(ConnectionError):
    """An SSL error occurred."""


# ``Timeout`` inherits from ``RequestException`` (not ``ConnectionError``) so
# that ``ConnectTimeout(ConnectionError, Timeout)`` has a valid MRO — the same
# design used by ``requests``.
class Timeout(RequestException):
    """The request timed out.

    Catching this catches both :class:`ConnectTimeout` and
    :class:`ReadTimeout`.
    """


class ConnectTimeout(ConnectionError, Timeout):
    """Timed out while trying to connect to the remote server."""


class ReadTimeout(Timeout):
    """The server did not send data in the allotted time."""


class URLRequired(RequestException, ValueError):
    """A valid URL is required."""


class TooManyRedirects(RequestException):
    """Too many redirects."""


class MissingSchema(RequestException, ValueError):
    """The URL scheme (e.g. ``http``/``https``) is missing."""


class InvalidSchema(RequestException, ValueError):
    """The URL scheme is invalid."""


class InvalidURL(RequestException, ValueError):
    """The URL is invalid."""


class InvalidHeader(RequestException, ValueError):
    """An invalid header value was provided."""


class ChunkedEncodingError(RequestException):
    """The server declared chunked encoding but sent an invalid chunk."""


class ContentDecodingError(RequestException):
    """Failed to decode response content."""


class RetryError(RequestException):
    """Request retries exhausted."""
