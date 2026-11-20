# lazyreqs

`lazyreqs` is a high-performance HTTP library for Python built on Rust
(`PyO3` + `reqwest` + `Tokio`). It is designed as a **100% API-compatible
drop-in replacement for both `pyureq` and `requests`**, and adds zero-`await`
concurrent execution through `LazyResponse` and `LazyDict`.

```python
import lazyreqs as requests   # or: import lazyreqs as pyureq

r = requests.get("https://example.com")
print(r.status_code, r.text)

# Lazy concurrency: fire every request, resolve on access, never await.
pending = [lazyreqs.get(url, lazy=True) for url in urls]
bodies = [r.text for r in pending]
```

---

## Why

`requests` is convenient but synchronous and slow: every call blocks, and a
few hundred concurrent calls block one thread each. `pyureq` already runs
requests on a Rust core, but still returns them one at a time.

`lazyreqs` keeps the exact API you already know and changes only *when* the
response is materialised:

- **Blocking mode** — identical to `requests`/`pyureq`. `lazyreqs.get(url)`
  returns a fully populated `Response`, releasing the GIL while the network
  I/O runs.
- **Lazy mode** — `lazyreqs.get(url, lazy=True)` returns a `LazyResponse` in
  microseconds. The request is already running on a shared Tokio runtime; the
  first attribute access (`r.status_code`, `r.text`, `r.json()`, …) blocks
  until it finishes. Every request in a batch is in flight at once, with no
  `await` and no thread per request.
- **Lazy JSON** — `lazyreqs.get(url, lazy_json=True).json()` returns a
  `LazyDict`: a real `dict` subclass that parses the JSON document only when
  its contents are first touched.

Everything is driven by one process-wide Tokio multi-threaded runtime with
HTTP/1.1 keep-alive and HTTP/2 multiplexing, a configurable semaphore
(`max_concurrency`, default 100) and automatic connection pooling.

## Installation

```bash
pip install lazyreqs

# from a checkout
maturin develop --release
```

The wheel targets `abi3-py39`, so a single build runs on CPython 3.9+.

## Usage

### requests / pyureq parity

```python
import lazyreqs

r = lazyreqs.get("https://example.com", params={"q": "hello"}, timeout=5)
r = lazyreqs.post("https://example.com/api", json={"a": 1}, auth=("user", "pass"))
r = lazyreqs.put(url, data={"form": "value"})
r = lazyreqs.patch(url, json={"patch": True})
r = lazyreqs.delete(url)
r = lazyreqs.head(url, allow_redirects=False)
r = lazyreqs.options(url)
r = lazyreqs.request("GET", url, headers={"X-Token": "..."})
```

Response attributes and methods:

```python
r.status_code      # int
r.headers          # CaseInsensitiveDict
r.content          # bytes
r.text             # decoded str
r.encoding         # charset (or detected)
r.url              # final URL after redirects
r.ok               # status < 400
r.elapsed          # datetime.timedelta
r.json()           # parsed JSON
r.cookies          # {"name": "value"}
r.links            # parsed Link headers
r.history          # [] (redirects are followed inline)
r.raise_for_status()   # HTTPError for 4xx/5xx
r.iter_content(chunk_size=1024)
r.iter_lines()
```

Sessions:

```python
with lazyreqs.Session() as s:
    s.headers.update({"X-API": "key"})
    s.auth = ("user", "pass")
    s.cookies["session"] = "abc"
    s.proxies = {"http://": "http://127.0.0.1:8080"}
    s.timeout = 10
    r = s.get("https://example.com")
```

### Lazy concurrency (no `await`)

```python
import lazyreqs

with lazyreqs.LazySession() as s:          # s.lazy == True
    pending = [s.get(url) for url in urls]  # fires all requests immediately
    print([r.status_code for r in pending])  # resolves on first access

# Per-call
pending = lazyreqs.get(url, lazy=True)
assert isinstance(pending, lazyreqs.Response)   # LazyResponse subclasses Response
pending.resolve()                              # explicit
pending.wait()                                 # alias
pending.is_ready()                             # non-blocking check
```

Resolution is thread-safe: several threads touching the same `LazyResponse`
share a single background fetch.

### Setting lazy once, not per call

`lazy=` can be pinned at three levels; the most specific wins:

```python
lazyreqs.set_default_lazy(True)   # process-wide default for lazyreqs.get(...)
lazyreqs.default_lazy()           # -> True

s = lazyreqs.Session(lazy=True)   # per session, set at construction
s.lazy = True                    # ...or as an attribute

with lazyreqs.LazySession() as s: # preset: lazy=True
    ...

lazyreqs.get(url, lazy=False)     # per-call override
```

