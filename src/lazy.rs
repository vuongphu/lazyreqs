//! Lazy, zero-``await`` request handles.
//!
//! [`LazyResponse`] owns a Tokio ``oneshot`` receiver for a request that is
//! already running on the shared runtime.  The first attribute access from
//! Python synchronously waits for the result (releasing the GIL), then caches
//! the [`RawResponse`] so subsequent accesses are free.

use std::sync::{Arc, Condvar, Mutex};
use std::time::Duration;

use pyo3::prelude::*;
use pyo3::types::PyAny;
use tokio::sync::oneshot;

use crate::error::{ErrorKind, ReqError};
use crate::response::{make_raw, RawResponse, RequestInfo, ResponseData};

type LazyResult = Result<ResponseData, ReqError>;

/// Connection failures on a proxied request are proxy failures from the
/// caller's point of view (they asked for a proxy and did not get through it).
pub fn classify_proxy_error(err: ReqError, proxied: bool) -> ReqError {
    if proxied
        && matches!(
            err.kind,
            ErrorKind::Connection | ErrorKind::ConnectTimeout | ErrorKind::Ssl
        )
    {
        return ReqError::new(ErrorKind::Proxy, err.message);
    }
    err
}

enum LazyState {
    Pending,
    Fetching,
    Done(LazyResult),
}

pub struct LazyInner {
    state: Mutex<LazyState>,
    wake: Condvar,
    receiver: Mutex<Option<oneshot::Receiver<LazyResult>>>,
    /// Optional Python callback invoked with the ``Set-Cookie`` list once the
    /// response is available (used to keep ``Session.cookies`` in sync).
    cookie_sink: Option<Py<PyAny>>,
    request: RequestInfo,
}

impl LazyInner {
    pub fn new(
        receiver: oneshot::Receiver<LazyResult>,
        cookie_sink: Option<Py<PyAny>>,
        request: RequestInfo,
    ) -> Self {
        Self {
            state: Mutex::new(LazyState::Pending),
            wake: Condvar::new(),
            receiver: Mutex::new(Some(receiver)),
            cookie_sink,
            request,
        }
    }
}

/// Handle for an in-flight request.
#[pyclass(module = "lazyreqs._lazyreqs")]
pub struct LazyResponse {
    inner: Arc<LazyInner>,
}

impl LazyResponse {
    pub fn create(
        receiver: oneshot::Receiver<LazyResult>,
        cookie_sink: Option<Py<PyAny>>,
        request: RequestInfo,
    ) -> Self {
        Self {
            inner: Arc::new(LazyInner::new(receiver, cookie_sink, request)),
        }
    }

    fn convert(&self, py: Python<'_>, result: LazyResult) -> PyResult<Py<RawResponse>> {
        match result {
            Ok(data) => make_raw(py, &data, &self.inner.request),
            Err(err) => {
                let err = classify_proxy_error(err, self.inner.request.proxy);
                Err(err.to_pyerr(py, None, None))
            }
        }
    }

    fn fetch(&self, py: Python<'_>) -> LazyResult {
        let receiver = self.inner.receiver.lock().unwrap().take();
        let result = match receiver {
            Some(receiver) => py
                .detach(|| crate::rt().block_on(receiver))
                .unwrap_or_else(|_| {
                    Err(ReqError::other(
                        "lazyreqs: background request was cancelled before completion",
                    ))
                }),
            None => Err(ReqError::other(
                "lazyreqs: background request was already consumed",
            )),
        };

        if let Ok(data) = &result {
            if let Some(callback) = &self.inner.cookie_sink {
                let _ = callback.bind(py).call1((data.set_cookies.clone(),));
            }
        }
        result
    }

    /// Block until the response is available and return a ``RawResponse``.
    fn resolve_impl(&self, py: Python<'_>) -> PyResult<Py<RawResponse>> {
        loop {
            {
                let mut state = self.inner.state.lock().unwrap();
                match &*state {
                    LazyState::Done(result) => {
                        let result = result.clone();
                        drop(state);
                        return self.convert(py, result);
                    }
                    LazyState::Fetching => {
                        drop(state);
                    }
                    LazyState::Pending => {
                        *state = LazyState::Fetching;
                        drop(state);

                        let result = self.fetch(py);

                        let mut state = self.inner.state.lock().unwrap();
                        *state = LazyState::Done(result);
                        drop(state);
                        self.inner.wake.notify_all();
                        continue;
                    }
                }
            }

            // Another thread is fetching: wait without holding the GIL so the
            // fetcher can acquire it once its I/O completes.
            py.detach(|| {
                let guard = self.inner.state.lock().unwrap();
                if !matches!(&*guard, LazyState::Done(_)) {
                    let _ = self
                        .inner
                        .wake
                        .wait_timeout(guard, Duration::from_secs(120));
                }
            });
        }
    }
}

#[pymethods]
impl LazyResponse {
    /// Wait for the request to finish and return the raw response.
    fn resolve(&self, py: Python<'_>) -> PyResult<Py<RawResponse>> {
        self.resolve_impl(py)
    }

    /// Alias for :meth:`resolve`.
    fn wait(&self, py: Python<'_>) -> PyResult<Py<RawResponse>> {
        self.resolve_impl(py)
    }

    /// ``True`` once the background request has completed (no blocking).
    fn is_ready(&self) -> bool {
        matches!(&*self.inner.state.lock().unwrap(), LazyState::Done(_))
    }

    #[getter]
    fn ready(&self) -> bool {
        self.is_ready()
    }

    #[getter]
    fn request_method(&self) -> String {
        self.inner.request.method.clone()
    }

    #[getter]
    fn request_url(&self) -> String {
        self.inner.request.url.clone()
    }

    fn __repr__(&self) -> String {
        let state = self.inner.state.lock().unwrap();
        match &*state {
            LazyState::Done(Ok(data)) => format!("<LazyResponse [{}] (resolved)>", data.status),
            LazyState::Done(Err(err)) => format!("<LazyResponse (error: {})>", err.message),
            _ => "<LazyResponse (pending)>".to_string(),
        }
    }
}
