//! Native JSONL transports. No benchmark or Python runtime dependency.
use crate::data::{Config, Result};
use serde_json::{json, Value};
use std::{
    io::{Read, Write},
    os::fd::{AsRawFd, RawFd},
    process::{Child, ChildStdin, ChildStdout, Command, Stdio},
    sync::{
        atomic::{AtomicBool, AtomicU64, Ordering},
        Arc,
    },
    time::{Duration, Instant},
};
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Family {
    Dispatch,
    Batch,
    Rewrite,
}
pub fn family(alias: &str) -> Result<Family> {
    if alias.starts_with("original-") {
        Ok(Family::Dispatch)
    } else if alias.starts_with("codex-") {
        Ok(Family::Batch)
    } else if alias.starts_with("pi-") || alias.starts_with("integrated-") {
        Ok(Family::Rewrite)
    } else {
        Err("Choose original-rust, codex-rust, pi-rust or integrated-rust (Go workers also supported)".into())
    }
}
pub fn command(config: &Config) -> Result<Command> {
    let f = family(&config.backend)?;
    let mut c = Command::new(&config.worker);
    if config.engine == "postgres" {
        c.arg("--postgres");
    }
    match f {
        Family::Dispatch => {
            c.arg("--protocol");
            if config.engine == "sqlite" {
                c.arg("--db").arg(&config.database);
            }
        }
        Family::Batch => {
            c.arg("--runner");
        }
        Family::Rewrite => {
            c.arg("--protocol=jsonl");
            if config.engine == "sqlite" {
                c.arg("--target=sqlite")
                    .arg("--database")
                    .arg(&config.database);
            }
        }
    }
    if config.read_only {
        c.arg("--read-only");
    }
    c.stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null());
    Ok(c)
}
pub fn envelope(config: &Config, op: &Value, id: u64) -> Result<Value> {
    let f = family(&config.backend)?;
    let mut op = op.clone();
    if config.backend.ends_with("-go") && op["op"] == "apply_batch" {
        if let Some(actions) = op.as_object_mut().unwrap().remove("actions") {
            let changes = actions
                .as_array()
                .ok_or("actions must be array")?
                .iter()
                .map(|a| {
                    let mut a = a.clone();
                    if let Some(kind) = a.as_object_mut().unwrap().remove("kind") {
                        a["op"] = kind;
                    }
                    a
                })
                .collect::<Vec<_>>();
            op["operations"] = json!(changes);
        }
    }
    if f != Family::Batch {
        if let Some(order) = op.as_object_mut().unwrap().remove("order_by") {
            if f == Family::Dispatch {
                op["sort"] = order;
            } else if let Some(a) = order.as_array() {
                op["order_by"] = json!({"column":a[0],"direction":a[1]});
            }
        }
        if op["op"] == "update" {
            if let Some(row) = op.as_object_mut().unwrap().remove("row") {
                op["updated"] = row;
            }
        }
    }
    let name = op["op"].as_str().unwrap_or("").to_string();
    if f == Family::Dispatch && name == "tables" {
        op["op"] = json!("list_tables");
    }
    if f == Family::Rewrite {
        op["op"] = json!(match name.as_str() {
            "tables" => "list_tables",
            "page" => "fetch_page",
            "count" => "count_rows",
            "raw" => "execute_readonly",
            _ => &name,
        });
    }
    let op = wire(op, f);
    Ok(match f {
        Family::Batch => {
            json!({"engine":config.engine,"database":if config.engine=="sqlite"{config.database.clone()}else{"postgres".into()},"read_only":config.read_only,"operations":[op]})
        }
        Family::Dispatch => op,
        Family::Rewrite => {
            let mut args = op;
            let name = args.as_object_mut().unwrap().remove("op").unwrap();
            json!({"id":id,"op":name,"args":args})
        }
    })
}
fn wire(v: Value, f: Family) -> Value {
    if crate::data::json_envelope(&v) {
        return v;
    }
    match v {
        Value::Array(a) => Value::Array(a.into_iter().map(|v| wire(v, f)).collect()),
        Value::Object(o) if o.len() == 1 && o.get("$bytes").and_then(Value::as_str).is_some() => {
            let h = o["$bytes"].as_str().unwrap();
            if let Ok(b) = crate::data::hex(h) {
                use base64::Engine;
                let b = base64::engine::general_purpose::STANDARD.encode(b);
                match f {
                    Family::Dispatch => json!({"$blob":b}),
                    Family::Rewrite => json!({"$type":"blob","base64":b}),
                    Family::Batch => Value::Object(o),
                }
            } else {
                Value::Object(o)
            }
        }
        Value::Object(o) => Value::Object(o.into_iter().map(|(k, v)| (k, wire(v, f))).collect()),
        _ => v,
    }
}
pub fn response(f: Family, response: Value) -> Result<Value> {
    let body = if f == Family::Batch {
        response
            .get("results")
            .and_then(Value::as_array)
            .and_then(|a| a.first())
            .cloned()
            .ok_or("Invalid worker batch response")?
    } else {
        response
    };
    if body["ok"] != true {
        let e = &body["error"];
        let category = e
            .as_str()
            .or_else(|| e["class"].as_str())
            .or_else(|| e["category"].as_str())
            .unwrap_or("WorkerError");
        return Err(format!("{category}: worker rejected operation"));
    }
    Ok(body[if f == Family::Batch {
        "value"
    } else {
        "result"
    }]
    .clone())
}
#[derive(Default)]
pub struct Control {
    pub child: std::sync::Mutex<Option<std::sync::Arc<std::sync::Mutex<Child>>>>,
    pub stopped: AtomicBool,
    pub generation: AtomicU64,
    pub writing: AtomicBool,
    pub submitting: std::sync::Mutex<()>,
}
impl Control {
    pub fn shutdown(&self) {
        self.stopped
            .store(true, std::sync::atomic::Ordering::Release);
        let child = self.child.lock().unwrap_or_else(|e| e.into_inner()).clone();
        if let Some(child) = child {
            let mut child = child.lock().unwrap_or_else(|e| e.into_inner());
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}
/// Limits cover startup and each request's complete pipe exchange, not DB execution alone.
#[derive(Clone, Copy)]
struct Limits {
    response: usize,
    request: usize,
    deadline: Duration,
}
impl Limits {
    fn environment() -> Result<Self> {
        fn value(name: &str, default: usize, max: usize) -> Result<usize> {
            match std::env::var(name) {
                Err(std::env::VarError::NotPresent) => Ok(default),
                Ok(v) => v
                    .parse::<usize>()
                    .ok()
                    .filter(|n| *n > 0 && *n <= max)
                    .ok_or_else(|| format!("{name} must be an integer in 1..={max}")),
                Err(_) => Err(format!("Invalid {name}")),
            }
        }
        Ok(Self {
            response: value(
                "DBCLI_WORKER_MAX_RESPONSE_BYTES",
                128 * 1024 * 1024,
                1024 * 1024 * 1024,
            )?,
            request: value(
                "DBCLI_WORKER_MAX_REQUEST_BYTES",
                128 * 1024 * 1024,
                1024 * 1024 * 1024,
            )?,
            deadline: Duration::from_millis(
                value("DBCLI_WORKER_DEADLINE_MS", 60000, 600000)? as u64
            ),
        })
    }
}
struct LimitedBytes {
    bytes: Vec<u8>,
    limit: usize,
}
impl Write for LimitedBytes {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        if bytes.len() > self.limit.saturating_sub(self.bytes.len()) {
            return Err(std::io::Error::other("Worker request exceeds byte limit"));
        }
        self.bytes.extend_from_slice(bytes);
        Ok(bytes.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}
fn nonblocking(fd: RawFd) -> Result<()> {
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFL) };
    if flags < 0 || unsafe { libc::fcntl(fd, libc::F_SETFL, flags | libc::O_NONBLOCK) } < 0 {
        return Err("Could not configure worker pipe".into());
    }
    Ok(())
}
pub struct Worker {
    child: std::sync::Arc<std::sync::Mutex<Child>>,
    input: ChildStdin,
    output: ChildStdout,
    control: Option<Arc<Control>>,
    generation: u64,
    cancellable: bool,
    failed: bool,
    limits: Limits,
    config: Config,
    serial: u64,
}
impl Worker {
    pub fn spawn(config: Config) -> Result<Self> {
        Self::spawn_controlled(config, None)
    }
    pub fn spawn_controlled(
        config: Config,
        control: Option<&std::sync::Arc<Control>>,
    ) -> Result<Self> {
        let generation = control.map_or(0, |c| c.generation.load(Ordering::Acquire));
        Self::spawn_request(config, control, generation, true)
    }
    pub(crate) fn spawn_request(
        config: Config,
        control: Option<&Arc<Control>>,
        generation: u64,
        cancellable: bool,
    ) -> Result<Self> {
        if control.is_some_and(|c| c.stopped.load(Ordering::Acquire)) {
            return Err("Worker startup cancelled".into());
        }
        let limits = Limits::environment()?;
        let mut child = command(&config)?
            .spawn()
            .map_err(|_| "Could not start native worker; check --worker path".to_string())?;
        let input = child.stdin.take().unwrap();
        let output = child.stdout.take().unwrap();
        let child = std::sync::Arc::new(std::sync::Mutex::new(child));
        if let Some(control) = control {
            *control.child.lock().unwrap_or_else(|e| e.into_inner()) = Some(child.clone());
            if control.stopped.load(std::sync::atomic::Ordering::Acquire) {
                control.shutdown();
                return Err("Worker startup cancelled".into());
            }
        }

        let mut w = Self {
            child,
            input,
            output,
            config,
            serial: 0,
            generation,
            cancellable,
            control: control.cloned(),
            failed: false,
            limits,
        };
        nonblocking(w.input.as_raw_fd())?;
        nonblocking(w.output.as_raw_fd())?;
        if family(&w.config.backend)? == Family::Rewrite {
            let ready = w.line(Instant::now() + w.limits.deadline)?;
            if ready["ready"] != 1 {
                return Err(
                    "Worker startup failed; check target and inherited connection environment"
                        .into(),
                );
            }
        }
        Ok(w)
    }
    pub fn context(&mut self, generation: u64, cancellable: bool) {
        self.generation = generation;
        self.cancellable = cancellable;
    }
    pub fn failed(&self) -> bool {
        self.failed
    }
    fn check(&self, deadline: Instant) -> Result<()> {
        if self.failed {
            return Err("Worker transport closed; start a new request".into());
        }
        if let Some(c) = &self.control {
            if c.stopped.load(Ordering::Acquire) {
                return Err("Worker stopped".into());
            }
            if self.cancellable && c.generation.load(Ordering::Acquire) != self.generation {
                return Err("Read cancelled".into());
            }
        }
        if Instant::now() >= deadline {
            return Err("Worker I/O deadline exceeded".into());
        }
        Ok(())
    }
    fn wait(&self, fd: RawFd, events: i16, deadline: Instant) -> Result<()> {
        loop {
            self.check(deadline)?;
            let ms = deadline
                .saturating_duration_since(Instant::now())
                .as_millis()
                .clamp(1, 25) as i32;
            let mut descriptor = libc::pollfd {
                fd,
                events,
                revents: 0,
            };
            // Pipes are nonblocking; polling never holds the child mutex.
            let n = unsafe { libc::poll(&mut descriptor, 1, ms) };
            if n > 0 {
                self.check(deadline)?;
                if descriptor.revents & libc::POLLNVAL != 0 {
                    return Err("Worker pipe closed".into());
                }
                return Ok(());
            }
            if n < 0 && std::io::Error::last_os_error().kind() != std::io::ErrorKind::Interrupted {
                return Err("Worker pipe polling failed".into());
            }
        }
    }
    fn line(&mut self, deadline: Instant) -> Result<Value> {
        let mut bytes = Vec::new();
        let mut chunk = [0u8; 16384];
        loop {
            self.wait(self.output.as_raw_fd(), libc::POLLIN, deadline)?;
            // Read at most one byte beyond the limit to detect oversized frames.
            let bound = chunk.len().min(self.limits.response - bytes.len() + 1);
            match self.output.read(&mut chunk[..bound]) {
                Ok(0) => return Err("Worker exited before a complete response".into()),
                Ok(n) => {
                    if let Some(end) = chunk[..n].iter().position(|b| *b == b'\n') {
                        if bytes.len() + end > self.limits.response {
                            return Err("Worker response exceeds byte limit".into());
                        }
                        if end + 1 != n {
                            return Err("Unexpected extra worker response data".into());
                        }
                        bytes.extend_from_slice(&chunk[..end]);
                        self.check(deadline)?;
                        let value = serde_json::from_slice(&bytes)
                            .map_err(|_| "Invalid worker JSON response".to_string())?;
                        self.check(deadline)?;
                        return Ok(value);
                    }
                    if n > self.limits.response - bytes.len() {
                        return Err("Worker response exceeds byte limit".into());
                    }
                    bytes.extend_from_slice(&chunk[..n]);
                }
                Err(e)
                    if matches!(
                        e.kind(),
                        std::io::ErrorKind::WouldBlock | std::io::ErrorKind::Interrupted
                    ) => {}
                Err(_) => return Err("Worker read failed".into()),
            }
        }
    }
    fn exchange(&mut self, request: &Value) -> Result<Value> {
        let deadline = Instant::now() + self.limits.deadline;
        let mut frame = LimitedBytes {
            bytes: Vec::new(),
            limit: self.limits.request,
        };
        serde_json::to_writer(&mut frame, request)
            .map_err(|_| "Worker request exceeds byte limit")?;
        frame.bytes.push(b'\n');
        let mut sent = 0;
        while sent < frame.bytes.len() {
            self.wait(self.input.as_raw_fd(), libc::POLLOUT, deadline)?;
            match self.input.write(&frame.bytes[sent..]) {
                Ok(0) => return Err("Worker write failed".into()),
                Ok(n) => sent += n,
                Err(e)
                    if matches!(
                        e.kind(),
                        std::io::ErrorKind::WouldBlock | std::io::ErrorKind::Interrupted
                    ) => {}
                Err(_) => return Err("Worker write failed".into()),
            }
        }
        self.line(deadline)
    }
    pub fn request(&mut self, op: Value) -> Result<Value> {
        if self.config.read_only
            && ["insert", "update", "delete", "apply_batch"]
                .contains(&op["op"].as_str().unwrap_or(""))
        {
            return Err("ReadOnlyError: this screen cannot write".into());
        }
        self.serial += 1;
        let request = envelope(&self.config, &op, self.serial)?;
        let f = family(&self.config.backend)?;
        let result = self.exchange(&request);
        if result.is_err() {
            // Partial frames cannot be reused: kill/reap, never retry this operation.
            self.invalidate();
        }
        let body = result?;
        if f == Family::Rewrite && body["id"].as_u64() != Some(self.serial) {
            self.invalidate();
            return Err("Mismatched worker response ID".into());
        }
        response(f, body)
    }
    fn invalidate(&mut self) {
        self.failed = true;
        let mut child = self.child.lock().unwrap_or_else(|e| e.into_inner());
        let _ = child.kill();
        let _ = child.wait();
    }
    pub fn pid(&self) -> u32 {
        self.child.lock().unwrap_or_else(|e| e.into_inner()).id()
    }
}
impl Drop for Worker {
    fn drop(&mut self) {
        let mut child = self.child.lock().unwrap_or_else(|e| e.into_inner());
        let _ = child.kill();
        let _ = child.wait();
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;
    fn c(alias: &str) -> Config {
        Config {
            backend: alias.into(),
            worker: PathBuf::from("worker"),
            ..Config::default()
        }
    }
    #[test]
    fn json_envelope_payload_never_recursively_decodes_wire_tags() {
        let value = json!({"$type":"json","value":{"$bytes":"00ff","nested":{"$type":"json","value":null}}});
        for family in [Family::Dispatch, Family::Batch, Family::Rewrite] {
            assert_eq!(wire(value.clone(), family), value);
        }
    }
    #[test]
    fn all_transports() {
        for alias in [
            "original-rust",
            "original-go",
            "codex-rust",
            "codex-go",
            "pi-rust",
            "pi-go",
            "integrated-rust",
            "integrated-go",
        ] {
            let c = c(alias);
            let e = envelope(
                &c,
                &json!({"op":"page","table":"t","order_by":["id","DESC"]}),
                3,
            )
            .unwrap();
            match family(alias).unwrap() {
                Family::Batch => assert_eq!(e["operations"][0]["order_by"], json!(["id", "DESC"])),
                Family::Dispatch => assert_eq!(e["sort"], json!(["id", "DESC"])),
                Family::Rewrite => {
                    assert_eq!(e["op"], "fetch_page");
                    assert_eq!(e["args"]["order_by"]["column"], "id");
                }
            }
        }
    }
    #[test]
    fn responses_and_byte_envelopes() {
        for (f, key) in [
            (Family::Dispatch, "$blob"),
            (Family::Batch, "$bytes"),
            (Family::Rewrite, "base64"),
        ] {
            assert!(wire(json!({"$bytes":"00ff"}), f).get(key).is_some());
        }
        assert_eq!(
            response(Family::Batch, json!({"results":[{"ok":true,"value":2}]})).unwrap(),
            json!(2)
        );
        assert!(response(
            Family::Dispatch,
            json!({"ok":false,"error":{"class":"ReadOnlyError","message":"private text"}})
        )
        .unwrap_err()
        .contains("ReadOnlyError"));
    }
}
