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
    collections::BTreeSet,
    fs,
    io::{self, Read, Write},
    path::{Path, PathBuf},
    process::Command,
    time::{Duration, SystemTime, UNIX_EPOCH},
};

struct TerminalGuard {
    active: bool,
}
impl TerminalGuard {
    fn new() -> io::Result<Self> {
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
fn draw(text: &str) -> io::Result<()> {
    let mut out = io::stdout();
    execute!(out, MoveTo(0, 0), Clear(ClearType::All))?;
    write!(out, "{text}")?;
    out.flush()
}
fn read_key() -> io::Result<KeyEvent> {
    loop {
        if let Event::Key(key) = event::read()? {
            return Ok(key);
        }
    }
}
fn prompt(guard: &mut TerminalGuard, label: &str, initial: &str) -> io::Result<String> {
    guard.suspend()?;
    print!("\n{label}");
    if !initial.is_empty() {
        print!("(current: {initial}) ");
    }
    io::stdout().flush()?;
    let mut value = String::new();
    io::stdin().read_line(&mut value)?;
    guard.resume()?;
    Ok(value.trim().to_owned())
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
fn json_roundtrip_for_edit(
    original: &Map<String, Value>,
    edited: Map<String, Value>,
) -> Map<String, Value> {
    edited
        .into_iter()
        .map(|(k, v)| {
            let converted = original
                .get(&k)
                .and_then(Value::as_str)
                .and_then(|s| serde_json::from_str::<Value>(s).ok())
                .is_some();
            let value = if converted && (v.is_object() || v.is_array()) {
                Value::String(crate::json_value_to_text(&v))
            } else {
                v
            };
            (k, value)
        })
        .collect()
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
        fs::write(
            &temporary.0,
            serde_json::to_vec_pretty(value).unwrap_or_default(),
        )?;
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
fn edit_json(value: Value, guard: &mut TerminalGuard) -> io::Result<Option<Value>> {
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
            .filter(|t| filter.is_empty() || t.to_lowercase().contains(&filter.to_lowercase()))
            .collect();
        cursor = cursor.min(active.len().saturating_sub(1));
        let mut lines = vec![
            format!("SQLite tables — {}", adapter.path().display()),
            format!("Filter: {}", filter),
            "j/k move | Enter browse | / filter | q exit".into(),
        ];
        for (i, t) in active.iter().enumerate().take(h.saturating_sub(5)) {
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
    browse_with_filter(adapter, table, None, guard)
}
fn browse_with_filter(
    adapter: &SQLiteAdapter,
    table: &str,
    initial_filter: Option<String>,
    guard: &mut TerminalGuard,
) -> Result<(), DbError> {
    let mut offset = 0i64;
    let mut cursor = 0usize;
    let mut focused_col = 0usize;
    let mut col_offset = 0usize;
    let mut filter = initial_filter;
    let mut sort: Option<Vec<String>> = None;
    let mut marked = BTreeSet::new();
    let mut hidden = BTreeSet::new();
    loop {
        let (width, height) = size();
        let limit = (height as i64 - 7).max(3);
        let current =
            match adapter.fetch_page(table, limit, offset, filter.as_deref(), sort.as_deref()) {
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
        let count = match adapter.count_rows(table, filter.as_deref()) {
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
            &marked,
            true,
            adapter.is_read_only(),
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
            KeyCode::Char('q') | KeyCode::Char('h') | KeyCode::Esc => return Ok(()),
            KeyCode::Char('j') | KeyCode::Down => {
                if cursor + 1 < current.rows.len() {
                    cursor += 1
                } else if current.rows.len() as i64 == limit {
                    offset += limit;
                    cursor = 0;
                    marked.clear()
                }
            }
            KeyCode::Char('k') | KeyCode::Up => {
                if cursor > 0 {
                    cursor -= 1
                } else if offset >= limit {
                    offset -= limit;
                    cursor = limit.saturating_sub(1) as usize;
                    marked.clear()
                }
            }
            KeyCode::Char('g') => {
                if event::poll(Duration::from_millis(300)).unwrap_or(false) {
                    if let Event::Key(k) = event::read().unwrap_or(Event::FocusLost) {
                        if k.code == KeyCode::Char('g') {
                            offset = 0;
                            cursor = 0;
                            marked.clear()
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
                    marked.clear()
                }
            }
            KeyCode::PageDown | KeyCode::Char('d')
                if key.modifiers.contains(KeyModifiers::CONTROL)
                    || key.code == KeyCode::PageDown =>
            {
                if current.rows.len() as i64 == limit {
                    offset = (offset + limit).min(count.saturating_sub(1).max(0));
                    cursor = 0;
                    marked.clear();
                }
            }
            KeyCode::PageUp | KeyCode::Char('u')
                if key.modifiers.contains(KeyModifiers::CONTROL) || key.code == KeyCode::PageUp =>
            {
                if offset >= limit {
                    offset -= limit;
                    cursor = 0;
                    marked.clear();
                }
            }
            KeyCode::Char('/') => {
                let f = prompt(
                    guard,
                    "Filter (text or SQL expression; empty clears): ",
                    filter.as_deref().unwrap_or(""),
                )
                .map_err(|e| DbError::adapter(e.to_string()))?;
                filter = if f.is_empty() { None } else { Some(f) };
                offset = 0;
                cursor = 0;
            }
            KeyCode::Char('o') | KeyCode::Char('O') => {
                let c = prompt(
                    guard,
                    "Sort column: ",
                    sort.as_ref()
                        .and_then(|s| s.first())
                        .map(String::as_str)
                        .unwrap_or(""),
                )
                .map_err(|e| DbError::adapter(e.to_string()))?;
                let d = prompt(
                    guard,
                    "Direction ASC/DESC: ",
                    sort.as_ref()
                        .and_then(|s| s.get(1))
                        .map(String::as_str)
                        .unwrap_or("ASC"),
                )
                .map_err(|e| DbError::adapter(e.to_string()))?;
                sort = if c.is_empty() {
                    None
                } else {
                    Some(vec![c, if d.is_empty() { "ASC".into() } else { d }])
                };
                offset = 0;
            }
            KeyCode::Char(' ') => {
                if marked.contains(&cursor) {
                    marked.remove(&cursor);
                } else {
                    marked.insert(cursor);
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
            KeyCode::Char('S') => {
                let r = adapter.schema_info(table)?;
                notice(guard, &format_result(&r)).map_err(|e| DbError::adapter(e.to_string()))?;
            }
            KeyCode::Char('I') => {
                let r = adapter.indexes(table)?;
                notice(guard, &format_result(&r)).map_err(|e| DbError::adapter(e.to_string()))?;
            }
            KeyCode::Char(':') => {
                let cmd = prompt(guard, "SQL/filter or row number: ", "")
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
                        let raw = adapter.execute_readonly(&cmd, &[], 50, 0)?;
                        notice(guard, &format_result(&raw))
                            .map_err(|e| DbError::adapter(e.to_string()))?;
                    } else {
                        filter = Some(cmd);
                        offset = 0;
                    }
                }
            }
            KeyCode::Char('f') => {
                if let Some(row) = current.rows.get(cursor) {
                    let item = row_map(&current.headers, row);
                    let fks = adapter.foreign_keys(table)?;
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
                            let next_filter = format!(
                                "{} = {}",
                                crate::quote_identifier(&fk.to_col).unwrap_or_default(),
                                sql_export_literal(val)
                            );
                            browse_with_filter(adapter, &fk.to_table, Some(next_filter), guard)?;
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
                            KeyCode::Esc | KeyCode::Char('q') | KeyCode::Char('h') | KeyCode::Enter
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
                        if k.code == KeyCode::PageDown {
                            detail_offset = (detail_offset + viewport / 2).min(max_offset);
                            continue;
                        }
                        if k.code == KeyCode::PageUp {
                            detail_offset = detail_offset.saturating_sub(viewport / 2);
                            continue;
                        }
                        if k.code == KeyCode::Char('e') && !adapter.is_read_only() {
                            let original = row_map(&current.headers, row);
                            if let Some(v) = edit_json(Value::Object(original.clone()), guard)
                                .map_err(|e| DbError::adapter(e.to_string()))?
                            {
                                if let Some(updated) = v.as_object() {
                                    let updated =
                                        json_roundtrip_for_edit(&original, updated.clone());
                                    adapter.update_row(table, &original, &updated)?;
                                    break;
                                }
                            }
                        }
                        if k.code == KeyCode::Char('d') && !adapter.is_read_only() {
                            if event::poll(Duration::from_millis(300)).unwrap_or(false) {
                                if matches!(
                                    event::read(),
                                    Ok(Event::Key(KeyEvent {
                                        code: KeyCode::Char('d'),
                                        ..
                                    }))
                                ) {
                                    let answer = prompt(guard, "Delete this row? type yes: ", "")
                                        .map_err(|e| DbError::adapter(e.to_string()))?;
                                    if answer == "yes" {
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
            KeyCode::Char('a') if !adapter.is_read_only() => {
                let template: Map<String, Value> = current
                    .headers
                    .iter()
                    .map(|h| (h.clone(), Value::Null))
                    .collect();
                if let Some(Value::Object(row)) = edit_json(Value::Object(template), guard)
                    .map_err(|e| DbError::adapter(e.to_string()))?
                {
                    adapter.insert_row(table, &row)?;
                }
            }
            KeyCode::Char('E') if !adapter.is_read_only() => {
                bulk_edit(adapter, table, filter.as_deref(), sort.as_deref(), guard)?;
            }
            KeyCode::Char('e') if !adapter.is_read_only() => {
                if let Some(row) = current.rows.get(cursor) {
                    let original = row_map(&current.headers, row);
                    if let Some(Value::Object(updated)) =
                        edit_json(Value::Object(original.clone()), guard)
                            .map_err(|e| DbError::adapter(e.to_string()))?
                    {
                        adapter.update_row(
                            table,
                            &original,
                            &json_roundtrip_for_edit(&original, updated),
                        )?;
                    }
                }
            }
            KeyCode::Char('d') if !adapter.is_read_only() => {
                if event::poll(Duration::from_millis(300)).unwrap_or(false) {
                    if matches!(
                        event::read(),
                        Ok(Event::Key(KeyEvent {
                            code: KeyCode::Char('d'),
                            ..
                        }))
                    ) {
                        if let Some(row) = current.rows.get(cursor) {
                            let answer = prompt(guard, "Delete selected row? type yes: ", "")
                                .map_err(|e| DbError::adapter(e.to_string()))?;
                            if answer == "yes" {
                                adapter.delete_row(table, &row_map(&current.headers, row))?;
                            }
                        }
                    }
                }
            }
            KeyCode::Char('x') | KeyCode::Char('X') => {
                let (hs, rs) = if marked.is_empty() {
                    let all_rows = adapter.fetch_page(
                        table,
                        100_000,
                        0,
                        filter.as_deref(),
                        sort.as_deref(),
                    )?;
                    (all_rows.headers, all_rows.rows)
                } else {
                    selected_export_data(&current, &marked)
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
                let path = export(table, &prefix, &hs, &rs, &typ)
                    .map_err(|e| DbError::adapter(e.to_string()))?;
                notice(guard, &format!("Exported to {}", path.display()))
                    .map_err(|e| DbError::adapter(e.to_string()))?;
            }
            _ => {}
        }
    }
}
fn bulk_edit(
    adapter: &SQLiteAdapter,
    table: &str,
    filter: Option<&str>,
    sort: Option<&[String]>,
    guard: &mut TerminalGuard,
) -> Result<(), DbError> {
    let original = adapter.fetch_page(table, 100_000, 0, filter, sort)?;
    if original.rows.is_empty() {
        return Ok(());
    }
    let schema = adapter.schema_info(table)?;
    let pk_indices: Vec<usize> = schema
        .rows
        .iter()
        .enumerate()
        .filter(|(_, row)| row.get(5).and_then(Value::as_str) == Some("YES"))
        .filter_map(|(i, _)| {
            original
                .headers
                .iter()
                .position(|h| h == &schema.rows[i][1].as_str().unwrap_or_default())
        })
        .collect();
    if pk_indices.is_empty() {
        return Err(DbError {
            class_name: "RowIdentityError".into(),
            message: "Bulk editing requires a primary key; use insert or edit rows individually"
                .into(),
        });
    }
    let original_maps: Vec<Map<String, Value>> = original
        .rows
        .iter()
        .map(|row| row_map(&original.headers, row))
        .collect();
    let editor_rows: Vec<Value> = original_maps
        .iter()
        .map(|map| {
            Value::Object(
                map.iter()
                    .map(|(key, value)| {
                        let editable = value
                            .as_str()
                            .and_then(|s| serde_json::from_str::<Value>(s).ok())
                            .filter(|v| v.is_object() || v.is_array())
                            .unwrap_or_else(|| value.clone());
                        (key.clone(), editable)
                    })
                    .collect(),
            )
        })
        .collect();
    let Some(Value::Array(updated_rows)) =
        edit_json(Value::Array(editor_rows), guard).map_err(|e| DbError::adapter(e.to_string()))?
    else {
        return Ok(());
    };
    let mut keyed_updates: Vec<(Vec<Value>, Map<String, Value>)> = Vec::new();
    for value in updated_rows {
        let Some(updated) = value.as_object() else {
            continue;
        };
        if updated.keys().any(|key| !original.headers.contains(key)) {
            return Err(DbError::adapter("Bulk editor contains an unknown column"));
        }
        let normalized = json_roundtrip_for_edit(&original_maps[0], updated.clone());
        let identity: Vec<Value> = pk_indices
            .iter()
            .map(|i| {
                normalized
                    .get(&original.headers[*i])
                    .cloned()
                    .unwrap_or(Value::Null)
            })
            .collect();
        if !original_maps.iter().any(|row| {
            pk_indices
                .iter()
                .map(|i| {
                    row.get(&original.headers[*i])
                        .cloned()
                        .unwrap_or(Value::Null)
                })
                .collect::<Vec<_>>()
                == identity
        }) {
            return Err(DbError {
                class_name: "RowIdentityError".into(),
                message: "Bulk editing cannot add rows or change primary keys; use the insert or single-row editor".into(),
            });
        }
        keyed_updates.push((identity, normalized));
    }
    let mut updates = Vec::new();
    let mut deletions = Vec::new();
    for original_row in &original_maps {
        let identity: Vec<Value> = pk_indices
            .iter()
            .map(|i| {
                original_row
                    .get(&original.headers[*i])
                    .cloned()
                    .unwrap_or(Value::Null)
            })
            .collect();
        if let Some((_, updated)) = keyed_updates.iter().find(|(key, _)| *key == identity) {
            if updated != original_row {
                updates.push((original_row.clone(), updated.clone()));
            }
        } else {
            deletions.push(original_row.clone());
        }
    }
    if updates.is_empty() && deletions.is_empty() {
        notice(guard, "No changes detected").map_err(|e| DbError::adapter(e.to_string()))?;
        return Ok(());
    }
    let preview = format!("Bulk edit preview for {table}: {} updated, {} deleted.\n\nChanges:\n{}\n\nOnly rows identified by their original primary keys are updated. New rows and key changes must use the single-row actions.",
        updates.len(), deletions.len(), updates.iter().take(12).map(|(old, new)| format!("- {}\n+ {}", serde_json::to_string_pretty(old).unwrap_or_default(), serde_json::to_string_pretty(new).unwrap_or_default())).collect::<Vec<_>>().join("\n"));
    notice(guard, &preview).map_err(|e| DbError::adapter(e.to_string()))?;
    let confirmation = prompt(guard, "Apply this bulk edit? type yes: ", "")
        .map_err(|e| DbError::adapter(e.to_string()))?;
    if confirmation != "yes" {
        return Ok(());
    }
    for (old, new) in updates {
        adapter.update_row(table, &old, &new)?;
    }
    for old in deletions {
        adapter.delete_row(table, &old)?;
    }
    Ok(())
}
fn sql_export_literal(value: &Value) -> String {
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
) -> io::Result<PathBuf> {
    let stem = prefix.replace('/', "_");
    let ext = match kind {
        "csv" => "csv",
        "sql" => "sql",
        _ => "json",
    };
    let path = PathBuf::from(format!("{stem}.{ext}"));
    match ext {
        "json" => {
            let values: Vec<Map<String, Value>> = rows
                .iter()
                .map(|r| {
                    headers
                        .iter()
                        .zip(r)
                        .map(|(h, v)| {
                            let decoded = v
                                .as_str()
                                .and_then(|s| serde_json::from_str::<Value>(s).ok())
                                .filter(|v| v.is_object() || v.is_array())
                                .unwrap_or_else(|| v.clone());
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
                    "INSERT INTO \"{}\" ({qh}) VALUES ({});\n",
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
