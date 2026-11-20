//! The Rust HTTP engine: per-session client caching, request preparation,
//! the shared Tokio runtime and the blocking / lazy dispatch paths.

use std::sync::{Arc, Mutex, RwLock};
use std::time::Duration;

use pyo3::prelude::*;
use pyo3::types::{PyAny, PyDict, PyList, PyTuple};
use reqwest::header::{HeaderName, HeaderValue};
use reqwest::{Certificate, Client, Method, RequestBuilder, Url};
use tokio::sync::{oneshot, Semaphore};

use crate::error::{ErrorKind, ReqError};
use crate::lazy::{classify_proxy_error, LazyResponse};
use crate::proxy;
use crate::response::{make_raw, RawResponse, RequestInfo, ResponseData};

/// Upper bound on simultaneously cached clients (one per distinct config).
const MAX_CACHED_CLIENTS: usize = 8;
/// Per-host idle connection pool size.
const POOL_IDLE_PER_HOST: usize = 32;

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum VerifyMode {
    Default,
    Insecure,
    CertPath(String),
}

impl VerifyMode {
    fn signature(&self) -> String {
        match self {
            VerifyMode::Default => "default".to_string(),
            VerifyMode::Insecure => "insecure".to_string(),
            VerifyMode::CertPath(path) => format!("pem:{path}"),
        }
    }
}

#[derive(Clone, Debug)]
pub struct ClientDefaults {
    verify: VerifyMode,
    timeout: Option<f64>,
    no_proxy_all: bool,
    max_concurrency: usize,
}

#[derive(Clone, Debug)]
pub struct FilePart {
    pub name: String,
    pub filename: Option<String>,
    pub content_type: Option<String>,
    pub content: Vec<u8>,
}

#[derive(Clone, Debug)]
pub struct ExecOptions {
    pub method: String,
    pub url: String,
    pub headers: Vec<(String, String)>,
    pub params: Vec<(String, String)>,
    pub body: Option<Vec<u8>>,
    pub content_type: Option<String>,
    pub auth: Option<(String, String)>,
    pub timeout: Option<f64>,
    pub connect_timeout: Option<f64>,
    pub verify: VerifyMode,
    pub allow_redirects: bool,
    pub cookies: Vec<(String, String)>,
    pub proxies: Vec<(String, String)>,
    pub no_proxy: Option<String>,
    pub no_proxy_all: bool,
    pub max_concurrency: usize,
    pub files: Vec<FilePart>,
    pub form_fields: Vec<(String, String)>,
}

