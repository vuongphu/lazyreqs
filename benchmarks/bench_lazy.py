"""Benchmarks for ``lazyreq``'s lazy concurrency and blocking modes.

Usage::

    python benchmarks/bench_lazy.py                 # local test server
    python benchmarks/bench_lazy.py --url https://example.com
    python benchmarks/bench_lazy.py --requests 200 --delay 0.05
    python benchmarks/bench_lazy.py --compare       # also time requests/pyureq

The benchmark makes the *network* the bottleneck (``--delay``) so the numbers
measure how well requests overlap, not how fast JSON is decoded.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tests"))

import lazyreq


def _local_server():
    from _server import _TestServer

    return _TestServer()


def time_call(fn):
    start = time.perf_counter()
    result = fn()
    return time.perf_counter() - start, result


def bench_blocking(base_url, count):
    session = lazyreq.Session()
    start = time.perf_counter()
    responses = [session.get(base_url) for _ in range(count)]
    elapsed = time.perf_counter() - start
    session.close()
    assert len(responses) == count
    return elapsed


def bench_lazy_dispatch(base_url, count):
    """Measure the cost of firing requests without resolving them."""
    session = lazyreq.LazySession()
    start = time.perf_counter()
    pending = [session.get(base_url) for _ in range(count)]
    dispatch = time.perf_counter() - start
    resolved = [response.status_code for response in pending]
    total = time.perf_counter() - start
    session.close()
    assert all(status == 200 for status in resolved)
    return dispatch, total


def bench_lazy_resolve(base_url, count):
    session = lazyreq.LazySession()
    pending = [session.get(base_url) for _ in range(count)]
    start = time.perf_counter()
    for response in pending:
        _ = response.status_code
    elapsed = time.perf_counter() - start
    session.close()
    return elapsed


def bench_lazy_json(base_url, count):
    session = lazyreq.LazySession()
    pending = [
        session.get(base_url, lazy_json=True) for _ in range(count)
    ]
    # Touching only one key still resolves the whole document on first access.
    start = time.perf_counter()
    values = [response.json()["hello"] for response in pending]
    elapsed = time.perf_counter() - start
    session.close()
    assert values and all(value == "world" for value in values)
    return elapsed


def bench_single_latency(base_url, samples):
    session = lazyreq.Session()
    timings = []
    for _ in range(samples):
        start = time.perf_counter()
        session.get(base_url)
        timings.append(time.perf_counter() - start)
    session.close()
    return statistics.median(timings), min(timings)


def bench_compare(count, delay, server):
    """Time ``requests``/``pyureq`` on the same workload.

    Each library is measured twice: a blocking loop over a single ``Session``
    (like-for-like with ``lazyreq`` blocking mode) and a thread pool with the
    same parallelism as the lazy run (what those libraries need for
    concurrency).
    """
    target = server.url(f"/delay/{delay}")
    workers = min(count, 64)
    results = {}
    for name in ("requests", "pyureq"):
        try:
            module = __import__(name)
        except ImportError:
            continue

        session = module.Session()
        try:
            start = time.perf_counter()
            for _ in range(count):
                session.get(target)
            results[f"{name} blocking"] = (time.perf_counter() - start, 1)

            from concurrent.futures import ThreadPoolExecutor

            def fetch(_, session=session):
                return session.get(target)

            start = time.perf_counter()
            with ThreadPoolExecutor(max_workers=workers) as pool:
                list(pool.map(fetch, range(count)))
            results[f"{name} ({workers} threads)"] = (
                time.perf_counter() - start,
                workers,
            )
        finally:
            close = getattr(session, "close", None)
            if close is not None:
                close()
    return results


def print_row(label, seconds, count, extra=""):
    per_request = seconds / count * 1000 if count else 0.0
    print(f"{label:<34} {seconds:8.3f}s  {per_request:9.2f} ms/req  {extra}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", "-n", type=int, default=100)
    parser.add_argument("--delay", "-d", type=float, default=0.05,
                        help="per-response server delay in seconds")
    parser.add_argument("--url", default=None,
                        help="target URL (defaults to the local test server)")
    parser.add_argument("--compare", action="store_true",
                        help="also benchmark installed requests/pyureq")
    args = parser.parse_args(argv)

    server = None
    if args.url:
        base_url = args.url
    else:
        server = _local_server()
        server.__enter__()
        base_url = server.url(f"/delay/{args.delay}")

    try:
        count = args.requests
        print(f"lazyreq benchmark: {count} requests to {base_url}\n")

        sequential = bench_blocking(base_url, count)
        print_row("blocking (sequential)", sequential, count)

        dispatch, lazy_total = bench_lazy_dispatch(base_url, count)
        print_row("lazy dispatch only", dispatch, count,
                  f"({dispatch / count * 1e6:.0f} us/req)")
        print_row("lazy total (dispatch+resolve)", lazy_total, count)

        resolve = bench_lazy_resolve(base_url, count)
        print_row("lazy resolve after dispatch", resolve, count)

        if server is not None:
            json_count = max(10, count // 4)
            lazy_json = bench_lazy_json(server.url("/json"), json_count)
            print_row(f"lazy_json ({json_count} docs)", lazy_json, json_count)

        median, fastest = bench_single_latency(base_url, max(5, count // 10))
        print_row("single-request latency (median)", median, 1)
        print_row("single-request latency (best)", fastest, 1)

        if count:
            print(f"\nspeedup vs blocking: {sequential / lazy_total:5.2f}x")

        if args.compare and server is not None:
            print("\ncomparison on the same local server and workload:")
            others = bench_compare(count, args.delay, server)
            for name, (seconds, _) in others.items():
                print_row(f"  {name}", seconds, count)
            for name, (seconds, _) in others.items():
                print(f"  lazyreq lazy vs {name}: "
                      f"{seconds / lazy_total:5.2f}x")
    finally:
        if server is not None:
            server.__exit__(None, None, None)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
