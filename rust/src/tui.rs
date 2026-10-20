//! POSIX terminal browser. Terminal mode and the alternate screen are restored on every exit path.
use crate::{render, DbError, ForeignKey, QueryResult, SQLiteAdapter};
use crossterm::{
    cursor::{Hide, MoveTo, Show},
    event::{self, Event, KeyCode, KeyEvent, KeyModifiers},
    execute,
    terminal::{self, Clear, ClearType, EnterAlternateScreen, LeaveAlternateScreen},
};
use serde_json::{Map, Value};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    io::{self, Read, Write},
    os::unix::fs::OpenOptionsExt,
    path::{Path, PathBuf},
    process::Command,
    time::{Duration, SystemTime, UNIX_EPOCH},
};

struct TerminalGuard {
    active: bool,
}
impl TerminalGuard {
    fn new() -> io::Result<Self> {
        install_signals();
        terminal::enable_raw_mode()?;
        if let Err(error) = execute!(io::stdout(), EnterAlternateScreen, Hide) {
            let _ = terminal::disable_raw_mode();
            return Err(error);
        }
        Ok(Self { active: true })
    }
    fn suspend(&mut self) -> io::Result<()> {
        if self.active {
            terminal::disable_raw_mode()?;
            execute!(io::stdout(), Show, LeaveAlternateScreen)?;
            self.active = false;
        }
        Ok(())
    }
    fn resume(&mut self) -> io::Result<()> {
        if !self.active {
            terminal::enable_raw_mode()?;
            if let Err(error) = execute!(io::stdout(), EnterAlternateScreen, Hide) {
                let _ = terminal::disable_raw_mode();
                return Err(error);
            }
            self.active = true;
        }
        Ok(())
    }
}
impl Drop for TerminalGuard {
    fn drop(&mut self) {
        if self.active {
            let _ = terminal::disable_raw_mode();
            let _ = execute!(io::stdout(), Show, LeaveAlternateScreen);
        }
    }
}
fn size() -> (usize, usize) {
    terminal::size()
        .map(|(w, h)| (w as usize, h as usize))
        .unwrap_or((100, 24))
}
fn terminal_lines(text: &str) -> String {
    text.replace("\r\n", "\n").replace('\n', "\r\n")
}
fn draw(text: &str) -> io::Result<()> {
    let mut out = io::stdout();
    execute!(out, MoveTo(0, 0), Clear(ClearType::All))?;
    write!(out, "{}", terminal_lines(text))?;
    out.flush()
}
fn read_key() -> io::Result<KeyEvent> {
    loop {
        if signal_flag().load(std::sync::atomic::Ordering::Relaxed) {
            return Ok(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        }
        if event::poll(Duration::from_millis(200))? {
            if let Event::Key(key) = event::read()? {
                if key.code == KeyCode::Char('c') && key.modifiers.contains(KeyModifiers::CONTROL) {
                    return Ok(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
                }
                return Ok(key);
            }
        }
    }
}
fn prompt(guard: &mut TerminalGuard, label: &str, initial: &str) -> io::Result<String> {
    prompt_complete(guard, label, initial, &[])
}
fn prompt_complete(
    guard: &mut TerminalGuard,
    label: &str,
    initial: &str,
    candidates: &[String],
) -> io::Result<String> {
    let mut text = initial.to_string();
    let mut cursor = text.chars().count();
    let mut completion = crate::completion::State::default();
    loop {
        print!("\r\x1b[2K\x1b[?25h{label}{text}");
        io::stdout().flush()?;
        let k = read_key()?;
        match k.code {
            KeyCode::Enter => {
                print!("\x1b[?25l");
                return Ok(text);
            }
            KeyCode::Esc => {
                print!("\x1b[?25l");
                return Ok(initial.into());
            }
            KeyCode::Char('g') if k.modifiers.contains(KeyModifiers::CONTROL) => {
                if let Some(edited) = edit_text(&text, "sql", guard)? {
                    text = edited.trim().into();
                    cursor = text.chars().count();
                }
            }
            KeyCode::Char('u') if k.modifiers.contains(KeyModifiers::CONTROL) => {
                text.clear();
                cursor = 0;
            }
            KeyCode::Backspace => {
                if cursor > 0 {
                    let mut cs = text.chars().collect::<Vec<_>>();
                    cs.remove(cursor - 1);
                    cursor -= 1;
                    text = cs.into_iter().collect();
                }
            }
            KeyCode::Left => cursor = cursor.saturating_sub(1),
            KeyCode::Right => cursor = (cursor + 1).min(text.chars().count()),
            KeyCode::Home => cursor = 0,
            KeyCode::End => cursor = text.chars().count(),
            KeyCode::Tab => {
                text = completion.next(&text, candidates);
                cursor = text.chars().count();
            }
            KeyCode::Char(ch) if !k.modifiers.contains(KeyModifiers::CONTROL) => {
                let mut cs = text.chars().collect::<Vec<_>>();
                cs.insert(cursor, ch);
                cursor += 1;
                text = cs.into_iter().collect();
            }
            _ => {}
        }
    }
}
fn edit_text(text: &str, suffix: &str, guard: &mut TerminalGuard) -> io::Result<Option<String>> {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    let temporary = TempJson(std::env::temp_dir().join(format!(
        "dbcli-rust-{}-{nonce}.{suffix}",
        std::process::id()
    )));
    let mut file = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&temporary.0)?;
    file.write_all(text.as_bytes())?;
    drop(file);
    let editor = std::env::var("EDITOR").unwrap_or_else(|_| {
        ["nvim", "vim", "nano"]
            .iter()
            .find(|s| command_exists(s))
            .unwrap_or(&"nano")
            .to_string()
    });
    let words = split_editor_command(&editor);
    if words.is_empty() {
        return Err(io::Error::other("invalid EDITOR command"));
    }
    guard.suspend()?;
    let status = Command::new(&words[0])
        .args(&words[1..])
        .arg(&temporary.0)
        .status();
    let restore = guard.resume();
    restore?;
    if status?.success() {
        fs::read_to_string(&temporary.0).map(Some)
    } else {
        Ok(None)
    }
}
fn pager(guard: &mut TerminalGuard, text: &str) -> io::Result<()> {
    let lines = text.lines().collect::<Vec<_>>();
    let mut offset = 0;
    loop {
        let (_, h) = size();
        let view = h.saturating_sub(3).max(1);
        offset = offset.min(lines.len().saturating_sub(1));
        draw(&format!(
            "{}\n[j/k Ctrl-D/U gg/G scroll; q finish preview]",
            lines[offset..]
                .iter()
                .take(view)
                .copied()
                .collect::<Vec<_>>()
                .join("\n")
        ))?;
        let k = read_key()?;
        match k.code {
            KeyCode::Char('q') | KeyCode::Char('h') | KeyCode::Char('b') | KeyCode::Esc => {
                return Ok(())
            }
            KeyCode::Char('j') => offset = (offset + 1).min(lines.len().saturating_sub(1)),
            KeyCode::Char('k') => offset = offset.saturating_sub(1),
            KeyCode::Char('d') if k.modifiers.contains(KeyModifiers::CONTROL) => {
                offset = (offset + view / 2).min(lines.len().saturating_sub(1))
            }
            KeyCode::Char('u') if k.modifiers.contains(KeyModifiers::CONTROL) => {
                offset = offset.saturating_sub(view / 2)
            }
            KeyCode::Char('g') => offset = 0,
            KeyCode::Char('G') => offset = lines.len().saturating_sub(view),
            _ => {}
        }
        let _ = guard;
    }
}
fn wait_any() -> io::Result<()> {
    let _ = read_key()?;
    Ok(())
}
fn notice(_guard: &mut TerminalGuard, message: &str) -> io::Result<()> {
    let _ = draw(&format!("{message}\n\nPress any key to continue"));
    wait_any()?;
    Ok(())
}
fn value_text(v: &Value) -> String {
    if crate::pg_read::json_envelope(v) {
        return crate::pg_read::json_payload(v).to_string();
    }
    if let Some(text) = crate::pg_read::scalar_text(v) {
        return text.into();
    }

    if v.is_null() {
        "NULL".into()
    } else if let Some(s) = v.as_str() {
        s.into()
    } else {
        v.to_string()
    }
}
fn row_map(headers: &[String], row: &[Value]) -> Map<String, Value> {
    headers
        .iter()
        .zip(row)
        .map(|(k, v)| (k.clone(), v.clone()))
        .collect()
}
fn confirm_pg(
    adapter: &SQLiteAdapter,
    guard: &mut TerminalGuard,
    actions: &[Value],
) -> Result<bool, DbError> {
    if !adapter.is_postgres() {
        return Ok(true);
    }
    pager(
        guard,
        &format!(
            "PostgreSQL transaction preview\n{}",
            serde_json::to_string_pretty(actions).unwrap()
        ),
    )
    .map_err(|e| DbError::adapter(e.to_string()))?;
    Ok(prompt(guard, "Apply transaction? y/N: ", "")
        .map_err(|e| DbError::adapter(e.to_string()))?
        == "y")
}
fn json_roundtrip_for_edit(
    original: &Map<String, Value>,
    updated: Map<String, Value>,
) -> Map<String, Value> {
    crate::writable::normalize_editor_row(
        &Value::Object(original.clone()),
        &Value::Object(updated.clone()),
    )
    .ok()
    .and_then(|v| v.as_object().cloned())
    .unwrap_or(updated)
}
struct TempJson(PathBuf);
impl TempJson {
    fn create(value: &Value) -> io::Result<Self> {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos();
        let path =
            std::env::temp_dir().join(format!("dbcli-rust-{}-{nonce}.json", std::process::id()));
        let temporary = Self(path);
        let mut file = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&temporary.0)?;
        file.write_all(&serde_json::to_vec_pretty(value).unwrap_or_default())?;
        Ok(temporary)
    }
    fn edit(self, guard: &mut TerminalGuard) -> io::Result<Option<Value>> {
        let editor = std::env::var("EDITOR").unwrap_or_else(|_| {
            if command_exists("nvim") {
                "nvim".into()
            } else if command_exists("vim") {
                "vim".into()
            } else {
                "nano".into()
            }
        });
        let words = split_editor_command(&editor);
        let executable = words.first().map(String::as_str).unwrap_or("nano");
        let mut cmd = Command::new(executable);
        cmd.args(words.iter().skip(1)).arg(&self.0);
        guard.suspend()?;
        let status = cmd.status();
        guard.resume()?;
        if status.map(|s| s.success()).unwrap_or(false) {
            let data = fs::read(&self.0)?;
            Ok(serde_json::from_slice(&data).ok())
        } else {
            Ok(None)
        }
    }
}
impl Drop for TempJson {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.0);
    }
}
fn command_exists(command: &str) -> bool {
    std::env::split_paths(&std::env::var_os("PATH").unwrap_or_default())
        .any(|p| p.join(command).is_file())
}
fn split_editor_command(command: &str) -> Vec<String> {
    let mut words = Vec::new();
    let mut current = String::new();
    let mut quote = None;
    let mut escaped = false;
    for ch in command.chars() {
        if escaped {
            current.push(ch);
            escaped = false;
            continue;
        }
        if ch == '\\' && quote != Some('\'') {
            escaped = true;
            continue;
        }
        if let Some(active) = quote {
            if ch == active {
                quote = None;
            } else {
                current.push(ch);
            }
        } else if ch == '\'' || ch == '"' {
            quote = Some(ch);
        } else if ch.is_whitespace() {
            if !current.is_empty() {
                words.push(std::mem::take(&mut current));
            }
        } else {
            current.push(ch);
        }
    }
    if escaped {
        current.push('\\');
    }
    if !current.is_empty() {
        words.push(current);
    }
    if quote.is_some() {
        Vec::new()
    } else {
        words
    }
}
fn edit_json(mut value: Value, guard: &mut TerminalGuard) -> io::Result<Option<Value>> {
    fn editable(value: &Value) -> Value {
        Value::Object(
            value
                .as_object()
                .unwrap()
                .iter()
                .map(|(k, v)| {
                    (
                        k.clone(),
                        v.as_str()
                            .and_then(|s| serde_json::from_str::<Value>(s).ok())
                            .filter(|v| v.is_object() || v.is_array())
                            .unwrap_or_else(|| v.clone()),
                    )
                })
                .collect(),
        )
    }
    if value.get("updates").is_some_and(Value::is_array)
        && value.get("delete").is_some_and(Value::is_array)
    {
        for pair in value["updates"].as_array_mut().unwrap() {
            pair["row"] = editable(&pair["row"]);
        }
    } else if value.is_object() {
        value = editable(&value)
    }
    TempJson::create(&value)?.edit(guard)
}

