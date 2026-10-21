#[path = "../../../rust/tests/support/postgres_fixture.rs"]
mod fixture;
use dbcli_ui_rust::data::*;
use std::{path::PathBuf, time::Duration};
#[test]
fn disposable_ratatui_postgres_export_replays_generated_and_identity_columns() {
    let Some(f) = fixture::Fixture::load() else {
        return;
    };
    let table = f.populate();
    std::env::set_var("DBCLI_POSTGRES_URL", &f.url);
    let worker = std::env::var("DBCLI_EXPORT_TEST_WORKER")
        .expect("Set DBCLI_EXPORT_TEST_WORKER to main's backend executable");
    let db = Async::start(Config {
        engine: "postgres".into(),
        backend: "original-rust".into(),
        worker: PathBuf::from(worker),
        read_only: true,
        ..Config::default()
    });
    db.send
        .send(Job::Load(Spec {
            table: table.clone(),
            limit: 25,
            sort: Some(("id".into(), "ASC".into())),
            ..Spec::default()
        }))
        .unwrap();
    let Reply::Loaded(bundle) = db
        .receive
        .recv_timeout(Duration::from_secs(15))
        .unwrap()
        .reply
    else {
        panic!("Failed loading disposable export fixture")
    };
    let folder = tempfile::tempdir().unwrap();
    let path = folder.path().join("rows.sql");
    export(&path, "sql", "postgres", &table, &bundle.page, &bundle.meta).unwrap();
    let sql = std::fs::read_to_string(path).unwrap();
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
    db.send
        .send(Job::Load(Spec {
            table: defaults.clone(),
            limit: 25,
            ..Spec::default()
        }))
        .unwrap();
    let Reply::Loaded(bundle) = db
        .receive
        .recv_timeout(Duration::from_secs(15))
        .unwrap()
        .reply
    else {
        panic!("Failed loading defaults fixture")
    };
    let path = folder.path().join("defaults.sql");
    export(
        &path,
        "sql",
        "postgres",
        &defaults,
        &bundle.page,
        &bundle.meta,
    )
    .unwrap();
    let sql = std::fs::read_to_string(path).unwrap();
    assert_eq!(sql.matches("DEFAULT VALUES").count(), 2);
    f.sql(&format!("TRUNCATE {defaults};{sql}"));
    assert_eq!(
        f.sql(&format!("SELECT count(*) FROM {defaults} WHERE v=2")),
        "2"
    );
    db.terminate();
}
