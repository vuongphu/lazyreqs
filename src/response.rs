//! Response data captured by the Tokio worker and exposed to Python as
//! [`RawResponse`].  The user-facing ``lazyreq.Response`` model wraps this
//! object and adds the ``requests``/``pyureq`` API surface.

use pyo3::prelude::*;
use pyo3::types::{PyAny, PyBytes};

/// Everything the worker thread needs to hand back to Python.
#[derive(Clone, Debug)]
pub struct ResponseData {
    pub status: u16,
    pub reason: String,
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
    pub url: String,
    pub elapsed_secs: f64,
    pub http_version: String,
    pub set_cookies: Vec<String>,
}

/// Metadata about the request that produced a response, used for ``.request``
/// on exceptions and for lazy task introspection.
#[derive(Clone, Debug)]
pub struct RequestInfo {
    pub method: String,
    pub url: String,
    pub headers: Vec<(String, String)>,
    /// ``True`` when the request was routed through an explicit proxy mapping;
    /// used to classify connection failures as ``ProxyError``.
    pub proxy: bool,
}

#[pyclass(frozen, module = "lazyreq._lazyreq")]
pub struct RawResponse {
    pub status_code: u16,
    pub reason: String,
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
    pub url: String,
    pub elapsed_secs: f64,
    pub encoding: Option<String>,
    pub http_version: String,
    pub set_cookies: Vec<String>,
    pub cookies: Vec<(String, String)>,
    pub request_method: String,
    pub request_url: String,
    pub request_headers: Vec<(String, String)>,
}

#[pymethods]
impl RawResponse {
    #[getter]
    fn status_code(&self) -> u16 {
        self.status_code
    }

    #[getter]
    fn reason(&self) -> String {
        self.reason.clone()
    }

    #[getter]
    fn headers(&self) -> Vec<(String, String)> {
        self.headers.clone()
    }

    /// Raw body bytes (decompressed by the transport layer).
    #[getter]
    fn body(&self, py: Python<'_>) -> Py<PyAny> {
        PyBytes::new(py, &self.body).into_any().unbind()
    }

    #[getter]
    fn url(&self) -> String {
        self.url.clone()
    }

    #[getter]
    fn elapsed_secs(&self) -> f64 {
        self.elapsed_secs
    }

    /// Charset advertised by the ``Content-Type`` header, if any.
    #[getter]
    fn encoding(&self) -> Option<String> {
        self.encoding.clone()
    }

    #[getter]
    fn http_version(&self) -> String {
        self.http_version.clone()
    }

    /// Every ``Set-Cookie`` header, unparsed and in wire order.
    #[getter]
    fn set_cookies(&self) -> Vec<String> {
        self.set_cookies.clone()
    }

    /// Parsed ``name -> value`` pairs from ``Set-Cookie`` (attributes dropped).
    #[getter]
    fn cookies(&self) -> Vec<(String, String)> {
        self.cookies.clone()
    }

    #[getter]
    fn request_method(&self) -> String {
        self.request_method.clone()
    }

    #[getter]
    fn request_url(&self) -> String {
        self.request_url.clone()
    }

    #[getter]
    fn request_headers(&self) -> Vec<(String, String)> {
        self.request_headers.clone()
    }

    fn __repr__(&self) -> String {
        format!("<RawResponse [{}]>", self.status_code)
    }
}

/// Build the Python-facing raw response from worker output.
pub fn make_raw(
    py: Python<'_>,
    data: &ResponseData,
    request: &RequestInfo,
) -> PyResult<Py<RawResponse>> {
    let raw = RawResponse {
        status_code: data.status,
        reason: data.reason.clone(),
        headers: data.headers.clone(),
        body: data.body.clone(),
        url: data.url.clone(),
        elapsed_secs: data.elapsed_secs,
        encoding: detect_charset(&data.headers),
        http_version: data.http_version.clone(),
        set_cookies: data.set_cookies.clone(),
        cookies: parse_cookies(&data.set_cookies),
        request_method: request.method.clone(),
        request_url: request.url.clone(),
        request_headers: request.headers.clone(),
    };
    Py::new(py, raw)
}

/// Extract ``charset=`` from the ``Content-Type`` header.
pub fn detect_charset(headers: &[(String, String)]) -> Option<String> {
    for (name, value) in headers {
        if !name.eq_ignore_ascii_case("content-type") {
            continue;
        }
        for part in value.split(';').skip(1) {
            let part = part.trim();
            if let Some((key, val)) = part.split_once('=') {
                if key.trim().eq_ignore_ascii_case("charset") {
                    let val = val.trim().trim_matches('"').trim_matches('\'').trim();
                    if !val.is_empty() {
                        return Some(val.to_string());
                    }
                }
            }
        }
    }
    None
}

/// Parse ``Set-Cookie`` header values into ``(name, value)`` pairs.
///
/// Each header is parsed independently, so commas inside ``Expires`` dates do
/// not split a single cookie.  Only the first ``name=value`` segment is kept.
pub fn parse_cookies(set_cookies: &[String]) -> Vec<(String, String)> {
    let mut out = Vec::new();
    for header in set_cookies {
        let first = header.split(';').next().unwrap_or("").trim();
        if let Some((name, value)) = first.split_once('=') {
            let name = name.trim();
            if !name.is_empty() {
                out.push((name.to_string(), value.trim().to_string()));
            }
        }
    }
    out
}
