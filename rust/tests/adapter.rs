use dbcli_rust::{
    build_filter_clause_for_dialect, build_order_clause_for_dialect, build_pk_where_for_dialect,
    DbError, SQLiteAdapter,
};
use rusqlite::Connection;
use serde_json::{json, Map, Value};
use std::{
    fs,
    path::PathBuf,
    time::{SystemTime, UNIX_EPOCH},
};

struct Fixture(PathBuf);
impl Fixture {
    fn new() -> Self {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let path = std::env::temp_dir().join(format!(
            "dbcli-rust-test-{}-{nonce}.sqlite",
            std::process::id()
        ));
        let conn = Connection::open(&path).unwrap();
        conn.execute_batch("PRAGMA foreign_keys=ON;
            CREATE TABLE records(id INTEGER PRIMARY KEY, name TEXT NOT NULL, doc TEXT, payload BLOB);
            CREATE TABLE composite(tenant INTEGER NOT NULL, item INTEGER NOT NULL, value TEXT, PRIMARY KEY(tenant,item));
            CREATE TABLE no_key(name TEXT, value TEXT, record_id INTEGER REFERENCES records(id) ON UPDATE CASCADE);
            CREATE INDEX idx_no_key_record ON no_key(record_id);
            CREATE TRIGGER no_key_touch AFTER INSERT ON no_key BEGIN
                UPDATE no_key SET value=NEW.value WHERE rowid=NEW.rowid;
            END;
            INSERT INTO records VALUES(1,'Ada', '{\"role\":\"admin\"}', x'00ff');
            INSERT INTO records VALUES(2,'O''Brien', NULL, NULL);
            INSERT INTO composite VALUES(7,11,'before');
            INSERT INTO no_key VALUES('tag','value',1);").unwrap();
        drop(conn);
        Self(path)
    }
    fn adapter(&self, read_only: bool) -> SQLiteAdapter {
        SQLiteAdapter::new(&self.0, read_only)
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.0);
    }
}
fn map(entries: &[(&str, Value)]) -> Map<String, Value> {
    entries
        .iter()
        .map(|(k, v)| (k.to_string(), v.clone()))
        .collect()
}
fn class(result: Result<impl Sized + std::fmt::Debug, DbError>) -> String {
    result.unwrap_err().class_name
}

#[test]
fn filtering_sort_paging_and_trace_use_bound_values() {
    let fixture = Fixture::new();
    let db = fixture.adapter(false);
    let order = vec!["name".to_string(), "DESC".to_string()];
    let result = db
        .fetch_page("records", 1, 0, Some("O'Brien"), Some(&order))
        .unwrap();
    assert_eq!(result.headers, vec!["id", "name", "doc", "payload"]);
    assert_eq!(result.rows[0][1], "O'Brien");
    assert_eq!(db.count_rows("records", Some("O'Brien")).unwrap(), 1);
    let trace = db.take_trace();
    let query = trace
        .iter()
        .find(|q| q.sql.starts_with("SELECT * FROM"))
        .unwrap();
    assert!(query.sql.contains("LIKE ?"));
    assert_eq!(
        query.params,
        vec![
            json!("%O'Brien%"),
            json!("%O'Brien%"),
            json!("%O'Brien%"),
            json!("%O'Brien%"),
            json!(1),
            json!(0)
        ]
    );
    assert_eq!(
        class(db.fetch_page("records", 0, 0, None, None)),
        "ValueError"
    );
    assert_eq!(
        class(db.fetch_page("records", 1, -1, None, None)),
        "ValueError"
    );
    let invalid = vec!["name".to_string(), "SIDEWAYS".to_string()];
    assert_eq!(
        class(db.fetch_page("records", 1, 0, None, Some(&invalid))),
        "ValueError"
    );
}

