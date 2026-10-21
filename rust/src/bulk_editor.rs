//! Private two-file Neovim diff handoff. No shell and no database access.
use std::{
    fs,
    io::Write,
    os::unix::fs::{DirBuilderExt, OpenOptionsExt},
    path::PathBuf,
    process::Command,
    sync::atomic::{AtomicU64, Ordering},
    time::{Duration, SystemTime, UNIX_EPOCH},
};
static NEXT: AtomicU64 = AtomicU64::new(0);
// Editors may leave termios or shared descriptor flags changed even after a
// successful exit. Restore the exact pre-editor state before the TUI resumes.
struct TerminalState {
    settings: Option<libc::termios>,
    flags: Vec<(i32, i32)>,
}
impl TerminalState {
    fn capture() -> Self {
        let mut settings = std::mem::MaybeUninit::<libc::termios>::uninit();
        let settings = if unsafe { libc::tcgetattr(0, settings.as_mut_ptr()) } == 0 {
            Some(unsafe { settings.assume_init() })
        } else {
            None
        };
        let flags = (0..=2)
            .filter_map(|fd| {
                let flag = unsafe { libc::fcntl(fd, libc::F_GETFL) };
                if flag >= 0 {
                    Some((fd, flag))
                } else {
                    None
                }
            })
            .collect();
        Self { settings, flags }
    }
}
impl Drop for TerminalState {
    fn drop(&mut self) {
        if let Some(settings) = &self.settings {
            unsafe {
                libc::tcsetattr(0, libc::TCSANOW, settings);
            }
        }
        for (fd, flags) in &self.flags {
            unsafe {
                libc::fcntl(*fd, libc::F_SETFL, *flags);
            }
        }
    }
}
pub struct Session {
    pub directory: PathBuf,
    pub original: PathBuf,
    pub edited: PathBuf,
    baseline: Vec<u8>,
}
#[derive(Debug, PartialEq)]
pub enum Outcome {
    Cancelled,
    Unchanged,
    Edited(String),
}
impl Session {
    pub fn create(text: &str) -> Result<Self, String> {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|_| "Clock unavailable")?
            .as_nanos();
        let directory = std::env::temp_dir().join(format!(
            "dbcli-bulk-{}-{nonce}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        fs::DirBuilder::new()
            .mode(0o700)
            .create(&directory)
            .map_err(|_| "Cannot create private bulk directory")?;
        let s = Self {
            original: directory.join("original.json"),
            edited: directory.join("edited.json"),
            directory,
            baseline: text.as_bytes().to_vec(),
        };
        for path in [&s.original, &s.edited] {
            let mut f = fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o600)
                .open(path)
                .map_err(|_| "Cannot create protected bulk file")?;
            f.write_all(&s.baseline)
                .map_err(|_| "Cannot write bulk file")?;
            f.flush().map_err(|_| "Cannot flush bulk file")?;
        }
        Ok(s)
    }
    pub fn command(&self, words: &[String]) -> Result<Command, String> {
        let (program, args) = words.split_first().ok_or("DBCLI_BULK_EDITOR is empty")?;
        let mut command = Command::new(program);
        command.args(args).args(["-n","-i","NONE","--cmd","set nomodeline noexrc noswapfile nobackup nowritebackup noundofile shada=","-d","-c","set nobackup nowritebackup noswapfile noundofile shada= | wincmd h | setlocal readonly nomodifiable noswapfile noundofile | wincmd l | setlocal noreadonly modifiable noswapfile noundofile","--"]).arg(&self.original).arg(&self.edited);
        Ok(command)
    }
    pub fn run(&self, words: &[String], cancelled: impl Fn() -> bool) -> Result<Outcome, String> {
        let _terminal = TerminalState::capture();
        let mut child = self
            .command(words)?
            .spawn()
            .map_err(|_| "Cannot run bulk editor; install nvim or set DBCLI_BULK_EDITOR")?;
        loop {
            if cancelled() {
                let _ = child.kill();
                let _ = child.wait();
                return Ok(Outcome::Cancelled);
            }
            match child.try_wait() {
                Ok(Some(status)) => {
                    if !status.success() {
                        return Ok(Outcome::Cancelled);
                    }
                    break;
                }
                Ok(None) => std::thread::sleep(Duration::from_millis(25)),
                Err(_) => {
                    let _ = child.kill();
                    let _ = child.wait();
                    return Err("Cannot wait for bulk editor".into());
                }
            }
        }
        if fs::read(&self.original).map_err(|_| "Cannot verify immutable original")?
            != self.baseline
        {
            return Err("Original snapshot file changed; bulk edit cancelled".into());
        }
        let edited = fs::read(&self.edited).map_err(|_| "Cannot read edited bulk file")?;
        if edited == self.baseline {
            return Ok(Outcome::Unchanged);
        }
        String::from_utf8(edited)
            .map(Outcome::Edited)
            .map_err(|_| "Edited JSON must be UTF-8".into())
    }
}
impl Drop for Session {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.original);
        let _ = fs::remove_file(&self.edited);
        let _ = fs::remove_dir(&self.directory);
    }
}
pub fn configured() -> String {
    std::env::var("DBCLI_BULK_EDITOR")
        .ok()
        .filter(|s| !s.trim().is_empty())
        .unwrap_or_else(|| "nvim".into())
}
pub fn words(command: &str) -> Result<Vec<String>, String> {
    let mut out = vec![];
    let (mut word, mut quote, mut escape, mut started) = (String::new(), None, false, false);
    for ch in command.chars() {
        if escape {
            word.push(ch);
            escape = false;
            started = true;
            continue;
        }
        match (quote, ch) {
            (Some('\''), '\'') | (Some('"'), '"') => quote = None,
            (Some('\''), _) => word.push(ch),
            (Some('"'), '\\') => escape = true,
            (Some(_), _) => word.push(ch),
            (None, '\\') => {
                escape = true;
                started = true
            }
            (None, '\'') | (None, '"') => {
                quote = Some(ch);
                started = true
            }
            (None, c) if c.is_whitespace() => {
                if started {
                    out.push(std::mem::take(&mut word));
                    started = false
                }
            }
            (None, c) => {
                word.push(c);
                started = true
            }
        }
    }
    if escape || quote.is_some() {
        return Err("Unclosed quote in DBCLI_BULK_EDITOR".into());
    }
    if started {
        out.push(word)
    }
    if out.is_empty() {
        return Err("DBCLI_BULK_EDITOR is empty".into());
    }
    Ok(out)
}
#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;
    #[test]
    fn private_files_and_quoted_arguments() {
        let s = Session::create("{\"secret\":1}\n").unwrap();
        let dir = s.directory.clone();
        assert_eq!(
            fs::metadata(&dir).unwrap().permissions().mode() & 0o777,
            0o700
        );
        for p in [&s.original, &s.edited] {
            assert_eq!(fs::metadata(p).unwrap().permissions().mode() & 0o777, 0o600)
        }
        let w = words("nvim --flag 'two words' \"$HOME\"").unwrap();
        assert_eq!(w, vec!["nvim", "--flag", "two words", "$HOME"]);
        let c = s.command(&w).unwrap();
        let args = c.get_args().collect::<Vec<_>>();
        assert_eq!(std::path::Path::new(args[args.len() - 1]), s.edited);
        assert!(args
            .iter()
            .any(|a| a.to_string_lossy().contains("nomodifiable")));
        drop(s);
        assert!(!dir.exists());
        assert!(words("nvim 'bad").is_err());
    }
    #[test]
    fn unchanged_and_nonzero_cancel_without_writes() {
        let s = Session::create("{}\n").unwrap();
        assert_eq!(
            s.run(&["true".into()], || false).unwrap(),
            Outcome::Unchanged
        );
        assert_eq!(
            s.run(&["false".into()], || false).unwrap(),
            Outcome::Cancelled
        );
        fs::write(&s.original, "changed").unwrap();
        assert!(s.run(&["true".into()], || false).is_err());
    }
}