fn detail_screen(
    headers: &[String],
    row: &[Value],
    index: usize,
    width: usize,
    read_only: bool,
) -> String {
    let card = width.saturating_sub(2).min(98);
    let line = "=".repeat(card);
    let mut out = vec![
        line.clone(),
        format!("{}", format!("ROW #{} DETAIL VIEW", index + 1)),
        line.clone(),
    ];
    let max = headers
        .iter()
        .map(|s| s.chars().count())
        .max()
        .unwrap_or(15);
    for (h, v) in headers.iter().zip(row) {
        let value = value_text(v);
        let rendered = if let Some(s) = v.as_str() {
            serde_json::from_str::<Value>(s)
                .ok()
                .filter(|j| j.is_array() || j.is_object())
                .map(|j| serde_json::to_string_pretty(&j).unwrap_or(value.clone()))
                .unwrap_or(value)
        } else {
            value
        };
        out.push(format!("   {h:<max$} : {rendered}"));
    }
    out.push(line);
    out.push(
        if read_only {
            "j/k scroll | gg/G top/bottom | h/q return"
        } else {
            "j/k scroll | e edit | dd delete | gg/G top/bottom | h/q return"
        }
        .into(),
    );
    out.join("\n")
}
fn table_pick(
    adapter: &SQLiteAdapter,
    guard: &mut TerminalGuard,
) -> Result<Option<String>, DbError> {
    let tables = adapter.list_tables(false)?;
    if tables.is_empty() {
        return Ok(None);
    };
    let mut cursor = 0usize;
    let mut filter = String::new();
    loop {
        let (w, h) = size();
        let active: Vec<_> = tables
            .iter()
            .filter(|t| crate::completion::fuzzy(&filter, t))
            .collect();
        cursor = cursor.min(active.len().saturating_sub(1));
        let mut lines = vec![
            format!("SQLite tables — {}", adapter.path().display()),
            format!("Filter: {}", filter),
            "j/k move | Enter browse | / filter | q exit".into(),
        ];
        for (i, t) in active
            .iter()
            .enumerate()
            .skip(cursor.saturating_sub(h.saturating_sub(6)))
            .take(h.saturating_sub(5))
        {
            lines.push(format!("{} {}", if i == cursor { ">" } else { " " }, t));
        }
        let _ = w;
        draw(&lines.join("\n")).map_err(|e| DbError::adapter(e.to_string()))?;
        match read_key()
            .map_err(|e| DbError::adapter(e.to_string()))?
            .code
        {
            KeyCode::Char('j') | KeyCode::Down => {
                cursor = cursor.saturating_add(1).min(active.len().saturating_sub(1))
            }
            KeyCode::Char('k') | KeyCode::Up => cursor = cursor.saturating_sub(1),
            KeyCode::Enter => return Ok(active.get(cursor).map(|s| s.to_string())),
            KeyCode::Char('/') => {
                filter = prompt(guard, "Filter tables: ", &filter)
                    .map_err(|e| DbError::adapter(e.to_string()))?;
                cursor = 0;
            }
            KeyCode::Char('q') | KeyCode::Esc => return Ok(None),
            _ => {}
        }
    }
}
fn pick_foreign_key(
    candidates: &[(&ForeignKey, Value)],
    _guard: &mut TerminalGuard,
) -> Result<Option<usize>, DbError> {
    if candidates.len() == 1 {
        return Ok(Some(0));
    }
    let mut cursor = 0usize;
    loop {
        let mut lines = vec![
            "Foreign-key relationships".to_string(),
            "j/k move | Enter follow | q cancel".into(),
        ];
        for (index, (fk, value)) in candidates.iter().enumerate() {
            lines.push(format!(
                "{} {}.{} = {}",
                if index == cursor { ">" } else { " " },
                fk.to_table,
                fk.to_col,
                value_text(value)
            ));
        }
        draw(&lines.join("\n")).map_err(|e| DbError::adapter(e.to_string()))?;
        match read_key()
            .map_err(|e| DbError::adapter(e.to_string()))?
            .code
        {
            KeyCode::Char('j') | KeyCode::Down => cursor = (cursor + 1).min(candidates.len() - 1),
            KeyCode::Char('k') | KeyCode::Up => cursor = cursor.saturating_sub(1),
            KeyCode::Enter | KeyCode::Char('l') => return Ok(Some(cursor)),
            KeyCode::Esc | KeyCode::Char('q') | KeyCode::Char('h') => return Ok(None),
            _ => {}
        }
    }
}
fn browse(adapter: &SQLiteAdapter, table: &str, guard: &mut TerminalGuard) -> Result<(), DbError> {
    browse_with_filter(adapter, table, None, None, guard)
}
fn browse_with_filter(
    adapter: &SQLiteAdapter,
    table: &str,
    initial_filter: Option<String>,
    raw: Option<String>,
    guard: &mut TerminalGuard,
) -> Result<(), DbError> {
    let mut offset = 0i64;
    let mut cursor = 0usize;
    let mut focused_col = 0usize;
    let mut col_offset = 0usize;
    let mut filter = initial_filter;
    let mut sort: Option<Vec<String>> = None;
    let mut marked: BTreeMap<String, Map<String, Value>> = BTreeMap::new();
    let keys = if raw.is_none() {
        adapter.primary_keys(table)?
    } else {
        vec![]
    };
    let tables = adapter.list_tables(false)?;
    let mut hidden = BTreeSet::new();
    loop {
        let (width, height) = size();
        let limit = (height as i64 - 7).max(3);
        let current = match if let Some(sql) = &raw {
            adapter.raw_page(sql, limit, offset, filter.as_deref(), sort.as_deref())
        } else {
            adapter.fetch_page(table, limit, offset, filter.as_deref(), sort.as_deref())
        } {
            Ok(page) => page,
            Err(error) => {
                filter = None;
                sort = None;
                offset = 0;
                cursor = 0;
                notice(guard, &error.message).map_err(|e| DbError::adapter(e.to_string()))?;
                continue;
            }
        };
        let count = match if let Some(sql) = &raw {
            adapter.raw_count(sql, filter.as_deref())
        } else {
            adapter.count_rows(table, filter.as_deref())
        } {
            Ok(count) => count,
            Err(error) if error.message.starts_with("Unknown SQLite table:") => return Err(error),
            Err(error) => {
                filter = None;
                sort = None;
                offset = 0;
                cursor = 0;
                notice(guard, &error.message).map_err(|e| DbError::adapter(e.to_string()))?;
                continue;
            }
        };
        cursor = cursor.min(current.rows.len().saturating_sub(1));
        let mut completions =
            crate::completion::candidates(&current.headers, &current.rows, &tables);
        if adapter.is_postgres() {
            completions.retain(|c| !c.contains("json_extract("));
            completions.extend(
                crate::pg_read::Reader::new(crate::pg_read::BlobStyle::Base64)
                    .completions(table, &current.headers, &current.rows, raw.as_deref())
                    .map_err(|e| DbError::new(e.kind, e.message))?,
            );
        }
        if raw.is_none() {
            for fk in adapter.navigation_foreign_keys(table)? {
                completions.push(format!(
                    "JOIN {} ON {}.{} = {}.{}",
                    fk.to_table, table, fk.from_col, fk.to_table, fk.to_col
                ));
            }
        }
        let (headers, rows) = if hidden.is_empty() {
            (current.headers.clone(), current.rows.clone())
        } else {
            let indices: Vec<usize> = current
                .headers
                .iter()
                .enumerate()
                .filter(|(_, h)| !hidden.contains(*h))
                .map(|(i, _)| i)
                .collect();
            (
                indices
                    .iter()
                    .map(|i| current.headers[*i].clone())
                    .collect(),
                current
                    .rows
                    .iter()
                    .map(|r| indices.iter().filter_map(|i| r.get(*i).cloned()).collect())
                    .collect(),
            )
        };
        let view = QueryResult { headers, rows };
        focused_col = focused_col.min(view.headers.len().saturating_sub(1));
        let mut screen = render::render_browser_state(
            &view,
            table,
            count,
            offset,
            width,
            height,
            filter.as_deref(),
            sort.as_deref(),
            cursor,
            focused_col,
            col_offset,
            &current
                .rows
                .iter()
                .enumerate()
                .filter(|(_, r)| {
                    marked.contains_key(&mark_id(&keys, &row_map(&current.headers, r)))
                })
                .map(|(i, _)| i)
                .collect(),
            true,
            adapter.is_read_only() || raw.is_some(),
        );
        if adapter.is_read_only() {
            if let Some(first) = screen.lines().next() {
                let marked = format!("{first} [READ ONLY]");
                screen = screen.replacen(first, &marked, 1);
            }
        }
        let _ = draw(&screen);
        let key = read_key().map_err(|e| DbError::adapter(e.to_string()))?;
        match key.code {
            KeyCode::Char('q') | KeyCode::Char('h') | KeyCode::Char('b') | KeyCode::Esc => {
                return Ok(())
            }
            KeyCode::Char('j') | KeyCode::Down => {
                if cursor + 1 < current.rows.len() {
                    cursor += 1
                } else if current.rows.len() as i64 == limit {
                    offset += limit;
                    cursor = 0;
                }
            }
            KeyCode::Char('k') | KeyCode::Up => {
                if cursor > 0 {
                    cursor -= 1
                } else if offset >= limit {
                    offset -= limit;
                    cursor = limit.saturating_sub(1) as usize;
                }
            }
            KeyCode::Char('g') if !key.modifiers.contains(KeyModifiers::CONTROL) => {
                if event::poll(Duration::from_millis(300)).unwrap_or(false) {
                    if let Event::Key(k) = event::read().unwrap_or(Event::FocusLost) {
                        if k.code == KeyCode::Char('g') {
                            offset = 0;
                            cursor = 0;
                        }
                    }
                }
            }
            KeyCode::Char('L') => {
                focused_col = (focused_col + 1).min(view.headers.len().saturating_sub(1));
            }
            KeyCode::Char('H') => {
                focused_col = focused_col.saturating_sub(1);
                col_offset = col_offset.min(focused_col);
            }
            KeyCode::Char('G') => {
                if count > 0 {
                    offset = ((count - 1) / limit) * limit;
                    cursor = ((count - 1) % limit) as usize;
                }
            }
            KeyCode::PageDown | KeyCode::Char('d')
                if key.modifiers.contains(KeyModifiers::CONTROL)
                    || key.code == KeyCode::PageDown =>
            {
                if current.rows.len() as i64 == limit {
                    offset = (offset + (limit / 2).max(1)).min(count.saturating_sub(1).max(0));
                    cursor = 0;
                }
            }
            KeyCode::PageUp | KeyCode::Char('u')
                if key.modifiers.contains(KeyModifiers::CONTROL) || key.code == KeyCode::PageUp =>
            {
                offset = (offset - (limit / 2).max(1)).max(0);
                cursor = 0;
            }
            KeyCode::Char('g') if key.modifiers.contains(KeyModifiers::CONTROL) => {
                if let Some(value) = edit_text(filter.as_deref().unwrap_or(""), "sql", guard)
                    .map_err(|e| DbError::adapter(e.to_string()))?
                {
                    filter = (!value.trim().is_empty()).then(|| value.trim().to_string());
                    offset = 0;
                    cursor = 0;
                }
            }
            KeyCode::Char('c') => {
                filter = None;
                sort = None;
                offset = 0;
                cursor = 0;
            }
            KeyCode::Char('/') => {
                let f = prompt_complete(
                    guard,
                    "Filter (text or SQL expression; empty clears): ",
                    filter.as_deref().unwrap_or(""),
                    &completions,
                )
                .map_err(|e| DbError::adapter(e.to_string()))?;
                filter = if f.is_empty() { None } else { Some(f) };
                offset = 0;
                cursor = 0;
            }
            KeyCode::Char('o') | KeyCode::Char('O') => {
                if let Some(column) = view.headers.get(focused_col) {
                    let next = vec![
                        column.clone(),
                        if key.code == KeyCode::Char('o') {
                            "ASC".into()
                        } else {
                            "DESC".into()
                        },
                    ];
                    sort = if sort.as_ref() == Some(&next) {
                        None
                    } else {
                        Some(next)
                    };
                    offset = 0;
                }
            }
            KeyCode::Char(' ') => {
                if let Some(row) = current.rows.get(cursor) {
                    let row = row_map(&current.headers, row);
                    let id = mark_id(&keys, &row);
                    if marked.remove(&id).is_none() {
                        marked.insert(id, row);
                    }
                }
            }
            KeyCode::Char('v') => {
                if !current.headers.is_empty() {
                    let c = prompt(guard, "Toggle hidden column (blank cancels): ", "")
                        .map_err(|e| DbError::adapter(e.to_string()))?;
                    if current.headers.contains(&c) {
                        if hidden.contains(&c) {
                            hidden.remove(&c);
                        } else if hidden.len() + 1 < current.headers.len() {
                            hidden.insert(c);
                        }
                    }
                }
            }
            KeyCode::Char('S') if raw.is_none() => {
                let r = adapter.schema_info(table)?;
                pager(guard, &format_result(&r)).map_err(|e| DbError::adapter(e.to_string()))?;
            }
            KeyCode::Char('I') if raw.is_none() => {
                let r = adapter.indexes(table)?;
                pager(guard, &format_result(&r)).map_err(|e| DbError::adapter(e.to_string()))?;
            }
            KeyCode::Char(':') => {
                let cmd = prompt_complete(guard, "SQL/filter or row number: ", "", &completions)
                    .map_err(|e| DbError::adapter(e.to_string()))?;
                if let Ok(n) = cmd.parse::<i64>() {
                    if n > 0 {
                        offset = ((n - 1) / limit) * limit;
                        cursor = ((n - 1) % limit) as usize;
                    }
                } else if !cmd.is_empty() {
                    if cmd.to_ascii_uppercase().starts_with("SELECT")
                        || cmd.to_ascii_uppercase().starts_with("WITH")
                        || cmd.to_ascii_uppercase().starts_with("EXPLAIN")
                    {
                        browse_with_filter(adapter, "Query", None, Some(cmd), guard)?;
                    } else {
                        filter = Some(cmd);
                        offset = 0;
                    }
                }
            }
            KeyCode::Char('f') if raw.is_none() => {
                if let Some(row) = current.rows.get(cursor) {
                    let item = row_map(&current.headers, row);
                    let fks = adapter.navigation_foreign_keys(table)?;
                    if fks.is_empty() {
                        notice(guard, "No foreign key relations found")
                            .map_err(|e| DbError::adapter(e.to_string()))?;
                    } else {
                        let candidates: Vec<_> = fks
                            .iter()
                            .filter_map(|fk| {
                                item.get(&fk.from_col)
                                    .filter(|v| !v.is_null())
                                    .map(|v| (fk, v.clone()))
                            })
                            .collect();
                        if candidates.is_empty() {
                            notice(guard, "Foreign key columns are NULL on this row")
                                .map_err(|e| DbError::adapter(e.to_string()))?;
                        } else if let Some(choice) = pick_foreign_key(&candidates, guard)? {
                            let fk = candidates[choice].0;
                            let val = &candidates[choice].1;
                            let next_filter = if !fk.pairs.is_empty() {
                                fk.pairs
                                    .iter()
                                    .map(|p| {
                                        format!(
                                            "{} = {}",
                                            crate::quote_identifier(p["to_col"].as_str().unwrap())
                                                .unwrap(),
                                            crate::pg_read::sql_literal(
                                                &item[p["from_col"].as_str().unwrap()]
                                            )
                                            .unwrap()
                                        )
                                    })
                                    .collect::<Vec<_>>()
                                    .join(" AND ")
                            } else {
                                format!(
                                    "{} = {}",
                                    crate::quote_identifier(&fk.to_col).unwrap_or_default(),
                                    sql_export_literal(val)
                                )
                            };
                            browse_with_filter(
                                adapter,
                                &fk.to_table,
                                Some(next_filter),
                                None,
                                guard,
                            )?;
                        }
                    }
                }
            }
            KeyCode::Enter | KeyCode::Char('l') => {
                if let Some(row) = current.rows.get(cursor) {
                    let detail = detail_screen(
                        &current.headers,
                        row,
                        (offset as usize) + cursor,
                        width,
                        adapter.is_read_only(),
                    );
                    let mut detail_offset = 0usize;
                    loop {
                        let (_, term_height) = size();
                        let detail_lines: Vec<&str> = detail.lines().collect();
                        let viewport = term_height.saturating_sub(3).max(5);
                        let max_offset = detail_lines.len().saturating_sub(viewport);
                        detail_offset = detail_offset.min(max_offset);
                        let mut visible = detail_lines
                            [detail_offset..(detail_offset + viewport).min(detail_lines.len())]
                            .join("\n");
                        visible.push_str(&format!(
                            "\n[{}/{}]",
                            detail_offset + 1,
                            detail_lines.len()
                        ));
                        let _ = draw(&visible);
                        let k = read_key().map_err(|e| DbError::adapter(e.to_string()))?;
                        if matches!(
                            k.code,
                            KeyCode::Esc
                                | KeyCode::Char('q')
                                | KeyCode::Char('h')
                                | KeyCode::Char('b')
                                | KeyCode::Enter
                        ) {
                            break;
                        }
                        if k.code == KeyCode::Char('j') || k.code == KeyCode::Down {
                            detail_offset = (detail_offset + 1).min(max_offset);
                            continue;
                        }
                        if k.code == KeyCode::Char('k') || k.code == KeyCode::Up {
                            detail_offset = detail_offset.saturating_sub(1);
                            continue;
                        }
                        if k.code == KeyCode::Char('g') {
                            detail_offset = 0;
                            continue;
                        }
                        if k.code == KeyCode::Char('G') {
                            detail_offset = max_offset;
                            continue;
                        }
                        if k.code == KeyCode::Char(' ')
                            || k.code == KeyCode::PageDown
                            || k.code == KeyCode::Char('d')
                                && k.modifiers.contains(KeyModifiers::CONTROL)
                        {
                            detail_offset = (detail_offset + viewport / 2).min(max_offset);
                            continue;
                        }
                        if k.code == KeyCode::PageUp
                            || k.code == KeyCode::Char('u')
                                && k.modifiers.contains(KeyModifiers::CONTROL)
                        {
                            detail_offset = detail_offset.saturating_sub(viewport / 2);
                            continue;
                        }
                        if k.code == KeyCode::Char('e') && !adapter.is_read_only() && raw.is_none()
                        {
                            let original = row_map(&current.headers, row);
                            if let Some(v) = edit_json(Value::Object(original.clone()), guard)
                                .map_err(|e| DbError::adapter(e.to_string()))?
                            {
                                if let Some(updated) = v.as_object() {
                                    let updated =
                                        json_roundtrip_for_edit(&original, updated.clone());
                                    if confirm_pg(
                                        adapter,
                                        guard,
                                        &[
                                            serde_json::json!({"kind":"update","original":original,"row":updated}),
                                        ],
                                    )? {
                                        adapter.update_row(table, &original, &updated)?;
                                    }
                                    break;
                                }
                            }
                        }
                        if k.code == KeyCode::Char('d') && !adapter.is_read_only() && raw.is_none()
                        {
                            if event::poll(Duration::from_millis(300)).unwrap_or(false) {
                                if matches!(
                                    event::read(),
                                    Ok(Event::Key(KeyEvent {
                                        code: KeyCode::Char('d'),
                                        ..
                                    }))
                                ) {
                                    let answer = prompt(guard, "Delete this row? y/N: ", "")
                                        .map_err(|e| DbError::adapter(e.to_string()))?;
                                    if answer == "y" || answer == "yes" {
                                        adapter
                                            .delete_row(table, &row_map(&current.headers, row))?;
                                        break;
                                    }
                                }
                            }
                        }
                    }
                }
            }
            KeyCode::Char('a') if !adapter.is_read_only() && raw.is_none() => {
                let insert_columns = adapter.insert_columns(table)?;
                let template = insert_columns
                    .iter()
                    .map(|h| (h.clone(), Value::Null))
                    .collect::<Map<_, _>>();
                if let Some(edited) = edit_json(Value::Object(template), guard)
                    .map_err(|e| DbError::adapter(e.to_string()))?
                {
                    let rows = if let Some(rows) = edited.as_array() {
                        rows.clone()
                    } else {
                        vec![edited]
                    };
                    let actions = rows
                        .into_iter()
                        .map(|row| serde_json::json!({"kind":"insert","row":row}))
                        .collect::<Vec<_>>();
                    if confirm_pg(adapter, guard, &actions)? {
                        adapter.apply_batch(table, &actions)?;
                    }
                }
            }
            KeyCode::Char('E') if !adapter.is_read_only() && raw.is_none() => {
                bulk_edit(
                    adapter,
                    table,
                    filter.as_deref(),
                    sort.as_deref(),
                    &marked,
                    guard,
                )?;
            }
            KeyCode::Char('e') if !adapter.is_read_only() && raw.is_none() => {
                if let Some(row) = current.rows.get(cursor) {
                    let original = row_map(&current.headers, row);
                    if let Some(Value::Object(updated)) =
                        edit_json(Value::Object(original.clone()), guard)
                            .map_err(|e| DbError::adapter(e.to_string()))?
                    {
                        let updated = json_roundtrip_for_edit(&original, updated);
                        if confirm_pg(
                            adapter,
                            guard,
                            &[
                                serde_json::json!({"kind":"update","original":original,"row":updated}),
                            ],
                        )? {
                            adapter.update_row(table, &original, &updated)?;
                        }
                    }
                }
            }
            KeyCode::Char('d') if !adapter.is_read_only() && raw.is_none() => {
                if event::poll(Duration::from_millis(300)).unwrap_or(false)
                    && read_key()
                        .map_err(|e| DbError::adapter(e.to_string()))?
                        .code
                        == KeyCode::Char('d')
                {
                    let selected = if marked.is_empty() {
                        current
                            .rows
                            .get(cursor)
                            .map(|r| vec![row_map(&current.headers, r)])
                            .unwrap_or_default()
                    } else {
                        marked.values().cloned().collect::<Vec<_>>()
                    };
                    if selected.is_empty() {
                        continue;
                    }
                    let actions = selected
                        .into_iter()
                        .map(|row| serde_json::json!({"kind":"delete","original":row}))
                        .collect::<Vec<_>>();
                    if !marked.is_empty() {
                        pager(
                            guard,
                            &format!(
                                "Marked deletion preview: {} rows\n{}",
                                actions.len(),
                                serde_json::to_string_pretty(&actions).unwrap()
                            ),
                        )
                        .map_err(|e| DbError::adapter(e.to_string()))?;
                    }
                    if ["y", "yes"].contains(
                        &prompt(
                            guard,
                            &format!("Delete {} selected row(s)? y/N: ", actions.len()),
                            "",
                        )
                        .map_err(|e| DbError::adapter(e.to_string()))?
                        .as_str(),
                    ) {
                        adapter.apply_batch(table, &actions)?;
                        marked.clear();
                    }
                }
            }
            KeyCode::Char('x') | KeyCode::Char('X') => {
                let (hs, rs) = if marked.is_empty() {
                    let all_rows = if let Some(sql) = &raw {
                        adapter.raw_page(sql, 100000, 0, filter.as_deref(), sort.as_deref())?
                    } else {
                        adapter.fetch_page(table, 100000, 0, filter.as_deref(), sort.as_deref())?
                    };
                    (all_rows.headers, all_rows.rows)
                } else {
                    (
                        current.headers.clone(),
                        marked
                            .values()
                            .map(|row| {
                                current
                                    .headers
                                    .iter()
                                    .map(|h| row.get(h).cloned().unwrap_or(Value::Null))
                                    .collect()
                            })
                            .collect(),
                    )
                };
                let typ = if key.code == KeyCode::Char('x') {
                    "json".to_string()
                } else {
                    let chosen = prompt(guard, "Export json/csv/sql: ", "json")
                        .map_err(|e| DbError::adapter(e.to_string()))?;
                    if chosen.is_empty() {
                        "json".into()
                    } else {
                        chosen
                    }
                };
                if rs.is_empty() {
                    notice(guard, "No rows to export")
                        .map_err(|e| DbError::adapter(e.to_string()))?;
                    continue;
                }
                let prefix = if marked.is_empty() {
                    format!("{table}_export")
                } else {
                    format!("{table}_marked")
                };
                let path = export(
                    table,
                    &prefix,
                    &hs,
                    &rs,
                    &typ,
                    adapter.is_postgres(),
                    raw.as_deref(),
                )
                .map_err(|e| DbError::adapter(e.to_string()))?;
                notice(guard, &format!("Exported to {}", path.display()))
                    .map_err(|e| DbError::adapter(e.to_string()))?;
            }
            _ => {}
        }
    }
}
fn mark_id(keys: &[String], row: &Map<String, Value>) -> String {
    crate::writable::identity(&Value::Object(row.clone()), keys)
        .unwrap_or_else(|_| serde_json::to_string(row).unwrap())
}
fn bulk_edit(
    adapter: &SQLiteAdapter,
    table: &str,
    filter: Option<&str>,
    sort: Option<&[String]>,
    marked: &BTreeMap<String, Map<String, Value>>,
    guard: &mut TerminalGuard,
) -> Result<(), DbError> {
    let source = if marked.is_empty() {
        let original = adapter.fetch_page(table, 100001, 0, filter, sort)?;
        if original.rows.len() > 100000 {
            return Err(DbError::adapter("bulk editing is limited to 100000 rows"));
        }
        original
            .rows
            .iter()
            .map(|r| Value::Object(row_map(&original.headers, r)))
            .collect::<Vec<_>>()
    } else {
        marked
            .values()
            .cloned()
            .map(Value::Object)
            .collect::<Vec<_>>()
    };
    let keys = adapter.primary_keys(table)?;
    for row in &source {
        crate::writable::identity(row, &keys).map_err(DbError::adapter)?;
    }
    let template = crate::writable::bulk_template(&source);
    let Some(edited) = edit_json(template, guard).map_err(|e| DbError::adapter(e.to_string()))?
    else {
        return Ok(());
    };
    let actions = crate::writable::bulk_plan(&source, &edited, &keys).map_err(DbError::adapter)?;
    if actions.is_empty() {
        return Ok(());
    }
    pager(
        guard,
        &format!(
            "Bulk preview: {} explicit actions\n{}",
            actions.len(),
            serde_json::to_string_pretty(&actions).unwrap()
        ),
    )
    .map_err(|e| DbError::adapter(e.to_string()))?;
    if actions.iter().any(|a| a["kind"] == "delete")
        && prompt(guard, "Confirm explicit deletions: type DELETE: ", "")
            .map_err(|e| DbError::adapter(e.to_string()))?
            != "DELETE"
    {
        return Ok(());
    }
    if prompt(guard, "Apply atomic bulk changes? y/N: ", "")
        .map_err(|e| DbError::adapter(e.to_string()))?
        == "y"
    {
        adapter.apply_batch(table, &actions)?;
    }
    Ok(())
}
fn sql_export_literal(value: &Value) -> String {
    if crate::pg_read::scalar_text(value).is_some() {
        return crate::pg_read::sql_literal(value).unwrap_or_else(|_| "NULL".into());
    }
    if value.is_null() {
        return "NULL".into();
    }
    if let Some(b) = value.as_bool() {
        return if b { "TRUE".into() } else { "FALSE".into() };
    }
    if value.is_number() {
        return value.to_string();
    }
    if let Some(encoded) = value.get("$blob").and_then(Value::as_str) {
        if let Ok(bytes) =
            base64::Engine::decode(&base64::engine::general_purpose::STANDARD, encoded)
        {
            return format!(
                "X'{}'",
                bytes.iter().map(|b| format!("{b:02x}")).collect::<String>()
            );
        }
    }
    let text = value
        .as_str()
        .map(str::to_owned)
        .unwrap_or_else(|| value.to_string());
    format!("'{}'", text.replace('\'', "''"))
}
fn format_result(result: &QueryResult) -> String {
    let mut out = vec![result.headers.join(" | ")];
    for row in &result.rows {
        out.push(row.iter().map(value_text).collect::<Vec<_>>().join(" | "));
    }
    if result.rows.is_empty() {
        out.push("(No data found)".into());
    }
    out.join("\n")
}
fn selected_export_data(
    result: &QueryResult,
    marked: &BTreeSet<usize>,
) -> (Vec<String>, Vec<Vec<Value>>) {
    if marked.is_empty() {
        (result.headers.clone(), result.rows.clone())
    } else {
        (
            result.headers.clone(),
            marked
                .iter()
                .filter_map(|i| result.rows.get(*i).cloned())
                .collect(),
        )
    }
}
fn export(
    table: &str,
    prefix: &str,
    headers: &[String],
    rows: &[Vec<Value>],
    kind: &str,
    postgres: bool,
    raw: Option<&str>,
) -> io::Result<PathBuf> {
    let stem = prefix.replace('/', "_");
    let ext = match kind {
        "csv" => "csv",
        "sql" => "sql",
        _ => "json",
    };
    let path = PathBuf::from(format!("{stem}.{ext}"));
    if postgres && ext == "sql" {
        let text = crate::pg_read::Reader::new(crate::pg_read::BlobStyle::Base64)
            .sql_export(table, headers, rows, raw)
            .map_err(io::Error::other)?;
        fs::write(&path, text)?;
        return Ok(path);
    }

    match ext {
        "json" => {
            let values: Vec<Map<String, Value>> = rows
                .iter()
                .map(|r| {
                    headers
                        .iter()
                        .zip(r)
                        .map(|(h, v)| {
                            let decoded = if postgres {
                                crate::pg_read::json_payload(v).clone()
                            } else {
                                v.as_str()
                                    .and_then(|s| serde_json::from_str::<Value>(s).ok())
                                    .filter(|v| v.is_object() || v.is_array())
                                    .unwrap_or_else(|| v.clone())
                            };
                            (h.clone(), decoded)
                        })
                        .collect()
                })
                .collect();
            fs::write(
                &path,
                serde_json::to_vec_pretty(&values).unwrap_or_default(),
            )?;
        }
        "csv" => {
            let mut out = String::new();
            out.push_str(
                &headers
                    .iter()
                    .map(|h| csv_field(h))
                    .collect::<Vec<_>>()
                    .join(","),
            );
            out.push('\n');
            for row in rows {
                out.push_str(
                    &row.iter()
                        .map(|v| csv_field(&csv_value(v)))
                        .collect::<Vec<_>>()
                        .join(","),
                );
                out.push('\n');
            }
            fs::write(&path, out)?;
        }
        _ => {
            let mut out = String::new();
            let qh = headers
                .iter()
                .map(|h| format!("\"{}\"", h.replace('"', "\"\"")))
                .collect::<Vec<_>>()
                .join(", ");
            for row in rows {
                out.push_str(&format!(
                    "INSERT INTO {} ({qh}) VALUES ({});\n",
                    table.replace('"', "\"\""),
                    row.iter()
                        .map(|v| if v.is_null() {
                            "NULL".into()
                        } else {
                            sql_export_literal(v)
                        })
                        .collect::<Vec<_>>()
                        .join(", ")
                ));
            }
            fs::write(&path, out)?;
        }
    }
    Ok(path)
}
fn csv_value(value: &Value) -> String {
    if let Some(text) = crate::pg_read::scalar_text(value) {
        return text.into();
    }

    if value.is_null() {
        String::new()
    } else {
        value_text(value)
    }
}
fn csv_field(s: &str) -> String {
    if s.contains([',', '"', '\n', '\r']) {
        format!("\"{}\"", s.replace('"', "\"\""))
    } else {
        s.to_owned()
    }
}

