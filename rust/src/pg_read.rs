//! Native PostgreSQL read adapter shared by the interactive and protocol paths.
//! No connection strings are accepted in commands or formatted into diagnostics.
#[path = "pg_write.rs"]
mod write;
use chrono::{NaiveDate, NaiveDateTime, NaiveTime, Timelike};
use postgres::fallible_iterator::FallibleIterator;
use postgres::{
    types::{FromSql, IsNull, Kind, ToSql, Type},
    Client, Config, NoTls,
};
use serde_json::{json, Value};
use std::{error::Error as StdError, str::FromStr, time::Duration};

#[derive(Debug, Clone)]
pub struct Error {
    pub kind: &'static str,
    pub message: String,
}
pub type Result<T> = std::result::Result<T, Error>;
fn error(kind: &'static str, message: impl Into<String>) -> Error {
    Error {
        kind,
        message: message.into(),
    }
}
fn db(e: postgres::Error) -> Error {
    error(
        "AdapterError",
        format!(
            "PostgreSQL operation failed (SQLSTATE {})",
            e.code().map(|c| c.code()).unwrap_or("driver")
        ),
    )
}
impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.message)
    }
}
impl StdError for Error {}
#[derive(Clone, Copy)]
pub enum BlobStyle {
    Hex,
    Base64,
    TypedBase64,
}
#[derive(Clone)]
pub struct Reader {
    pub blob: BlobStyle,
    pub read_only: bool,
}
impl Reader {
    pub fn new(blob: BlobStyle) -> Self {
        Self {
            blob,
            read_only: false,
        }
    }
    pub fn with_read_only(mut self, read_only: bool) -> Self {
        self.read_only = read_only;
        self
    }
    fn connect(&self) -> Result<Client> {
        self.connect_mode(true)
    }
    fn connect_mode(&self, read_only: bool) -> Result<Client> {
        let url = std::env::var("DBCLI_POSTGRES_URL")
            .or_else(|_| std::env::var("DBCLI_BENCH_POSTGRES_URL"))
            .or_else(|_| std::env::var("DBCLI_BENCH_DATABASE_URL"))
            .map_err(|_| error("AdapterError", "DBCLI_POSTGRES_URL is required"))?;
        let config = configuration_mode(&url, read_only)?;
        let mut client = config.connect(NoTls).map_err(db)?;
        client
            .batch_execute(if read_only {
                "BEGIN READ ONLY"
            } else {
                "BEGIN READ WRITE"
            })
            .map_err(db)?;
        let row=client.query_one("SELECT current_setting('transaction_read_only'),current_setting('default_transaction_read_only'),current_setting('statement_timeout'),current_setting('lock_timeout'),current_setting('TimeZone')",&[]).map_err(db)?;
        let settings = (
            row.get::<_, String>(0),
            row.get::<_, String>(1),
            row.get::<_, String>(2),
            row.get::<_, String>(3),
            row.get::<_, String>(4),
        );
        if settings.0 != if read_only { "on" } else { "off" }
            || settings.1 != if read_only { "on" } else { "off" }
            || milliseconds(&settings.2).is_none_or(|n| n == 0 || n > 3000)
            || milliseconds(&settings.3).is_none_or(|n| n == 0 || n > 1000)
            || settings.4 != "UTC"
        {
            return Err(error(
                "ReadOnlyError",
                "PostgreSQL session safety settings were not applied",
            ));
        }
        Ok(client)
    }
    pub fn sql_export(
        &self,
        table: &str,
        headers: &[String],
        rows: &[Vec<Value>],
        raw: Option<&str>,
    ) -> Result<String> {
        let mut c = self.connect()?;
        let (target, sql) = if let Some(sql) = raw {
            check_read_query(sql.trim().trim_end_matches(';'))?;
            (
                quote("query_result")?,
                sql.trim().trim_end_matches(';').to_string(),
            )
        } else {
            let reference = qualified(table)?.2;
            (
                reference.clone(),
                format!("SELECT * FROM {reference} LIMIT 0"),
            )
        };
        let stmt = c.prepare(&sql).map_err(db)?;
        let columns = stmt.columns();
        let mut types = vec![];
        for header in headers {
            types.push(
                columns
                    .iter()
                    .find(|c| c.name() == header)
                    .ok_or_else(|| error("AdapterError", "Export column missing from query"))?
                    .type_()
                    .clone(),
            )
        }
        let attributes = if raw.is_none() {
            write::columns(&mut c, table)?
        } else {
            vec![]
        };
        render_sql_export(&target, headers, rows, &types, &attributes)
    }

