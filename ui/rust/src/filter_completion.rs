//! Cursor-local filter completion from metadata and the already-loaded page only.
use crate::data::{self, Meta, Page};
use serde_json::Value;

#[derive(Clone, Debug)]
pub struct Item {
    pub label: String,
    pub replacement: String,
    pub kind: &'static str,
}
#[derive(Clone, Debug, Default)]
pub struct Suggestions {
    pub start: usize,
    pub end: usize,
    pub items: Vec<Item>,
    pub selected: usize,
}
impl Suggestions {
    pub fn apply(&self, text: &mut String, cursor: &mut usize) {
        let Some(item) = self.items.get(self.selected) else {
            return;
        };
        let mut chars: Vec<_> = text.chars().collect();
        if self.start > self.end || self.end > chars.len() {
            return;
        }
        chars.splice(self.start..self.end, item.replacement.chars());
        *cursor = self.start + item.replacement.chars().count();
        *text = chars.into_iter().collect();
    }
    pub fn move_selection(&mut self, forward: bool) {
        if self.items.is_empty() {
            return;
        }
        self.selected = if forward {
            (self.selected + 1) % self.items.len()
        } else {
            (self.selected + self.items.len() - 1) % self.items.len()
        };
    }
}
#[derive(Clone, Debug)]
struct Token {
    start: usize,
    end: usize,
    text: String,
    quote: Option<char>,
}
fn tokens(chars: &[char]) -> Vec<Token> {
    let mut out = Vec::new();
    let mut i = 0;
    while i < chars.len() {
        if chars[i].is_whitespace() {
            i += 1;
            continue;
        }
        let start = i;
        let quote = matches!(chars[i], '\'' | '"').then_some(chars[i]);
        i += 1;
        if let Some(q) = quote {
            while i < chars.len() {
                if chars[i] == q {
                    i += 1;
                    if i < chars.len() && chars[i] == q {
                        i += 1;
                        continue;
                    }
                    break;
                }
                i += 1;
            }
        } else if "=<>!".contains(chars[start]) {
            while i < chars.len() && "=<>!".contains(chars[i]) {
                i += 1;
            }
        } else if !"(),".contains(chars[start]) {
            while i < chars.len() && !chars[i].is_whitespace() && !"=<>!(),\"'".contains(chars[i]) {
                i += 1;
            }
        }
        out.push(Token {
            start,
            end: i,
            text: chars[start..i].iter().collect(),
            quote,
        });
    }
    out
}
fn unquote(value: &str, quote: Option<char>) -> String {
    if let Some(q) = quote {
        let value = value.strip_prefix(q).unwrap_or(value);
        let value = value.strip_suffix(q).unwrap_or(value);
        value.replace(&format!("{q}{q}"), &q.to_string())
    } else {
        value.to_string()
    }
}
fn identifier(token: &Token, headers: &[String]) -> Option<String> {
    if token.quote == Some('\'') {
        return None;
    }
    let name = unquote(&token.text, token.quote);
    headers
        .iter()
        .find(|h| h.eq_ignore_ascii_case(&name))
        .cloned()
}
fn matches_prefix(prefix: &str, candidate: &str) -> bool {
    candidate.to_lowercase().starts_with(&prefix.to_lowercase())
}

