"""Compatibility suite: ``lazyreqs`` as a drop-in ``pyureq``/``requests``.

Every test talks to a local threaded HTTP server (see ``_server.py``) so the
suite is deterministic and needs no network access.  The tests deliberately
mirror the behaviour that ``pyureq`` and ``requests`` users rely on: the
top-level helpers, the session attributes and context manager, the keyword
arguments, the response model and the exception hierarchy.
"""

from __future__ import annotations

import base64
import copy
import pickle
import socket
import time
from datetime import timedelta

import lazyreqs
import pytest
from _server import _TestServer
from lazyreqs import exceptions as exc
from lazyreqs.structures import CaseInsensitiveDict, LazyDict


@pytest.fixture(scope="session")
def server():
    with _TestServer() as srv:
        yield srv


def url(server, path):
    return server.base_url + path


def dead_port():
    """A port that is (almost certainly) closed on localhost."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


# ---------------------------------------------------------------------------
# Module surface
# ---------------------------------------------------------------------------
def test_module_exports():
    for name in (
        "request",
        "get",
        "post",
        "put",
        "patch",
        "delete",
        "head",
        "options",
        "session",
        "Session",
        "LazySession",
        "Response",
        "LazyResponse",
        "PreparedRequest",
        "CaseInsensitiveDict",
        "LazyDict",
    ):
        assert hasattr(lazyreqs, name), name
    for name in lazyreqs.__all__:
        assert hasattr(lazyreqs, name), name


def test_exception_hierarchy():
    assert issubclass(exc.ProxyError, exc.ConnectionError)
    assert issubclass(exc.SSLError, exc.ConnectionError)
    assert issubclass(exc.ConnectionError, exc.RequestException)
    assert issubclass(exc.HTTPError, exc.RequestException)
    assert issubclass(exc.Timeout, exc.RequestException)
    assert issubclass(exc.ConnectTimeout, exc.ConnectionError)
    assert issubclass(exc.ConnectTimeout, exc.Timeout)
    assert issubclass(exc.ReadTimeout, exc.Timeout)
    assert issubclass(exc.URLRequired, ValueError)
    assert issubclass(exc.InvalidURL, ValueError)
    assert issubclass(exc.InvalidHeader, ValueError)


def test_exception_response_request_kwargs():
    response = object()
    request = object()
    error = exc.ConnectionError("boom", response=response, request=request)
    assert error.response is response
    assert error.request is request
    assert isinstance(error, IOError)


# ---------------------------------------------------------------------------
# Response model
# ---------------------------------------------------------------------------
def test_response_basic_attributes(server):
    r = lazyreqs.get(url(server, "/json"))
    assert isinstance(r, lazyreqs.Response)
    assert r.status_code == 200
    assert r.ok is True
    assert r.content == r.text.encode("utf-8")
    assert r.url == url(server, "/json")
    assert isinstance(r.elapsed, timedelta)
    assert r.elapsed.total_seconds() >= 0
    assert r.reason
    assert r.request is not None
    assert r.request.method == "GET"
    assert r.history == []
    assert r.raw is not None
    assert repr(r) == "<Response [200]>"


def test_response_headers_case_insensitive(server):
    r = lazyreqs.get(url(server, "/json"))
    assert isinstance(r.headers, CaseInsensitiveDict)
    assert "content-type" in r.headers
    assert "Content-Type" in r.headers
    assert r.headers["CONTENT-TYPE"].startswith("application/json")
    assert r.headers.get("missing", "fallback") == "fallback"


def test_response_json(server):
    data = lazyreqs.get(url(server, "/json")).json()
    assert data["hello"] == "world"
    assert data["nested"]["a"] == [1, 2, 3]
    assert data["unicode"] == "héllo ☃"


def test_response_encoding_latin1(server):
    r = lazyreqs.get(url(server, "/latin1"))
    assert r.encoding.lower().replace("-", "") == "iso88591"
    assert r.text == "héllo"


def test_response_binary(server):
    r = lazyreqs.get(url(server, "/binary"))
    assert r.content == bytes(range(256))


def test_response_text_override(server):
    r = lazyreqs.get(url(server, "/latin1"))
    r.encoding = "utf-8"
    assert r.encoding == "utf-8"


def test_response_status_and_bool(server):
    r = lazyreqs.get(url(server, "/status/404"))
    assert r.status_code == 404
    assert bool(r) is False
    assert not r.ok


def test_response_links(server):
    r = lazyreqs.get(url(server, "/links"))
    assert "next" in r.links
    assert r.links["next"]["url"] == "https://example.com/next"


def test_response_iter_content_and_lines(server):
    r = lazyreqs.get(url(server, "/text"))
    chunks = list(r.iter_content(chunk_size=3))
    assert b"".join(chunks) == b"hello lazyreqs"
    assert list(r.iter_lines()) == [b"hello lazyreqs"]


def test_response_pickle_roundtrip(server):
    r = lazyreqs.get(url(server, "/json"))
    clone = pickle.loads(pickle.dumps(r))
    assert clone.status_code == 200
    assert clone.json()["hello"] == "world"
    assert clone.url == r.url


def test_response_copy(server):
    r = lazyreqs.get(url(server, "/json"))
    clone = copy.copy(r)
    assert clone.status_code == 200
    assert clone.text == r.text


# ---------------------------------------------------------------------------
# Verbs, params, bodies
# ---------------------------------------------------------------------------
def test_get_params_dict_and_sequence(server):
    r = lazyreqs.get(url(server, "/get"), params={"a": "1", "b": ["2", "3"]})
    args = r.json()["args"]
    assert args["a"] == ["1"]
    assert args["b"] == ["2", "3"]

    r = lazyreqs.get(url(server, "/get"), params=[("x", "y z"), ("x", "w")])
    assert r.json()["args"]["x"] == ["y z", "w"]


def test_post_json(server):
    payload = {"a": 1, "b": [1, 2, 3], "c": "héllo"}
    r = lazyreqs.post(url(server, "/echo"), json=payload)
    data = r.json()
    assert data["method"] == "POST"
    assert data["content_type"] == "application/json"
    import json as jsonlib

    assert jsonlib.loads(data["body_text"]) == payload


def test_put_patch_delete_options(server):
    for method in ("put", "patch", "delete", "options"):
        r = getattr(lazyreqs, method)(url(server, "/echo"), data=b"payload")
        assert r.json()["method"] == method.upper()


def test_post_form_data(server):
    r = lazyreqs.post(url(server, "/echo"), data={"a": "1", "b": ["2", "3"]})
    data = r.json()
    assert data["content_type"] == "application/x-www-form-urlencoded"
    assert data["body_text"] == "a=1&b=2&b=3"


def test_post_raw_body(server):
    r = lazyreqs.post(url(server, "/echo"), data=b"\x00\x01raw")
    data = r.json()
    assert data["body_len"] == 5
    r = lazyreqs.post(url(server, "/echo"), data="text body")
    assert r.json()["body_text"] == "text body"


def test_multipart_files_and_fields(server):
    files = {
        "file": ("notes.txt", b"file contents", "text/plain"),
        "raw": b"bare bytes",
    }
    r = lazyreqs.post(
        url(server, "/echo"), files=files, data={"note": "hi", "tags": "a"}
    )
    data = r.json()
    assert data["content_type"].startswith("multipart/form-data")
    parts = {part["name"]: part for part in data["multipart"]}
    assert parts["file"]["filename"] == "notes.txt"
    assert parts["file"]["data"] == "file contents"
    assert parts["raw"]["data"] == "bare bytes"
    assert parts["note"]["data"] == "hi"
    assert parts["tags"]["data"] == "a"


def test_user_content_type_header(server):
    # A Content-Type supplied by the caller must reach the wire unchanged and
    # must not be replaced by an inferred one (regression: it was sent as the
    # string "None" for bodyless requests).
    r = lazyreqs.get(
        url(server, "/get"), headers={"Content-Type": "application/json"}
    )
    headers = {key.lower(): value for key, value in r.json()["headers"].items()}
    assert headers["content-type"] == "application/json"

    r = lazyreqs.post(
        url(server, "/echo"),
        headers={"Content-Type": "application/vnd.kucoin+json"},
        json={"a": 1},
    )
    assert r.json()["content_type"] == "application/vnd.kucoin+json"


def test_custom_headers(server):
    r = lazyreqs.get(
        url(server, "/get"),
        headers={"X-Custom": "value", "User-Agent": "lazyreqs-test"},
    )
    headers = {key.lower(): value for key, value in r.json()["headers"].items()}
    assert headers.get("x-custom") == "value"
    assert headers.get("user-agent") == "lazyreqs-test"


def test_basic_auth(server):
    r = lazyreqs.get(url(server, "/auth"), auth=("user", "pass"))
    expected = base64.b64encode(b"user:pass").decode("ascii")
    assert r.json()["authorization"] == f"Basic {expected}"


def test_per_request_cookies(server):
    r = lazyreqs.get(url(server, "/cookies"), cookies={"token": "xyz"})
    assert r.json()["cookie"] == "token=xyz"


# ---------------------------------------------------------------------------
# Redirects
# ---------------------------------------------------------------------------
def test_redirects_followed(server):
    r = lazyreqs.get(url(server, "/redirect/3"))
    assert r.status_code == 200
    assert r.url == url(server, "/redirect/0")
    assert r.json()["method"] == "GET"


def test_redirects_disabled(server):
    r = lazyreqs.get(url(server, "/redirect/3"), allow_redirects=False)
    assert r.status_code == 302
    assert r.url == url(server, "/redirect/3")
    assert r.headers["Location"] == "/redirect/2"


def test_head_defaults_no_redirect(server):
    r = lazyreqs.head(url(server, "/status/200"))
    assert r.status_code == 200
    assert r.content == b""


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------
def test_session_attributes():
    s = lazyreqs.Session()
    assert isinstance(s.headers, CaseInsensitiveDict)
    assert "User-Agent" in s.headers
    assert s.auth is None
    assert s.cookies == {}
    assert s.proxies == {}
    assert s.timeout is None
    assert s.verify is True
    assert s.allow_redirects is True
    assert s.lazy is False
    s.close()


def test_session_context_manager(server):
    with lazyreqs.Session() as s:
        s.headers["X-Session"] = "1"
        r = s.get(url(server, "/get"))
        assert r.status_code == 200
    assert isinstance(s, lazyreqs.Session)


def test_session_persistent_headers_and_params(server):
    s = lazyreqs.Session()
    s.headers["X-Session"] = "yes"
    s.params = {"global": "1"}
    r = s.get(url(server, "/get"), params={"local": "2"})
    data = r.json()
    headers = {key.lower(): value for key, value in data["headers"].items()}
    assert headers.get("x-session") == "yes"
    assert data["args"]["global"] == ["1"]
    assert data["args"]["local"] == ["2"]
    s.close()


def test_session_cookies_persist(server):
    s = lazyreqs.Session()
    s.get(url(server, "/setcookie"))
    assert s.cookies["sess"] == "abc123"
    r = s.get(url(server, "/cookies"))
    assert r.json()["cookie"] == "sess=abc123"
    s.close()


def test_session_cookie_header_from_cookie_kwarg(server):
    r = lazyreqs.get(
        url(server, "/cookies"), cookies={"a": "1", "b": "2"}
    )
    assert r.json()["cookie"] == "a=1; b=2"


def test_session_multiple_set_cookie(server):
    r = lazyreqs.get(url(server, "/setcookies"))
    assert r.cookies == {"a": "1", "b": "2"}


def test_session_auth(server):
    s = lazyreqs.Session()
    s.auth = ("u", "p")
    r = s.get(url(server, "/auth"))
    expected = base64.b64encode(b"u:p").decode("ascii")
    assert r.json()["authorization"] == f"Basic {expected}"
    s.close()


def test_session_timeout_attribute(server):
    s = lazyreqs.Session()
    s.timeout = 10
    assert s.get(url(server, "/json")).status_code == 200
    s.close()


def test_session_verify_toggle(server):
    s = lazyreqs.Session()
    s.verify = False
    assert s.verify is False
    assert s.get(url(server, "/json")).status_code == 200
    s.verify = True
    assert s.verify is True
    s.close()


def test_session_allow_redirects_attribute(server):
    s = lazyreqs.Session()
    s.allow_redirects = False
    r = s.request("GET", url(server, "/redirect/1"))
    assert r.status_code == 302
    s.close()


def test_session_proxies(server):
    s = lazyreqs.Session()
    s.proxies = {"http://": f"http://127.0.0.1:{dead_port()}"}
    with pytest.raises(exc.ProxyError):
        s.get(url(server, "/json"))
    s.proxies = {}
    assert s.get(url(server, "/json")).status_code == 200
    s.close()


def test_session_merge_does_not_leak(server):
    s = lazyreqs.Session()
    s.get(url(server, "/get"), headers={"X-Once": "1"})
    r = s.get(url(server, "/get"))
    assert "X-Once" not in r.json()["headers"]
    s.close()


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
def test_raise_for_status_client_and_server(server):
    with pytest.raises(exc.HTTPError) as info:
        lazyreqs.get(url(server, "/status/404")).raise_for_status()
    assert info.value.response.status_code == 404
    assert info.value.request.method == "GET"

    with pytest.raises(exc.HTTPError):
        lazyreqs.get(url(server, "/status/500")).raise_for_status()


def test_raise_for_status_ok(server):
    lazyreqs.get(url(server, "/status/204")).raise_for_status()


def test_connection_error():
    port = dead_port()
    with pytest.raises(exc.ConnectionError) as info:
        lazyreqs.get(f"http://127.0.0.1:{port}/")
    assert isinstance(info.value, exc.RequestException)
    assert info.value.request is not None
    assert info.value.request.method == "GET"


def test_proxy_error(server):
    with pytest.raises(exc.ProxyError) as info:
        lazyreqs.get(
            url(server, "/json"),
            proxies={"http://": f"http://127.0.0.1:{dead_port()}"},
        )
    assert info.value.request is not None


def test_proxy_bypass_no_proxy(server):
    dead = dead_port()
    r = lazyreqs.get(
        url(server, "/json"),
        proxies={"http://": f"http://127.0.0.1:{dead}"},
        no_proxy="127.0.0.1,localhost",
    )
    assert r.status_code == 200


def test_no_proxy_all_disables_environment(server, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{dead_port()}")
    r = lazyreqs.get(url(server, "/json"), trust_env=False)
    assert r.status_code == 200


def test_timeout_read(server):
    with pytest.raises(exc.Timeout) as info:
        lazyreqs.get(url(server, "/delay/2"), timeout=0.3)
    assert isinstance(info.value, exc.Timeout)
    assert info.value.request is not None


def test_timeout_tuple(server):
    with pytest.raises(exc.Timeout):
        lazyreqs.get(url(server, "/delay/2"), timeout=(5, 0.3))


def test_invalid_url_missing_schema():
    with pytest.raises(exc.MissingSchema):
        lazyreqs.get("example.com")


def test_invalid_url_bad_host():
    with pytest.raises(exc.InvalidURL):
        lazyreqs.get("http://")


def test_invalid_header_newline():
    with pytest.raises(exc.InvalidHeader):
        lazyreqs.get("http://example.com", headers={"X-Bad": "a\r\nb"})


def test_too_many_redirects():
    # A redirect loop against a dead-port-free target is not available here;
    # the local server always terminates.  The exception type is still part of
    # the public surface.
    assert issubclass(exc.TooManyRedirects, exc.RequestException)


# ---------------------------------------------------------------------------
# Lazy responses
# ---------------------------------------------------------------------------
def test_lazy_response_type_and_resolution(server):
    r = lazyreqs.get(url(server, "/json"), lazy=True)
    assert isinstance(r, lazyreqs.LazyResponse)
    assert isinstance(r, lazyreqs.Response)
    assert r.status_code == 200
    assert r.json()["hello"] == "world"
    assert r.text == r.content.decode("utf-8")
    assert r.elapsed.total_seconds() >= 0


def test_session_lazy_constructor(server):
    s = lazyreqs.Session(lazy=True)
    assert s.lazy is True
    r = s.get(url(server, "/json"))
    assert isinstance(r, lazyreqs.LazyResponse)
    assert r.status_code == 200
    s.close()


def test_default_lazy_toggle(server):
    try:
        lazyreqs.set_default_lazy(True)
        assert lazyreqs.default_lazy() is True
        assert isinstance(lazyreqs.get(url(server, "/json")), lazyreqs.LazyResponse)
        with lazyreqs.session() as s:
            assert s.lazy is True
            assert isinstance(s.get(url(server, "/json")), lazyreqs.LazyResponse)
    finally:
        lazyreqs.set_default_lazy(False)
    assert lazyreqs.default_lazy() is False
    assert not isinstance(lazyreqs.get(url(server, "/json")), lazyreqs.LazyResponse)


def test_lazy_session(server):
    s = lazyreqs.LazySession()
    assert s.lazy is True
    r = s.get(url(server, "/json"))
    assert isinstance(r, lazyreqs.LazyResponse)
    assert r.status_code == 200
    s.close()


def test_lazy_is_ready_and_resolve(server):
    r = lazyreqs.get(url(server, "/delay/0.2"), lazy=True)
    assert r.is_ready() in (True, False)
    assert r.resolve() is r
    assert r.is_ready() is True
    assert r.status_code == 200


def test_lazy_concurrent_dispatch(server):
    urls = [url(server, "/delay/0.25") for _ in range(8)]
    s = lazyreqs.LazySession()
    start = time.perf_counter()
    pending = [s.get(u) for u in urls]
    dispatch = time.perf_counter() - start
    assert dispatch < 1.0, dispatch

    start = time.perf_counter()
    bodies = [r.json() for r in pending]
    total = time.perf_counter() - start
    assert all(body["delay"] == 0.25 for body in bodies)
    assert total < 1.5, total
    s.close()


def test_lazy_dispatch_is_cheap(server):
    s = lazyreqs.LazySession()
    start = time.perf_counter()
    pending = [s.get(url(server, "/delay/0.3")) for _ in range(50)]
    dispatch = time.perf_counter() - start
    assert dispatch < 1.0, dispatch
    assert all(r.status_code == 200 for r in pending)
    s.close()


def test_max_concurrency_limits(server):
    urls = [url(server, "/delay/0.15") for _ in range(4)]
    s = lazyreqs.LazySession()
    s.max_concurrency = 1
    start = time.perf_counter()
    pending = [s.get(u) for u in urls]
    bodies = [r.json() for r in pending]
    total = time.perf_counter() - start
    assert len(bodies) == 4
    assert total >= 0.5, total
    s.close()


def test_lazy_timeout_exception(server):
    r = lazyreqs.get(url(server, "/delay/2"), lazy=True, timeout=0.3)
    with pytest.raises(exc.Timeout) as info:
        _ = r.status_code
    assert info.value.request is not None
    assert info.value.request.url.endswith("/delay/2")


def test_lazy_error_has_request(server):
    r = lazyreqs.get(f"http://127.0.0.1:{dead_port()}/", lazy=True)
    with pytest.raises(exc.ConnectionError) as info:
        _ = r.status_code
    assert info.value.request.method == "GET"


def test_lazy_response_repr(server):
    r = lazyreqs.get(url(server, "/delay/0.3"), lazy=True)
    assert "LazyResponse" in repr(r)
    r.resolve()
    assert "LazyResponse [200]" in repr(r)


def test_lazy_response_wait_alias(server):
    r = lazyreqs.get(url(server, "/json"), lazy=True)
    assert r.wait() is r
    assert r.status_code == 200


def test_lazy_response_pickle(server):
    r = lazyreqs.get(url(server, "/json"), lazy=True)
    clone = pickle.loads(pickle.dumps(r))
    assert clone.status_code == 200
    assert clone.json()["hello"] == "world"
    assert clone.is_ready() is True


# ---------------------------------------------------------------------------
# LazyDict
# ---------------------------------------------------------------------------
def test_lazy_json_returns_lazy_dict(server):
    r = lazyreqs.get(url(server, "/json"), lazy_json=True)
    data = r.json()
    assert isinstance(data, LazyDict)
    assert isinstance(data, dict)
    assert data.resolved is False
    assert "hello" in data
    assert data.resolved is True
    assert data["hello"] == "world"
    assert data["nested"]["a"] == [1, 2, 3]


def test_lazy_json_defers_parsing(server):
    r = lazyreqs.get(url(server, "/json"), lazy_json=True)
    data = r.json()
    assert "pending" in repr(data)
    assert dict(data) == {"hello": "world", "nested": {"a": [1, 2, 3]},
                          "unicode": "héllo ☃"}
    assert data.resolved is True


def test_lazy_json_with_lazy_response(server):
    r = lazyreqs.get(url(server, "/json"), lazy=True, lazy_json=True)
    data = r.json()
    assert isinstance(data, LazyDict)
    assert data["hello"] == "world"


def test_lazy_dict_mutation(server):
    r = lazyreqs.get(url(server, "/json"), lazy_json=True)
    data = r.json()
    data["extra"] = 1
    assert data["extra"] == 1
    assert data["hello"] == "world"
    del data["hello"]
    assert "hello" not in data


def test_lazy_dict_loader_error():
    def boom():
        raise ValueError("not json")

    data = LazyDict(boom)
    with pytest.raises(ValueError):
        data["x"]
    # Errors are cached, not retried.
    with pytest.raises(ValueError):
        len(data)


def test_lazy_dict_not_required_by_default(server):
    data = lazyreqs.get(url(server, "/json")).json()
    assert type(data) is dict


# ---------------------------------------------------------------------------
# pyureq-style extras
# ---------------------------------------------------------------------------
def test_proxy_kwarg_forms(server):
    dead = dead_port()
    with pytest.raises(exc.ProxyError):
        lazyreqs.get(url(server, "/json"), proxies={"http": f"http://127.0.0.1:{dead}"})
    with pytest.raises(exc.ProxyError):
        lazyreqs.get(url(server, "/json"), proxies={"HTTP://": f"http://127.0.0.1:{dead}"})


def test_proxy_bare_string(server):
    dead = dead_port()
    with pytest.raises(exc.ProxyError):
        lazyreqs.get(url(server, "/json"), proxies=f"http://127.0.0.1:{dead}")


def test_no_proxy_key_in_proxies(server):
    dead = dead_port()
    r = lazyreqs.get(
        url(server, "/json"),
        proxies={"http://": f"http://127.0.0.1:{dead}", "no_proxy": "127.0.0.1"},
    )
    assert r.status_code == 200


def test_socks_proxy_url_accepted(server):
    # A SOCKS proxy pointing at a closed port must fail as a proxy error,
    # which proves the URL was routed through the SOCKS path.
    with pytest.raises(exc.ProxyError):
        lazyreqs.get(
            url(server, "/json"),
            proxies={"all": f"socks5://127.0.0.1:{dead_port()}"},
        )


def test_worker_threads_and_version():
    assert lazyreqs._lazyreqs.__version__ == lazyreqs.__version__
    assert lazyreqs._lazyreqs.worker_threads >= 2


def test_session_repr():
    assert repr(lazyreqs.Session()) == "<lazyreqs.Session>"
    assert repr(lazyreqs.LazySession()) == "<lazyreqs.LazySession>"


def test_head_method_via_request(server):
    r = lazyreqs.request("HEAD", url(server, "/status/200"))
    assert r.status_code == 200
    assert r.content == b""


def test_case_insensitive_response_headers(server):
    r = lazyreqs.get(url(server, "/json"))
    assert r.headers["Content-Type"] == r.headers["content-type"]
    assert dict(r.headers.lower_items())["content-type"] == r.headers["Content-Type"]
