//! Atomic SQLite writes and explicit bulk plans. Values are always bound.
use rusqlite::{params_from_iter, types::Value as SqlValue, Connection, TransactionBehavior};
use serde_json::{json, Value};
use std::collections::HashSet;

fn quote(s: &str) -> Result<String, String> {
    if s.is_empty() || s.contains('\0') {
        return Err("empty or NUL identifier".into());
    }
    Ok(format!("\"{}\"", s.replace('"', "\"\"")))
}
pub fn primary_keys(c: &Connection, table: &str) -> Result<Vec<String>, String> {
    let mut st = c
        .prepare(&format!("PRAGMA table_info({})", quote(table)?))
        .map_err(|e| e.to_string())?;
    let mut keys = st
        .query_map([], |r| Ok((r.get::<_, i64>(5)?, r.get::<_, String>(1)?)))
        .map_err(|e| e.to_string())?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| e.to_string())?;
    keys.retain(|(n, _)| *n > 0);
    keys.sort_by_key(|(n, _)| *n);
    Ok(keys.into_iter().map(|(_, s)| s).collect())
}
pub fn identity(row: &Value, keys: &[String]) -> Result<String, String> {
    if keys.is_empty() {
        return Err("table has no primary key; refusing ambiguous mutation".into());
    }
    let vs = keys
        .iter()
        .map(|k| {
            row.get(k)
                .filter(|v| !v.is_null())
                .cloned()
                .ok_or_else(|| format!("missing or NULL original primary key: {k}"))
        })
        .collect::<Result<Vec<_>, _>>()?;
    serde_json::to_string(&vs).map_err(|e| e.to_string())
}
/// Preserve the storage type of JSON TEXT, including objects resembling wire BLOB tags.
pub fn normalize_editor_row(original: &Value, edited: &Value) -> Result<Value, String> {
    fn json_text(v: &Value) -> String {
        match v {
            Value::Array(a) => format!(
                "[{}]",
                a.iter().map(json_text).collect::<Vec<_>>().join(", ")
            ),
            Value::Object(m) => format!(
                "{{{}}}",
                m.iter()
                    .map(|(k, v)| format!(
                        "{}: {}",
                        serde_json::to_string(k).unwrap(),
                        json_text(v)
                    ))
                    .collect::<Vec<_>>()
                    .join(", ")
            ),
            _ => v.to_string(),
        }
    }
    let mut row = edited
        .as_object()
        .ok_or("editor row must be an object")?
        .clone();
    if let Some(original) = original.as_object() {
        for (key, old) in original {
            if let Some(text) = old.as_str() {
                if let Ok(decoded) = serde_json::from_str::<Value>(text) {
                    if decoded.is_object() || decoded.is_array() {
                        if let Some(new) = row.get_mut(key) {
                            if new.is_object() || new.is_array() {
                                *new = if *new == decoded {
                                    old.clone()
                                } else {
                                    Value::String(json_text(new))
                                };
                            }
                        }
                    }
                }
            }
        }
    }
    Ok(Value::Object(row))
}
pub fn bulk_template(rows: &[Value]) -> Value {
    json!({"updates":rows.iter().map(|r|json!({"original":r,"row":r})).collect::<Vec<_>>(),"delete":[],"insert":[]})
}
pub fn bulk_plan(source: &[Value], edited: &Value, keys: &[String]) -> Result<Vec<Value>, String> {
    let object = edited
        .as_object()
        .ok_or("bulk editor needs an object with updates and delete arrays")?;
    if object
        .keys()
        .any(|k| k != "updates" && k != "delete" && k != "insert")
    {
        return Err("unknown bulk field".into());
    }
    let updates = edited
        .get("updates")
        .and_then(Value::as_array)
        .ok_or("updates must be an array")?;
    let deletions = edited
        .get("delete")
        .and_then(Value::as_array)
        .ok_or("delete must be an array")?;
    let mut originals = std::collections::HashMap::new();
    for row in source {
        let id = identity(row, keys)?;
        if originals.insert(id, row).is_some() {
            return Err("duplicate source identity".into());
        }
    }
    let mut seen = HashSet::new();
    let mut actions = vec![];
    for (kind, entries) in [("update", updates), ("delete", deletions)] {
        for entry in entries {
            let old = if kind == "update" {
                entry.get("original").ok_or("update needs original")?
            } else {
                entry
            };
            let id = identity(old, keys)?;
            if !seen.insert(id.clone()) {
                return Err("duplicate identity in bulk plan".into());
            }
            let source = originals
                .get(&id)
                .ok_or("identity is outside the selected source")?;
            if *source != old {
                return Err("original snapshot must remain unchanged".into());
            }
            if kind == "delete" {
                actions.push(json!({"kind":"delete","original":old}));
            } else {
                let row = normalize_editor_row(
                    old,
                    entry.get("row").ok_or("update row must be an object")?,
                )?;
                if identity(&row, keys)? != id {
                    return Err("bulk key changes require single-row edit".into());
                }
                if &row != old {
                    actions.push(json!({"kind":"update","original":old,"row":row}));
                }
            }
        }
    }
    if let Some(inserts) = edited.get("insert") {
        for row in inserts.as_array().ok_or("insert must be an array")? {
            if !row.is_object() {
                return Err("insert rows must be objects".into());
            }
            actions.push(json!({"kind":"insert","row":row}));
        }
    }
    Ok(actions)
}
/// One IMMEDIATE transaction; any invalid identity, stale row, multi-row change,
/// trigger/constraint failure or bad bound value rolls back the complete batch.
pub fn apply(
    c: &mut Connection,
    table: &str,
    actions: &[Value],
    bind: impl Fn(&Value) -> Result<SqlValue, String>,
) -> Result<usize, String> {
    apply_checked(c, table, actions, bind, true)
}
pub fn apply_single(
    c: &mut Connection,
    table: &str,
    action: Value,
    bind: impl Fn(&Value) -> Result<SqlValue, String>,
) -> Result<usize, String> {
    apply_checked(c, table, &[action], bind, false)
}
fn sql_equal(a: &SqlValue, b: &SqlValue) -> bool {
    match (a, b) {
        (SqlValue::Integer(i), SqlValue::Real(f)) | (SqlValue::Real(f), SqlValue::Integer(i)) => {
            f.is_finite() && f.fract() == 0.0 && *f as i128 == *i as i128
        }
        _ => a == b,
    }
}
fn apply_checked(
    c: &mut Connection,
    table: &str,
    actions: &[Value],
    bind: impl Fn(&Value) -> Result<SqlValue, String>,
    strict: bool,
) -> Result<usize, String> {
    let table = quote(table)?;
    let tx = c
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(|e| e.to_string())?;
    let (columns, keys) = {
        let mut st = tx
            .prepare(&format!("PRAGMA table_info({table})"))
            .map_err(|e| e.to_string())?;
        let cs = st
            .query_map([], |r| Ok((r.get::<_, String>(1)?, r.get::<_, i64>(5)?)))
            .map_err(|e| e.to_string())?
            .collect::<Result<Vec<_>, _>>()
            .map_err(|e| e.to_string())?;
        let mut ks = cs
            .iter()
            .filter(|(_, n)| *n > 0)
            .map(|(s, n)| (*n, s.clone()))
            .collect::<Vec<_>>();
        ks.sort();
        (
            cs.into_iter().map(|(s, _)| s).collect::<Vec<_>>(),
            ks.into_iter().map(|(_, s)| s).collect::<Vec<_>>(),
        )
    };
    if columns.is_empty() {
        return Err("unknown table or unsupported writable schema".into());
    }
    let mut seen = HashSet::new();
    let mut total = 0;
    for action in actions {
        let kind = action
            .get("kind")
            .and_then(Value::as_str)
            .ok_or("action needs kind")?;
        let row = action.get("row").and_then(Value::as_object);
        if let Some(row) = row {
            if row.keys().any(|k| !columns.contains(k)) {
                return Err("unknown write column".into());
            }
        }
        let mut params = vec![];
        let mut predicate = String::new();
        if kind != "insert" {
            let original = action
                .get("original")
                .filter(|v| v.is_object())
                .ok_or("action needs original object")?;
            identity(original, &keys)?;
            // Include original supplied fields to detect concurrent edits. JSON/BLOB
            // values are compared as bound values, never interpolated into SQL.
            let fields = original.as_object().unwrap();
            if fields.keys().any(|k| !columns.contains(k)) {
                return Err("unknown original column".into());
            }
            let mut parts = vec![];
            for (key, value) in fields {
                if !strict && !keys.contains(key) {
                    continue;
                }
                parts.push(format!("{} IS ?", quote(key)?));
                params.push(bind(value)?);
            }
            predicate = parts.join(" AND ");
            let count: i64 = tx
                .query_row(
                    &format!("SELECT COUNT(*) FROM {table} WHERE {predicate}"),
                    params_from_iter(&params),
                    |r| r.get(0),
                )
                .map_err(|e| e.to_string())?;
            if count == 0 && !strict {
                continue;
            }
            if count != 1 {
                return Err(
                    "original row is missing, ambiguous, or changed; refresh before writing".into(),
                );
            }
            let key_sql = keys
                .iter()
                .map(|k| quote(k))
                .collect::<Result<Vec<_>, _>>()?
                .join(",");
            let physical = tx
                .query_row(
                    &format!("SELECT {key_sql} FROM {table} WHERE {predicate}"),
                    params_from_iter(&params),
                    |r| {
                        (0..keys.len())
                            .map(|i| r.get::<_, SqlValue>(i))
                            .collect::<Result<Vec<_>, _>>()
                    },
                )
                .map_err(|e| e.to_string())?;
            if !seen.insert(format!("{physical:?}")) {
                return Err("duplicate mutation identity".into());
            }
        }
        let sql = match kind {
            "insert" => {
                let row = row.ok_or("insert needs row object")?;
                params = row.values().map(&bind).collect::<Result<Vec<_>, _>>()?;
                if row.is_empty() {
                    format!("INSERT INTO {table} DEFAULT VALUES")
                } else {
                    format!(
                        "INSERT INTO {table} ({}) VALUES ({})",
                        row.keys()
                            .map(|k| quote(k))
                            .collect::<Result<Vec<_>, _>>()?
                            .join(","),
                        vec!["?"; row.len()].join(",")
                    )
                }
            }
            "delete" => format!("DELETE FROM {table} WHERE {predicate}"),
            "update" => {
                let row = row.ok_or("update needs row object")?;
                let original = action["original"].as_object().unwrap();
                let mut sets = vec![];
                let mut changed = vec![];
                for (key, value) in row {
                    let bound = bind(value)?;
                    if !sql_equal(&bind(original.get(key).unwrap_or(&Value::Null))?, &bound) {
                        sets.push(format!("{} = ?", quote(key)?));
                        changed.push(bound);
                    }
                }
                if sets.is_empty() {
                    continue;
                }
                changed.extend(params);
                params = changed;
                format!("UPDATE {table} SET {} WHERE {predicate}", sets.join(","))
            }
            _ => return Err("unknown mutation action".into()),
        };
        let n = tx
            .execute(&sql, params_from_iter(&params))
            .map_err(|e| e.to_string())?;
        if n != 1 {
            return Err("mutation did not affect exactly one row; entire batch rolled back".into());
        }
        total += n;
    }
    tx.commit().map_err(|e| e.to_string())?;
    Ok(total)
}

