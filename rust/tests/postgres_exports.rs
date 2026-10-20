#[path = "support/postgres_fixture.rs"]
mod fixture;
use dbcli_rust::pg_read::{BlobStyle, Reader};
use serde_json::json;
#[test]
fn disposable_legacy_postgres_export_replays_generated_and_identity_columns() {
    let Some(f) = fixture::Fixture::load() else {
        return;
    };
    let table = f.populate();
    std::env::set_var("DBCLI_POSTGRES_URL", &f.url);
    let reader = Reader::new(BlobStyle::Base64).with_read_only(true);
    let value = reader.operation(&json!({"op":"page","table":table,"limit":25,"offset":0,"sort":["id","ASC"],"json_envelopes":true})).unwrap();
    let headers: Vec<String> = serde_json::from_value(value["headers"].clone()).unwrap();
    let rows: Vec<Vec<serde_json::Value>> = serde_json::from_value(value["rows"].clone()).unwrap();
    let sql = reader.sql_export(&table, &headers, &rows, None).unwrap();
    assert!(sql.contains("OVERRIDING SYSTEM VALUE"));
    assert!(!sql.contains("\"computed\""));
    f.sql(&format!("TRUNCATE {table};{sql}"));
    assert_eq!(f.sql(&format!("SELECT string_agg(id||':'||base||':'||computed||':'||other,',' ORDER BY id) FROM {table}")),"42:3:6:81,43:4:8:82");
    assert_eq!(
        f.sql(&format!(
            "SELECT (payload IS NULL)::text FROM {table} ORDER BY id"
        )),
        "false\ntrue"
    );
    let defaults = format!("{}.defaults", f.schema);
    f.sql(&format!("CREATE TABLE {defaults}(v INTEGER GENERATED ALWAYS AS(2) STORED);INSERT INTO {defaults} DEFAULT VALUES;INSERT INTO {defaults} DEFAULT VALUES;"));
    let value = reader
        .operation(&json!({"op":"page","table":defaults,"limit":25,"offset":0}))
        .unwrap();
    let headers: Vec<String> = serde_json::from_value(value["headers"].clone()).unwrap();
    let rows: Vec<Vec<serde_json::Value>> = serde_json::from_value(value["rows"].clone()).unwrap();
    let sql = reader.sql_export(&defaults, &headers, &rows, None).unwrap();
    assert_eq!(sql.matches("DEFAULT VALUES").count(), 2);
    f.sql(&format!("TRUNCATE {defaults};{sql}"));
    assert_eq!(
        f.sql(&format!("SELECT count(*) FROM {defaults} WHERE v=2")),
        "2"
    );
}