    pub fn completions(
        &self,
        table: &str,
        headers: &[String],
        rows: &[Vec<Value>],
        raw: Option<&str>,
    ) -> Result<Vec<String>> {
        let mut c = self.connect()?;
        let sql = if let Some(sql) = raw {
            check_read_query(sql.trim().trim_end_matches(';'))?;
            sql.trim().trim_end_matches(';').to_string()
        } else {
            format!("SELECT * FROM {} LIMIT 0", qualified(table)?.2)
        };
        let stmt = c.prepare(&sql).map_err(db)?;
        let mut output = vec![];
        fn walk(out: &mut Vec<String>, expr: &str, v: &Value, depth: usize) {
            if depth > 3 || out.len() > 1000 {
                return;
            }
            match v {
                Value::Object(m) => {
                    for (k, v) in m.iter().take(24) {
                        let k = format!("'{}'", k.replace('\'', "''"));
                        let next = format!("({expr}->{k})");
                        out.push(next.clone());
                        out.push(format!("({expr}->>{k})"));
                        walk(out, &next, v, depth + 1)
                    }
                }
                Value::Array(a) => {
                    for (i, v) in a.iter().take(5).enumerate() {
                        let next = format!("({expr}->{i})");
                        out.push(next.clone());
                        out.push(format!("({expr}->>{i})"));
                        walk(out, &next, v, depth + 1)
                    }
                }
                _ => {
                    if let Ok(l) = sql_literal(v) {
                        out.push(l)
                    }
                }
            }
        }
        for (i, h) in headers.iter().enumerate() {
            let ty = stmt
                .columns()
                .iter()
                .find(|c| c.name() == h)
                .map(|c| c.type_());
            for row in rows.iter().take(20) {
                if let Some(v) = row.get(i) {
                    if scalar_text(v).is_some() {
                        output.push(sql_literal(v)?)
                    }
                    if ty.is_some_and(|t| *t == Type::JSON || *t == Type::JSONB) {
                        walk(&mut output, &quote(h)?, json_payload(v), 0)
                    }
                }
            }
        }
        output.sort();
        output.dedup();
        output.truncate(1500);
        Ok(output)
    }
    pub fn operation(&self, op: &Value) -> Result<Value> {
        let name = op["op"].as_str().unwrap_or("");
        let table = op["table"].as_str().unwrap_or("items");
        if [
            "insert",
            "update",
            "delete",
            "insert_row",
            "update_row",
            "delete_row",
            "apply_batch",
            "bulk",
        ]
        .contains(&name)
        {
            let actions = if name == "apply_batch" || name == "bulk" {
                op["actions"]
                    .as_array()
                    .cloned()
                    .ok_or_else(|| error("ValueError", "actions must be an array"))?
            } else {
                let kind = name.trim_end_matches("_row");
                vec![
                    json!({"kind":kind,"row":op.get("row").filter(|v|!v.is_null()).or_else(||op.get("updated")).cloned().unwrap_or_else(||json!({})),"original":op.get("original").cloned().unwrap_or(Value::Null)}),
                ]
            };
            return Ok(json!(write::apply(self, table, &actions)?));
        }
        let mut c = self.connect_mode(true)?;
        let query = |c: &mut Client, sql: &str, params: &[Value], blob, window| {
            query_mode(
                c,
                sql,
                params,
                blob,
                window,
                op["json_envelopes"].as_bool().unwrap_or(false),
            )
        };
        let table = op["table"].as_str().unwrap_or("items");
        let limit = integer(op, "limit", 50)?;
        let offset = integer(op, "offset", 0)?;
        if limit < 1 || limit > 1_000_000 || offset < 0 {
            return Err(error(
                "ValueError",
                "Invalid page bounds (limit 1..1000000, offset nonnegative)",
            ));
        }
        let filter = match op.get("filter") {
            None | Some(Value::Null) => None,
            Some(Value::String(s)) => Some(s.as_str()),
            _ => return Err(error("ValueError", "filter must be text or null")),
        };
        let sort = op
            .get("order_by")
            .or_else(|| op.get("sort"))
            .filter(|v| !v.is_null());
        match name {
            "write_columns" | "insert_columns" => {
                let cols = write::columns(&mut c, table)?;
                if name == "insert_columns" {
                    Ok(json!(cols
                        .into_iter()
                        .filter(|c| !c.generated && !c.default && c.identity.is_empty())
                        .map(|c| c.name)
                        .collect::<Vec<_>>()))
                } else {
                    Ok(json!(cols.iter().map(|c|json!({"name":c.name,"generated":c.generated,"identity":c.identity,"has_default":c.default})).collect::<Vec<_>>()))
                }
            }
            "version" => query(
                &mut c,
                "SELECT current_setting('server_version') AS version",
                &[],
                self.blob,
                None,
            ),
            "list_databases" => {
                let r = query(&mut c, "SELECT current_database()", &[], self.blob, None)?;
                Ok(json!(r["rows"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|r| r[0].clone())
                    .collect::<Vec<_>>()))
            }
            "schemas" => {
                let r=query(&mut c,"SELECT nspname FROM pg_catalog.pg_namespace WHERE nspname !~ '^pg_' AND nspname <> 'information_schema' ORDER BY nspname",&[],self.blob,None)?;
                Ok(json!(r["rows"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|r| r[0].clone())
                    .collect::<Vec<_>>()))
            }
            "tables" | "list_tables" => {
                let views = op["include_views"].as_bool().unwrap_or(false);
                let sql = if views {
                    "SELECT table_schema,table_name FROM information_schema.tables WHERE table_schema !~ '^pg_' AND table_schema <> 'information_schema' AND table_type IN ('BASE TABLE','VIEW') ORDER BY table_schema,table_name"
                } else {
                    "SELECT table_schema,table_name FROM information_schema.tables WHERE table_schema !~ '^pg_' AND table_schema <> 'information_schema' AND table_type='BASE TABLE' ORDER BY table_schema,table_name"
                };
                let r = query(&mut c, sql, &[], self.blob, None)?;
                Ok(json!(r["rows"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|row| display_table(row[0].as_str().unwrap(), row[1].as_str().unwrap()))
                    .collect::<Vec<_>>()))
            }
            "schema" => {
                let (s, t, _) = qualified(table)?;
                let mut r=query(&mut c,"SELECT column_name,data_type,is_nullable,column_default FROM information_schema.columns WHERE table_schema=$1 AND table_name=$2 ORDER BY ordinal_position",&[json!(s),json!(t)],self.blob,None)?;
                if r["rows"].as_array().unwrap().is_empty() {
                    return Err(error("AdapterError", "Unknown PostgreSQL table"));
                }
                r["headers"] = json!(["Column Name", "Type", "Nullable", "Default"]);
                Ok(r)
            }
            "primary_keys" => {
                let (s, t, _) = qualified(table)?;
                let r=query(&mut c,"SELECT a.attname FROM pg_catalog.pg_index i JOIN pg_catalog.pg_class rel ON rel.oid=i.indrelid JOIN pg_catalog.pg_namespace ns ON ns.oid=rel.relnamespace CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY k(attnum,ord) JOIN pg_catalog.pg_attribute a ON a.attrelid=rel.oid AND a.attnum=k.attnum WHERE ns.nspname=$1 AND rel.relname=$2 AND i.indisprimary ORDER BY k.ord",&[json!(s),json!(t)],self.blob,None)?;
                Ok(json!(r["rows"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|r| r[0].clone())
                    .collect::<Vec<_>>()))
            }
            "foreign_keys" | "navigation_foreign_keys" => {
                let (s, t, _) = qualified(table)?;
                columns(&mut c, &s, &t, self.blob)?;
                let r=query(&mut c,"SELECT con.oid::bigint,src_col.attname,dst_ns.nspname,dst_rel.relname,dst_col.attname FROM pg_catalog.pg_constraint con JOIN pg_catalog.pg_class src_rel ON src_rel.oid=con.conrelid JOIN pg_catalog.pg_namespace src_ns ON src_ns.oid=src_rel.relnamespace JOIN pg_catalog.pg_class dst_rel ON dst_rel.oid=con.confrelid JOIN pg_catalog.pg_namespace dst_ns ON dst_ns.oid=dst_rel.relnamespace CROSS JOIN LATERAL unnest(con.conkey) WITH ORDINALITY src_key(attnum,ord) CROSS JOIN LATERAL unnest(con.confkey) WITH ORDINALITY dst_key(attnum,ord) JOIN pg_catalog.pg_attribute src_col ON src_col.attrelid=src_rel.oid AND src_col.attnum=src_key.attnum JOIN pg_catalog.pg_attribute dst_col ON dst_col.attrelid=dst_rel.oid AND dst_col.attnum=dst_key.attnum WHERE con.contype='f' AND src_ns.nspname=$1 AND src_rel.relname=$2 AND src_key.ord=dst_key.ord ORDER BY con.oid,src_key.ord",&[json!(s),json!(t)],self.blob,None)?;
                let mut out: Vec<Value> = vec![];
                for row in r["rows"].as_array().unwrap() {
                    let target = display_table(row[2].as_str().unwrap(), row[3].as_str().unwrap());
                    let pair = json!({"from_col":row[1],"to_col":row[4]});
                    if name == "navigation_foreign_keys" {
                        if let Some(existing) = out.iter_mut().find(|f| f["constraint"] == row[0]) {
                            existing["pairs"].as_array_mut().unwrap().push(pair);
                            continue;
                        }
                        out.push(json!({"from_col":row[1],"to_table":target,"to_col":row[4],"constraint":row[0],"pairs":[pair]}));
                    } else {
                        out.push(json!({"from_col":row[1],"to_table":target,"to_col":row[4]}));
                    }
                }
                Ok(json!(out))
            }
            "inspector" | "indexes" => {
                let (s, t, _) = qualified(table)?;
                columns(&mut c, &s, &t, self.blob)?;
                let r=query(&mut c,"SELECT 'index'::text AS kind,indexname::text AS name,indexdef::text AS definition FROM pg_catalog.pg_indexes WHERE schemaname=$1 AND tablename=$2 UNION ALL SELECT 'constraint',con.conname,pg_catalog.pg_get_constraintdef(con.oid,true) FROM pg_catalog.pg_constraint con JOIN pg_catalog.pg_class rel ON rel.oid=con.conrelid JOIN pg_catalog.pg_namespace ns ON ns.oid=rel.relnamespace WHERE ns.nspname=$1 AND rel.relname=$2 UNION ALL SELECT 'trigger',tr.tgname,pg_catalog.pg_get_triggerdef(tr.oid,true) FROM pg_catalog.pg_trigger tr JOIN pg_catalog.pg_class rel ON rel.oid=tr.tgrelid JOIN pg_catalog.pg_namespace ns ON ns.oid=rel.relnamespace WHERE ns.nspname=$1 AND rel.relname=$2 AND NOT tr.tgisinternal ORDER BY kind,name",&[json!(s),json!(t)],self.blob,None)?;
                Ok(r)
            }
            "page" | "fetch_page" | "count" | "count_rows" => {
                let (s, t, reference) = qualified(table)?;
                let cols = columns(&mut c, &s, &t, self.blob)?;
                let (w, mut p) = filter_sql(filter, &cols)?;
                if name == "count" || name == "count_rows" {
                    let r = query(
                        &mut c,
                        &format!("SELECT COUNT(*) FROM {reference} {w}"),
                        &p,
                        self.blob,
                        None,
                    )?;
                    return Ok(r["rows"][0][0].clone());
                }
                let order = order_sql(sort, &cols)?;
                let n = p.len();
                p.extend([json!(limit), json!(offset)]);
                query(
                    &mut c,
                    &format!(
                        "SELECT * FROM {reference} {w} {order} LIMIT ${} OFFSET ${}",
                        n + 1,
                        n + 2
                    ),
                    &p,
                    self.blob,
                    None,
                )
            }
            "raw" | "execute_readonly" | "raw_page" | "raw_count" => {
                let sql = op["sql"]
                    .as_str()
                    .ok_or_else(|| error("ValueError", "sql must be text"))?
                    .trim()
                    .trim_end_matches(';')
                    .trim();
                check_read_query(sql)?;
                let mut p = op
                    .get("params")
                    .and_then(Value::as_array)
                    .cloned()
                    .unwrap_or_default();
                let sql =
                    placeholders(sql, p.len(), op["bind_template"].as_bool().unwrap_or(false));
                if sql.trim_start().to_ascii_uppercase().starts_with("EXPLAIN") {
                    if filter.is_some_and(|f| !f.is_empty()) || sort.is_some() {
                        return Err(error(
                            "ValueError",
                            "EXPLAIN supports paging, but filter/sort require SELECT output",
                        ));
                    }
                    return query(&mut c, &sql, &p, self.blob, Some((offset, limit)));
                }
                let headers = c
                    .prepare(&sql)
                    .map_err(db)?
                    .columns()
                    .iter()
                    .map(|x| x.name().to_string())
                    .collect::<Vec<_>>();
                let (w, mut fp) = filter_sql(filter, &headers)?;
                let order = order_sql(sort, &headers)?;
                // Filter placeholders start after the raw query's original parameters.
                let shifted = shift_parameters(&w, p.len());
                p.append(&mut fp);
                let n = p.len();
                if name == "raw_count" {
                    let r = query(
                        &mut c,
                        &format!("SELECT COUNT(*) FROM ({sql}) _raw_count {shifted}"),
                        &p,
                        self.blob,
                        None,
                    )?;
                    return Ok(r["rows"][0][0].clone());
                }
                p.extend([json!(limit), json!(offset)]);
                query(
                    &mut c,
                    &format!(
                        "SELECT * FROM ({sql}) _dbcli_query {shifted} {order} LIMIT ${} OFFSET ${}",
                        n + 1,
                        n + 2
                    ),
                    &p,
                    self.blob,
                    None,
                )
            }
            _ => Err(error("ValueError", "Unsupported PostgreSQL read operation")),
        }
    }
}
fn configuration(url: &str) -> Result<Config> {
    configuration_mode(url, true)
}
fn configuration_mode(url: &str, read_only: bool) -> Result<Config> {
    let mut config = Config::from_str(url).map_err(|_| {
        error(
            "AdapterError",
            "Invalid PostgreSQL connection configuration",
        )
    })?;
    let previous = config.get_options().unwrap_or("").to_string();
    let statement = timeout_option(&previous, "statement_timeout", 3000);
    let lock = timeout_option(&previous, "lock_timeout", 1000);
    config.options(&format!("{previous} -c default_transaction_read_only={} -c statement_timeout={statement} -c lock_timeout={lock} -c TimeZone=UTC",if read_only{"on"}else{"off"}));
    let connect = config
        .get_connect_timeout()
        .copied()
        .filter(|d| !d.is_zero())
        .unwrap_or(Duration::from_secs(3))
        .min(Duration::from_secs(3));
    config.connect_timeout(connect);

    Ok(config)
}