`set_default_lazy(True)` makes the top-level helpers return `LazyResponse`
objects, which subclass `Response` and behave identically on attribute access.
The one visible difference: transport errors (timeouts, refused connections)
surface when the response is first touched instead of at the call site.

### Lazy JSON

```python
data = lazyreqs.get(url, lazy_json=True).json()  # LazyDict, still unparsed
print("parsed lazily", data["results"])         # parse happens here
assert isinstance(data, dict)                   # it is a real dict
```

### Concurrency control

```python
# Limit in-flight requests (semaphore), default 100
s = lazyreqs.Session()
s.max_concurrency = 8

# Or per request
lazyreqs.get(url, lazy=True, max_concurrency=4)
```

### Proxies

```python
lazyreqs.get(url, proxies={"http://": "http://user:pass@proxy:3128"})
lazyreqs.get(url, proxies={"all": "socks5://127.0.0.1:1080"},
            no_proxy="localhost,127.0.0.1")     # bypass list
lazyreqs.get(url, proxies={"http://": ""})      # disable all proxies
lazyreqs.get(url, trust_env=False)              # ignore *_PROXY env vars
```

### Exceptions

`lazyreqs.exceptions` mirrors `requests.exceptions` and `pyureq.exceptions`;
each exception carries `.response` and `.request` when available.

```python
import lazyreqs

try:
    lazyreqs.get("https://example.com", timeout=1)
except lazyreqs.Timeout:
    pass
except lazyreqs.ProxyError:
    pass
except lazyreqs.ConnectionError:
    pass
except lazyreqs.HTTPError as exc:
    print(exc.response.status_code, exc.request.method)
except lazyreqs.RequestException:
    pass
```

Hierarchy: `RequestException` → `ConnectionError` (`ProxyError`, `SSLError`),
`HTTPError`, `Timeout` (`ConnectTimeout`, `ReadTimeout`), `InvalidURL`,
`MissingSchema`, `InvalidHeader`, `TooManyRedirects`, `URLRequired`, …
`ConnectTimeout` inherits from both `ConnectionError` and `Timeout`, exactly
like `requests`.

## Compatibility matrix

| Surface | Status |
| --- | --- |
| Top-level `request`, `get`, `post`, `put`, `patch`, `delete`, `head`, `options`, `session()` | ✅ |
| `Session` / `LazySession` with `with` support | ✅ |
| `Session` attributes `headers`, `cookies`, `proxies`, `timeout`, `auth`, `verify`, `allow_redirects`, `lazy`, `params` | ✅ |
| kwargs `params`, `data`, `json`, `headers`, `cookies`, `auth`, `timeout`, `allow_redirects`, `verify`, `proxies` | ✅ |
| `proxies`: `http://`, `https://`, `all`, `socks4/5/5h`, auth in URL, `no_proxy` bypass | ✅ |
| Response `.status_code`, `.headers`, `.content`, `.text`, `.encoding`, `.url`, `.ok`, `.elapsed`, `.json()`, `.raise_for_status()` | ✅ |
| `Response.cookies`, `.links`, `.history`, `.raw`, `.request`, `iter_content`, `iter_lines` | ✅ |
| `CaseInsensitiveDict`, `LazyDict` | ✅ |
| HTTP/2 multiplexing, gzip/br/deflate/zstd decompression | ✅ |
| Lazy `LazyResponse` + `lazy_json=True` | ✅ |
| `Session.history` / `PreparedRequest` detail parity with `requests` | partial (redirects are followed inline; `history` is always `[]`) |

Known limitations:

- Redirect history is not retained (`Response.history == []`); the final
  `Response.url` is correct.
- Host-scoped proxy keys such as `{"http://example.com": ...}` are treated as
  scheme-wide (`http`) — reqwest applies proxies per client.
- `json.dumps(lazy_dict)` bypasses the lazy loader: CPython's C encoder walks
  dict subclasses with `PyDict_Next`, so call `.loads()`/`dict(...)` first if
  you need the contents serialised.
- Request header names are lower-cased on the wire (same as `pyureq`; HTTP/2
  requires lowercase and reqwest normalises both protocols).
- Certificate/CA-bundle handling uses the platform trust store by default;
  `verify=False` disables verification for the whole pool.

## Benchmarks

```bash
python benchmarks/bench_lazy.py                  # local threaded test server
python benchmarks/bench_lazy.py -n 200 -d 0.05
python benchmarks/bench_lazy.py --url https://example.com --compare
```

