//! Bounded JSON discovery from loaded data; no connections or database queries.
use crate::data::{self, Meta, Page};
use serde_json::Value;

#[derive(Clone, Debug)]
pub struct Field {
    pub column: String,
    pub selector: String,
    pub parent: String,
    pub branch: bool,
    pub label: String,
    pub expression: String,
    pub values: Vec<Value>,
}
#[derive(Clone)]
enum Part {
    Key(String),
    Index(usize),
}
struct Discovery {
    fields: Vec<Field>,
    nodes: usize,
    text_bytes: usize,
}
fn literal(text: &str, engine: &str) -> Option<String> {
    data::sql_literal(&Value::String(text.into()), engine, "text").ok()
}
fn expression(column: &str, path: &[Part], engine: &str) -> Option<String> {
    let column = data::quote(column).ok()?;
    if engine == "postgres" {
        let parts = path
            .iter()
            .map(|part| match part {
                Part::Key(key) => literal(key, engine),
                Part::Index(n) => literal(&n.to_string(), engine),
            })
            .collect::<Option<Vec<_>>>()?;
        return Some(format!("({column} #>> ARRAY[{}])", parts.join(", ")));
    }
    let mut selector = String::from("$");
    for part in path {
        match part {
            Part::Key(key) => {
                // Omit labels whose SQLite JSON-path escaping is ambiguous.
                if key.contains(['"', '\\']) || key.chars().any(char::is_control) {
                    return None;
                }
                selector.push('.');
                selector.push_str(&serde_json::to_string(key).ok()?);
            }
            Part::Index(n) => selector.push_str(&format!("[{n}]")),
        }
    }
    // A TEXT column may contain non-JSON rows outside the loaded page.
    Some(format!(
        "(CASE WHEN json_valid({column}) THEN json_extract({column}, {}) END)",
        literal(&selector, engine)?
    ))
}
fn label(column: &str, path: &[Part]) -> String {
    let mut out = column.to_string();
    for part in path {
        match part {
            Part::Key(key)
                if !key.is_empty() && key.chars().all(|c| c.is_alphanumeric() || c == '_') =>
            {
                out.push('.');
                out.push_str(key);
            }
            Part::Key(key) => out.push_str(&format!("[{}]", serde_json::to_string(key).unwrap())),
            Part::Index(n) => out.push_str(&format!("[{n}]")),
        }
    }
    out
}
impl Discovery {
    fn walk(&mut self, column: &str, value: &Value, path: &mut Vec<Part>, engine: &str) {
        if self.nodes == 0 || path.len() > 5 {
            return;
        }
        self.nodes -= 1;
        if !path.is_empty() {
            let Some(expression) = expression(column, path, engine) else {
                return;
            };
            let index = self.fields.iter().position(|f| f.expression == expression);
            let index = match index {
                Some(index) => index,
                None if self.fields.len() < 128 => {
                    self.fields.push(Field {
                        column: column.into(),
                        selector: label(&data::quote(column).unwrap(), path),
                        parent: label(&data::quote(column).unwrap(), &path[..path.len() - 1]),
                        branch: false,
                        label: label(column, path),
                        expression,
                        values: vec![],
                    });
                    self.fields.len() - 1
                }
                None => return,
            };
            self.fields[index].branch |= matches!(value, Value::Object(_) | Value::Array(_));
            let scalar = match value {
                Value::Null | Value::Bool(_) | Value::Number(_) => Some(value.clone()),
                Value::String(s) if s.len() <= 160 => Some(value.clone()),
                _ => None,
            };
            if let Some(value) = scalar {
                // PostgreSQL #>> returns text even for JSON numbers and booleans.
                let value = if engine == "postgres" && !value.is_null() {
                    Value::String(match value {
                        Value::String(s) => s,
                        v => v.to_string(),
                    })
                } else {
                    value
                };
                let values = &mut self.fields[index].values;
                if values.len() < 12 && !values.contains(&value) {
                    values.push(value);
                }
            }
        }
        if path.len() == 5 {
            return;
        }
        match value {
            Value::Object(object) => {
                for (key, child) in object.iter().take(32) {
                    if key.len() > 128 {
                        continue;
                    }
                    path.push(Part::Key(key.clone()));
                    self.walk(column, child, path, engine);
                    path.pop();
                    if self.nodes == 0 {
                        break;
                    }
                }
            }
            Value::Array(array) => {
                for (index, child) in array.iter().take(8).enumerate() {
                    path.push(Part::Index(index));
                    self.walk(column, child, path, engine);
                    path.pop();
                    if self.nodes == 0 {
                        break;
                    }
                }
            }
            _ => {}
        }
    }
}
pub fn discover(page: &Page, meta: &Meta, engine: &str) -> Vec<Field> {
    let mut discovery = Discovery {
        fields: vec![],
        nodes: 2048,
        text_bytes: 256 * 1024,
    };
    for row in page.rows.iter().take(50) {
        for (column, value) in page.headers.iter().zip(row).take(64) {
            let declared = meta
                .types
                .get(column)
                .is_some_and(|t| data::is_json_type(t));
            if engine == "postgres" {
                if declared {
                    discovery.walk(column, data::json_payload(value), &mut vec![], engine);
                }
            } else if let Some(text) = value.as_str() {
                if text.len() > 64 * 1024 || text.len() > discovery.text_bytes {
                    continue;
                }
                let trimmed = text.trim_start();
                if !trimmed.starts_with(['{', '[']) {
                    continue;
                }
                discovery.text_bytes -= text.len();
                if let Ok(value) = serde_json::from_str::<Value>(text) {
                    discovery.walk(column, &value, &mut vec![], engine);
                }
            }
            if discovery.nodes == 0 {
                return discovery.fields;
            }
        }
    }
    discovery.fields
}