fn milliseconds(s: &str) -> Option<u64> {
    let s = s.trim();
    if let Some(n) = s.strip_suffix("ms") {
        n.trim().parse().ok()
    } else if let Some(n) = s.strip_suffix('s') {
        n.trim().parse::<u64>().ok()?.checked_mul(1000)
    } else if let Some(n) = s.strip_suffix("min") {
        n.trim().parse::<u64>().ok()?.checked_mul(60000)
    } else {
        s.parse().ok()
    }
}
fn timeout_option(options: &str, key: &str, cap: u64) -> u64 {
    options
        .split_whitespace()
        .filter_map(|word| {
            word.trim_start_matches("-c")
                .strip_prefix(&format!("{key}="))
        })
        .filter_map(milliseconds)
        .filter(|n| *n > 0)
        .fold(cap, u64::min)
}
fn integer(op: &Value, name: &str, default: i64) -> Result<i64> {
    match op.get(name) {
        None | Some(Value::Null) => Ok(default),
        Some(Value::Number(n)) => n
            .as_i64()
            .ok_or_else(|| error("ValueError", "page bounds must be integers")),
        _ => Err(error("ValueError", "page bounds must be integers")),
    }
}
pub fn quote(s: &str) -> Result<String> {
    if s.is_empty() || s.contains('\0') {
        return Err(error("InvalidIdentifierError", "Empty or NUL identifier"));
    }
    Ok(format!("\"{}\"", s.replace('"', "\"\"")))
}
pub fn qualified(input: &str) -> Result<(String, String, String)> {
    let mut parts = vec![];
    let mut part = String::new();
    let mut chars = input.chars().peekable();
    let mut quoted = false;
    while let Some(c) = chars.next() {
        match c {
            '"' => {
                if quoted && chars.peek() == Some(&'"') {
                    part.push('"');
                    chars.next();
                } else {
                    quoted = !quoted;
                }
            }
            '.' if !quoted => {
                parts.push(part.trim().to_string());
                part.clear();
            }
            _ => part.push(c),
        }
    }
    if quoted {
        return Err(error(
            "InvalidIdentifierError",
            "Unclosed quoted identifier",
        ));
    }
    parts.push(part.trim().to_string());
    let (s, t) = match parts.as_slice() {
        [t] => ("public".to_string(), t.clone()),
        [s, t] => (s.clone(), t.clone()),
        _ => {
            return Err(error(
                "InvalidIdentifierError",
                "Expected table or schema.table",
            ))
        }
    };
    let reference = format!("{}.{}", quote(&s)?, quote(&t)?);
    Ok((s, t, reference))
}
fn display_component(s: &str) -> String {
    if !s.is_empty()
        && s.chars().all(|c| c.is_ascii_alphanumeric() || c == '_')
        && !s.chars().next().unwrap().is_ascii_digit()
    {
        s.into()
    } else {
        format!("\"{}\"", s.replace('"', "\"\""))
    }
}
pub fn display_table(schema: &str, table: &str) -> String {
    format!("{}.{}", display_component(schema), display_component(table))
}
fn columns(c: &mut Client, s: &str, t: &str, blob: BlobStyle) -> Result<Vec<String>> {
    let r=query(c,"SELECT column_name FROM information_schema.columns WHERE table_schema=$1 AND table_name=$2 ORDER BY ordinal_position",&[json!(s),json!(t)],blob,None)?;
    let cols = r["rows"]
        .as_array()
        .unwrap()
        .iter()
        .map(|r| r[0].as_str().unwrap().to_string())
        .collect::<Vec<_>>();
    if cols.is_empty() {
        Err(error("AdapterError", "Unknown PostgreSQL table"))
    } else {
        Ok(cols)
    }
}
fn expression(f: &str) -> bool {
    let u = f.to_ascii_uppercase();
    ["!=", "<=", ">=", "<>", "=", "<", ">", "::", "->"]
        .iter()
        .any(|x| u.contains(x))
        || u.split(|c: char| !c.is_alphanumeric() && c != '_')
            .any(|w| ["AND", "BETWEEN", "ILIKE", "IN", "IS", "LIKE", "NOT", "OR"].contains(&w))
}
fn filter_sql(filter: Option<&str>, cols: &[String]) -> Result<(String, Vec<Value>)> {
    let Some(f) = filter.filter(|s| !s.is_empty()) else {
        return Ok((String::new(), vec![]));
    };
    if expression(f) {
        return Ok((format!("WHERE ({f})"), vec![]));
    }
    let terms = cols
        .iter()
        .enumerate()
        .map(|(i, c)| Ok(format!("CAST({} AS TEXT) LIKE ${}", quote(c)?, i + 1)))
        .collect::<Result<Vec<_>>>()?;
    Ok((
        if terms.is_empty() {
            "WHERE 1=0".into()
        } else {
            format!("WHERE {}", terms.join(" OR "))
        },
        vec![json!(format!("%{f}%")); terms.len()],
    ))
}
fn order_sql(sort: Option<&Value>, cols: &[String]) -> Result<String> {
    let Some(sort) = sort else {
        return Ok(String::new());
    };
    let (c, d) = if let Some(a) = sort.as_array() {
        if a.len() != 2 {
            return Err(error(
                "ValueError",
                "sort must contain column and direction",
            ));
        }
        (a[0].as_str(), a[1].as_str())
    } else {
        (sort["column"].as_str(), sort["direction"].as_str())
    };
    let col = c.ok_or_else(|| error("ValueError", "Invalid sort column"))?;
    let direction = d.unwrap_or("").to_ascii_uppercase();
    if !cols.iter().any(|c| c == col) {
        return Err(error("InvalidIdentifierError", "Unknown sort column"));
    }
    if !["ASC", "DESC"].contains(&direction.as_str()) {
        return Err(error("ValueError", "Invalid sort direction"));
    }
    Ok(format!("ORDER BY {} {direction}", quote(col)?))
}