pub fn run(adapter: &SQLiteAdapter) -> Result<(), DbError> {
    let mut guard = TerminalGuard::new().map_err(|e| DbError::adapter(e.to_string()))?;
    loop {
        match table_pick(adapter, &mut guard)? {
            Some(t) => browse(adapter, &t, &mut guard)?,
            None => break,
        }
    }
    drop(guard);
    Ok(())
}
pub fn run_table(adapter: &SQLiteAdapter, table: &str) -> Result<(), DbError> {
    let mut guard = TerminalGuard::new().map_err(|e| DbError::adapter(e.to_string()))?;
    browse(adapter, table, &mut guard)?;
    drop(guard);
    Ok(())
}

pub fn discover_sqlite_files(dir: &Path) -> Vec<PathBuf> {
    let mut files = Vec::new();
    if let Ok(entries) = std::fs::read_dir(dir) {
        for e in entries.flatten() {
            let p = e.path();
            if !p.is_file() {
                continue;
            }
            let mut buf = [0u8; 16];
            if let Ok(mut f) = std::fs::File::open(&p) {
                if f.read_exact(&mut buf).is_ok() && &buf == b"SQLite format 3\0" {
                    files.push(p);
                }
            }
        }
    }
    files.sort();
    files
}

pub fn select_database() -> Result<Option<PathBuf>, DbError> {
    let files = crate::completion::discover(Path::new("."));
    if files.is_empty() {
        return Ok(None);
    }
    let mut guard = TerminalGuard::new().map_err(|e| DbError::adapter(e.to_string()))?;
    let mut cursor = 0usize;
    let mut filter = String::new();
    loop {
        let active = files
            .iter()
            .filter(|p| crate::completion::fuzzy(&filter, &p.to_string_lossy()))
            .collect::<Vec<_>>();
        cursor = cursor.min(active.len().saturating_sub(1));
        let (_, h) = size();
        let mut lines = vec!["Local SQLite databases — j/k Enter / search q exit".into()];
        lines.extend(
            active
                .iter()
                .enumerate()
                .skip(cursor.saturating_sub(h.saturating_sub(5)))
                .take(h.saturating_sub(3))
                .map(|(i, p)| format!("{} {}", if i == cursor { ">" } else { " " }, p.display())),
        );
        draw(&lines.join("\n")).map_err(|e| DbError::adapter(e.to_string()))?;
        match read_key()
            .map_err(|e| DbError::adapter(e.to_string()))?
            .code
        {
            KeyCode::Char('q') | KeyCode::Esc => return Ok(None),
            KeyCode::Char('j') | KeyCode::Down => {
                cursor = (cursor + 1).min(active.len().saturating_sub(1))
            }
            KeyCode::Char('k') | KeyCode::Up => cursor = cursor.saturating_sub(1),
            KeyCode::Enter if !active.is_empty() => return Ok(Some(active[cursor].to_path_buf())),
            KeyCode::Char('/') => {
                filter = prompt(&mut guard, "Filter databases: ", &filter)
                    .map_err(|e| DbError::adapter(e.to_string()))?;
                cursor = 0
            }
            _ => {}
        }
    }
}

