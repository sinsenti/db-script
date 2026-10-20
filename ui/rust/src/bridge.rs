//! Native JSONL transports. No benchmark or Python runtime dependency.
use crate::data::{Config, Result};
use serde_json::{json, Value};
use std::{
    io::{BufRead, BufReader, Write},
    process::{Child, ChildStdin, ChildStdout, Command, Stdio},
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
    pub stopped: std::sync::atomic::AtomicBool,
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
pub struct Worker {
    child: std::sync::Arc<std::sync::Mutex<Child>>,
    input: ChildStdin,
    output: BufReader<ChildStdout>,
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
        if control.is_some_and(|c| c.stopped.load(std::sync::atomic::Ordering::Acquire)) {
            return Err("Worker startup cancelled".into());
        }
        let mut child = command(&config)?
            .spawn()
            .map_err(|_| "Could not start native worker; check --worker path".to_string())?;
        let input = child.stdin.take().unwrap();
        let output = BufReader::new(child.stdout.take().unwrap());
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
        };
        if family(&w.config.backend)? == Family::Rewrite {
            let ready = w.line()?;
            if ready["ready"] != 1 {
                return Err(
                    "Worker startup failed; check target and inherited connection environment"
                        .into(),
                );
            }
        }
        Ok(w)
    }
    fn line(&mut self) -> Result<Value> {
        let mut s = String::new();
        self.output
            .read_line(&mut s)
            .map_err(|_| "Worker read failed")?;
        if s.is_empty() {
            return Err("Worker exited unexpectedly".into());
        }
        serde_json::from_str(&s).map_err(|_| "Invalid worker JSON response".into())
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
        serde_json::to_writer(&mut self.input, &request).map_err(|_| "Worker write failed")?;
        writeln!(self.input).map_err(|_| "Worker write failed")?;
        self.input.flush().map_err(|_| "Worker flush failed")?;
        let f = family(&self.config.backend)?;
        response(f, self.line()?)
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