Example report on a local server, 100 requests each delayed 50 ms
(Windows 11, CPython 3.12; absolute numbers are machine-dependent):

```
blocking (sequential)                 5.070s      50.70 ms/req
lazy dispatch only                    0.003s       0.03 ms/req
lazy total (dispatch+resolve)         0.070s       0.70 ms/req
lazy resolve after dispatch           0.066s       0.66 ms/req
lazy_json (25 docs)                   0.013s       0.51 ms/req

speedup vs blocking: 72.48x

comparison on the same local server and workload:
  requests blocking                   5.124s      51.24 ms/req
  requests (64 threads)               0.137s       1.37 ms/req
  pyureq blocking                     5.073s      50.73 ms/req
  pyureq (64 threads)                 0.116s       1.16 ms/req
  lazyreqs lazy vs pyureq blocking:       72.51x
  lazyreqs lazy vs pyureq (64 threads):    1.66x
```

`--compare` runs `requests` and `pyureq` on the identical workload, both as a
blocking loop over one `Session` and through a 64-thread pool — the way those
libraries get concurrency. `lazyreqs` matches `pyureq` per request in blocking
mode and beats the thread-pool approach with no threads, no pool sizing and no
per-request `lazy=True` noise.

Run the benchmark against your own target:

```bash
python benchmarks/bench_lazy.py -n 200 -d 0.05 --compare
```

The lazy path is bounded by the server, not by Python: 100 requests that each
take 50 ms finish in ~130 ms instead of 5 s.

## Architecture

```
Python layer  (python/lazyreqs/)
  __init__.py      top-level API + lazy_session()
  sessions.py      Session/LazySession, kwargs merge, dispatch
  models.py        PreparedRequest, Response, LazyResponse
  structures.py    CaseInsensitiveDict, LazyDict
  exceptions.py    requests/pyureq-compatible hierarchy
        │  (RustClient method, plain Python values)
        ▼
Rust layer    (src/)
  lib.rs       shared Tokio runtime (OnceLock<Runtime>), worker threads
  session.rs   ExecOptions parsing, client cache (keyed by config),
               semaphore, request preparation and execution
  response.rs  RawResponse (status/headers/body/encoding/cookies)
  lazy.rs      LazyResponse: oneshot receiver + Condvar, no GIL held
  proxy.rs     proxies/no_proxy handling (http/https/socks/all)
  error.rs     reqwest error → lazyreqs.exceptions classification
```

The Python layer owns *configuration* (headers, cookies, proxies, timeouts,
auth, verify, redirects, lazy policy); the Rust core owns *transport*
(pooling, TLS, HTTP/2, decompression, concurrency). This split keeps the API
exactly compatible while the hot path stays in Rust.

## Development

```bash
python -m venv .venv && .venv/Scripts/python -m pip install maturin pytest
.venv/Scripts/python -m maturin develop --release
.venv/Scripts/python -m pytest tests -q
```

`Cargo.toml` builds with `lto = "fat"`, `codegen-units = 1`,
`panic = "abort"` and stripped symbols for a small, fast binary.

## Release pipeline

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs on every push and
pull request:

- **Lint** — `ruff check python/`, `cargo clippy --all-targets -- -D warnings`,
  `cargo fmt --check`
- **Test** — Python 3.9–3.14: build the abi3 wheel, install it, run `pytest tests/`
- **Build wheels** — Ubuntu (x86_64), macOS (arm64 + x86_64), Windows (x64):
  one `cp39-abi3` wheel per platform, plus a source distribution

On a `v*` tag push, a final job publishes to PyPI and creates the GitHub
Release with the built artifacts attached.

### Publishing setup (one-time, trusted publishing / OIDC — no token stored)

1. On PyPI, open the project → **Publishing** → **Add a new pending publisher**.
2. Choose **GitHub Actions** and fill in:
   - owner: your GitHub user/organisation
   - repository: `lazyreqs`
   - workflow file: `ci.yml`
   - environment: `pypi`
3. That's it — the release job's `id-token: write` permission authenticates it.

### Cutting a release

```bash
# 1. bump the version in all three places
#    Cargo.toml, pyproject.toml, python/lazyreqs/_version.py
# 2. commit, tag and push
git add -A && git commit -m "Release 0.1.1"
git tag v0.1.1
git push origin main --follow-tags
```

The tag push triggers the build matrix; the `release` job waits for every
matrix leg, downloads the wheels, publishes them to PyPI with
`pypa/gh-action-pypi-publish`, then creates the release with
`softprops/action-gh-release`.