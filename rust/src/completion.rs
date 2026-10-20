//! Bounded contextual completion used by SQL/filter prompts; never executes text.
use serde_json::Value;
fn lit(v: &Value) -> Option<String> {
    match v {
        Value::Null => Some("NULL".into()),
        Value::String(s) => Some(format!("'{}'", s.replace('\'', "''"))),
        Value::Number(_) | Value::Bool(_) => Some(v.to_string()),
        _ => None,
    }
}
fn json_paths(out: &mut Vec<String>, column: &str, v: &Value, path: &str, depth: usize) {
    if depth > 3 || out.len() > 1000 {
        return;
    }
    match v {
        Value::Object(m) => {
            for (k, v) in m.iter().take(24) {
                let next = format!("{path}.\"{}\"", k.replace('"', "\\\""));
                out.push(format!(
                    "json_extract(\"{}\", '{}')",
                    column.replace('"', "\"\""),
                    next.replace('\'', "''")
                ));
                out.push(format!("'{}'", k.replace('\'', "''")));
                if let Some(l) = lit(v) {
                    out.push(l)
                }
                json_paths(out, column, v, &next, depth + 1)
            }
        }
        Value::Array(a) => {
            for (i, v) in a.iter().take(5).enumerate() {
                let next = format!("{path}[{i}]");
                out.push(format!(
                    "json_extract(\"{}\", '{}')",
                    column.replace('"', "\"\""),
                    next.replace('\'', "''")
                ));
                if let Some(value) = lit(v) {
                    out.push(value)
                }
                json_paths(out, column, v, &next, depth + 1);
            }
        }
        _ => {}
    }
}
pub fn candidates(headers: &[String], rows: &[Vec<Value>], tables: &[String]) -> Vec<String> {
    let mut out = headers.to_vec();
    out.extend(tables.iter().cloned());
    out.extend(
        [
            "SELECT",
            "SELECT * FROM",
            "FROM",
            "WHERE",
            "ORDER BY",
            "GROUP BY",
            "LIMIT",
            "OFFSET",
            "AND",
            "OR",
            "IS NULL",
            "IS NOT NULL",
            "LIKE",
            "IN",
            "JOIN",
            "LEFT JOIN",
            "ON",
            "ASC",
            "DESC",
        ]
        .iter()
        .map(|s| s.to_string()),
    );
    for row in rows.iter().take(20) {
        for (h, v) in headers.iter().zip(row) {
            if let Some(l) = lit(v) {
                if l.len() < 200 {
                    out.push(l)
                }
            }
            let decoded = v
                .as_str()
                .and_then(|s| serde_json::from_str::<Value>(s).ok());
            if let Some(json) = decoded {
                if json.is_object() || json.is_array() {
                    json_paths(&mut out, h, &json, "$", 0)
                }
            }
        }
    }
    out.sort();
    out.dedup();
    out.truncate(1500);
    out
}
pub fn suggest<'a>(prefix: &str, candidates: &'a [String]) -> Option<&'a str> {
    let lower = prefix.to_lowercase();
    candidates
        .iter()
        .find(|s| s.to_lowercase().starts_with(&lower))
        .or_else(|| {
            candidates
                .iter()
                .filter(|s| {
                    let mut chars = lower.chars();
                    let mut wanted = chars.next();
                    for c in s.to_lowercase().chars() {
                        if wanted == Some(c) {
                            wanted = chars.next()
                        }
                    }
                    wanted.is_none()
                })
                .min_by_key(|s| s.len())
        })
        .map(String::as_str)
}

#[derive(Default)]
pub struct State {
    last: String,
    base: String,
    start: usize,
    matches: Vec<String>,
    index: usize,
}
impl State {
    pub fn next(&mut self, text: &str, candidates: &[String]) -> String {
        if text == self.last && !self.matches.is_empty() {
            self.index = (self.index + 1) % self.matches.len();
        } else {
            let mut start = text
                .rfind(|c: char| c.is_whitespace() || c == '(' || c == ',')
                .map(|i| i + 1)
                .unwrap_or(0);
            if let Some(join) = text.to_ascii_uppercase().rfind("JOIN ") {
                start = join;
            }
            let prefix = &text[start..];
            let lower = prefix.to_lowercase();
            self.base = text.to_string();
            self.start = start;
            self.index = 0;
            self.matches = candidates
                .iter()
                .filter(|s| s.to_lowercase().starts_with(&lower))
                .cloned()
                .collect();
            if self.matches.is_empty() {
                self.matches = candidates
                    .iter()
                    .filter(|s| {
                        let mut needle = lower.chars();
                        let mut next = needle.next();
                        for c in s.to_lowercase().chars() {
                            if Some(c) == next {
                                next = needle.next()
                            }
                        }
                        next.is_none()
                    })
                    .cloned()
                    .collect();
            }
            self.matches.sort_by_key(|s| s.len());
        }
        if self.matches.is_empty() {
            return text.into();
        }
        let mut result = self.base[..self.start].to_string();
        result.push_str(&self.matches[self.index]);
        self.last = result.clone();
        result
    }
}
pub fn discover(dir: &std::path::Path) -> Vec<std::path::PathBuf> {
    use std::io::Read;
    let mut paths = vec![];
    if let Ok(entries) = std::fs::read_dir(dir) {
        for entry in entries.flatten() {
            let path = entry.path();
            if !path.is_file() {
                continue;
            }
            let mut signature = [0; 16];
            if std::fs::File::open(&path)
                .and_then(|mut f| f.read_exact(&mut signature))
                .is_ok()
                && &signature == b"SQLite format 3\0"
            {
                paths.push(path)
            }
        }
    }
    paths.sort();
    paths
}
#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    #[test]
    fn columns_values_json_paths_fuzzy_and_cycles() {
        let candidates = candidates(
            &["payload".into(), "leading".into()],
            &[vec![
                json!("{\"name\":\"名\",\"nested\":{\"x\":1}}"),
                json!("00123"),
            ]],
            &["tasks".into()],
        );
        assert!(candidates.contains(&"'00123'".into()));
        assert!(candidates
            .iter()
            .any(|s| s.contains("json_extract") && s.contains("nested")));
        assert_eq!(suggest("pyld", &candidates), Some("payload"));
        let mut state = State::default();
        let c = vec!["SELECT".into(), "SELECT * FROM".into()];
        let first = state.next("SEL", &c);
        assert_eq!(first, "SELECT");
        assert_eq!(state.next(&first, &c), "SELECT * FROM");
    }
    #[test]
    fn fk_join_completion_keeps_on_clause() {
        let mut state = State::default();
        let cs = vec!["JOIN parent ON child.parent_id = parent.id".into()];
        assert_eq!(
            state.next("SELECT * FROM child JOIN pa", &cs),
            "SELECT * FROM child JOIN parent ON child.parent_id = parent.id"
        );
    }
}

pub fn fuzzy(query: &str, target: &str) -> bool {
    let query = query.to_lowercase();
    let mut chars = query.chars();
    let mut next = chars.next();
    for c in target.to_lowercase().chars() {
        if next == Some(c) {
            next = chars.next()
        }
    }
    next.is_none()
}