#[cfg(test)]
mod tests {
    use super::*;
    static NEXT: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
    struct Fixture(std::path::PathBuf);
    impl Fixture {
        fn new() -> Self {
            Self(std::env::temp_dir().join(format!(
                "dbcli-atomic-test-{}-{}.sqlite",
                std::process::id(),
                NEXT.fetch_add(1, std::sync::atomic::Ordering::Relaxed)
            )))
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = std::fs::remove_file(&self.0);
        }
    }
    fn bind(v: &Value) -> Result<SqlValue, String> {
        Ok(match v {
            Value::Null => SqlValue::Null,
            Value::Number(n) => SqlValue::Integer(n.as_i64().ok_or("integer range")?),
            Value::String(s) => SqlValue::Text(s.clone()),
            _ => SqlValue::Text(v.to_string()),
        })
    }
    #[test]
    fn atomic_constraint_failure_restores_all_rows_and_fts() {
        let file = Fixture::new();
        let mut c = Connection::open(&file.0).unwrap();
        c.execute_batch("CREATE TABLE t(a TEXT,b INTEGER,label TEXT UNIQUE,PRIMARY KEY(a,b)); CREATE VIRTUAL TABLE search USING fts5(label); CREATE TRIGGER ins AFTER INSERT ON t BEGIN INSERT INTO search(rowid,label) VALUES(new.rowid,new.label); END; CREATE TRIGGER upd AFTER UPDATE ON t BEGIN UPDATE search SET label=new.label WHERE rowid=old.rowid; END; INSERT INTO t VALUES('a',1,'first'),('a',2,'second');").unwrap();
        assert!(apply(&mut c,"t",&[json!({"kind":"update","original":{"a":"a","b":1,"label":"first"},"row":{"label":"changed"}}),json!({"kind":"insert","row":{"a":"a","b":3,"label":"second"}})],bind).is_err());
        assert_eq!(
            c.query_row("SELECT label FROM t WHERE b=1", [], |r| r
                .get::<_, String>(0))
                .unwrap(),
            "first"
        );
        assert_eq!(
            c.query_row(
                "SELECT COUNT(*) FROM search WHERE search MATCH 'changed'",
                [],
                |r| r.get::<_, i64>(0)
            )
            .unwrap(),
            0
        );
    }
    #[test]
    fn compact_editor_tokens_expand_to_full_conflict_snapshots_before_atomic_apply() {
        let file = Fixture::new();
        let mut c = Connection::open(&file.0).unwrap();
        c.execute_batch("CREATE TABLE t(a TEXT,b INTEGER,v TEXT,PRIMARY KEY(a,b));INSERT INTO t VALUES('001',1,'first'),('001',2,'second');").unwrap();
        let source = vec![
            json!({"a":"001","b":1,"v":"first"}),
            json!({"a":"001","b":2,"v":"second"}),
        ];
        let keys = vec!["a".into(), "b".into()];
        let template = crate::bulk_editor::compact_document(&source, &keys, Clone::clone).unwrap();
        let edited = json!({"updates":[{"original":{"a":"001","b":1},"row":{"a":"001","b":1,"v":"edited"}}],"delete":[{"a":"001","b":2}],"insert":[{"a":"001","b":3,"v":"third"}]});
        let changed = crate::bulk_editor::changed_plan(edited, &template, &keys).unwrap();
        let expanded = crate::bulk_editor::expand_plan(changed, &source, &keys).unwrap();
        let actions = bulk_plan(&source, &expanded, &keys).unwrap();
        assert_eq!(actions.len(), 3);
        assert_eq!(actions[0]["original"], source[0]);
        // External edits are detected from the full snapshot, not only PK tokens.
        c.execute("UPDATE t SET v='external' WHERE b=1", [])
            .unwrap();
        assert!(apply(&mut c, "t", &actions, bind).is_err());
        assert_eq!(
            c.query_row("SELECT count(*) FROM t", [], |r| r.get::<_, i64>(0))
                .unwrap(),
            2
        );
        assert_eq!(
            c.query_row("SELECT v FROM t WHERE b=1", [], |r| r.get::<_, String>(0))
                .unwrap(),
            "external"
        );
        c.execute("UPDATE t SET v='first' WHERE b=1", []).unwrap();
        assert_eq!(apply(&mut c, "t", &actions, bind).unwrap(), 3);
        assert_eq!(
            c.query_row("SELECT v FROM t WHERE b=1", [], |r| r.get::<_, String>(0))
                .unwrap(),
            "edited"
        );
        assert_eq!(
            c.query_row("SELECT count(*) FROM t WHERE b=2", [], |r| r
                .get::<_, i64>(0))
                .unwrap(),
            0
        );
    }