impl ExecOptions {
    fn from_kwargs(
        kwargs: Option<&Bound<'_, PyDict>>,
        defaults: &ClientDefaults,
        method: Option<&str>,
        url: Option<&str>,
    ) -> PyResult<Self> {
        let get = |key: &str| kwarg(kwargs, key);

        let method = match method {
            Some(method) if !method.is_empty() => method.to_string(),
            _ => get("method")
                .map(|value| proxy::stringify(&value))
                .transpose()?
                .unwrap_or_else(|| "GET".to_string()),
        };
        let url = match url {
            Some(url) if !url.is_empty() => url.to_string(),
            _ => get("url")
                .map(|value| proxy::stringify(&value))
                .transpose()?
                .unwrap_or_default(),
        };

        let headers = dict_pairs(get("headers").as_ref())?;
        let params = sequence_pairs(get("params").as_ref())?;
        let cookies = dict_pairs(get("cookies").as_ref())?;
        let form_fields = sequence_pairs(get("form_fields").as_ref())?;
        let files = parse_files(get("files").as_ref())?;

        let auth = parse_auth(get("auth").as_ref())?;

        let (connect_timeout, mut timeout) = parse_timeout(get("timeout").as_ref())?;
        if timeout.is_none() {
            timeout = defaults.timeout;
        }
        let verify = match get("verify") {
            Some(value) => parse_verify(Some(&value))?,
            None => defaults.verify.clone(),
        };
        let allow_redirects = get("allow_redirects")
            .map(|value| value.extract::<bool>())
            .transpose()?
            .unwrap_or(true);

        let mut proxies = match get("proxies") {
            Some(value) => proxy::parse_proxies(Some(&value))?,
            None => Vec::new(),
        };
        // A single ``proxy=`` override (pyureq style) wins for all schemes.
        if let Some(value) = get("proxy") {
            let text = proxy::stringify(&value)?;
            if !text.trim().is_empty() {
                proxies.retain(|(key, _)| !key.eq_ignore_ascii_case("all"));
                proxies.push(("all".to_string(), text));
            }
        }
        let no_proxy = proxy::parse_no_proxy(get("no_proxy").as_ref())?;

        let trust_env = get("trust_env")
            .map(|value| value.extract::<bool>())
            .transpose()?
            .unwrap_or(true);
        let explicit_no_proxy = get("no_proxy_all")
            .map(|value| value.extract::<bool>())
            .transpose()?
            .unwrap_or(false);
        let proxies_disabled =
            !proxies.is_empty() && proxies.iter().all(|(_, value)| value.trim().is_empty());
        let no_proxy_all =
            explicit_no_proxy || !trust_env || proxies_disabled || defaults.no_proxy_all;

        let max_concurrency = get("max_concurrency")
            .map(|value| value.extract::<usize>())
            .transpose()?
            .unwrap_or(defaults.max_concurrency)
            .clamp(1, 100_000);

        let mut body = parse_body(get("body").as_ref())?;
        // ``None`` must not become the string "None": the Python layer always
        // passes a content_type key (often ``None``) alongside the headers.
        let content_type = match get("content_type") {
            Some(value) if !value.is_none() => {
                let text = proxy::stringify(&value)?;
                if text.is_empty() {
                    None
                } else {
                    Some(text)
                }
            }
            _ => None,
        };

        // ``json=`` is accepted by the Rust API directly (the Python layer
        // normally pre-serialises it, which is faster and preserves Python's
        // number/string semantics).
        if body.is_none() {
            if let Some(value) = get("json") {
                if !value.is_none() {
                    let parsed: serde_json::Value =
                        pythonize::depythonize(&value).map_err(|err| {
                            pyo3::exceptions::PyValueError::new_err(format!(
                                "Could not serialize json body: {err}"
                            ))
                        })?;
                    let bytes = serde_json::to_vec(&parsed).map_err(|err| {
                        pyo3::exceptions::PyValueError::new_err(format!(
                            "Could not serialize json body: {err}"
                        ))
                    })?;
                    body = Some(bytes);
                }
            }
        }

        Ok(Self {
            method,
            url,
            headers,
            params,
            body,
            content_type,
            auth,
            timeout,
            connect_timeout,
            verify,
            allow_redirects,
            cookies,
            proxies,
            no_proxy,
            no_proxy_all,
            max_concurrency,
            files,
            form_fields,
        })
    }
}

// ---------------------------------------------------------------------------
// Python -> Rust argument helpers
// ---------------------------------------------------------------------------

fn kwarg<'py>(kwargs: Option<&Bound<'py, PyDict>>, key: &str) -> Option<Bound<'py, PyAny>> {
    kwargs.and_then(|dict| dict.get_item(key).ok().flatten())
}

fn dict_pairs(value: Option<&Bound<'_, PyAny>>) -> PyResult<Vec<(String, String)>> {
    let mut out = Vec::new();
    let Some(value) = value else { return Ok(out) };
    if value.is_none() {
        return Ok(out);
    }
    if let Ok(dict) = value.cast::<PyDict>() {
        for (key, item) in dict.iter() {
            out.push((proxy::stringify(&key)?, proxy::stringify(&item)?));
        }
        return Ok(out);
    }
    if let Ok(items) = value.call_method0("items") {
        for item in items.try_iter()? {
            let pair = item?;
            if let Ok(tuple) = pair.cast::<PyTuple>() {
                if tuple.len() >= 2 {
                    out.push((
                        proxy::stringify(&tuple.get_item(0)?)?,
                        proxy::stringify(&tuple.get_item(1)?)?,
                    ));
                }
            }
        }
    }
    Ok(out)
}

/// Parse a sequence of 2-item pairs (params, form fields, cookies).
fn sequence_pairs(value: Option<&Bound<'_, PyAny>>) -> PyResult<Vec<(String, String)>> {
    let mut out = Vec::new();
    let Some(value) = value else { return Ok(out) };
    if value.is_none() {
        return Ok(out);
    }
    if let Ok(dict) = value.cast::<PyDict>() {
        for (key, item) in dict.iter() {
            out.push((proxy::stringify(&key)?, proxy::stringify(&item)?));
        }
        return Ok(out);
    }
    for item in value.try_iter()? {
        let item = item?;
        let pair = pair_items(&item)?;
        if pair.len() >= 2 {
            out.push((proxy::stringify(&pair[0])?, proxy::stringify(&pair[1])?));
        }
    }
    Ok(out)
}

