//! `lazyreq` — a Tokio + `reqwest` engine with a `requests`/`pyureq`
//! compatible Python surface and zero-``await`` lazy concurrency.
//!
//! Python API lives in ``python/lazyreq``; this crate provides the transport:
//! [`RustClient`](session::RustClient), [`RawResponse`](response::RawResponse)
//! and [`LazyResponse`](lazy::LazyResponse).

mod error;
mod lazy;
mod proxy;
mod response;
mod session;

use std::sync::OnceLock;

use pyo3::prelude::*;
use tokio::runtime::{Builder, Runtime};

/// Number of worker threads used by the shared Tokio runtime.
pub fn worker_threads() -> usize {
    std::thread::available_parallelism()
        .map(|count| count.get())
        .unwrap_or(4)
        .clamp(2, 16)
}

/// Global, process-wide Tokio runtime shared by every session.
///
/// A single multi-threaded runtime avoids spawning one runtime per
/// ``Session`` (which would multiply worker threads and destroy connection
/// reuse across sessions).
pub fn rt() -> &'static Runtime {
    static RUNTIME: OnceLock<Runtime> = OnceLock::new();
    RUNTIME.get_or_init(|| {
        Builder::new_multi_thread()
            .worker_threads(worker_threads())
            .thread_name("lazyreq-worker")
            .enable_all()
            .build()
            .expect("lazyreq: failed to start the Tokio runtime")
    })
}

#[pymodule]
fn _lazyreq(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<session::RustClient>()?;
    module.add_class::<response::RawResponse>()?;
    module.add_class::<lazy::LazyResponse>()?;
    module.add("__version__", env!("CARGO_PKG_VERSION"))?;
    module.add("worker_threads", worker_threads())?;
    Ok(())
}