#[cfg(test)]
mod writable_ui_tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;
    #[test]
    fn raw_terminal_lines_reset_columns_once() {
        assert_eq!(terminal_lines("a\nb\r\nc"), "a\r\nb\r\nc");
    }
    #[test]
    fn editor_files_are_private_and_removed() {
        let temporary = TempJson::create(&serde_json::json!({"only":"synthetic"})).unwrap();
        let path = temporary.0.clone();
        assert_eq!(
            fs::metadata(&path).unwrap().permissions().mode() & 0o777,
            0o600
        );
        drop(temporary);
        assert!(!path.exists());
    }
}

fn signal_flag() -> &'static std::sync::Arc<std::sync::atomic::AtomicBool> {
    static FLAG: std::sync::OnceLock<std::sync::Arc<std::sync::atomic::AtomicBool>> =
        std::sync::OnceLock::new();
    FLAG.get_or_init(|| std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false)))
}
fn install_signals() {
    static ONCE: std::sync::Once = std::sync::Once::new();
    ONCE.call_once(|| {
        for signal in [
            signal_hook::consts::SIGTERM,
            signal_hook::consts::SIGHUP,
            signal_hook::consts::SIGINT,
        ] {
            let _ = signal_hook::flag::register(signal, signal_flag().clone());
        }
    });
}
