//! Opt-in real native-worker tests. Only temporary on-disk SQLite is mutated.
use dbcli_ui_rust::{bridge::Worker, data::*};
use serde_json::json;
use std::{collections::BTreeMap, path::PathBuf, time::Duration};
#[test]
fn disposable_native_workers() {
    let Ok(configured) = std::env::var("DBCLI_UI_TEST_WORKERS") else {
        eprintln!("SKIP live native workers: set DBCLI_UI_TEST_WORKERS JSON alias->executable");
        return;
    };
    let workers: BTreeMap<String, String> = serde_json::from_str(&configured).unwrap();
    assert!(!workers.is_empty());
    for (alias, path) in workers {
        let dir = tempfile::tempdir().unwrap();
        let database = dir.path().join("fixture.sqlite");
        {
            let c = rusqlite::Connection::open(&database).unwrap();
            c.execute_batch("PRAGMA foreign_keys=ON;CREATE TABLE items(id INTEGER PRIMARY KEY,code TEXT,name TEXT,payload TEXT,data BLOB);INSERT INTO items VALUES(1,'00123','alpha','{\"nested\":[1,2]}',X'00FF');CREATE TABLE parent(a TEXT,b INTEGER,label TEXT,PRIMARY KEY(a,b));INSERT INTO parent VALUES('tenant',1,'parent');CREATE TABLE child(id INTEGER PRIMARY KEY,a TEXT,b INTEGER,FOREIGN KEY(a,b) REFERENCES parent(a,b));INSERT INTO child VALUES(1,'tenant',1);CREATE UNIQUE INDEX idx_name ON items(name);CREATE VIRTUAL TABLE task_search USING fts5(name);").unwrap();
        }
        let config = Config {
            backend: alias.clone(),
            worker: PathBuf::from(path),
            database: database.to_string_lossy().into(),
            table: Some("items".into()),
            ..Config::default()
        };
        let mut w = Worker::spawn(config.clone()).unwrap();
        let tables = w.request(json!({"op":"tables"})).unwrap();
        assert!(
            tables.as_array().unwrap().contains(&json!("items")),
            "{alias}"
        );
        let original=page(w.request(json!({"op":"page","table":"items","limit":10,"offset":0,"filter":"","order_by":["id","ASC"]})).unwrap()).unwrap();
        assert_eq!(original.rows.len(), 1);
        w.request(json!({"op":"apply_batch","table":"items","actions":[{"kind":"insert","row":{"id":2,"code":"00234","name":"beta","payload":{"nested":[3]},"data":{"$bytes":"00ff"}}}]})).unwrap();
        let inserted = page(
            w.request(json!({"op":"page","table":"items","limit":10,"offset":0,"filter":"id=2"}))
                .unwrap(),
        )
        .unwrap();
        assert_eq!(inserted.rows[0][1], "00234");
        assert!(w.request(json!({"op":"apply_batch","table":"items","actions":[{"kind":"insert","row":{"id":3,"name":"gamma"}},{"kind":"insert","row":{"id":1,"name":"duplicate"}}]})).is_err());
        assert_eq!(
            w.request(json!({"op":"count","table":"items"})).unwrap(),
            json!(2)
        );
        let original = json!({"a":"tenant","b":1,"label":"parent"});
        w.request(json!({"op":"apply_batch","table":"parent","actions":[{"kind":"update","original":original,"row":{"a":"tenant","b":1,"label":"changed"}}]})).unwrap();
        let db = Async::start(config.clone());
        db.send
            .send(Job::Load(Spec {
                table: "child".into(),
                limit: 24,
                ..Spec::default()
            }))
            .unwrap();
        let r = db
            .receive
            .recv_timeout(Duration::from_secs(15))
            .unwrap()
            .reply;
        if let Reply::Loaded(b) = r {
            assert_eq!(b.meta.keys, vec!["id"]);
            assert_eq!(b.meta.relations.len(), 1);
            assert_eq!(
                b.meta.relations[0].pairs,
                vec![("a".into(), "a".into()), ("b".into(), "b".into())]
            );
        } else {
            panic!("{alias}: metadata load failed {r:?}")
        }
        db.send
            .send(Job::Load(Spec {
                raw: Some("SELECT 1 AS n".into()),
                limit: 10,
                ..Spec::default()
            }))
            .unwrap();
        match db
            .receive
            .recv_timeout(Duration::from_secs(10))
            .unwrap()
            .reply
        {
            Reply::Loaded(b) => assert_eq!(b.page.rows, vec![vec![json!(1)]]),
            other => panic!("{alias}: SQL without selected table failed {other:?}"),
        };
        db.terminate();
        let mut ro = config;
        ro.read_only = true;
        let mut readonly = Worker::spawn(ro).unwrap();
        assert!(readonly
            .request(json!({"op":"insert","table":"items","row":{"id":999}}))
            .unwrap_err()
            .contains("ReadOnlyError"));
        let c = rusqlite::Connection::open(&database).unwrap();
        assert_eq!(
            c.query_row("PRAGMA integrity_check", [], |r| r.get::<_, String>(0))
                .unwrap(),
            "ok"
        );
    }
}