/// Drop unchanged editable rows before JSON-TEXT normalization can rewrite their
/// original spelling. Still reject duplicate identities and modified snapshots.
fn plan_identity(row: &serde_json::Value, keys: &[String]) -> Result<String, String> {
    use serde_json::Value;
    fn canonical(v: &Value) -> Value {
        match v {
            Value::Object(m) => {
                let mut keys = m.keys().collect::<Vec<_>>();
                keys.sort();
                Value::Object(
                    keys.into_iter()
                        .map(|k| (k.clone(), canonical(&m[k])))
                        .collect(),
                )
            }
            Value::Array(a) => Value::Array(a.iter().map(canonical).collect()),
            _ => v.clone(),
        }
    }
    if keys.is_empty() {
        return Err("Bulk editing requires a primary key".into());
    }
    let values = keys
        .iter()
        .map(|k| {
            row.get(k)
                .filter(|v| !v.is_null())
                .map(canonical)
                .ok_or_else(|| format!("Missing non-NULL original primary key {k}"))
        })
        .collect::<Result<Vec<_>, _>>()?;
    Ok(Value::Array(values).to_string())
}

/// Keep complete immutable snapshots in the application, but only PK tokens in
/// the two editor files. The row field still contains all editable values.
pub fn compact_document(
    rows: &[serde_json::Value],
    keys: &[String],
    editable: impl Fn(&serde_json::Value) -> serde_json::Value,
) -> Result<serde_json::Value, String> {
    use serde_json::json;
    use std::collections::HashSet;
    if keys.is_empty() {
        return Err("Bulk editing requires a primary key".into());
    }
    let mut seen = HashSet::new();
    let mut updates = Vec::with_capacity(rows.len());
    for row in rows {
        let id = plan_identity(row, keys)?;
        if !seen.insert(id) {
            return Err("Duplicate source primary key".into());
        }
        updates.push(json!({"original":identity_token(row,keys)?,"row":editable(row)}));
    }
    Ok(json!({"updates":updates,"insert":[],"delete":[]}))
}