fn query(
    c: &mut Client,
    sql: &str,
    params: &[Value],
    blob: BlobStyle,
    window: Option<(i64, i64)>,
) -> Result<Value> {
    query_mode(c, sql, params, blob, window, false)
}
fn query_mode(
    c: &mut Client,
    sql: &str,
    params: &[Value],
    blob: BlobStyle,
    window: Option<(i64, i64)>,
    json_envelopes: bool,
) -> Result<Value> {
    let ps = params.iter().cloned().map(Param).collect::<Vec<_>>();
    let types = ps.iter().map(Param::hint).collect::<Vec<_>>();
    let st = c.prepare_typed(sql, &types).map_err(db)?;
    let headers = st
        .columns()
        .iter()
        .map(|c| c.name().to_string())
        .collect::<Vec<_>>();
    let refs = ps
        .iter()
        .map(|p| p as &(dyn ToSql + Sync))
        .collect::<Vec<_>>();
    let mut cursor = c.query_raw(&st, refs).map_err(db)?;
    let mut rows = vec![];
    let mut index = 0;
    while let Some(row) = cursor.next().map_err(db)? {
        if window.is_none_or(|(offset, _)| index >= offset) {
            let mut cells = vec![];
            for i in 0..headers.len() {
                let cell = row
                    .try_get::<_, Option<Cell>>(i)
                    .map_err(db)?
                    .map(|c| {
                        let ty = row.columns()[i].type_();
                        if json_envelopes {
                            ui_json(c.0, blob, ty)
                        } else {
                            wire_blob(c.0, blob, ty)
                        }
                    })
                    .unwrap_or(Value::Null);
                cells.push(cell)
            }
            rows.push(cells);
            if window.is_some_and(|(_, limit)| rows.len() >= limit as usize) {
                break;
            }
        }
        index += 1;
    }
    Ok(json!({"headers":headers,"rows":rows}))
}
fn ui_json(v: Value, blob: BlobStyle, t: &Type) -> Value {
    if let Kind::Domain(base) = t.kind() {
        return ui_json(v, blob, base);
    }
    if json_type(t) {
        return json!({"$type":"json","value":json_payload(&v)});
    }
    if let Kind::Array(element) = t.kind() {
        if json_type(element) {
            return Value::Array(
                v.as_array()
                    .unwrap()
                    .iter()
                    .map(|v| {
                        if v.is_null() {
                            Value::Null
                        } else {
                            json!({"$type":"json","value":json_payload(v)})
                        }
                    })
                    .collect(),
            );
        }
    }
    wire_blob(v, blob, t)
}
fn wire_blob(v: Value, style: BlobStyle, ty: &Type) -> Value {
    match ty.kind() {
        Kind::Domain(base) => return wire_blob(v, style, base),
        Kind::Array(elem) => {
            fn nested(v: Value, style: BlobStyle, elem: &Type) -> Value {
                match v {
                    Value::Array(a) => {
                        Value::Array(a.into_iter().map(|v| nested(v, style, elem)).collect())
                    }
                    Value::Null => Value::Null,
                    _ => wire_blob(v, style, elem),
                }
            }
            return nested(v, style, elem);
        }
        _ => {}
    }
    if *ty != Type::BYTEA {
        return v;
    }
    let hex = v["$bytes"].as_str().unwrap_or("");
    let bytes = hex_decode(hex).unwrap_or_default();
    use base64::Engine;
    match style {
        BlobStyle::Hex => v,
        BlobStyle::Base64 => {
            json!({"$blob":base64::engine::general_purpose::STANDARD.encode(bytes)})
        }
        BlobStyle::TypedBase64 => {
            json!({"$type":"blob","base64":base64::engine::general_purpose::STANDARD.encode(bytes)})
        }
    }
}

