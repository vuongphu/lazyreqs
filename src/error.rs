//! Exception mapping for the `lazyreq.exceptions` hierarchy.
//!
//! The exception *types* live in Python (``lazyreq/exceptions.py``) so that the
//! hierarchy mirrors ``requests.exceptions`` exactly.  This module only
//! classifies transport failures and builds the corresponding Python
//! exception instance, attaching ``response``/``request`` metadata when the
//! caller has it available.

use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::types::PyAny;

/// Coarse classification of a failed request.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ErrorKind {
    /// Generic request failure.
    Request,
    /// TCP/DNS failure.
    Connection,
    /// Failure talking to a proxy.
    Proxy,
    /// TLS handshake / certificate failure.
    Ssl,
    /// The connection could not be established in time.
    ConnectTimeout,
    /// The server did not send data in time.
    ReadTimeout,
    /// Redirect loop or redirect policy violation.
    TooManyRedirects,
    /// Malformed URL.
    InvalidUrl,
    /// URL without a scheme (``http://`` / ``https://``).
    MissingSchema,
    /// Header name/value rejected by the HTTP stack.
    InvalidHeader,
    /// Response body was truncated / chunked encoding broken.
    Body,
    /// Response body could not be decoded (compression, charset).
    Decode,
}

impl ErrorKind {
    pub fn class_name(self) -> &'static str {
        match self {
            ErrorKind::Request => "RequestException",
            ErrorKind::Connection => "ConnectionError",
            ErrorKind::Proxy => "ProxyError",
            ErrorKind::Ssl => "SSLError",
            ErrorKind::ConnectTimeout => "ConnectTimeout",
            ErrorKind::ReadTimeout => "ReadTimeout",
            ErrorKind::TooManyRedirects => "TooManyRedirects",
            ErrorKind::InvalidUrl => "InvalidURL",
            ErrorKind::MissingSchema => "MissingSchema",
            ErrorKind::InvalidHeader => "InvalidHeader",
            ErrorKind::Body => "ChunkedEncodingError",
            ErrorKind::Decode => "ContentDecodingError",
        }
    }
}

/// Transport-level failure that has not yet been turned into a Python error.
#[derive(Clone, Debug)]
pub struct ReqError {
    pub kind: ErrorKind,
    pub message: String,
}

impl ReqError {
    pub fn new(kind: ErrorKind, message: impl Into<String>) -> Self {
        Self {
            kind,
            message: message.into(),
        }
    }

    pub fn other(message: impl Into<String>) -> Self {
        Self::new(ErrorKind::Request, message)
    }

    pub fn invalid_url(url: &str) -> Self {
        if url.split_whitespace().next().unwrap_or("").contains("://") {
            Self::new(
                ErrorKind::InvalidUrl,
                format!("Invalid URL '{url}': no valid host was supplied"),
            )
        } else {
            Self::new(
                ErrorKind::MissingSchema,
                format!(
                    "Invalid URL '{url}': No scheme supplied. Perhaps you meant https://{url}?"
                ),
            )
        }
    }

    /// Classify a `reqwest::Error` by inspecting its well-known predicates and
    /// message, since `reqwest` deliberately erases the concrete source type.
    pub fn from_reqwest(err: reqwest::Error) -> Self {
        let message = err.to_string();
        let lower = message.to_lowercase();
        let kind = if err.is_timeout() {
            if lower.contains("connect") {
                ErrorKind::ConnectTimeout
            } else {
                ErrorKind::ReadTimeout
            }
        } else if err.is_redirect() {
            ErrorKind::TooManyRedirects
        } else if err.is_body() {
            ErrorKind::Body
        } else if err.is_decode() {
            ErrorKind::Decode
        } else if err.is_connect() {
            if lower.contains("tls")
                || lower.contains("certificate")
                || lower.contains("ssl")
                || lower.contains("handshake")
            {
                ErrorKind::Ssl
            } else if lower.contains("proxy") {
                ErrorKind::Proxy
            } else {
                ErrorKind::Connection
            }
        } else if err.is_request() {
            if lower.contains("header") {
                ErrorKind::InvalidHeader
            } else if lower.contains("proxy") {
                ErrorKind::Proxy
            } else if lower.contains("redirect") {
                ErrorKind::TooManyRedirects
            } else {
                ErrorKind::Request
            }
        } else {
            ErrorKind::Request
        };
        Self::new(kind, message)
    }

    /// Build the matching Python exception instance.
    ///
    /// `response`/`request` are attached as attributes when supplied.  If the
    /// Python exception module cannot be imported (e.g. a partially installed
    /// package), a plain `RuntimeError` is raised instead of masking the
    /// original failure.
    pub fn to_pyerr(
        &self,
        py: Python<'_>,
        response: Option<&Bound<'_, PyAny>>,
        request: Option<&Bound<'_, PyAny>>,
    ) -> PyErr {
        let message = self.message.clone();
        let exceptions = py.import("lazyreq.exceptions");

        if let Ok(module) = exceptions {
            if let Ok(class) = module.getattr(self.kind.class_name()) {
                if let Ok(instance) = class.call1((message,)) {
                    if let Some(response) = response {
                        let _ = instance.setattr("response", response);
                    }
                    if let Some(request) = request {
                        let _ = instance.setattr("request", request);
                    }
                    return PyErr::from_value(instance);
                }
            }
        }

        PyRuntimeError::new_err(self.message.clone())
    }
}