fn identity_token(row: &serde_json::Value, keys: &[String]) -> Result<serde_json::Value, String> {
    plan_identity(row, keys)?;
    Ok(serde_json::Value::Object(
        keys.iter()
            .map(|key| (key.clone(), row[key].clone()))
            .collect(),
    ))
}

/// Restore full original snapshots only for the actions actually requested.
/// The existing strict validators and transaction conflict checks then run unchanged.
pub fn expand_plan(
    mut plan: serde_json::Value,
    source: &[serde_json::Value],
    keys: &[String],
) -> Result<serde_json::Value, String> {
    use std::collections::{HashMap, HashSet};
    let mut originals = HashMap::new();
    for row in source {
        if originals.insert(plan_identity(row, keys)?, row).is_some() {
            return Err("Duplicate source primary key".into());
        }
    }
    let mut seen = HashSet::new();
    for field in ["updates", "delete"] {
        let entries = plan
            .as_object_mut()
            .ok_or("Bulk plan must be an object")?
            .get_mut(field)
            .and_then(serde_json::Value::as_array_mut)
            .ok_or_else(|| format!("{field} must be an array"))?;
        for entry in entries {
            let token = if field == "updates" {
                &entry["original"]
            } else {
                &*entry
            };
            let id = plan_identity(token, keys)?;
            if !seen.insert(id.clone()) {
                return Err("Duplicate update/delete identity".into());
            }
            let original = *originals
                .get(&id)
                .ok_or("Identity outside immutable selection")?;
            if token != &identity_token(original, keys)? {
                return Err(
                    "Original identity token must contain only the unchanged primary key".into(),
                );
            }
            if field == "updates" {
                entry["original"] = original.clone();
            } else {
                *entry = original.clone();
            }
        }
    }
    Ok(plan)
}