#[derive(Debug)]
struct Cell(Value);
impl<'a> FromSql<'a> for Cell {
    fn accepts(_: &Type) -> bool {
        true
    }
    fn from_sql(
        t: &Type,
        raw: &'a [u8],
    ) -> std::result::Result<Self, Box<dyn StdError + Sync + Send>> {
        Ok(Cell(decode(t, raw)?))
    }
}
fn bytes<const N: usize>(
    raw: &[u8],
) -> std::result::Result<[u8; N], Box<dyn StdError + Sync + Send>> {
    raw.try_into()
        .map_err(|_| "Invalid PostgreSQL binary value length".into())
}
fn text(raw: &[u8]) -> std::result::Result<String, Box<dyn StdError + Sync + Send>> {
    Ok(std::str::from_utf8(raw)?.into())
}
fn tag(t: &str, value: impl Into<String>) -> Value {
    json!({"$type":t,"value":value.into()})
}
fn time_text(micros: i64) -> String {
    let secs = micros / 1_000_000;
    let mut s = format!(
        "{:02}:{:02}:{:02}",
        secs / 3600,
        (secs / 60) % 60,
        secs % 60
    );
    if micros % 1_000_000 != 0 {
        s += &format!(".{:06}", micros % 1_000_000)
    }
    s
}
fn datetime_text(value: NaiveDateTime) -> String {
    format!(
        "{}T{}",
        value.date().format("%Y-%m-%d"),
        time_text(
            value.time().num_seconds_from_midnight() as i64 * 1_000_000
                + value.time().nanosecond() as i64 / 1000
        )
    )
}
fn decode(t: &Type, raw: &[u8]) -> std::result::Result<Value, Box<dyn StdError + Sync + Send>> {
    if let Kind::Domain(base) = t.kind() {
        return decode(base, raw);
    }
    if let Kind::Array(element) = t.kind() {
        return decode_array(element, raw);
    }
    Ok(match *t {
        Type::BOOL => json!(bytes::<1>(raw)?[0] != 0),
        Type::INT2 => json!(i16::from_be_bytes(bytes(raw)?)),
        Type::INT4 => json!(i32::from_be_bytes(bytes(raw)?)),
        Type::INT8 => json!(i64::from_be_bytes(bytes(raw)?)),
        Type::OID => json!(u32::from_be_bytes(bytes(raw)?)),
        Type::FLOAT4 => {
            let f = f32::from_be_bytes(bytes(raw)?) as f64;
            if !f.is_finite() {
                tag("float", f.to_string())
            } else {
                json!(f)
            }
        }
        Type::FLOAT8 => {
            let f = f64::from_be_bytes(bytes(raw)?);
            if !f.is_finite() {
                tag("float", f.to_string())
            } else {
                json!(f)
            }
        }
        Type::UUID => {
            let b = bytes::<16>(raw)?;
            let h = b.iter().map(|b| format!("{b:02x}")).collect::<String>();
            tag(
                "uuid",
                format!(
                    "{}-{}-{}-{}-{}",
                    &h[..8],
                    &h[8..12],
                    &h[12..16],
                    &h[16..20],
                    &h[20..]
                ),
            )
        }
        Type::NUMERIC => tag("decimal", decode_numeric(raw)?),
        Type::BYTEA => json!({"$bytes":raw.iter().map(|b|format!("{b:02x}")).collect::<String>()}),
        Type::DATE => {
            let days = i32::from_be_bytes(bytes(raw)?);
            if days == i32::MAX {
                tag("date", "infinity")
            } else if days == i32::MIN {
                tag("date", "-infinity")
            } else {
                let date = NaiveDate::from_ymd_opt(2000, 1, 1)
                    .unwrap()
                    .checked_add_signed(chrono::Duration::days(days as i64))
                    .ok_or("PostgreSQL date outside supported range")?;
                tag("date", date.format("%Y-%m-%d").to_string())
            }
        }
        Type::TIME => tag("time", time_text(i64::from_be_bytes(bytes(raw)?))),
        Type::TIMETZ => {
            if raw.len() != 12 {
                return Err("Invalid timetz length".into());
            }
            let micros = i64::from_be_bytes(bytes(&raw[..8])?);
            let east = -i32::from_be_bytes(bytes(&raw[8..])?);
            tag(
                "time",
                format!(
                    "{}{}{:02}:{:02}",
                    time_text(micros),
                    if east < 0 { '-' } else { '+' },
                    east.abs() / 3600,
                    (east.abs() / 60) % 60
                ),
            )
        }
        Type::TIMESTAMP | Type::TIMESTAMPTZ => {
            let us = i64::from_be_bytes(bytes(raw)?);
            if us == i64::MAX {
                tag("datetime", "infinity")
            } else if us == i64::MIN {
                tag("datetime", "-infinity")
            } else {
                let date = NaiveDate::from_ymd_opt(2000, 1, 1)
                    .unwrap()
                    .and_hms_opt(0, 0, 0)
                    .unwrap()
                    .checked_add_signed(chrono::Duration::microseconds(us))
                    .ok_or("PostgreSQL timestamp outside supported range")?;
                tag(
                    "datetime",
                    format!(
                        "{}{}",
                        datetime_text(date),
                        if *t == Type::TIMESTAMPTZ {
                            "+00:00"
                        } else {
                            ""
                        }
                    ),
                )
            }
        }
        Type::JSON => json_read(serde_json::from_slice(raw)?),
        Type::JSONB => {
            if raw.first() != Some(&1) {
                return Err("Unknown JSONB binary format".into());
            }
            json_read(serde_json::from_slice(&raw[1..])?)
        }
        Type::TEXT
        | Type::VARCHAR
        | Type::BPCHAR
        | Type::NAME
        | Type::UNKNOWN
        | Type::XML
        | Type::CHAR => json!(text(raw)?),
        _ => {
            if matches!(t.kind(), Kind::Enum(_)) {
                json!(text(raw)?)
            } else {
                return Err(format!("Unsupported PostgreSQL type: {}", t.name()).into());
            }
        }
    })
}
fn decode_numeric(raw: &[u8]) -> std::result::Result<String, Box<dyn StdError + Sync + Send>> {
    if raw.len() < 8 {
        return Err("Invalid numeric length".into());
    }
    let u = |i| u16::from_be_bytes([raw[i], raw[i + 1]]);
    let n = u(0) as usize;
    let weight = i16::from_be_bytes([raw[2], raw[3]]) as isize;
    let sign = u(4);
    let scale = u(6) as usize;
    if raw.len() != 8 + n * 2 {
        return Err("Invalid numeric digits".into());
    }
    match sign {
        0xC000 => return Ok("NaN".into()),
        0xD000 => return Ok("Infinity".into()),
        0xF000 => return Ok("-Infinity".into()),
        0 | 0x4000 => {}
        _ => return Err("Invalid numeric sign".into()),
    }
    let groups = (0..n).map(|i| u(8 + i * 2)).collect::<Vec<_>>();
    if groups.iter().any(|d| *d >= 10000) {
        return Err("Invalid numeric digit".into());
    }
    let group = |power: isize| {
        let i = weight - power;
        if i < 0 {
            0
        } else {
            groups.get(i as usize).copied().unwrap_or(0)
        }
    };
    let mut s = if sign == 0x4000 {
        "-".to_string()
    } else {
        String::new()
    };
    if weight < 0 {
        s.push('0')
    } else {
        for power in (0..=weight).rev() {
            let d = group(power);
            if power == weight {
                s += &d.to_string()
            } else {
                s += &format!("{d:04}")
            }
        }
    }
    if scale > 0 {
        s.push('.');
        for step in 1..=scale.div_ceil(4) {
            s += &format!("{:04}", group(-(step as isize)))
        }
        s.truncate(s.find('.').unwrap() + 1 + scale)
    }
    Ok(s)
}
fn decode_array(
    element: &Type,
    raw: &[u8],
) -> std::result::Result<Value, Box<dyn StdError + Sync + Send>> {
    let mut at = 0;
    let mut integer = || {
        if at + 4 > raw.len() {
            return Err::<i32, Box<dyn StdError + Sync + Send>>("Invalid array length".into());
        }
        let i = i32::from_be_bytes(bytes(&raw[at..at + 4])?);
        at += 4;
        Ok(i)
    };
    let ndim = integer()?;
    let _nulls = integer()?;
    let _oid = integer()?;
    if !(0..=6).contains(&ndim) {
        return Err("Invalid PostgreSQL array dimensions".into());
    }
    let mut dims = vec![];
    let mut count = if ndim == 0 { 0 } else { 1usize };
    for _ in 0..ndim {
        let n = integer()?;
        let _lower = integer()?;
        if n < 0 {
            return Err("Negative array dimension".into());
        }
        count = count
            .checked_mul(n as usize)
            .filter(|n| *n <= 1_000_000)
            .ok_or("Array exceeds decoder limit")?;
        dims.push(n as usize)
    }
    let mut flat = vec![];
    for _ in 0..count {
        if at + 4 > raw.len() {
            return Err("Truncated array".into());
        }
        let n = i32::from_be_bytes(bytes(&raw[at..at + 4])?);
        at += 4;
        if n == -1 {
            flat.push(Value::Null)
        } else {
            let n = usize::try_from(n)?;
            if at + n > raw.len() {
                return Err("Truncated array element".into());
            }
            flat.push(decode(element, &raw[at..at + n])?);
            at += n;
        }
    }
    if at != raw.len() {
        return Err("Trailing array bytes".into());
    }
    fn nest(dims: &[usize], flat: &mut std::vec::IntoIter<Value>) -> Value {
        if dims.is_empty() {
            return flat.next().unwrap_or(Value::Null);
        }
        Value::Array((0..dims[0]).map(|_| nest(&dims[1..], flat)).collect())
    }
    if ndim == 0 {
        Ok(json!([]))
    } else {
        Ok(nest(&dims, &mut flat.into_iter()))
    }
}
#[derive(Debug)]
struct Param(Value);
impl Param {
    fn hint(&self) -> Type {
        match &self.0 {
            Value::Bool(_) => Type::BOOL,
            Value::Number(n) if n.as_i64().is_some() => Type::INT8,
            Value::Number(n)
                if n.to_string()
                    .bytes()
                    .all(|b| b.is_ascii_digit() || b == b'-') =>
            {
                Type::NUMERIC
            }
            Value::Number(_) => Type::FLOAT8,
            Value::Object(m) => match m.get("$type").and_then(Value::as_str) {
                Some("uuid") => Type::UUID,
                Some("decimal") => Type::NUMERIC,
                Some("date") => Type::DATE,
                Some("time") => Type::TIME,
                Some("datetime") => {
                    if m["value"]
                        .as_str()
                        .is_some_and(|s| chrono::DateTime::parse_from_rfc3339(s).is_ok())
                    {
                        Type::TIMESTAMPTZ
                    } else {
                        Type::TIMESTAMP
                    }
                }
                Some("blob") => Type::BYTEA,
                _ => {
                    if m.contains_key("$bytes") || m.contains_key("$blob") {
                        Type::BYTEA
                    } else {
                        Type::UNKNOWN
                    }
                }
            },
            _ => Type::UNKNOWN,
        }
    }
}
impl ToSql for Param {
    fn accepts(_: &Type) -> bool {
        true
    }
    fn to_sql(
        &self,
        t: &Type,
        out: &mut postgres::types::private::BytesMut,
    ) -> std::result::Result<IsNull, Box<dyn StdError + Sync + Send>> {
        if self.0.is_null() {
            return Ok(IsNull::Yes);
        }
        if let Kind::Domain(base) = t.kind() {
            return self.to_sql(base, out);
        }
        if *t == Type::JSON || *t == Type::JSONB {
            let json = json_payload(&self.0);
            if *t == Type::JSONB {
                out.extend_from_slice(&[1]);
            }
            out.extend_from_slice(&serde_json::to_vec(json)?);
            return Ok(IsNull::No);
        }
        let tag = self.0.get("$type").and_then(Value::as_str);
        let expected = match *t {
            Type::UUID => Some("uuid"),
            Type::NUMERIC => Some("decimal"),
            Type::FLOAT4 | Type::FLOAT8 => Some("float"),
            Type::DATE => Some("date"),
            Type::TIME | Type::TIMETZ => Some("time"),
            Type::TIMESTAMP | Type::TIMESTAMPTZ => Some("datetime"),
            _ => None,
        };
        let v = if expected.is_some() && tag == expected {
            &self.0["value"]
        } else {
            &self.0
        };
        let as_text = || {
            v.as_str()
                .map(str::to_string)
                .unwrap_or_else(|| v.to_string())
        };
        match *t {
            Type::INT2 => out.extend_from_slice(
                &i16::try_from(v.as_i64().ok_or("integer parameter required")?)?.to_be_bytes(),
            ),
            Type::INT4 => out.extend_from_slice(
                &i32::try_from(v.as_i64().ok_or("integer parameter required")?)?.to_be_bytes(),
            ),
            Type::INT8 => out.extend_from_slice(
                &v.as_i64()
                    .ok_or("integer parameter required")?
                    .to_be_bytes(),
            ),
            Type::OID => out.extend_from_slice(
                &u32::try_from(v.as_u64().ok_or("unsigned integer required")?)?.to_be_bytes(),
            ),
            Type::FLOAT4 => {
                out.extend_from_slice(&(v.as_f64().ok_or("number required")? as f32).to_be_bytes())
            }
            Type::FLOAT8 => {
                out.extend_from_slice(&v.as_f64().ok_or("number required")?.to_be_bytes())
            }
            Type::BOOL => {
                out.extend_from_slice(&[u8::from(v.as_bool().ok_or("boolean required")?)])
            }
            Type::UUID => {
                let s = as_text().replace('-', "");
                let b = hex_decode(&s)?;
                if b.len() != 16 {
                    return Err("invalid UUID".into());
                }
                out.extend_from_slice(&b)
            }
            Type::NUMERIC => out.extend_from_slice(&encode_numeric(&as_text())?),
            Type::BYTEA => {
                use base64::Engine;
                let bytes = if let Some(hex) = self.0.get("$bytes").and_then(Value::as_str) {
                    hex_decode(hex)?
                } else if let Some(base64) = self
                    .0
                    .get("$blob")
                    .or_else(|| self.0.get("base64"))
                    .and_then(Value::as_str)
                {
                    base64::engine::general_purpose::STANDARD.decode(base64)?
                } else {
                    return Err("bytea parameter needs a byte tag".into());
                };
                out.extend_from_slice(&bytes)
            }
            Type::JSON | Type::JSONB => {
                let json = if let Some(s) = v.as_str() {
                    serde_json::from_str::<Value>(s)?
                } else {
                    v.clone()
                };
                if *t == Type::JSONB {
                    out.extend_from_slice(&[1])
                }
                out.extend_from_slice(&serde_json::to_vec(&json)?)
            }
            Type::DATE => {
                let d = NaiveDate::parse_from_str(&as_text(), "%Y-%m-%d")?;
                let days = d
                    .signed_duration_since(NaiveDate::from_ymd_opt(2000, 1, 1).unwrap())
                    .num_days();
                out.extend_from_slice(&i32::try_from(days)?.to_be_bytes())
            }
            Type::TIMETZ => {
                let text = as_text();
                let time = chrono::DateTime::parse_from_rfc3339(&format!("2000-01-01T{text}"))?;
                let micros = time.time().num_seconds_from_midnight() as i64 * 1_000_000
                    + time.time().nanosecond() as i64 / 1000;
                out.extend_from_slice(&micros.to_be_bytes());
                out.extend_from_slice(&(-time.offset().local_minus_utc()).to_be_bytes());
            }
            Type::TIME => {
                let time = NaiveTime::parse_from_str(&as_text(), "%H:%M:%S%.f")?;
                out.extend_from_slice(
                    &(time.num_seconds_from_midnight() as i64 * 1_000_000
                        + time.nanosecond() as i64 / 1000)
                        .to_be_bytes(),
                )
            }
            Type::TIMESTAMP | Type::TIMESTAMPTZ => {
                let s = as_text();
                let dt = if *t == Type::TIMESTAMPTZ {
                    chrono::DateTime::parse_from_rfc3339(&s)?.naive_utc()
                } else {
                    NaiveDateTime::parse_from_str(&s.replace('T', " "), "%Y-%m-%d %H:%M:%S%.f")?
                };
                let us = dt
                    .signed_duration_since(
                        NaiveDate::from_ymd_opt(2000, 1, 1)
                            .unwrap()
                            .and_hms_opt(0, 0, 0)
                            .unwrap(),
                    )
                    .num_microseconds()
                    .ok_or("timestamp overflow")?;
                out.extend_from_slice(&us.to_be_bytes())
            }
            _ => {
                if let Kind::Array(element) = t.kind() {
                    encode_array(element, &self.0, out)?
                } else {
                    out.extend_from_slice(as_text().as_bytes())
                }
            }
        }
        Ok(IsNull::No)
    }
    postgres::types::to_sql_checked!();
}
fn hex_decode(s: &str) -> std::result::Result<Vec<u8>, Box<dyn StdError + Sync + Send>> {
    if s.len() % 2 != 0 || !s.bytes().all(|b| b.is_ascii_hexdigit()) {
        return Err("Invalid hex bytes".into());
    }
    s.as_bytes()
        .chunks_exact(2)
        .map(|b| Ok(u8::from_str_radix(std::str::from_utf8(b)?, 16)?))
        .collect()
}
fn encode_numeric(text: &str) -> std::result::Result<Vec<u8>, Box<dyn StdError + Sync + Send>> {
    let (negative, s) = text.strip_prefix('-').map_or((false, text), |s| (true, s));
    let special = match s.to_ascii_lowercase().as_str() {
        "nan" => Some(0xC000u16),
        "infinity" | "inf" => Some(if negative { 0xF000 } else { 0xD000 }),
        _ => None,
    };
    if let Some(sign) = special {
        let mut out = vec![0; 8];
        out[4..6].copy_from_slice(&sign.to_be_bytes());
        return Ok(out);
    }
    let mut parts = s.split('.');
    let integer = parts.next().unwrap_or("");
    let fraction = parts.next().unwrap_or("");
    if parts.next().is_some()
        || integer.is_empty()
        || !integer.bytes().all(|b| b.is_ascii_digit())
        || !fraction.bytes().all(|b| b.is_ascii_digit())
        || fraction.len() > 16383
    {
        return Err("Invalid exact decimal".into());
    }
    let integer = integer.trim_start_matches('0');
    let integral = format!("{}{}", "0".repeat((4 - integer.len() % 4) % 4), integer);
    let nint = integral.len() / 4;
    let fractional = format!("{}{}", fraction, "0".repeat((4 - fraction.len() % 4) % 4));
    let mut groups = integral
        .as_bytes()
        .chunks(4)
        .chain(fractional.as_bytes().chunks(4))
        .map(|chunk| Ok(std::str::from_utf8(chunk)?.parse::<u16>()?))
        .collect::<std::result::Result<Vec<_>, Box<dyn StdError + Sync + Send>>>()?;
    let mut weight = i16::try_from(nint)? - 1;
    while groups.first() == Some(&0) {
        groups.remove(0);
        weight = weight.checked_sub(1).ok_or("decimal weight overflow")?
    }
    while groups.last() == Some(&0) {
        groups.pop();
    }
    if groups.is_empty() {
        weight = 0
    }
    let mut out = vec![];
    out.extend_from_slice(&u16::try_from(groups.len())?.to_be_bytes());
    out.extend_from_slice(&weight.to_be_bytes());
    out.extend_from_slice(
        &(if negative && !groups.is_empty() {
            0x4000u16
        } else {
            0
        })
        .to_be_bytes(),
    );
    out.extend_from_slice(&(fraction.len() as u16).to_be_bytes());
    for group in groups {
        out.extend_from_slice(&group.to_be_bytes())
    }
    Ok(out)
}
fn encode_array(
    element: &Type,
    value: &Value,
    out: &mut postgres::types::private::BytesMut,
) -> std::result::Result<(), Box<dyn StdError + Sync + Send>> {
    fn shape(
        v: &Value,
        dims: &mut Vec<usize>,
        depth: usize,
        flat: &mut Vec<Value>,
    ) -> std::result::Result<(), Box<dyn StdError + Sync + Send>> {
        if let Some(a) = v.as_array() {
            if depth > 5 {
                return Err("too many array dimensions".into());
            }
            if dims.len() == depth {
                dims.push(a.len())
            } else if dims[depth] != a.len() {
                return Err("nonrectangular array".into());
            }
            for x in a {
                shape(x, dims, depth + 1, flat)?
            }
        } else {
            if depth != dims.len() {
                return Err("mixed array dimensions".into());
            }
            flat.push(v.clone())
        }
        Ok(())
    }
    let mut dims = vec![];
    let mut flat = vec![];
    if json_type(element) {
        let values = value.as_array().ok_or("array parameter needs an array")?;
        dims.push(values.len());
        flat = values.clone();
    } else {
        shape(value, &mut dims, 0, &mut flat)?;
    }
    if dims.first() == Some(&0) {
        dims.clear()
    }
    out.extend_from_slice(&(dims.len() as i32).to_be_bytes());
    out.extend_from_slice(&(i32::from(flat.iter().any(Value::is_null))).to_be_bytes());
    out.extend_from_slice(&element.oid().to_be_bytes());
    for dim in dims {
        out.extend_from_slice(&i32::try_from(dim)?.to_be_bytes());
        out.extend_from_slice(&1i32.to_be_bytes())
    }
    for v in flat {
        if v.is_null() {
            out.extend_from_slice(&(-1i32).to_be_bytes())
        } else {
            let mut cell = postgres::types::private::BytesMut::new();
            Param(v).to_sql(element, &mut cell)?;
            out.extend_from_slice(&i32::try_from(cell.len())?.to_be_bytes());
            out.extend_from_slice(&cell)
        }
    }
    Ok(())
}