fn pair_items<'py>(item: &Bound<'py, PyAny>) -> PyResult<Vec<Bound<'py, PyAny>>> {
    if let Ok(tuple) = item.cast::<PyTuple>() {
        return (0..tuple.len())
            .map(|index| tuple.get_item(index))
            .collect();
    }
    if let Ok(list) = item.cast::<PyList>() {
        return (0..list.len()).map(|index| list.get_item(index)).collect();
    }
    Ok(vec![item.clone()])
}

fn parse_auth(value: Option<&Bound<'_, PyAny>>) -> PyResult<Option<(String, String)>> {
    let Some(value) = value else { return Ok(None) };
    if value.is_none() {
        return Ok(None);
    }
    let items = pair_items(value)?;
    if items.len() >= 2 {
        return Ok(Some((
            proxy::stringify(&items[0])?,
            proxy::stringify(&items[1])?,
        )));
    }
    // requests-style auth object: ``username`` / ``password`` attributes.
    if let (Ok(user), Ok(password)) = (value.getattr("username"), value.getattr("password")) {
        return Ok(Some((
            proxy::stringify(&user)?,
            proxy::stringify(&password)?,
        )));
    }
    Ok(None)
}

fn parse_timeout(value: Option<&Bound<'_, PyAny>>) -> PyResult<(Option<f64>, Option<f64>)> {
    let Some(value) = value else {
        return Ok((None, None));
    };
    if value.is_none() {
        return Ok((None, None));
    }
    if let Ok(seconds) = value.extract::<f64>() {
        if seconds.is_finite() && seconds > 0.0 {
            return Ok((None, Some(seconds)));
        }
        return Ok((None, None));
    }
    let items = pair_items(value)?;
    if items.len() >= 2 {
        let connect = items[0]
            .extract::<f64>()
            .ok()
            .filter(|v| v.is_finite() && *v > 0.0);
        let total = items[1]
            .extract::<f64>()
            .ok()
            .filter(|v| v.is_finite() && *v > 0.0);
        return Ok((connect, total));
    }
    Ok((None, None))
}

fn parse_verify(value: Option<&Bound<'_, PyAny>>) -> PyResult<VerifyMode> {
    let Some(value) = value else {
        return Ok(VerifyMode::Default);
    };
    if value.is_none() {
        return Ok(VerifyMode::Default);
    }
    if let Ok(enabled) = value.extract::<bool>() {
        return Ok(if enabled {
            VerifyMode::Default
        } else {
            VerifyMode::Insecure
        });
    }
    if let Ok(path) = value.extract::<String>() {
        if !path.trim().is_empty() {
            return Ok(VerifyMode::CertPath(path));
        }
    }
    Ok(VerifyMode::Default)
}

fn parse_body(value: Option<&Bound<'_, PyAny>>) -> PyResult<Option<Vec<u8>>> {
    let Some(value) = value else { return Ok(None) };
    if value.is_none() {
        return Ok(None);
    }
    if let Ok(bytes) = value.extract::<Vec<u8>>() {
        return Ok(Some(bytes));
    }
    if let Ok(text) = value.extract::<String>() {
        return Ok(Some(text.into_bytes()));
    }
    Ok(Some(value.str()?.to_string().into_bytes()))
}

fn parse_files(value: Option<&Bound<'_, PyAny>>) -> PyResult<Vec<FilePart>> {
    let mut out = Vec::new();
    let Some(value) = value else { return Ok(out) };
    if value.is_none() {
        return Ok(out);
    }
    for item in value.try_iter()? {
        let item = item?;
        let items = pair_items(&item)?;
        if items.len() < 4 {
            return Err(pyo3::exceptions::PyValueError::new_err(
                "lazyreq: each file entry must be (name, filename, content_type, bytes)",
            ));
        }
        let name = proxy::stringify(&items[0])?;
        let filename = if items[1].is_none() {
            None
        } else {
            Some(proxy::stringify(&items[1])?)
        };
        let content_type = if items[2].is_none() {
            None
        } else {
            Some(proxy::stringify(&items[2])?)
        };
        let content = items[3].extract::<Vec<u8>>()?;
        out.push(FilePart {
            name,
            filename,
            content_type,
            content,
        });
    }
    Ok(out)
}

// ---------------------------------------------------------------------------
// Client cache
// ---------------------------------------------------------------------------