pub fn suggest(text: &str, cursor: usize, page: &Page, meta: &Meta, engine: &str) -> Suggestions {
    let chars: Vec<_> = text.chars().collect();
    let cursor = cursor.min(chars.len());
    let tokens = tokens(&chars);
    let current = tokens.iter().find(|t| t.start <= cursor && cursor <= t.end);
    let (start, end, prefix, quote) = if let Some(t) = current {
        (
            t.start,
            t.end,
            chars[t.start..cursor].iter().collect::<String>(),
            t.quote,
        )
    } else {
        (cursor, cursor, String::new(), None)
    };
    let before: Vec<_> = tokens.iter().filter(|t| t.end <= start).collect();
    let mut headers = page.headers.clone();
    if headers.is_empty() {
        headers = meta.types.keys().cloned().collect();
        headers.sort();
    }
    let last = before.last().copied();
    let is_word = |t: &Token, word: &str| t.quote.is_none() && t.text.eq_ignore_ascii_case(word);
    let operator = |t: &Token| {
        t.quote.is_none()
            && [
                "=", "!=", "<>", "<", ">", "<=", ">=", "LIKE", "ILIKE", "IN", "IS", "BETWEEN",
            ]
            .iter()
            .any(|op| t.text.eq_ignore_ascii_case(op))
    };
    let column_at_end = last.and_then(|t| identifier(t, &headers));
    let column_for_value = before.iter().rev().find_map(|t| identifier(t, &headers));
    let after_is = last.is_some_and(|t| is_word(t, "IS"));
    let after_is_not = last.is_some_and(|t| is_word(t, "NOT"))
        && before.iter().rev().nth(1).is_some_and(|t| is_word(t, "IS"));
    let after_not = last.is_some_and(|t| is_word(t, "NOT")) && !after_is_not;
    let between_value = last.is_some_and(|t| is_word(t, "AND"))
        && before
            .iter()
            .rposition(|t| is_word(t, "BETWEEN"))
            .is_some_and(|at| before[at..].iter().filter(|t| is_word(t, "AND")).count() == 1);
    let expecting_value = last.is_some_and(|t| {
        operator(t)
            || after_is_not
            || between_value
            || t.text == "(" && before.iter().rev().skip(1).any(|t| is_word(t, "IN"))
            || t.text == "," && before.iter().any(|t| is_word(t, "IN"))
    });
    let expecting_column = before.is_empty()
        || last.is_some_and(|t| {
            !expecting_value && (is_word(t, "AND") || is_word(t, "OR") || t.text == "(")
        });
    let mut out = Suggestions {
        start,
        end,
        ..Suggestions::default()
    };
    let suffix_has_space = chars.get(end).is_some_and(|c| c.is_whitespace());
    let spaced = |s: &str| {
        if suffix_has_space {
            s.to_string()
        } else {
            format!("{s} ")
        }
    };
    let mut push = |label: String, replacement: String, kind| {
        if !out.items.iter().any(|i| i.replacement == replacement) && out.items.len() < 40 {
            out.items.push(Item {
                label: label.replace(['\n', '\r', '\t'], " "),
                replacement,
                kind,
            });
        }
    };
    if expecting_column && quote != Some('\'') {
        let prefix = unquote(&prefix, quote);
        for h in &headers {
            if matches_prefix(&prefix, h) {
                if let Ok(name) = data::quote(h) {
                    push(h.clone(), spaced(&name), "column");
                }
            }
        }
    }
    // Single-word plain searches can also complete cached text without turning
    // them into SQL literals or expressions. Predicate completion stays separate.
    if before.is_empty() && quote.is_none() && !chars.iter().any(|c| c.is_whitespace()) {
        for row in page.rows.iter().take(50) {
            for value in row.iter().take(64).filter_map(Value::as_str) {
                if value.len() <= 160
                    && !value.chars().any(|c| c.is_whitespace())
                    && !data::expression(value)
                    && matches_prefix(&prefix, value)
                {
                    push(value.into(), value.into(), "cached text");
                }
            }
        }
    }
    if after_not && quote.is_none() {
        let mut operators = vec!["LIKE", "IN (", "BETWEEN"];
        if engine == "postgres" {
            operators.push("ILIKE");
        }
        for op in operators {
            if matches_prefix(&prefix, op) {
                push(op.into(), spaced(op), "predicate");
            }
        }
    } else if column_at_end.is_some() && quote.is_none() {
        let mut operators = vec![
            "=",
            "!=",
            "<",
            "<=",
            ">",
            ">=",
            "LIKE",
            "NOT LIKE",
            "IN (",
            "IS NULL",
            "IS NOT NULL",
            "BETWEEN",
        ];
        if engine == "postgres" {
            operators.extend(["ILIKE", "NOT ILIKE"]);
        }
        for op in operators {
            if matches_prefix(&prefix, op) {
                push(op.into(), spaced(op), "predicate");
            }
        }
    } else if expecting_value {
        let needle = unquote(&prefix, quote);
        if let Some(column) = column_for_value
            .as_ref()
            .filter(|_| !after_is && !after_is_not)
        {
            if let Some(index) = page.headers.iter().position(|h| h == column) {
                for row in page.rows.iter().take(50) {
                    let Some(v) = row.get(index) else {
                        continue;
                    };
                    let bounded = match v {
                        Value::Null | Value::Bool(_) | Value::Number(_) => true,
                        Value::String(s) => s.len() <= 160,
                        _ => false,
                    };
                    if !bounded {
                        continue;
                    }
                    let display = data::text(v);
                    if matches_prefix(&needle, &display) {
                        let typ = meta.types.get(column).map(String::as_str).unwrap_or("");
                        if let Ok(literal) = data::sql_literal(v, engine, typ) {
                            push(literal.clone(), spaced(&literal), "cached value");
                        }
                    }
                }
            }
        }
        if quote.is_none() {
            let mut literals = vec!["NULL", "TRUE", "FALSE"];
            if after_is {
                literals.push("NOT NULL");
            }
            for value in literals {
                if matches_prefix(&prefix, value) {
                    push(value.into(), spaced(value), "literal");
                }
            }
        }
    } else if !expecting_column && quote.is_none() {
        for word in ["AND", "OR", "IS NULL", "IS NOT NULL"] {
            if matches_prefix(&prefix, word) {
                push(word.into(), spaced(word), "keyword");
            }
        }
    }
    out
}