// SQL lexical scanning preserves strings, dollar strings and nested comments.
fn tokens(sql: &str) -> Result<Vec<(usize, usize, String)>> {
    let bytes = sql.as_bytes();
    let mut i = 0;
    let mut out = vec![];
    while i < bytes.len() {
        let start = i;
        match bytes[i] {
            b'\'' | b'"' => {
                let q = bytes[i];
                i += 1;
                let mut closed = false;
                while i < bytes.len() {
                    if bytes[i] == q {
                        if i + 1 < bytes.len() && bytes[i + 1] == q {
                            i += 2
                        } else {
                            i += 1;
                            closed = true;
                            break;
                        }
                    } else if bytes[i] == b'\\' && q == b'\'' && i + 1 < bytes.len() {
                        i += 2
                    } else {
                        i += 1
                    }
                }
                if !closed {
                    return Err(error("UnsupportedQueryError", "Unclosed SQL quote"));
                }
            }
            b'-' if bytes.get(i + 1) == Some(&b'-') => {
                while i < bytes.len() && bytes[i] != b'\n' {
                    i += 1
                }
            }
            b'/' if bytes.get(i + 1) == Some(&b'*') => {
                i += 2;
                let mut depth = 1;
                while i < bytes.len() && depth > 0 {
                    if bytes.get(i..i + 2) == Some(b"/*") {
                        depth += 1;
                        i += 2
                    } else if bytes.get(i..i + 2) == Some(b"*/") {
                        depth -= 1;
                        i += 2
                    } else {
                        i += 1
                    }
                }
                if depth != 0 {
                    return Err(error("UnsupportedQueryError", "Unclosed SQL comment"));
                }
            }
            b'$' => {
                let mut end = i + 1;
                while end < bytes.len()
                    && (bytes[end].is_ascii_alphanumeric() || bytes[end] == b'_')
                {
                    end += 1
                }
                if bytes.get(end) == Some(&b'$')
                    && !bytes.get(i + 1).is_some_and(u8::is_ascii_digit)
                {
                    let marker = &sql[i..=end];
                    let rest = &sql[end + 1..];
                    let at = rest
                        .find(marker)
                        .ok_or_else(|| error("UnsupportedQueryError", "Unclosed dollar string"))?;
                    i = end + 1 + at + marker.len()
                } else {
                    out.push((i, i + 1, "$".into()));
                    i += 1
                }
            }
            b';' => {
                out.push((i, i + 1, ";".into()));
                i += 1
            }
            c if c.is_ascii_alphabetic() || c == b'_' => {
                i += 1;
                while i < bytes.len() && (bytes[i].is_ascii_alphanumeric() || bytes[i] == b'_') {
                    i += 1
                }
                out.push((start, i, sql[start..i].to_ascii_uppercase()))
            }
            _ => {
                i += 1;
            }
        }
    }
    Ok(out)
}
fn check_read_query(sql: &str) -> Result<()> {
    let words = tokens(sql)?;
    if !words
        .first()
        .is_some_and(|(_, _, w)| ["SELECT", "WITH", "EXPLAIN"].contains(&w.as_str()))
    {
        return Err(error(
            "UnsupportedQueryError",
            "Only SELECT/WITH/EXPLAIN accepted",
        ));
    }
    if words.iter().any(|(_, _, w)| {
        [
            ";", "INSERT", "UPDATE", "DELETE", "ALTER", "DROP", "CREATE", "TRUNCATE", "GRANT",
            "REVOKE", "CALL", "DO", "COPY", "VACUUM", "ANALYZE", "SET", "RESET",
        ]
        .contains(&w.as_str())
    }) {
        return Err(error(
            "UnsupportedQueryError",
            "Read-only query contains a second statement or mutating command",
        ));
    }
    Ok(())
}
fn placeholders(sql: &str, count: usize, template: bool) -> String {
    if count == 0 {
        return sql.into();
    }
    let Ok(words) = tokens(sql) else {
        return sql.into();
    };
    let ranges = words
        .iter()
        .filter(|(_, _, w)| w == "$")
        .collect::<Vec<_>>();
    if !ranges.is_empty() {
        return sql.into();
    }
    // Preserve quoted/comment regions by marking only normal lexical bytes.
    let mut out = String::new();
    let mut i = 0;
    let mut n = 0;
    let bytes = sql.as_bytes();
    while i < bytes.len() {
        if bytes[i] == b'\''
            || bytes[i] == b'"'
            || bytes.get(i..i + 2) == Some(b"--")
            || bytes.get(i..i + 2) == Some(b"/*")
            || bytes[i] == b'$'
        {
            let end = protected_end(sql, i).unwrap_or(bytes.len());
            out.push_str(&sql[i..end]);
            i = end;
            continue;
        }
        if bytes.get(i..i + 2) == Some(b"%s") {
            n += 1;
            out += &format!("${n}");
            i += 2;
        } else if bytes[i] == b'?'
            && (template || sql.bytes().filter(|b| *b == b'?').count() == count)
        {
            n += 1;
            out += &format!("${n}");
            i += 1;
        } else {
            let ch = sql[i..].chars().next().unwrap();
            out.push(ch);
            i += ch.len_utf8();
        }
    }
    out
}
fn protected_end(sql: &str, start: usize) -> Option<usize> {
    let b = sql.as_bytes();
    let mut i = start;
    match b[i] {
        b'\'' | b'"' => {
            let q = b[i];
            i += 1;
            while i < b.len() {
                if b[i] == q {
                    if b.get(i + 1) == Some(&q) {
                        i += 2
                    } else {
                        return Some(i + 1);
                    }
                } else if b[i] == b'\\' && q == b'\'' && i + 1 < b.len() {
                    i += 2
                } else {
                    i += 1
                }
            }
            None
        }
        b'-' => Some(
            sql[start..]
                .find('\n')
                .map(|n| start + n + 1)
                .unwrap_or(b.len()),
        ),
        b'/' => {
            i += 2;
            let mut depth = 1;
            while i < b.len() {
                if b.get(i..i + 2) == Some(b"/*") {
                    depth += 1;
                    i += 2
                } else if b.get(i..i + 2) == Some(b"*/") {
                    depth -= 1;
                    i += 2;
                    if depth == 0 {
                        return Some(i);
                    }
                } else {
                    i += 1
                }
            }
            None
        }
        b'$' => {
            i += 1;
            while i < b.len() && (b[i].is_ascii_alphanumeric() || b[i] == b'_') {
                i += 1
            }
            if b.get(i) != Some(&b'$') {
                return Some(start + 1);
            }
            let marker = &sql[start..=i];
            Some(i + 1 + sql[i + 1..].find(marker)? + marker.len())
        }
        _ => None,
    }
}
fn shift_parameters(sql: &str, shift: usize) -> String {
    if shift == 0 {
        return sql.into();
    }
    let mut out = String::new();
    let mut i = 0;
    while i < sql.len() {
        let b = sql.as_bytes()[i];
        if b == b'\''
            || b == b'"'
            || sql.as_bytes().get(i..i + 2) == Some(b"--")
            || sql.as_bytes().get(i..i + 2) == Some(b"/*")
        {
            let end = protected_end(sql, i).unwrap_or(sql.len());
            out.push_str(&sql[i..end]);
            i = end;
        } else if b == b'$' {
            let mut end = i + 1;
            while end < sql.len() && sql.as_bytes()[end].is_ascii_digit() {
                end += 1
            }
            if end > i + 1 {
                let n = sql[i + 1..end].parse::<usize>().unwrap();
                out += &format!("${}", n + shift);
                i = end
            } else {
                let end = protected_end(sql, i).unwrap_or(i + 1);
                out.push_str(&sql[i..end]);
                i = end
            }
        } else {
            let ch = sql[i..].chars().next().unwrap();
            out.push(ch);
            i += ch.len_utf8();
        }
    }
    out
}