#[derive(Clone, Debug)]
struct ClientKey {
    follow_redirects: bool,
    verify: VerifyMode,
    connect_timeout: Option<u64>,
    proxies: Vec<(String, String)>,
    no_proxy: Option<String>,
    no_proxy_all: bool,
}

impl ClientKey {
    fn from_options(options: &ExecOptions) -> Self {
        Self {
            follow_redirects: options.allow_redirects,
            verify: options.verify.clone(),
            connect_timeout: options
                .connect_timeout
                .filter(|value| value.is_finite() && *value > 0.0)
                .map(|value| (value * 1000.0).ceil() as u64),
            proxies: options.proxies.clone(),
            no_proxy: options.no_proxy.clone(),
            no_proxy_all: options.no_proxy_all,
        }
    }

    fn signature(&self) -> String {
        format!(
            "redirects={}|verify={}|connect={:?}|proxies={:?}|no_proxy={:?}|no_proxy_all={}",
            self.follow_redirects,
            self.verify.signature(),
            self.connect_timeout,
            self.proxies,
            self.no_proxy,
            self.no_proxy_all
        )
    }
}

pub struct ClientInner {
    clients: RwLock<Vec<(String, Client)>>,
    semaphore: Mutex<Option<(usize, Arc<Semaphore>)>>,
}

impl ClientInner {
    pub fn new() -> Self {
        Self {
            clients: RwLock::new(Vec::new()),
            semaphore: Mutex::new(None),
        }
    }

    fn semaphore_for(&self, limit: usize) -> Arc<Semaphore> {
        let mut guard = self.semaphore.lock().unwrap();
        if let Some((capacity, semaphore)) = guard.as_ref() {
            if *capacity == limit {
                return semaphore.clone();
            }
        }
        let semaphore = Arc::new(Semaphore::new(limit));
        *guard = Some((limit, semaphore.clone()));
        semaphore
    }

    fn client_for(&self, key: &ClientKey) -> Result<Client, ReqError> {
        let signature = key.signature();
        if let Ok(guard) = self.clients.read() {
            if let Some((_, client)) = guard.iter().find(|(cached, _)| *cached == signature) {
                return Ok(client.clone());
            }
        }

        let client = build_client(key)?;
        if let Ok(mut guard) = self.clients.write() {
            if let Some((_, client)) = guard.iter().find(|(cached, _)| *cached == signature) {
                return Ok(client.clone());
            }
            if guard.len() >= MAX_CACHED_CLIENTS {
                guard.remove(0);
            }
            guard.push((signature, client.clone()));
        }
        Ok(client)
    }

    fn clear(&self) {
        if let Ok(mut guard) = self.clients.write() {
            guard.clear();
        }
    }
}

fn build_client(key: &ClientKey) -> Result<Client, ReqError> {
    let mut builder = Client::builder()
        // rustls (with the platform verifier) + HTTP/2 negotiation by ALPN.
        .tls_backend_rustls()
        .redirect(if key.follow_redirects {
            reqwest::redirect::Policy::limited(10)
        } else {
            reqwest::redirect::Policy::none()
        })
        .pool_max_idle_per_host(POOL_IDLE_PER_HOST);

    if let Some(connect_timeout) = key.connect_timeout {
        builder = builder.connect_timeout(Duration::from_millis(connect_timeout));
    }

    match &key.verify {
        VerifyMode::Default => {}
        VerifyMode::Insecure => {
            builder = builder.tls_danger_accept_invalid_certs(true);
        }
        VerifyMode::CertPath(path) => {
            let data = std::fs::read(path).map_err(|err| {
                ReqError::new(
                    ErrorKind::Ssl,
                    format!("Unable to read CA bundle '{path}': {err}"),
                )
            })?;
            let certificates = Certificate::from_pem_bundle(&data).map_err(|err| {
                ReqError::new(ErrorKind::Ssl, format!("Invalid CA bundle '{path}': {err}"))
            })?;
            builder = builder.tls_certs_merge(certificates);
        }
    }

    builder = proxy::apply(
        builder,
        &key.proxies,
        key.no_proxy.as_deref(),
        key.no_proxy_all,
    )?;

    builder.build().map_err(|err| {
        ReqError::new(
            ErrorKind::Connection,
            format!("Could not build HTTP client: {err}"),
        )
    })
}

// ---------------------------------------------------------------------------
// Request execution
// ---------------------------------------------------------------------------

pub struct Prepared {
    pub builder: RequestBuilder,
    pub semaphore: Arc<Semaphore>,
    pub request: RequestInfo,
}