/// Skip unchanged editable rows before normalization; originals must stay immutable.
pub fn changed_plan(
    mut edited: serde_json::Value,
    initial: &serde_json::Value,
    keys: &[String],
) -> Result<serde_json::Value, String> {
    use serde_json::Value;
    use std::collections::{HashMap, HashSet};
    let identity = |row: &Value| plan_identity(row, keys);
    let mut source = HashMap::new();
    for entry in initial["updates"]
        .as_array()
        .ok_or("Invalid original bulk plan")?
    {
        if source
            .insert(identity(&entry["original"])?, entry)
            .is_some()
        {
            return Err("Duplicate source primary key".into());
        }
    }
    let updates = edited["updates"]
        .as_array()
        .ok_or("updates must be an array")?;
    let mut seen = HashSet::new();
    let mut changed = vec![];
    for entry in updates {
        let key = identity(&entry["original"])?;
        if !seen.insert(key.clone()) {
            return Err("Duplicate bulk identity".into());
        }
        let old = source
            .get(&key)
            .ok_or("Identity outside immutable selection")?;
        if entry["original"] != old["original"] {
            return Err("Original snapshot must remain unchanged".into());
        }
        if entry["row"] != old["row"] {
            changed.push(entry.clone())
        }
    }
    for row in edited["delete"]
        .as_array()
        .ok_or("delete must be an array")?
    {
        if !seen.insert(identity(row)?) {
            return Err("Duplicate update/delete identity".into());
        }
    }
    edited["updates"] = Value::Array(changed);
    Ok(edited)
}
pub fn summary(actions: &[serde_json::Value]) -> String {
    let count = |kind| actions.iter().filter(|a| a["kind"] == kind).count();
    format!(
        "{} updates · {} inserts · {} deletes · {} total actions",
        count("update"),
        count("insert"),
        count("delete"),
        actions.len()
    )
}

#[cfg(test)]
mod plan_tests {
    use super::*;
    use serde_json::json;
    #[test]
    fn compact_files_keep_one_editable_copy_and_expand_only_changed_actions() {
        let source = vec![
            json!({"tenant":"001","id":1,"payload":"wide synthetic text ".repeat(1000),"code":"00123"}),
            json!({"tenant":"001","id":2,"payload":{"nested":[true,null]},"code":"00234"}),
        ];
        let keys = vec!["tenant".into(), "id".into()];
        let compact = compact_document(&source, &keys, Clone::clone).unwrap();
        assert_eq!(
            compact["updates"][0]["original"],
            json!({"tenant":"001","id":1})
        );
        assert_eq!(compact["updates"][0]["row"], source[0]);
        let full = json!({"updates":source.iter().map(|row|json!({"original":row,"row":row})).collect::<Vec<_>>(),"insert":[],"delete":[]});
        let compact_bytes = serde_json::to_vec_pretty(&compact).unwrap().len();
        let full_bytes = serde_json::to_vec_pretty(&full).unwrap().len();
        assert!(
            compact_bytes * 100 < full_bytes * 60,
            "wide-row editor payload should drop by at least 40%"
        );
        let no_changes = changed_plan(compact.clone(), &compact, &keys).unwrap();
        assert!(no_changes["updates"].as_array().unwrap().is_empty());
        let mut edited = compact.clone();
        edited["updates"][0]["row"]["code"] = json!("00999");
        edited["updates"].as_array_mut().unwrap().pop(); // Omission never deletes.
        let expanded = expand_plan(
            changed_plan(edited, &compact, &keys).unwrap(),
            &source,
            &keys,
        )
        .unwrap();
        assert_eq!(expanded["updates"].as_array().unwrap().len(), 1);
        assert_eq!(expanded["updates"][0]["original"], source[0]);
        assert_eq!(expanded["updates"][0]["row"]["code"], "00999");
        assert!(expanded["delete"].as_array().unwrap().is_empty());
        let delete = json!({"updates":[],"insert":[],"delete":[{"tenant":"001","id":2}]});
        assert_eq!(
            expand_plan(delete, &source, &keys).unwrap()["delete"][0],
            source[1]
        );
    }