fn json_type(t: &Type) -> bool {
    match t.kind() {
        Kind::Domain(base) => json_type(base),
        _ => *t == Type::JSON || *t == Type::JSONB,
    }
}
pub fn json_payload(v: &Value) -> &Value {
    if v.as_object().is_some_and(|o| {
        o.len() == 2
            && o.get("$type").and_then(Value::as_str) == Some("json")
            && o.contains_key("value")
    }) {
        &v["value"]
    } else {
        v
    }
}
pub fn json_read(v: Value) -> Value {
    if v.is_null() || !std::ptr::eq(json_payload(&v), &v) {
        json!({"$type":"json","value":v})
    } else {
        v
    }
}
pub fn json_envelope(v: &Value) -> bool {
    !std::ptr::eq(json_payload(v), v)
}

pub fn scalar_text(v: &Value) -> Option<&str> {
    let t = v.get("$type")?.as_str()?;
    if ["uuid", "decimal", "date", "time", "datetime", "float"].contains(&t) {
        v["value"].as_str()
    } else {
        None
    }
}
pub fn sql_literal(v: &Value) -> Result<String> {
    if json_envelope(v) {
        return Ok(format!(
            "'{}'::jsonb",
            json_payload(v).to_string().replace('\'', "''")
        ));
    }
    if v.is_null() {
        return Ok("NULL".into());
    }
    if let Some(s) = scalar_text(v) {
        return Ok(match v["$type"].as_str().unwrap() {
            "decimal" if s.bytes().all(|b| b.is_ascii_digit() || b"+-.".contains(&b)) => s.into(),
            "uuid" => format!("'{}'::uuid", s.replace('\'', "''")),
            "date" => format!("'{}'::date", s.replace('\'', "''")),
            "time" => format!("'{}'::time", s.replace('\'', "''")),
            "datetime" => format!(
                "'{}'::{}",
                s.replace('\'', "''"),
                if chrono::DateTime::parse_from_rfc3339(s).is_ok() {
                    "timestamptz"
                } else {
                    "timestamp"
                }
            ),
            _ => format!("'{}'", s.replace('\'', "''")),
        });
    }
    if let Some(b) = v.as_bool() {
        return Ok(if b { "TRUE" } else { "FALSE" }.into());
    }
    if v.is_number() {
        return Ok(v.to_string());
    }
    if let Some(bytes) = v.get("$bytes").and_then(Value::as_str) {
        hex_decode(bytes).map_err(|_| error("ValueError", "Invalid bytes"))?;
        return Ok(format!("decode('{bytes}','hex')"));
    }
    if v.get("$blob").is_some() || v.get("$type").and_then(Value::as_str) == Some("blob") {
        use base64::Engine;
        let encoded = v
            .get("$blob")
            .or_else(|| v.get("base64"))
            .and_then(Value::as_str)
            .ok_or_else(|| error("ValueError", "Invalid blob"))?;
        let b = base64::engine::general_purpose::STANDARD
            .decode(encoded)
            .map_err(|_| error("ValueError", "Invalid blob"))?;
        return Ok(format!(
            "decode('{}','hex')",
            b.iter().map(|b| format!("{b:02x}")).collect::<String>()
        ));
    }
    if let Some(array) = v.as_array() {
        fn item(v: &Value) -> String {
            if v.is_null() {
                "NULL".into()
            } else if let Some(a) = v.as_array() {
                format!("{{{}}}", a.iter().map(item).collect::<Vec<_>>().join(","))
            } else {
                let text = scalar_text(v)
                    .map(str::to_string)
                    .or_else(|| v.as_str().map(str::to_string))
                    .unwrap_or_else(|| v.to_string());
                format!("\"{}\"", text.replace('\\', "\\\\").replace('"', "\\\""))
            }
        }
        let text = format!(
            "{{{}}}",
            array.iter().map(item).collect::<Vec<_>>().join(",")
        );
        return Ok(format!("'{}'", text.replace('\'', "''")));
    }
    let s = v
        .as_str()
        .map(str::to_string)
        .unwrap_or_else(|| v.to_string());
    Ok(format!("'{}'", s.replace('\'', "''")))
}

