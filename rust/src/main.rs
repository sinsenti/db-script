use dbcli_rust::{
    remote::{RemoteAdapter, RemoteEngine},
    tui, ProtocolRequest, SQLiteAdapter,
};
use serde_json::{json, Value};
use std::{
    env,
    io::{self, BufRead, Write},
    path::PathBuf,
    process,
};

fn usage() {
    println!("dbcli-rust 0.1.0 — SQLite terminal browser\n\nUSAGE:\n  dbcli-rust [OPTIONS] [DATABASE]\n\nOPTIONS:\n  --db PATH          SQLite database path\n  --read-only        Refuse writes and hide mutation actions\n  --protocol         Persistent JSONL parity/automation runner\n  --engine ENGINE    Protocol backend: postgres or mysql (uses DBCLI_*_URL)\n  --list-tables      Print table names as JSON\n  --schema TABLE     Print schema metadata as JSON\n  --query SQL        Run bounded SELECT/WITH/EXPLAIN query\n  --table TABLE      Open this table directly in the browser\n  --limit N          Query result bound (default 50)\n  --offset N         Query result offset (default 0)\n  --version          Print version\n  -h, --help         Show this help");
}
fn main() {
    let mut args = env::args().skip(1);
    let mut database: Option<PathBuf> = None;
    let mut read_only = false;
    let mut protocol = false;
    let mut engine = None;
    let mut list = false;
    let mut schema = None;
    let mut query = None;
    let mut table = None;
    let mut limit = 50i64;
    let mut offset = 0i64;
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--version" => {
                println!("dbcli-rust 0.1.0 (SQLite {})", rusqlite::version());
                return;
            }
            "-h" | "--help" => {
                usage();
                return;
            }
            "--read-only" => read_only = true,
            "--protocol" => protocol = true,
            "--engine" => engine = args.next(),
            "--db" => database = args.next().map(PathBuf::from),
            "--list-tables" => list = true,
            "--schema" => schema = args.next(),
            "--query" => query = args.next(),
            "--table" => table = args.next(),
            "--limit" => limit = args.next().and_then(|v| v.parse().ok()).unwrap_or(limit),
            "--offset" => offset = args.next().and_then(|v| v.parse().ok()).unwrap_or(offset),
            x if x.starts_with('-') => {
                eprintln!("unknown option: {x}");
                process::exit(2)
            }
            x => database = Some(PathBuf::from(x)),
        }
    }
    if protocol {
        if let Some(engine) = engine {
            let engine = RemoteEngine::parse(&engine).unwrap_or_else(|error| {
                eprintln!("{}", error.message);
                process::exit(2)
            });
            let adapter = RemoteAdapter::new(engine, read_only);
            run_protocol(|request| adapter.protocol(request));
        } else {
            let database = database.unwrap_or_else(|| {
                eprintln!("a SQLite path is required for the SQLite protocol");
                process::exit(2)
            });
            let adapter = SQLiteAdapter::new(&database, read_only);
            run_protocol(|request| adapter.protocol(request));
        }
        return;
    }
    if engine.is_some() {
        eprintln!("--engine is supported only with --protocol");
        process::exit(2);
    }
    let database = match database {
        Some(path) => path,
        None => match tui::select_database() {
            Ok(Some(path)) => path,
            Ok(None) => {
                eprintln!("No SQLite database selected; pass DATABASE or --db PATH");
                return;
            }
            Err(error) => fail(error),
        },
    };
    if !database.exists() {
        eprintln!("database does not exist: {}", database.display());
        process::exit(2)
    }
    let adapter = SQLiteAdapter::new(&database, read_only);
    if list {
        match adapter.list_tables(true) {
            Ok(t) => println!("{}", serde_json::to_string_pretty(&t).unwrap()),
            Err(e) => fail(e),
        }
        return;
    }
    if let Some(table) = schema {
        match adapter.schema_info(&table) {
            Ok(r) => println!("{}", serde_json::to_string_pretty(&r).unwrap()),
            Err(e) => fail(e),
        }
        return;
    }
    if let Some(sql) = query {
        match adapter.execute_readonly(&sql, &[], limit, offset) {
            Ok(r) => println!("{}", serde_json::to_string_pretty(&r).unwrap()),
            Err(e) => fail(e),
        }
        return;
    }
    if let Some(table) = table {
        if let Err(e) = tui::run_table(&adapter, &table) {
            fail(e)
        }
        return;
    }
    if let Err(e) = tui::run(&adapter) {
        fail(e)
    }
}
fn run_protocol<F>(dispatch: F)
where
    F: Fn(ProtocolRequest) -> Value,
{
    let stdin = io::stdin();
    let mut stdout = io::BufWriter::new(io::stdout().lock());
    for line in stdin.lock().lines() {
        let line = match line {
            Ok(value) => value,
            Err(error) => {
                eprintln!("stdin: {error}");
                break;
            }
        };
        if line.trim().is_empty() {
            continue;
        }
        let response = match serde_json::from_str::<ProtocolRequest>(&line) {
            Ok(request) => dispatch(request),
            Err(error) => {
                json!({"ok":false,"error":{"class":"ProtocolError","message":error.to_string()},"trace":[]})
            }
        };
        if serde_json::to_writer(&mut stdout, &response).is_err()
            || writeln!(stdout).is_err()
            || stdout.flush().is_err()
        {
            process::exit(1);
        }
    }
}

fn fail(error: dbcli_rust::DbError) -> ! {
    eprintln!("{}: {}", error.class_name, error.message);
    process::exit(1)
}