#[test]
fn writes_bind_data_and_identify_by_original_composite_primary_key() {
    let fixture = Fixture::new();
    let db = fixture.adapter(false);
    let inserted = map(&[
        ("id", json!(3)),
        ("name", json!("Robert'); DROP TABLE records; --")),
        ("doc", json!({"array":[1,"雪"]})),
        ("payload", json!({"$blob":"AP8="})),
    ]);
    assert_eq!(db.insert_row("records", &inserted).unwrap(), 1);
    let loaded = db
        .fetch_page(
            "records",
            5,
            0,
            Some("Robert'); DROP TABLE records; --"),
            None,
        )
        .unwrap();
    assert_eq!(loaded.rows.len(), 1);
    assert_eq!(loaded.rows[0][2], "{\"array\": [1, \"雪\"]}");
    assert_eq!(loaded.rows[0][3], json!({"$blob":"AP8="}));
    let original = map(&[("id", json!(1)), ("name", json!("Ada"))]);
    let updated = map(&[("id", json!(91)), ("name", json!("Ada updated"))]);
    assert_eq!(db.update_row("records", &original, &updated).unwrap(), 1);
    assert_eq!(
        db.fetch_page("records", 2, 0, Some("id = 91"), None)
            .unwrap()
            .rows[0][1],
        "Ada updated"
    );
    let composite = map(&[("tenant", json!(7)), ("item", json!(11))]);
    let changed = map(&[
        ("tenant", json!(7)),
        ("item", json!(11)),
        ("value", json!("after")),
    ]);
    assert_eq!(db.update_row("composite", &composite, &changed).unwrap(), 1);
    let missing = map(&[("tenant", json!(7))]);
    assert_eq!(
        class(db.delete_row("composite", &missing)),
        "RowIdentityError"
    );
    assert_eq!(db.delete_row("composite", &composite).unwrap(), 1);
    assert_eq!(
        class(db.delete_row("no_key", &map(&[("name", json!("tag"))]))),
        "RowIdentityError"
    );
    assert_eq!(
        db.list_tables(false).unwrap(),
        vec!["composite", "no_key", "records"]
    );
}

#[test]
fn raw_queries_are_bounded_and_write_actions_are_rejected() {
    let fixture = Fixture::new();
    let db = fixture.adapter(false);
    let result = db
        .execute_readonly("SELECT id, name FROM records ORDER BY id", &[], 1, 1)
        .unwrap();
    assert_eq!(result.rows, vec![vec![json!(2), json!("O'Brien")]]);
    assert_eq!(
        class(db.execute_readonly("DELETE FROM records", &[], 10, 0)),
        "UnsupportedQueryError"
    );
    assert_eq!(
        class(db.execute_readonly("WITH x AS (SELECT 1) DELETE FROM records", &[], 10, 0)),
        "AdapterError"
    );
    assert_eq!(db.count_rows("records", None).unwrap(), 2);
}

#[test]
fn read_only_and_schema_discovery_are_enforced() {
    let fixture = Fixture::new();
    let db = fixture.adapter(true);
    assert_eq!(
        db.list_tables(true).unwrap(),
        vec!["composite", "no_key", "records"]
    );
    let schema = db.schema_info("composite").unwrap();
    assert_eq!(schema.rows[0][5], "YES");
    assert_eq!(schema.rows[1][5], "YES");
    let inspector = db.indexes("no_key").unwrap();
    let details = inspector
        .rows
        .iter()
        .filter_map(|row| row.first()?.as_str())
        .collect::<Vec<_>>()
        .join("\n");
    assert!(details.contains("idx_no_key_record (NON-UNIQUE) ➔ Columns: record_id"));
    assert!(details.contains("record_id ➔ records.id"));
    assert!(details.contains("no_key_touch:"));
    assert_eq!(
        class(db.insert_row("records", &map(&[("id", json!(3))]))),
        "ReadOnlyError"
    );
    assert_eq!(
        class(db.update_row(
            "records",
            &map(&[("id", json!(1))]),
            &map(&[("name", json!("blocked"))])
        )),
        "ReadOnlyError"
    );
    assert_eq!(
        class(db.delete_row("records", &map(&[("id", json!(1))]))),
        "ReadOnlyError"
    );
}

#[test]
fn remote_sql_helpers_keep_dialect_quoting_and_canonical_placeholders() {
    let columns = vec!["id".to_string(), "full_name".to_string()];
    let (filter, params) =
        build_filter_clause_for_dialect(Some("O'Brien"), &columns, "mysql").unwrap();
    assert_eq!(
        filter,
        "WHERE CAST(`id` AS CHAR) LIKE ? OR CAST(`full_name` AS CHAR) LIKE ?"
    );
    assert_eq!(params, vec![json!("%O'Brien%"), json!("%O'Brien%")]);
    assert_eq!(
        build_order_clause_for_dialect(Some(&["id".into(), "DESC".into()]), &columns, "postgres")
            .unwrap(),
        "ORDER BY \"id\" DESC"
    );
    let original = map(&[("id", json!(7))]);
    let (predicate, params) =
        build_pk_where_for_dialect(&original, &["id".into()], "postgres").unwrap();
    assert_eq!(predicate, "\"id\" = ?");
    assert_eq!(params, vec![json!(7)]);
}

#[test]
fn protocol_returns_exact_shape_and_tagged_blob_values() {
    let fixture = Fixture::new();
    let db = fixture.adapter(false);
    let req = serde_json::from_value(
        json!({"op":"page","table":"records","limit":1,"offset":0,"sort":["id","ASC"]}),
    )
    .unwrap();
    let response = db.protocol(req);
    assert_eq!(response["ok"], true);
    assert_eq!(response["result"]["rows"][0][3], json!({"$blob":"AP8="}));
    assert_eq!(response["trace"][0]["sql"], "PRAGMA foreign_keys = ON");
}