// Raw query exports target query_result and have no destination-table attributes.
fn render_sql_export(
    target: &str,
    headers: &[String],
    rows: &[Vec<Value>],
    types: &[Type],
    attributes: &[write::Column],
) -> Result<String> {
    if headers.len() != types.len() {
        return Err(error("ValueError", "Export type width mismatch"));
    }
    let included = headers
        .iter()
        .enumerate()
        .filter(|(_, name)| {
            !attributes
                .iter()
                .any(|column| &column.name == *name && column.generated)
        })
        .map(|(index, _)| index)
        .collect::<Vec<_>>();
    let names = included
        .iter()
        .map(|&index| quote(&headers[index]))
        .collect::<Result<Vec<_>>>()?
        .join(", ");
    let overriding = if included.iter().any(|&index| {
        attributes
            .iter()
            .any(|column| column.name == headers[index] && column.identity == "a")
    }) {
        " OVERRIDING SYSTEM VALUE"
    } else {
        ""
    };
    let mut output = String::new();
    for row in rows {
        if row.len() != headers.len() {
            return Err(error("ValueError", "Export row width mismatch"));
        }
        if included.is_empty() {
            output.push_str(&format!("INSERT INTO {target} DEFAULT VALUES;\n"));
            continue;
        }
        let values = included
            .iter()
            .map(|&index| sql_typed_literal(&row[index], &types[index]))
            .collect::<Result<Vec<_>>>()?
            .join(", ");
        output.push_str(&format!(
            "INSERT INTO {target} ({names}){overriding} VALUES ({values});\n"
        ));
    }
    Ok(output)
}

fn sql_typed_literal(v: &Value, t: &Type) -> Result<String> {
    if v.is_null() {
        return Ok("NULL".into());
    }
    if let Kind::Domain(base) = t.kind() {
        return sql_typed_literal(v, base);
    }
    if let Kind::Array(element) = t.kind() {
        fn array_text(v: &Value, element: &Type, root: bool) -> Result<String> {
            if let Some(a) = v.as_array().filter(|_| root || !json_type(element)) {
                return Ok(format!(
                    "{{{}}}",
                    a.iter()
                        .map(|v| array_text(v, element, false))
                        .collect::<Result<Vec<_>>>()?
                        .join(",")
                ));
            }
            if v.is_null() {
                return Ok("NULL".into());
            }
            let text = if *element == Type::BYTEA {
                use base64::Engine;
                let bytes = if let Some(hex) = v.get("$bytes").and_then(Value::as_str) {
                    hex_decode(hex).map_err(|_| error("ValueError", "Invalid blob"))?
                } else {
                    let text = v
                        .get("$blob")
                        .or_else(|| v.get("base64"))
                        .and_then(Value::as_str)
                        .ok_or_else(|| error("ValueError", "Invalid blob"))?;
                    base64::engine::general_purpose::STANDARD
                        .decode(text)
                        .map_err(|_| error("ValueError", "Invalid blob"))?
                };
                format!(
                    "\\x{}",
                    bytes.iter().map(|b| format!("{b:02x}")).collect::<String>()
                )
            } else if *element == Type::JSON || *element == Type::JSONB {
                json_payload(v).to_string()
            } else {
                scalar_text(v)
                    .or_else(|| v.as_str())
                    .map(str::to_string)
                    .unwrap_or_else(|| v.to_string())
            };
            Ok(format!(
                "\"{}\"",
                text.replace('\\', "\\\\").replace('\"', "\\\"")
            ))
        }
        let text = array_text(v, element, true)?;
        return Ok(format!(
            "'{}'::{}.{}",
            text.replace('\'', "''"),
            quote(t.schema())?,
            quote(t.name())?
        ));
    }
    if *t == Type::JSON || *t == Type::JSONB {
        return Ok(format!(
            "'{}'::{}",
            json_payload(v).to_string().replace('\'', "''"),
            t.name()
        ));
    }
    sql_literal(v)
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn sql_export_omits_generated_and_overrides_only_always_identity() {
        let headers = vec!["computed".into(), "id".into(), "label".into()];
        let rows = vec![vec![json!(6), json!(42), json!("it's fine")]];
        let types = vec![Type::INT4, Type::INT4, Type::TEXT];
        let attributes = vec![
            write::Column {
                name: "computed".into(),
                ty: Type::INT4,
                generated: true,
                identity: String::new(),
                default: false,
            },
            write::Column {
                name: "id".into(),
                ty: Type::INT4,
                generated: false,
                identity: "a".into(),
                default: true,
            },
        ];
        let output =
            render_sql_export("\"public\".\"items\"", &headers, &rows, &types, &attributes)
                .unwrap();
        assert_eq!(output,"INSERT INTO \"public\".\"items\" (\"id\", \"label\") OVERRIDING SYSTEM VALUE VALUES (42, 'it''s fine');\n");
        let output = render_sql_export("\"query_result\"", &headers, &rows, &types, &[]).unwrap();
        assert!(output.contains("computed"));
        assert!(!output.contains("OVERRIDING"));
        let output = render_sql_export(
            "items",
            &["computed".into()],
            &[vec![json!(6)]],
            &[Type::INT4],
            &attributes,
        )
        .unwrap();
        assert_eq!(output, "INSERT INTO items DEFAULT VALUES;\n");
        assert!(render_sql_export("items", &headers, &[vec![]], &types, &attributes).is_err());
    }

    #[test]
    fn exact_numeric_codec() {
        for s in [
            "0",
            "0.00001",
            "-0.00001",
            "0.000000001234",
            "12345678901234567890.12345678",
            "-12.3400",
            "10000.0000",
            "NaN",
            "Infinity",
            "-Infinity",
        ] {
            assert_eq!(decode_numeric(&encode_numeric(s).unwrap()).unwrap(), s);
        }
    }
    #[test]
    fn uuid_dates_and_times() {
        assert_eq!(
            decode(&Type::UUID, &[0u8; 16]).unwrap(),
            json!({"$type":"uuid","value":"00000000-0000-0000-0000-000000000000"})
        );
        assert_eq!(
            decode(&Type::DATE, &0i32.to_be_bytes()).unwrap()["value"],
            "2000-01-01"
        );
        assert_eq!(
            decode(&Type::TIMESTAMPTZ, &(-1i64).to_be_bytes()).unwrap()["value"],
            "1999-12-31T23:59:59.999999+00:00"
        );
        assert_eq!(
            decode(&Type::TIME, &1i64.to_be_bytes()).unwrap()["value"],
            "00:00:00.000001"
        );
    }
    #[test]
    fn arrays_nulls_and_bounds() {
        let v = json!([[1, null], [2, 3]]);
        let mut bytes = postgres::types::private::BytesMut::new();
        encode_array(&Type::INT4, &v, &mut bytes).unwrap();
        assert_eq!(decode_array(&Type::INT4, &bytes).unwrap(), v);
        assert!(decode_array(&Type::INT4, &bytes[..bytes.len() - 1]).is_err());
        assert_eq!(decode_array(&Type::INT4, &[0; 12]).unwrap(), json!([]));
    }
    #[test]
    fn json_byte_tag_is_not_bytea() {
        let v = json!({"$bytes":"invalid hex"});
        assert_eq!(wire_blob(v.clone(), BlobStyle::Base64, &Type::JSONB), v);
        assert_eq!(
            sql_typed_literal(&json!([1, null]), &Type::JSONB).unwrap(),
            "'[1,null]'::jsonb"
        );
    }
    #[test]
    fn identifiers_and_lexical_binds() {
        assert_eq!(
            qualified("dbcli_alt.\"Mixed Table\"").unwrap().2,
            "\"dbcli_alt\".\"Mixed Table\""
        );
        assert_eq!(
            placeholders("SELECT %s, '%s', $$%s$$ -- %s", 1, true),
            "SELECT $1, '%s', $$%s$$ -- %s"
        );
        assert!(check_read_query("WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x").is_err());
        assert!(check_read_query("SELECT '; DELETE', $$DROP$$ /* UPDATE */").is_ok());
        assert!(check_read_query("SELECT 1; SELECT 2").is_err());
    }
    #[test]
    fn timeouts_and_errors_do_not_echo_secrets() {
        assert_eq!(
            timeout_option("-c statement_timeout=100ms", "statement_timeout", 3000),
            100
        );
        assert_eq!(
            timeout_option("-c statement_timeout=10s", "statement_timeout", 3000),
            3000
        );
        let safe = configuration("postgres://alice:sentinel-password@localhost:invalid/db")
            .err()
            .unwrap();
        let config=configuration("host=localhost options='-c default_transaction_read_only=off -c statement_timeout=50ms -c lock_timeout=100ms' connect_timeout=30").unwrap();
        assert_eq!(config.get_connect_timeout(), Some(&Duration::from_secs(3)));
        assert!(config.get_options().unwrap().ends_with("-c default_transaction_read_only=on -c statement_timeout=50 -c lock_timeout=100 -c TimeZone=UTC"));
        assert!(!safe.to_string().contains("sentinel-password"));
    }
    #[test]
    fn mutation_rejected_without_connection() {
        assert_eq!(
            Reader::new(BlobStyle::Hex)
                .with_read_only(true)
                .operation(&json!({"op":"insert","row":{}}))
                .unwrap_err()
                .kind,
            "ReadOnlyError"
        );
    }
}