    #[test]
    fn compact_tokens_preserve_typed_keys_and_reject_unknown_altered_or_duplicate_identity() {
        let source = vec![
            json!({"id":{"$type":"decimal","value":"1.00"},"payload":{"$type":"json","value":null},"data":{"$bytes":"00ff"}}),
        ];
        let keys = vec!["id".into()];
        let initial = compact_document(&source, &keys, Clone::clone).unwrap();
        assert_eq!(initial["updates"][0]["original"]["id"], source[0]["id"]);
        let mut edited = initial.clone();
        edited["updates"][0]["row"]["payload"] = serde_json::Value::Null;
        let expanded = expand_plan(
            changed_plan(edited, &initial, &keys).unwrap(),
            &source,
            &keys,
        )
        .unwrap();
        assert_eq!(
            expanded["updates"][0]["original"]["payload"],
            json!({"$type":"json","value":null})
        );
        assert!(expanded["updates"][0]["row"]["payload"].is_null());
        for token in [
            json!({"id":{"$type":"decimal","value":"2.00"}}),
            json!({"id":null}),
            json!({"id":{"$type":"decimal","value":"1.00"},"extra":"not a key"}),
        ] {
            assert!(expand_plan(
                json!({"updates":[],"insert":[],"delete":[token]}),
                &source,
                &keys
            )
            .is_err());
        }
        let token = initial["updates"][0]["original"].clone();
        assert!(expand_plan(
            json!({"updates":[],"insert":[],"delete":[token.clone(),token]}),
            &source,
            &keys
        )
        .is_err());
        assert!(
            compact_document(&[source[0].clone(), source[0].clone()], &keys, Clone::clone).is_err()
        );
        assert!(compact_document(&source, &[], Clone::clone).is_err());
        let mut tampered = initial.clone();
        tampered["updates"][0]["original"]["extra"] = json!(1);
        assert!(changed_plan(tampered, &initial, &keys).is_err());
    }

    #[test]
    fn unchanged_rows_skip_normalization_and_duplicates_still_fail() {
        let old = json!({"id":1,"payload":"{\"x\":1}","code":"001"});
        let initial = json!({"updates":[{"original":old,"row":{"id":1,"payload":{"x":1},"code":"001"}}],"insert":[],"delete":[]});
        let keys = vec!["id".into()];
        assert!(
            changed_plan(initial.clone(), &initial, &keys).unwrap()["updates"]
                .as_array()
                .unwrap()
                .is_empty()
        );
        let mut changed = initial.clone();
        changed["updates"][0]["row"]["code"] = json!("002");
        assert_eq!(
            changed_plan(changed.clone(), &initial, &keys).unwrap()["updates"]
                .as_array()
                .unwrap()
                .len(),
            1
        );
        let duplicate = changed["updates"][0].clone();
        changed["updates"].as_array_mut().unwrap().push(duplicate);
        assert!(changed_plan(changed, &initial, &keys).is_err());
    }
    #[test]
    fn null_and_json_tag_changes_are_not_discarded() {
        let initial = json!({"updates":[{"original":{"id":1,"payload":{"$type":"json","value":null}},"row":{"id":1,"payload":{"$type":"json","value":null}}}],"insert":[],"delete":[]});
        let mut edited = initial.clone();
        edited["updates"][0]["row"]["payload"] = serde_json::Value::Null;
        let changed = changed_plan(edited, &initial, &["id".into()]).unwrap();
        assert_eq!(changed["updates"].as_array().unwrap().len(), 1);
    }
}
