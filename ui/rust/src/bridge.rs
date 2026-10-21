//! Native JSONL transports. No benchmark or Python runtime dependency.
use crate::data::{Config, Result};
use serde_json::{json, Value};
use std::{
    io::{Read, Write},
    os::fd::{AsRawFd, RawFd},
    os::unix::{net::UnixStream, process::CommandExt},
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
    c.env_remove("DBCLI_WORKER_CANCEL_FD");
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
/// Absolute grace budget, including partial frames and interrupted polls.
fn attempt_cancel(socket: &mut UnixStream, id: u64) {
    let frame = id.to_le_bytes();
    let (mut sent, mut received) = (0, 0);
    let mut receipt = [0u8; 8];
    let deadline = Instant::now() + Duration::from_millis(350);
    while Instant::now() < deadline {
        let mut fd = libc::pollfd {
            fd: socket.as_raw_fd(),
            events: if sent < frame.len() {
                libc::POLLOUT
            } else {
                libc::POLLIN
            },
            revents: 0,
        };
        let ms = deadline
            .saturating_duration_since(Instant::now())
            .as_millis()
            .clamp(1, 25) as i32;
        if unsafe { libc::poll(&mut fd, 1, ms) } <= 0 {
            continue;
        }
        let result = if sent < frame.len() {
            socket.write(&frame[sent..])
        } else {
            socket.read(&mut receipt[received..])
        };
        match result {
            Ok(0) => return,
            Ok(n) if sent < frame.len() => sent += n,
            Ok(n) => {
                received += n;
                if received == receipt.len() {
                    return;
                }
            }
            Err(e)
                if matches!(
                    e.kind(),
                    std::io::ErrorKind::WouldBlock | std::io::ErrorKind::Interrupted
                ) => {}
            Err(_) => return,
        }
    }
}
pub struct Worker {
    child: std::sync::Arc<std::sync::Mutex<Child>>,
    input: ChildStdin,
    output: ChildStdout,
    cancel: Option<UnixStream>,
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
        let mut cmd = command(&config)?;
        let (cancel, inherited) =
            if config.engine == "postgres" && config.backend == "original-rust" {
                let (parent, worker) =
                    UnixStream::pair().map_err(|_| "Cannot create cancellation channel")?;
                parent
                    .set_nonblocking(true)
                    .map_err(|_| "Cannot bound cancellation channel")?;
                let fd = worker.as_raw_fd();
                cmd.env("DBCLI_WORKER_CANCEL_FD", fd.to_string());
                // Only this socket is intentionally inherited. The pre-exec closure
                // performs async-signal-safe fcntl; it neither allocates nor locks.
                unsafe {
                    cmd.pre_exec(move || {
                        if libc::fcntl(fd, libc::F_SETFD, 0) < 0 {
                            return Err(std::io::Error::last_os_error());
                        }
                        Ok(())
                    });
                }
                (Some(parent), Some(worker))
            } else {
                (None, None)
            };
        let mut child = cmd
            .spawn()
            .map_err(|_| "Could not start native worker; check --worker path".to_string())?;
        drop(inherited);
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
            cancel,
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
        let mut request = envelope(&self.config, &op, self.serial)?;
        if self.cancel.is_some() {
            request["_request_id"] = json!(self.serial);
        }
        let read = !["insert", "update", "delete", "apply_batch"]
            .contains(&op["op"].as_str().unwrap_or(""));
        let f = family(&self.config.backend)?;
        let result = self.exchange(&request);
        if result.is_err() {
            // Partial frames cannot be reused: kill/reap, never retry this operation.
            self.invalidate(self.cancellable && read);
        }
        let body = result?;
        if f == Family::Rewrite && body["id"].as_u64() != Some(self.serial) {
            self.invalidate(self.cancellable && read);
            return Err("Mismatched worker response ID".into());
        }
        response(f, body)
    }
    fn invalidate(&mut self, cancel_read: bool) {
        self.failed = true;
        if cancel_read {
            if let Some(socket) = &mut self.cancel {
                attempt_cancel(socket, self.serial);
            }
        }
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
    fn stalled_cancel_has_bounded_fallback_and_writes_send_no_cancel_frame() {
        use std::os::unix::fs::PermissionsExt;
        let dir = tempfile::tempdir().unwrap();
        let script = dir.path().join("worker");
        std::fs::write(&script, "#!/bin/sh\nwhile IFS= read -r line; do :; done\n").unwrap();
        std::fs::set_permissions(&script, std::fs::Permissions::from_mode(0o700)).unwrap();
        for write in [false, true] {
            let mut config = c("original-rust");
            config.engine = "postgres".into();
            config.worker = script.clone();
            let mut worker = Worker::spawn(config).unwrap();
            worker.limits.deadline = Duration::from_millis(50);
            let (channel, mut observer) = UnixStream::pair().unwrap();
            channel.set_nonblocking(true).unwrap();
            observer
                .set_read_timeout(Some(Duration::from_millis(50)))
                .unwrap();
            worker.cancel = Some(channel);
            let started = Instant::now();
            let op = if write {
                json!({"op":"apply_batch","table":"items","actions":[]})
            } else {
                json!({"op":"tables"})
            };
            assert!(worker.request(op).unwrap_err().contains("deadline"));
            assert!(started.elapsed() < Duration::from_secs(2));
            assert!(worker.child.lock().unwrap().try_wait().unwrap().is_some());
            let mut frame = [0; 8];
            if write {
                assert!(observer.read_exact(&mut frame).is_err());
            } else {
                observer.read_exact(&mut frame).unwrap();
                assert_eq!(u64::from_le_bytes(frame), 1);
            }
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