pub fn prepare(inner: &ClientInner, options: &ExecOptions) -> Result<Prepared, ReqError> {
    let mut url = Url::parse(&options.url).map_err(|_| ReqError::invalid_url(&options.url))?;
    if !options.params.is_empty() {
        let mut query = url.query_pairs_mut();
        for (name, value) in &options.params {
            query.append_pair(name, value);
        }
    }

    let method = Method::from_bytes(options.method.as_bytes()).map_err(|_| {
        ReqError::new(
            ErrorKind::InvalidHeader,
            format!("Invalid HTTP method: {}", options.method),
        )
    })?;

    let key = ClientKey::from_options(options);
    let client = inner.client_for(&key)?;
    let semaphore = inner.semaphore_for(options.max_concurrency);

    let mut builder = client.request(method, url.clone());

    for (name, value) in &options.headers {
        let name = HeaderName::from_bytes(name.as_bytes()).map_err(|_| {
            ReqError::new(
                ErrorKind::InvalidHeader,
                format!("Invalid header name: {name:?}"),
            )
        })?;
        let value = HeaderValue::from_str(value).map_err(|_| {
            ReqError::new(
                ErrorKind::InvalidHeader,
                format!("Invalid value for header '{name}': {value:?}"),
            )
        })?;
        builder = builder.header(name, value);
    }

    if let Some((username, password)) = &options.auth {
        builder = builder.basic_auth(username.clone(), Some(password.clone()));
    }

    if let Some(seconds) = options.timeout {
        builder = builder.timeout(Duration::from_secs_f64(seconds.max(0.000_001)));
    }

    if !options.files.is_empty() {
        let mut form = reqwest::multipart::Form::new();
        for (name, value) in &options.form_fields {
            form = form.text(name.clone(), value.clone());
        }
        for file in &options.files {
            let mut part = reqwest::multipart::Part::bytes(file.content.clone());
            if let Some(filename) = &file.filename {
                part = part.file_name(filename.clone());
            }
            if let Some(content_type) = &file.content_type {
                part = part.mime_str(content_type).map_err(|err| {
                    ReqError::new(
                        ErrorKind::InvalidHeader,
                        format!("Invalid file MIME type: {err}"),
                    )
                })?;
            }
            form = form.part(file.name.clone(), part);
        }
        builder = builder.multipart(form);
    } else if let Some(body) = &options.body {
        builder = builder.body(body.clone());
    }

    if let Some(content_type) = &options.content_type {
        // A caller-provided Content-Type header wins over the inferred one.
        let has_header = options
            .headers
            .iter()
            .any(|(name, _)| name.eq_ignore_ascii_case("content-type"));
        if !has_header {
            builder = builder.header(reqwest::header::CONTENT_TYPE, content_type.as_str());
        }
    }

    if !options.cookies.is_empty()
        && !options
            .headers
            .iter()
            .any(|(name, _)| name.eq_ignore_ascii_case("cookie"))
    {
        let cookie = options
            .cookies
            .iter()
            .map(|(name, value)| format!("{name}={value}"))
            .collect::<Vec<_>>()
            .join("; ");
        if let Ok(value) = HeaderValue::from_str(&cookie) {
            builder = builder.header(reqwest::header::COOKIE, value);
        }
    }

    Ok(Prepared {
        builder,
        semaphore,
        request: RequestInfo {
            method: options.method.clone(),
            url: url.to_string(),
            headers: options.headers.clone(),
            proxy: !options.proxies.is_empty(),
        },
    })
}

pub async fn execute(
    builder: RequestBuilder,
    semaphore: Arc<Semaphore>,
) -> Result<ResponseData, ReqError> {
    let _permit = semaphore.acquire_owned().await.map_err(|_| {
        ReqError::other("lazyreq: the concurrency semaphore was closed unexpectedly")
    })?;

    let started = std::time::Instant::now();
    let response = builder.send().await.map_err(ReqError::from_reqwest)?;
    let status = response.status();
    let version = response.version();
    let final_url = response.url().clone();

    let mut headers = Vec::new();
    for (name, value) in response.headers().iter() {
        if let Ok(value) = value.to_str() {
            headers.push((name.as_str().to_string(), value.to_string()));
        }
    }

    let mut set_cookies = Vec::new();
    for value in response
        .headers()
        .get_all(reqwest::header::SET_COOKIE)
        .iter()
    {
        if let Ok(value) = value.to_str() {
            set_cookies.push(value.to_string());
        }
    }

    let body = response
        .bytes()
        .await
        .map_err(ReqError::from_reqwest)?
        .to_vec();

    Ok(ResponseData {
        status: status.as_u16(),
        reason: status.canonical_reason().unwrap_or("").to_string(),
        headers,
        body,
        url: final_url.to_string(),
        elapsed_secs: started.elapsed().as_secs_f64(),
        http_version: format!("{:?}", version),
        set_cookies,
    })
}