#[test]
fn disposable_postgres_worker_reads() {
    let Ok(fixture) = std::env::var("DBCLI_UI_TEST_POSTGRES_FIXTURE") else {
        eprintln!("SKIP PostgreSQL reads: set private disposable fixture descriptor path");
        return;
    };
    use std::os::unix::fs::PermissionsExt;
    assert_eq!(
        std::fs::metadata(&fixture).unwrap().permissions().mode() & 0o077,
        0
    );
    let data: serde_json::Value = serde_json::from_slice(&std::fs::read(fixture).unwrap()).unwrap();
    assert!(matches!(
        (data["fixture_marker"].as_str(), data["database"].as_str()),
        (
            Some("dbcli-postgres-read-disposable-v1"),
            Some("dbcli_postgres_read_fixture")
        ) | (
            Some("dbcli-postgres-write-disposable-v1"),
            Some("dbcli_postgres_write_fixture")
        )
    ));
    let output = std::process::Command::new("docker")
        .args([
            "inspect",
            "--format",
            "{{json .Id}} {{json .Config.Labels}}",
            data["container"].as_str().unwrap(),
        ])
        .output()
        .unwrap();
    assert!(output.status.success());
    let inspect = String::from_utf8(output.stdout).unwrap();
    let (id, labels) = inspect.split_once(' ').unwrap();
    assert_eq!(
        serde_json::from_str::<serde_json::Value>(id).unwrap(),
        data["container_id"]
    );
    let labels: serde_json::Value = serde_json::from_str(labels).unwrap();
    assert!(matches!(
        labels["dbcli.fixture"].as_str(),
        Some("postgres-write") | Some("postgres-read")
    ));
    let url = data["url"].as_str().unwrap();
    assert!(!url.contains(":5432/"));
    std::env::set_var("DBCLI_POSTGRES_URL", url);
    std::env::set_var("DBCLI_BENCH_POSTGRES_URL", url);
    std::env::set_var("DBCLI_BENCH_DATABASE_URL", url);
    let configured = std::env::var("DBCLI_UI_TEST_WORKERS").expect("Set test worker paths");
    let workers: BTreeMap<String, String> = serde_json::from_str(&configured).unwrap();
    for (alias, path) in workers {
        let config = Config {
            engine: "postgres".into(),
            read_only: true,
            backend: alias.clone(),
            worker: PathBuf::from(path),
            table: Some("public.items".into()),
            ..Config::default()
        };
        let mut worker = Worker::spawn(config.clone()).unwrap();
        let settings=page(worker.request(json!({"op":"raw","sql":"SELECT current_setting('transaction_read_only'),current_setting('default_transaction_read_only')","limit":1,"offset":0})).unwrap()).unwrap();
        assert_eq!(settings.rows[0], vec![json!("on"), json!("on")]);
        assert!(worker
            .request(json!({"op":"insert","table":"public.items","row":{"id":999}}))
            .is_err());
        let db = Async::start(config);
        db.send
            .send(Job::Load(Spec {
                table: "public.items".into(),
                limit: 8,
                sort: Some(("id".into(), "ASC".into())),
                ..Spec::default()
            }))
            .unwrap();
        let reply = db
            .receive
            .recv_timeout(Duration::from_secs(20))
            .unwrap()
            .reply;
        if let Reply::Loaded(b) = reply {
            assert_eq!(b.page.rows.len(), 8, "{alias}");
            assert_eq!(b.meta.keys, vec!["id"]);
            assert_eq!(b.page.sql_nulls.len(), 8);
            let temp = tempfile::tempdir().unwrap();
            export(
                &temp.path().join("pg.sql"),
                "sql",
                "postgres",
                "public.items",
                &b.page,
                &b.meta,
            )
            .unwrap();
        } else {
            panic!("{alias}: bounded PG load failed {reply:?}")
        }
        db.send
            .send(Job::Load(Spec {
                table: "dbcli_alt.children".into(),
                limit: 8,
                ..Spec::default()
            }))
            .unwrap();
        let reply = db
            .receive
            .recv_timeout(Duration::from_secs(20))
            .unwrap()
            .reply;
        if let Reply::Loaded(b) = reply {
            assert_eq!(b.meta.relations.len(), 1);
            assert_eq!(b.meta.relations[0].pairs.len(), 2);
        } else {
            panic!("{alias}: PG composite FK failed {reply:?}")
        }
        db.terminate();
    }
}

#[test]
fn blocked_worker_and_startup_are_killed_and_reaped() {
    use std::os::unix::fs::PermissionsExt;
    let dir = tempfile::tempdir().unwrap();
    let script = dir.path().join("blocked-worker");
    std::fs::write(&script, "#!/bin/sh\nwhile IFS= read -r line; do :; done\n").unwrap();
    std::fs::set_permissions(&script, std::fs::Permissions::from_mode(0o700)).unwrap();
    for alias in ["codex-rust", "pi-rust"] {
        let config = Config {
            worker: script.clone(),
            backend: alias.into(),
            ..Config::default()
        };
        let db = Async::start(config);
        db.send.send(Job::Init).unwrap();
        let until = std::time::Instant::now() + Duration::from_secs(3);
        while db.control.child.lock().unwrap().is_none() {
            assert!(std::time::Instant::now() < until);
            std::thread::yield_now();
        }
        let start = std::time::Instant::now();
        db.terminate();
        assert!(start.elapsed() < Duration::from_secs(2));
        let child = db.control.child.lock().unwrap().clone().unwrap();
        assert!(child.lock().unwrap().try_wait().unwrap().is_some());
    }
    // Exit before the background thread has necessarily created the child.
    for _ in 0..10 {
        let db = Async::start(Config {
            worker: script.clone(),
            backend: "pi-rust".into(),
            ..Config::default()
        });
        let control = db.control.clone();
        drop(db);
        std::thread::sleep(Duration::from_millis(10));
        if let Some(child) = control.child.lock().unwrap().clone() {
            assert!(child.lock().unwrap().try_wait().unwrap().is_some());
        };
    }
}