    #[test]
    fn original_composite_key_and_concurrency_are_checked() {
        let file = Fixture::new();
        let mut c = Connection::open(&file.0).unwrap();
        c.execute_batch("CREATE TABLE t(a TEXT,b INT,v TEXT,PRIMARY KEY(a,b)); INSERT INTO t VALUES('01',1,'old');").unwrap();
        assert!(apply(
            &mut c,
            "t",
            &[json!({"kind":"delete","original":{"a":"01"}})],
            bind
        )
        .is_err());
        assert!(apply(
            &mut c,
            "t",
            &[json!({"kind":"update","original":{"a":"01","b":1,"v":"stale"},"row":{"v":"wrong"}})],
            bind
        )
        .is_err());
        assert_eq!(apply(&mut c,"t",&[json!({"kind":"update","original":{"a":"01","b":1,"v":"old"},"row":{"a":"02","v":"new"}})],bind).unwrap(),1);
        assert_eq!(
            c.query_row("SELECT a FROM t", [], |r| r.get::<_, String>(0))
                .unwrap(),
            "02"
        );
    }
    #[test]
    fn duplicate_batch_identity_rolls_back() {
        let file = Fixture::new();
        let mut c = Connection::open(&file.0).unwrap();
        c.execute_batch(
            "CREATE TABLE t(id INTEGER PRIMARY KEY,v TEXT); INSERT INTO t VALUES(1,'old');",
        )
        .unwrap();
        let action = json!({"kind":"update","original":{"id":1},"row":{"v":"new"}});
        assert!(apply(&mut c, "t", &[action.clone(), action], bind).is_err());
        assert_eq!(
            c.query_row("SELECT v FROM t", [], |r| r.get::<_, String>(0))
                .unwrap(),
            "old"
        );
    }
    #[test]
    fn bulk_missing_entries_never_delete_and_inserts_are_explicit() {
        let rows = vec![json!({"id":1,"v":"a"}), json!({"id":2,"v":"b"})];
        let keys = vec!["id".into()];
        let empty = json!({"updates":[],"delete":[],"insert":[]});
        assert!(bulk_plan(&rows, &empty, &keys).unwrap().is_empty());
        let mut plan = bulk_template(&rows);
        plan["updates"] = json!([]);
        plan["delete"] = json!([rows[1]]);
        plan["insert"] = json!([{"id":3,"v":"c"}]);
        let actions = bulk_plan(&rows, &plan, &keys).unwrap();
        assert_eq!(actions.len(), 2);
        assert_eq!(actions[0]["kind"], "delete");
        assert_eq!(actions[1]["kind"], "insert");
    }
    #[test]
    fn bulk_rejects_duplicate_and_altered_snapshots() {
        let rows = vec![json!({"id":1,"v":"a"})];
        let keys = vec!["id".into()];
        let mut plan = bulk_template(&rows);
        let entry = plan["updates"][0].clone();
        plan["updates"].as_array_mut().unwrap().push(entry);
        assert!(bulk_plan(&rows, &plan, &keys).is_err());
        plan = bulk_template(&rows);
        plan["updates"][0]["original"]["v"] = json!("spoof");
        assert!(bulk_plan(&rows, &plan, &keys).is_err());
    }
}

#[cfg(test)]
mod json_storage_tests {
    use super::*;
    #[test]
    fn blob_looking_json_text_is_never_rebound_as_blob() {
        for tag in [
            json!({"$bytes":"00ff"}),
            json!({"$blob":"AP8="}),
            json!({"$type":"blob","base64":"AP8="}),
        ] {
            let text = tag.to_string();
            let old = json!({"payload":text,"binary":tag});
            let edited = json!({"payload":tag,"binary":tag});
            let normalized = normalize_editor_row(&old, &edited).unwrap();
            assert!(normalized["payload"].is_string());
            assert_eq!(normalized["payload"], old["payload"]);
            assert_eq!(normalized["binary"], tag);
        }
    }
}