// ---------------------------------------------------------------------------
// Python-visible client
// ---------------------------------------------------------------------------

/// Persistent HTTP client with connection pooling and per-session settings.
#[pyclass(module = "lazyreq._lazyreq")]
pub struct RustClient {
    inner: Arc<ClientInner>,
    defaults: ClientDefaults,
}

#[pymethods]
impl RustClient {
    #[new]
    #[pyo3(signature = (verify = None, timeout = None, no_proxy_all = false, max_concurrency = 100))]
    fn new(
        verify: Option<&Bound<'_, PyAny>>,
        timeout: Option<&Bound<'_, PyAny>>,
        no_proxy_all: bool,
        max_concurrency: usize,
    ) -> PyResult<Self> {
        let (_, total) = parse_timeout(timeout)?;
        Ok(Self {
            inner: Arc::new(ClientInner::new()),
            defaults: ClientDefaults {
                verify: parse_verify(verify)?,
                timeout: total,
                no_proxy_all,
                max_concurrency: max_concurrency.clamp(1, 100_000),
            },
        })
    }

    /// Execute a request and block until the response has been read.
    ///
    /// The GIL is released for the duration of the network I/O.
    #[pyo3(signature = (method = None, url = None, **kwargs))]
    fn request(
        &self,
        py: Python<'_>,
        method: Option<&str>,
        url: Option<&str>,
        kwargs: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<Py<RawResponse>> {
        let options = ExecOptions::from_kwargs(kwargs, &self.defaults, method, url)?;
        options_check(py, &options)?;
        let prepared =
            prepare(&self.inner, &options).map_err(|err| err.to_pyerr(py, None, None))?;
        let result =
            py.detach(|| crate::rt().block_on(execute(prepared.builder, prepared.semaphore)));
        match result {
            Ok(data) => make_raw(py, &data, &prepared.request),
            Err(err) => Err(classify(err, &options).to_pyerr(py, None, None)),
        }
    }

    /// Start a request without blocking and return a handle immediately.
    ///
    /// The work happens on the shared Tokio runtime; no ``await`` is required
    /// in Python.  Errors surface when the handle is resolved.
    #[pyo3(signature = (method = None, url = None, **kwargs))]
    fn request_lazy(
        &self,
        py: Python<'_>,
        method: Option<&str>,
        url: Option<&str>,
        kwargs: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<LazyResponse> {
        let options = ExecOptions::from_kwargs(kwargs, &self.defaults, method, url)?;
        options_check(py, &options)?;

        let cookie_sink = kwarg(kwargs, "cookie_callback").map(|value| value.unbind());
        let prepared =
            prepare(&self.inner, &options).map_err(|err| err.to_pyerr(py, None, None))?;

        let (sender, receiver) = oneshot::channel();
        crate::rt().spawn(async move {
            let result = execute(prepared.builder, prepared.semaphore).await;
            let _ = sender.send(result);
        });

        Ok(LazyResponse::create(
            receiver,
            cookie_sink,
            prepared.request,
        ))
    }

    /// Drop all cached clients (and their connection pools).
    fn close(&self) {
        self.inner.clear();
    }

    /// Number of requests currently in flight.
    fn in_flight(&self) -> usize {
        if let Some((capacity, semaphore)) = self.inner.semaphore.lock().unwrap().as_ref() {
            capacity.saturating_sub(semaphore.available_permits())
        } else {
            0
        }
    }

    fn __repr__(&self) -> String {
        "<lazyreq.RustClient>".to_string()
    }
}

fn classify(err: ReqError, options: &ExecOptions) -> ReqError {
    classify_proxy_error(err, !options.proxies.is_empty())
}

fn options_check(py: Python<'_>, options: &ExecOptions) -> PyResult<()> {
    if options.url.trim().is_empty() {
        return Err(
            ReqError::new(ErrorKind::MissingSchema, "Invalid URL: no URL supplied")
                .to_pyerr(py, None, None),
        );
    }
    if options.method.trim().is_empty() {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "lazyreq: HTTP method must not be empty",
        ));
    }
    Ok(())
}
