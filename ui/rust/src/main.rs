use dbcli_ui_rust::{
    app::{App, Edit, Modal},
    data::{Async, Config, Job},
};
use ratatui::crossterm::{
    event::{self, Event, KeyEventKind},
    execute,
    terminal::{disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen},
};
use std::{
    io::{self, Write},
    path::PathBuf,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    time::Duration,
};
fn config() -> Result<Config, String> {
    let mut c = Config::default();
    c.backend = "original-rust".into();
    let mut args = std::env::args().skip(1);
    while let Some(a) = args.next() {
        match a.as_str() {
            "--help" | "-h" => {
                println!("dbcli-ui-rust — Ratatui database browser\n\n--engine sqlite|postgres --database PATH --table NAME\n--backend original-rust|codex-rust|pi-rust|integrated-rust --worker PATH\n--read-only (disable all mutations)\n\nTab sidebar/grid/views · / filter · : SQL · Ctrl+R run/preview · Ctrl+G editor\nEsc cancels a pending read; newer reads supersede old results\na/e/E SQLite/PostgreSQL JSON edits · dd atomic deletion · x/X exports · ? help");
                std::process::exit(0)
            }
            "--version" => {
                println!("dbcli-ui-rust 0.1.0 (Ratatui 0.30.2)");
                std::process::exit(0)
            }
            "--engine" => c.engine = args.next().ok_or("Missing engine")?,
            "--database" => c.database = args.next().ok_or("Missing database")?,
            "--table" => c.table = args.next(),
            "--backend" => c.backend = args.next().ok_or("Missing backend")?,
            "--worker" => c.worker = PathBuf::from(args.next().ok_or("Missing worker")?),
            "--read-only" => c.read_only = true,
            _ => return Err("Unknown option; use --help".into()),
        }
    }
    if !["sqlite", "postgres"].contains(&c.engine.as_str()) {
        return Err("Engine must be sqlite or postgres".into());
    }
    if c.engine == "postgres" {
        if !c.database.is_empty() {
            return Err(
                "PostgreSQL credentials belong in DBCLI_POSTGRES_URL, not --database".into(),
            );
        }
    }
    if c.engine == "sqlite" && c.database.is_empty() {
        return Err("SQLite requires --database PATH".into());
    }
    if c.worker.as_os_str().is_empty() {
        return Err("Supply a native --worker executable path".into());
    }
    dbcli_ui_rust::bridge::family(&c.backend)?;
    Ok(c)
}
struct Restore;
impl Drop for Restore {
    fn drop(&mut self) {
        let _ = disable_raw_mode();
        let _ = execute!(
            io::stdout(),
            LeaveAlternateScreen,
            ratatui::crossterm::cursor::Show
        );
    }
}
fn editor(text: &str, sql: bool, stopped: &AtomicBool) -> Result<String, String> {
    use std::os::unix::fs::PermissionsExt;
    let mut file = tempfile::Builder::new()
        .prefix("dbcli-ui-")
        .suffix(if sql { ".sql" } else { ".json" })
        .tempfile()
        .map_err(|_| "Could not create editor file")?;
    file.as_file()
        .set_permissions(std::fs::Permissions::from_mode(0o600))
        .map_err(|_| "Could not protect editor file")?;
    file.write_all(text.as_bytes())
        .map_err(|_| "Editor file write failed")?;
    file.flush().map_err(|_| "Editor flush failed")?;
    let configured = std::env::var("EDITOR")
        .ok()
        .filter(|s| !s.trim().is_empty());
    let candidates = configured
        .map(|s| vec![s])
        .unwrap_or_else(|| vec!["nvim".into(), "vim".into(), "nano".into()]);
    for command in candidates {
        let args = shell_words::split(&command).map_err(|_| "Invalid EDITOR command")?;
        if args.is_empty() {
            continue;
        }
        let mut command = std::process::Command::new(&args[0]);
        command.args(&args[1..]).arg(file.path());
        let mut child = match command.spawn() {
            Ok(child) => child,
            Err(e) if e.kind() == io::ErrorKind::NotFound => continue,
            Err(_) => return Err("Could not run editor".into()),
        };
        loop {
            if stopped.load(Ordering::Relaxed) {
                let _ = child.kill();
                let _ = child.wait();
                return Err("Editor cancelled during termination".into());
            }
            match child.try_wait() {
                Ok(Some(status)) if status.success() => {
                    return std::fs::read_to_string(file.path())
                        .map_err(|_| "Could not read edited UTF-8 file".into())
                }
                Ok(Some(_)) => return Err("Editor failed; changes cancelled".into()),
                Ok(None) => std::thread::sleep(Duration::from_millis(25)),
                Err(_) => {
                    let _ = child.kill();
                    let _ = child.wait();
                    return Err("Could not wait for editor".into());
                }
            }
        }
    }
    Err("Set EDITOR or install nvim/vim/nano".into())
}
fn run(c: Config) -> Result<(), Box<dyn std::error::Error>> {
    let stopped = Arc::new(AtomicBool::new(false));
    for signal in [
        signal_hook::consts::SIGTERM,
        signal_hook::consts::SIGHUP,
        signal_hook::consts::SIGINT,
    ] {
        signal_hook::flag::register(signal, stopped.clone())?;
    }
    let db = Async::start(c.clone());
    let mut app = App::new(c);
    app.submit(Job::Init, &db);
    let _restore = Restore;
    enable_raw_mode()?;
    execute!(io::stdout(), EnterAlternateScreen)?;
    std::panic::set_hook(Box::new(|_| {
        let _ = disable_raw_mode();
        let _ = execute!(
            io::stdout(),
            LeaveAlternateScreen,
            ratatui::crossterm::cursor::Show
        );
        eprintln!("UI error; terminal restored");
    }));
    let mut terminal =
        ratatui::Terminal::new(ratatui::backend::CrosstermBackend::new(io::stdout()))?;
    let result = (|| -> Result<(), Box<dyn std::error::Error>> {
        loop {
            while let Ok(reply) = db.receive.try_recv() {
                app.receive(reply, &db);
            }
            if stopped.load(Ordering::Relaxed) || app.quit {
                break;
            }
            let area = terminal.size()?;
            app.width = area.width;
            app.height = area.height;
            terminal.draw(|f| dbcli_ui_rust::view::draw(f, &app))?;
            if let Some((original, initial)) = app.bulk_external.take() {
                let text = serde_json::to_string_pretty(&initial)?;
                disable_raw_mode()?;
                execute!(io::stdout(), LeaveAlternateScreen)?;
                let outcome = (|| {
                    let session = dbcli_ui_rust::bulk_editor::Session::create(&text)?;
                    let words = dbcli_ui_rust::bulk_editor::words(
                        &dbcli_ui_rust::bulk_editor::configured(),
                    )?;
                    session.run(&words, || stopped.load(Ordering::Relaxed))
                })();
                execute!(io::stdout(), EnterAlternateScreen)?;
                enable_raw_mode()?;
                terminal.clear()?;
                match outcome {
                    Ok(dbcli_ui_rust::bulk_editor::Outcome::Edited(text)) => {
                        if let Err(e) = app.finish_bulk(original, initial, text) {
                            app.status = e;
                        }
                    }
                    Ok(dbcli_ui_rust::bulk_editor::Outcome::Unchanged) => {
                        app.status = "No changes — no writes".into()
                    }
                    Ok(dbcli_ui_rust::bulk_editor::Outcome::Cancelled) => {
                        app.status = "Bulk edit cancelled — no writes".into()
                    }
                    Err(e) => app.status = e,
                }
                continue;
            }
            if app.external {
                app.external = false;
                if let Some(Modal::Input { kind, text, .. }) = &app.modal {
                    let text = text.clone();
                    let sql = matches!(kind, Edit::Sql | Edit::Filter);
                    disable_raw_mode()?;
                    execute!(io::stdout(), LeaveAlternateScreen)?;
                    let edited = editor(&text, sql, &stopped);
                    execute!(io::stdout(), EnterAlternateScreen)?;
                    enable_raw_mode()?;
                    terminal.clear()?;
                    app.external_finished(edited, &db);
                }
                continue;
            }
            if event::poll(Duration::from_millis(35))? {
                match event::read()? {
                    Event::Key(k) if k.kind == KeyEventKind::Press => app.key(k, &db),
                    Event::Resize(_, h) => {
                        app.spec.limit = h.saturating_sub(12).max(1) as usize;
                    }
                    _ => {}
                }
            }
        }
        Ok(())
    })();
    db.terminate();
    if app.write_pending {
        eprintln!("Write outcome not confirmed. Refresh the database before retrying; exit does not imply rollback.");
    }
    result
}
fn main() {
    let c = match config() {
        Ok(c) => c,
        Err(e) => {
            eprintln!("{e}");
            std::process::exit(2)
        }
    };
    if run(c).is_err() {
        eprintln!("UI stopped; check native worker and terminal");
        std::process::exit(1)
    }
}
