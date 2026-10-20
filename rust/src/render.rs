//! Deterministic table rendering shared by the JSONL parity runner and interactive UI.
use crate::QueryResult;
use serde_json::Value;
use std::collections::BTreeSet;

fn cell_text(value: &Value) -> String {
    if crate::pg_read::json_envelope(value) {
        return crate::pg_read::json_payload(value).to_string();
    }
    if let Some(text) = crate::pg_read::scalar_text(value) {
        return text.into();
    }

    match value {
        Value::Null => "NULL".into(),
        Value::String(s) => s.clone(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        Value::Object(map) if map.contains_key("$blob") => format!(
            "<blob:{} bytes>",
            map.get("$blob").and_then(Value::as_str).unwrap_or("").len() * 3 / 4
        ),
        other => other.to_string(),
    }
}
fn is_json_preview(s: &str) -> bool {
    (s.starts_with('{') && s.ends_with('}')) || (s.starts_with('[') && s.ends_with(']'))
}
fn json_preview(s: &str) -> String {
    if s.chars().count() > 20 {
        let chars: Vec<char> = s.chars().collect();
        format!(
            "{}...{}",
            chars[..14].iter().collect::<String>(),
            chars[chars.len() - 3..].iter().collect::<String>()
        )
    } else {
        s.into()
    }
}
fn visible_width(value: &Value, max_width: usize) -> usize {
    if value.is_null() || value.as_str() == Some("") {
        return 4;
    }
    let s = cell_text(value).replace('\n', " ").trim().to_owned();
    if is_json_preview(&s) {
        return json_preview(&s).chars().count();
    }
    s.chars().count().min(max_width)
}
fn all_column_widths(headers: &[String], rows: &[Vec<Value>]) -> Vec<usize> {
    headers
        .iter()
        .enumerate()
        .map(|(c, h)| {
            rows.iter()
                .fold(h.chars().count(), |w, row| {
                    w.max(row.get(c).map(|v| visible_width(v, 35)).unwrap_or(0))
                })
                .min(35)
        })
        .collect()
}
fn fitted_columns_from(
    headers: &[String],
    rows: &[Vec<Value>],
    start: usize,
    width: usize,
) -> (usize, usize, Vec<usize>) {
    if headers.is_empty() || rows.is_empty() {
        return (start, start + 1, vec![]);
    }
    let widths = all_column_widths(headers, rows);
    let target = width.saturating_sub(2).max(20);
    let mut used = 1usize;
    let mut end = start.min(headers.len());
    while end < headers.len() {
        let w = widths[end].min(target.saturating_sub(3));
        if used + w + 3 > target && end > start {
            break;
        }
        used += w + 3;
        end += 1;
    }
    (
        start,
        end,
        widths[start..end]
            .iter()
            .map(|w| (*w).min(target.saturating_sub(3)))
            .collect(),
    )
}
fn pad(text: &str, width: usize) -> String {
    format!("{text:<width$}")
}
fn format_cell(value: &Value, width: usize) -> String {
    if value.is_null() || value.as_str() == Some("") {
        return pad("NULL", width);
    }
    let s = cell_text(value).replace('\n', " ").trim().to_owned();
    let display = if is_json_preview(&s) {
        json_preview(&s)
    } else {
        s.clone()
    };
    if display.chars().count() > width {
        let truncated = if width > 3 {
            format!("{}...", display.chars().take(width - 3).collect::<String>())
        } else {
            display.chars().take(width).collect()
        };
        pad(&truncated, width)
    } else {
        pad(&display, width)
    }
}
fn table_lines(
    headers: &[String],
    rows: &[Vec<Value>],
    widths: &[usize],
    cursor: usize,
    focused: Option<usize>,
    marked: &BTreeSet<usize>,
    sort: Option<&[String]>,
    ansi: bool,
) -> Vec<String> {
    if headers.is_empty() || rows.is_empty() || widths.is_empty() {
        return vec!["   (No data found)".into()];
    }
    let sep = format!(
        "+{}+",
        widths
            .iter()
            .map(|w| "-".repeat(w + 2))
            .collect::<Vec<_>>()
            .join("+")
    );
    let mut lines = vec![sep.clone()];
    let mut header_cells = Vec::new();
    for (i, h) in headers.iter().enumerate() {
        let mut label = h.clone();
        if let Some(s) = sort {
            if s.first().is_some_and(|c| c == h) {
                label.push_str(if s.get(1).is_some_and(|d| d == "ASC") {
                    " ▲"
                } else {
                    " ▼"
                });
            }
        }
        let text = pad(&label, widths[i]);
        header_cells.push(if ansi && focused == Some(i) {
            format!("\x1b[7m\x1b[1m{text}\x1b[0m")
        } else {
            text
        });
    }
    lines.push(format!("| {} |", header_cells.join(" | ")));
    lines.push(sep.replace('-', "="));
    for (ri, row) in rows.iter().enumerate() {
        let mut cells: Vec<String> = (0..headers.len())
            .map(|ci| format_cell(row.get(ci).unwrap_or(&Value::Null), widths[ci]))
            .collect();
        if ansi && marked.contains(&ri) && !cells.is_empty() {
            cells[0] = format!("\x1b[92m\x1b[1m{}\x1b[0m", cells[0]);
        }
        let line = format!("| {} |", cells.join(" | "));
        lines.push(if ansi && ri == cursor {
            format!("\x1b[7m\x1b[1m{line}\x1b[0m")
        } else {
            line
        });
    }
    lines.push(sep);
    lines
}

/// Fixed-dimension, ANSI-free Python-compatible production data-browser screen.
pub fn render_browser(
    result: &QueryResult,
    table: &str,
    count: i64,
    offset: i64,
    width: usize,
    height: usize,
    filter: Option<&str>,
    sort: Option<&[String]>,
    _read_only: bool,
) -> String {
    render_browser_state(
        result,
        table,
        count,
        offset,
        width,
        height,
        filter,
        sort,
        0,
        0,
        0,
        &BTreeSet::new(),
        false,
        false,
    )
}
/// Render the same production screen with interactive cursor/column/mark state.
pub fn render_browser_state(
    result: &QueryResult,
    table: &str,
    count: i64,
    offset: i64,
    width: usize,
    height: usize,
    filter: Option<&str>,
    sort: Option<&[String]>,
    cursor: usize,
    focused_col: usize,
    col_offset: usize,
    marked: &BTreeSet<usize>,
    ansi: bool,
    read_only: bool,
) -> String {
    let mut start = col_offset.min(result.headers.len().saturating_sub(1));
    if focused_col < start {
        start = focused_col;
    }
    let (mut c_start, mut end, mut widths) =
        fitted_columns_from(&result.headers, &result.rows, start, width);
    while focused_col >= end && start < result.headers.len().saturating_sub(1) {
        start += 1;
        (c_start, end, widths) = fitted_columns_from(&result.headers, &result.rows, start, width);
    }
    let headers = &result.headers[c_start..end.min(result.headers.len())];
    let rows: Vec<Vec<Value>> = result
        .rows
        .iter()
        .map(|r| r[c_start..end.min(r.len())].to_vec())
        .collect();
    let col_status = if headers.is_empty() {
        "Cols 0-0".into()
    } else {
        format!("Cols {}-{}/{}", c_start + 1, end, result.headers.len())
    };
    let sort_status = sort
        .filter(|s| s.len() == 2)
        .map(|s| format!(" [Sort: {} {}]", s[0], s[1]))
        .unwrap_or_default();
    let filter_status = filter
        .filter(|s| !s.is_empty())
        .map(|f| format!(" (Filter: '{f}')"))
        .unwrap_or_default();
    let row_start = offset + 1;
    let row_end = offset + result.rows.len() as i64;
    let mut lines=vec![format!(" Data Browser: {table} [Rows {row_start}-{row_end} of {count}] [{col_status}]{sort_status}{filter_status}")];
    let focused = if focused_col >= c_start && focused_col < end {
        Some(focused_col - c_start)
    } else {
        None
    };
    lines.extend(table_lines(
        headers, &rows, &widths, cursor, focused, marked, sort, ansi,
    ));
    let mut help=" Vim Nav: j/k Up/Down | l/Enter Card | H/L Focus | gg/G Jump | a Add | e Edit | E Bulk Edit | dd Del | o/O Sort | v Visibility | SPACE Mark | f FK | S/I Schema/Indexes | : SQL/Jump | x/X Export | h/q Back".to_string();
    if read_only {
        help = help.replace(" | a Add | e Edit | E Bulk Edit | dd Del", "");
    }
    lines.push(help);
    lines.truncate(height);
    lines.join("\n")
}
