//! Proxy resolution: requests-compatible ``proxies`` mappings, authentication
//! embedded in the proxy URL and ``no_proxy`` bypass rules.

use pyo3::prelude::*;
use pyo3::types::PyAny;
use reqwest::{ClientBuilder, NoProxy, Proxy};

use crate::error::ReqError;

/// Read a requests-style ``proxies`` mapping into an ordered key/value list.
///
/// Supported keys are ``http``, ``https`` and ``all`` (case-insensitive).
/// The ``no_proxy`` key is *not* a proxy; it is returned separately by
/// [`parse_no_proxy`].
pub fn parse_proxies(value: Option<&Bound<'_, PyAny>>) -> PyResult<Vec<(String, String)>> {
    let mut out = Vec::new();
    let Some(value) = value else { return Ok(out) };
    if value.is_none() {
        return Ok(out);
    }
    if let Ok(dict) = value.cast::<pyo3::types::PyDict>() {
        for (key, item) in dict.iter() {
            let key = stringify(&key)?;
            if key.eq_ignore_ascii_case("no_proxy") {
                continue;
            }
            out.push((normalize_key(&key), stringify(&item)?));
        }
    } else {
        // A bare string means "use this proxy for everything".
        out.push(("all".to_string(), stringify(value)?));
    }
    Ok(out)
}

/// Normalise a ``proxies`` key.
///
/// ``requests`` accepts ``"http"``, ``"https"``, ``"http://"`` and
/// host-scoped keys such as ``"http://example.com"``.  The Rust core applies
/// proxies per scheme, so a host-scoped key degrades to its scheme (a
/// documented approximation).
fn normalize_key(key: &str) -> String {
    let key = key.trim();
    let scheme = key.split("://").next().unwrap_or(key);
    scheme.trim().trim_end_matches(':').to_ascii_lowercase()
}

/// Read the ``no_proxy`` bypass list (string or sequence of hosts).
pub fn parse_no_proxy(value: Option<&Bound<'_, PyAny>>) -> PyResult<Option<String>> {
    let Some(value) = value else { return Ok(None) };
    if value.is_none() {
        return Ok(None);
    }
    if let Ok(text) = value.extract::<String>() {
        let text = text.trim();
        return Ok(if text.is_empty() {
            None
        } else {
            Some(text.to_string())
        });
    }
    if let Ok(items) = value.try_iter() {
        let mut parts = Vec::new();
        for item in items {
            parts.push(stringify(&item?)?);
        }
        let joined = parts.join(",");
        return Ok(if joined.is_empty() {
            None
        } else {
            Some(joined)
        });
    }
    Ok(None)
}

/// Apply the parsed proxy configuration to a `reqwest` client builder.
pub fn apply(
    mut builder: ClientBuilder,
    proxies: &[(String, String)],
    no_proxy: Option<&str>,
    no_proxy_all: bool,
) -> Result<ClientBuilder, ReqError> {
    if no_proxy_all {
        // Explicit opt-out of every proxy, including environment proxies.
        return Ok(builder.no_proxy());
    }

    let bypass = no_proxy.and_then(NoProxy::from_string);
    let find = |name: &str| {
        proxies
            .iter()
            .find(|(key, _)| key.eq_ignore_ascii_case(name))
            .map(|(_, value)| value.as_str())
            .filter(|value| !value.trim().is_empty())
    };

    if let Some(url) = find("all") {
        let proxy = Proxy::all(url).map_err(|err| bad_proxy(url, err))?;
        return Ok(builder.proxy(with_bypass(proxy, bypass.as_ref())));
    }

    if let Some(url) = find("http") {
        let proxy = Proxy::http(url).map_err(|err| bad_proxy(url, err))?;
        builder = builder.proxy(with_bypass(proxy, bypass.as_ref()));
    }
    if let Some(url) = find("https") {
        let proxy = Proxy::https(url).map_err(|err| bad_proxy(url, err))?;
        builder = builder.proxy(with_bypass(proxy, bypass.as_ref()));
    }

    Ok(builder)
}

fn with_bypass(mut proxy: Proxy, no_proxy: Option<&NoProxy>) -> Proxy {
    if let Some(no_proxy) = no_proxy {
        proxy = proxy.no_proxy(Some(no_proxy.clone()));
    }
    proxy
}

fn bad_proxy(url: &str, err: reqwest::Error) -> ReqError {
    ReqError::new(
        crate::error::ErrorKind::Proxy,
        format!("Invalid proxy URL '{url}': {err}"),
    )
}

/// Convert an arbitrary Python scalar to a string the way `requests` does:
/// text is kept verbatim, bytes are interpreted as latin-1, everything else
/// falls back to ``str()``.
pub fn stringify(value: &Bound<'_, PyAny>) -> PyResult<String> {
    if let Ok(text) = value.extract::<String>() {
        return Ok(text);
    }
    if let Ok(bytes) = value.extract::<Vec<u8>>() {
        return Ok(bytes.iter().map(|byte| *byte as char).collect());
    }
    Ok(value.str()?.to_string())
}
