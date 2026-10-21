//! Production SQLite adapter used by the Rust CLI and JSONL parity runner.
//! Connections are intentionally short-lived, matching the Python reference.
use base64::{engine::general_purpose::STANDARD as BASE64, Engine};
use rusqlite::{
    hooks::{AuthAction, AuthContext, Authorization},
    Connection, OpenFlags, OptionalExtension, Row, Statement,
};
use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Number, Value};
use std::{
    cell::RefCell,
    path::{Path, PathBuf},
    rc::Rc,
};

pub mod completion;
pub mod pg_cancel;
pub mod pg_read;
pub mod remote;
pub mod render;
pub mod tui;
pub mod writable;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct QueryResult {
    pub headers: Vec<String>,
    pub rows: Vec<Vec<Value>>,
}
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct TraceEntry {
    pub sql: String,
    pub params: Vec<Value>,
}
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct DbError {
    #[serde(rename = "class")]
    pub class_name: String,
    pub message: String,
}
impl DbError {
    pub fn new(class: &str, message: impl Into<String>) -> Self {
        Self {
            class_name: class.to_string(),
            message: message.into(),
        }
    }
    pub fn adapter(message: impl Into<String>) -> Self {
        Self::new("AdapterError", message)
    }
}
impl std::fmt::Display for DbError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.message)
    }
}
impl std::error::Error for DbError {}

#[derive(Clone)]
pub struct SQLiteAdapter {
    path: PathBuf,
    read_only: bool,
    postgres: bool,
    trace: Rc<RefCell<Vec<TraceEntry>>>,
}
impl SQLiteAdapter {
    pub fn new(path: impl AsRef<Path>, read_only: bool) -> Self {
        Self {
            path: path.as_ref().to_path_buf(),
            read_only,
            postgres: false,
            trace: Rc::new(RefCell::new(Vec::new())),
        }
    }
    pub fn postgres(read_only: bool) -> Self {
        let mut a = Self::new("PostgreSQL", read_only);
        a.postgres = true;
        a
    }
    pub fn is_postgres(&self) -> bool {
        self.postgres
    }
    fn pg<T: serde::de::DeserializeOwned>(&self, mut op: Value) -> Result<T, DbError> {
        op["json_envelopes"] = json!(true);
        let v = pg_read::Reader::new(pg_read::BlobStyle::Base64)
            .with_read_only(self.read_only)
            .operation(&op)
            .map_err(|e| DbError::new(e.kind, e.message))?;
        serde_json::from_value(v).map_err(|_| DbError::adapter("PostgreSQL result shape mismatch"))
    }
    pub fn path(&self) -> &Path {
        &self.path
    }
    pub fn is_read_only(&self) -> bool {
        self.read_only
    }
    pub fn clear_trace(&self) {
        self.trace.borrow_mut().clear();
    }
    pub fn take_trace(&self) -> Vec<TraceEntry> {
        std::mem::take(&mut *self.trace.borrow_mut())
    }
    fn record(&self, sql: &str, params: &[Value]) {
        self.trace.borrow_mut().push(TraceEntry {
            sql: sql.to_owned(),
            params: params.iter().map(trace_value).collect(),
        });
    }
    fn connect(&self) -> Result<Connection, DbError> {
        if self.postgres {
            return Err(DbError::adapter(
                "PostgreSQL cannot use a SQLite connection",
            ));
        }
        let path_string = self.path.to_string_lossy().to_string();
        let opened = if self.read_only {
            Connection::open_with_flags(
                &self.path,
                OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
            )
        } else {
            Connection::open_with_flags(
                &self.path,
                OpenFlags::SQLITE_OPEN_READ_WRITE
                    | OpenFlags::SQLITE_OPEN_CREATE
                    | OpenFlags::SQLITE_OPEN_NO_MUTEX,
            )
        };
        let conn = opened.map_err(|e| {
            DbError::adapter(format!(
                "Unable to open SQLite database {}: {e}",
                python_repr(&path_string)
            ))
        })?;
        self.record("PRAGMA foreign_keys = ON", &[]);
        conn.pragma_update(None, "foreign_keys", "ON")
            .map_err(|e| DbError::adapter(format!("SQLite error: {e}")))?;
        Ok(conn)
    }
    fn table_info_conn(&self, conn: &Connection, table: &str) -> Result<Vec<TableColumn>, DbError> {
        quote_identifier(table)?;
        let sql = format!("PRAGMA table_info({})", quote_identifier(table)?);
        self.record(&sql, &[]);
        let mut stmt = conn.prepare(&sql).map_err(sqlite_error)?;
        let cols = stmt
            .query_map([], |r| {
                Ok(TableColumn {
                    cid: r.get(0)?,
                    name: r.get(1)?,
                    typ: r
                        .get::<_, Option<String>>(2)?
                        .filter(|s| !s.is_empty())
                        .unwrap_or_else(|| "TEXT".to_owned()),
                    notnull: r.get::<_, i64>(3)? != 0,
                    default: default_value_to_string(r.get(4)?),
                    pk: r.get::<_, i64>(5)?,
                })
            })
            .map_err(sqlite_error)?
            .collect::<Result<Vec<_>, _>>()
            .map_err(sqlite_error)?;
        if cols.is_empty() {
            let check =
                "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ? LIMIT 1";
            self.record(check, &[Value::String(table.to_owned())]);
            let exists: Option<i64> = conn
                .query_row(check, [table], |r| r.get(0))
                .optional()
                .map_err(sqlite_error)?;
            if exists.is_none() {
                return Err(DbError::adapter(format!("Unknown SQLite table: {table}")));
            }
        }
        Ok(cols)
    }
    fn columns_conn(&self, conn: &Connection, table: &str) -> Result<Vec<String>, DbError> {
        Ok(self
            .table_info_conn(conn, table)?
            .into_iter()
            .map(|c| c.name)
            .collect())
    }
    fn pk_conn(&self, conn: &Connection, table: &str) -> Result<Vec<String>, DbError> {
        let mut cols: Vec<_> = self
            .table_info_conn(conn, table)?
            .into_iter()
            .filter(|c| c.pk > 0)
            .collect();
        cols.sort_by_key(|c| c.pk);
        Ok(cols.into_iter().map(|c| c.name).collect())
    }
    fn validate_write(&self) -> Result<(), DbError> {
        if self.read_only {
            Err(DbError::new(
                "ReadOnlyError",
                "This SQLite connection is read-only",
            ))
        } else {
            Ok(())
        }
    }
    pub fn list_databases(&self) -> Vec<String> {
        vec![self.path.to_string_lossy().to_string()]
    }
    pub fn list_tables(&self, include_views: bool) -> Result<Vec<String>, DbError> {
        if self.postgres {
            return self.pg(json!({"op":"list_tables","include_views":include_views}));
        }

        let conn = self.connect()?;
        let type_clause = if include_views {
            "type IN ('table', 'view')"
        } else {
            "type = 'table'"
        };
        let sql = format!("SELECT name FROM sqlite_master WHERE {type_clause} AND name NOT LIKE 'sqlite_%' ORDER BY name");
        self.record(&sql, &[]);
        let mut stmt = conn.prepare(&sql).map_err(sqlite_error)?;
        let tables = stmt
            .query_map([], |r| r.get::<_, String>(0))
            .map_err(sqlite_error)?
            .collect::<Result<Vec<_>, _>>()
            .map_err(sqlite_error)?;
        Ok(tables)
    }
    pub fn schema_info(&self, table: &str) -> Result<QueryResult, DbError> {
        if self.postgres {
            return self.pg(json!({"op":"schema","table":table}));
        }

        let conn = self.connect()?;
        let info = self.table_info_conn(&conn, table)?;
        Ok(QueryResult {
            headers: vec![
                "CID".into(),
                "Column Name".into(),
                "Type".into(),
                "NotNull".into(),
                "Default".into(),
                "PK".into(),
            ],
            rows: info
                .into_iter()
                .map(|c| {
                    vec![
                        json!(c.cid.to_string()),
                        json!(c.name),
                        json!(c.typ),
                        json!(if c.notnull { "YES" } else { "NO" }),
                        json!(c.default.unwrap_or_else(|| "NULL".into())),
                        json!(if c.pk > 0 { "YES" } else { "NO" }),
                    ]
                })
                .collect(),
        })
    }
    pub fn foreign_keys(&self, table: &str) -> Result<Vec<ForeignKey>, DbError> {
        if self.postgres {
            return self.pg(json!({"op":"foreign_keys","table":table}));
        }

        let conn = self.connect()?;
        self.table_info_conn(&conn, table)?;
        let sql = format!("PRAGMA foreign_key_list({})", quote_identifier(table)?);
        self.record(&sql, &[]);
        let mut stmt = conn.prepare(&sql).map_err(sqlite_error)?;
        let keys = stmt
            .query_map([], |r| {
                Ok(ForeignKey {
                    pairs: vec![],
                    from_col: r.get(3)?,
                    to_table: r.get(2)?,
                    to_col: r
                        .get::<_, Option<String>>(4)?
                        .unwrap_or_else(|| "None".into()),
                })
            })
            .map_err(sqlite_error)?
            .collect::<Result<Vec<_>, _>>()
            .map_err(sqlite_error)?;
        Ok(keys)
    }
    pub fn navigation_foreign_keys(&self, table: &str) -> Result<Vec<ForeignKey>, DbError> {
        if self.postgres {
            return self.pg(json!({"op":"navigation_foreign_keys","table":table}));
        }

        let conn = self.connect()?;
        let mut keys = self.foreign_keys(table)?;
        let mut stmt = conn
            .prepare(&format!(
                "PRAGMA foreign_key_list({})",
                quote_identifier(table)?
            ))
            .map_err(sqlite_error)?;
        let seq = stmt
            .query_map([], |r| r.get::<_, usize>(1))
            .map_err(sqlite_error)?
            .collect::<Result<Vec<_>, _>>()
            .map_err(sqlite_error)?;
        for (key, seq) in keys.iter_mut().zip(seq) {
            if key.to_col == "None" {
                key.to_col = self
                    .pk_conn(&conn, &key.to_table)?
                    .get(seq)
                    .cloned()
                    .ok_or_else(|| {
                        DbError::adapter("implicit FK target has no corresponding primary key")
                    })?;
            }
        }
        Ok(keys)
    }
    pub fn insert_columns(&self, table: &str) -> Result<Vec<String>, DbError> {
        if self.postgres {
            self.pg(json!({"op":"insert_columns","table":table}))
        } else {
            self.columns_conn(&self.connect()?, table)
        }
    }
    pub fn primary_keys(&self, table: &str) -> Result<Vec<String>, DbError> {
        if self.postgres {
            return self.pg(json!({"op":"primary_keys","table":table}));
        }

        self.pk_conn(&self.connect()?, table)
    }
    pub fn apply_batch(&self, table: &str, actions: &[Value]) -> Result<usize, DbError> {
        self.validate_write()?;
        if self.postgres {
            return self.pg(json!({"op":"apply_batch","table":table,"actions":actions}));
        }
        writable::apply(&mut self.connect()?, table, actions, |v| {
            value_to_sql(&normalize_json_value(v)).map_err(|e| e.to_string())
        })
        .map_err(write_error)
    }
    fn filter_order(
        &self,
        conn: &Connection,
        table: &str,
        filter: Option<&str>,
        sort: Option<&[String]>,
    ) -> Result<(String, Vec<Value>, String), DbError> {
        let cols = self.columns_conn(conn, table)?;
        let (where_sql, params) = build_filter_clause(filter, &cols)?;
        let order = build_order_clause(sort, &cols)?;
        Ok((where_sql, params, order))
    }
    pub fn fetch_page(
        &self,
        table: &str,
        limit: i64,
        offset: i64,
        filter: Option<&str>,
        sort: Option<&[String]>,
    ) -> Result<QueryResult, DbError> {
        if self.postgres {
            return self.pg(json!({"op":"page","table":table,"limit":limit,"offset":offset,"filter":filter,"sort":sort}));
        }

        validate_page(limit, offset)?;
        let conn = self.connect()?;
        let (where_sql, mut params, order) = self.filter_order(&conn, table, filter, sort)?;
        let sql = format!(
            "SELECT * FROM {} {} {} LIMIT ? OFFSET ?",
            quote_identifier(table)?,
            where_sql,
            order
        );
        params.push(json!(limit));
        params.push(json!(offset));
        self.record(&sql, &params);
        let mut stmt = conn.prepare(&sql).map_err(sqlite_error)?;
        let headers = column_headers(&stmt);
        let rows = query_all(&mut stmt, &params)?;
        Ok(QueryResult { headers, rows })
    }
    pub fn count_rows(&self, table: &str, filter: Option<&str>) -> Result<i64, DbError> {
        if self.postgres {
            return self.pg(json!({"op":"count","table":table,"filter":filter}));
        }

        let conn = self.connect()?;
        let (where_sql, params, _) = self.filter_order(&conn, table, filter, None)?;
        let sql = format!(
            "SELECT COUNT(*) FROM {} {}",
            quote_identifier(table)?,
            where_sql
        );
        self.record(&sql, &params);
        conn.query_row(
            &sql,
            rusqlite::params_from_iter(params_from_values(&params)?.iter()),
            |r| r.get(0),
        )
        .map_err(sqlite_error)
    }
    fn single_write(&self, table: &str, action: Value) -> Result<usize, DbError> {
        self.validate_write()?;
        if self.postgres {
            return self.pg(json!({"op":"apply_batch","table":table,"actions":[action]}));
        }
        writable::apply_single(&mut self.connect()?, table, action, |v| {
            value_to_sql(&normalize_json_value(v)).map_err(|e| e.to_string())
        })
        .map_err(write_error)
    }
    pub fn insert_row(&self, table: &str, row: &Map<String, Value>) -> Result<usize, DbError> {
        self.single_write(table, json!({"kind":"insert","row":row}))
    }
    pub fn update_row(
        &self,
        table: &str,
        original: &Map<String, Value>,
        updated: &Map<String, Value>,
    ) -> Result<usize, DbError> {
        self.single_write(
            table,
            json!({"kind":"update","original":original,"row":updated}),
        )
    }
    pub fn delete_row(&self, table: &str, original: &Map<String, Value>) -> Result<usize, DbError> {
        self.single_write(table, json!({"kind":"delete","original":original}))
    }
    pub fn raw_page(
        &self,
        sql: &str,
        limit: i64,
        offset: i64,
        filter: Option<&str>,
        sort: Option<&[String]>,
    ) -> Result<QueryResult, DbError> {
        if self.postgres {
            return self.pg(json!({"op":"raw_page","sql":sql,"limit":limit,"offset":offset,"filter":filter,"sort":sort}));
        }

        if sql.trim_start().to_ascii_uppercase().starts_with("EXPLAIN") {
            if filter.is_some_and(|s| !s.is_empty()) || sort.is_some() {
                return Err(DbError::adapter(
                    "EXPLAIN supports paging; filter/sort require SELECT output",
                ));
            }
            return self.execute_readonly(sql, &[], limit, offset);
        }
        let headers = self.execute_readonly(sql, &[], 1, 0)?.headers;
        let (w, p) = build_filter_clause(filter, &headers)?;
        let o = build_order_clause(sort, &headers)?;
        self.execute_readonly(
            &format!(
                "SELECT * FROM ({}) AS _raw_view {w} {o}",
                sql.trim().trim_end_matches(';')
            ),
            &p,
            limit,
            offset,
        )
    }
    pub fn raw_count(&self, sql: &str, filter: Option<&str>) -> Result<i64, DbError> {
        if self.postgres {
            return self.pg(json!({"op":"raw_count","sql":sql,"filter":filter}));
        }

        if sql.trim_start().to_ascii_uppercase().starts_with("EXPLAIN") {
            return Ok(self.execute_readonly(sql, &[], 100000, 0)?.rows.len() as i64);
        }
        let headers = self.execute_readonly(sql, &[], 1, 0)?.headers;
        let (w, p) = build_filter_clause(filter, &headers)?;
        Ok(self
            .execute_readonly(
                &format!(
                    "SELECT COUNT(*) FROM ({}) _raw_count {w}",
                    sql.trim().trim_end_matches(';')
                ),
                &p,
                1,
                0,
            )?
            .rows[0][0]
            .as_i64()
            .unwrap_or(0))
    }
    pub fn execute_readonly(
        &self,
        sql: &str,
        params: &[Value],
        limit: i64,
        offset: i64,
    ) -> Result<QueryResult, DbError> {
        if self.postgres {
            return self
                .pg(json!({"op":"raw","sql":sql,"params":params,"limit":limit,"offset":offset}));
        }

        validate_page(limit, offset)?;
        if sql.trim().is_empty() {
            return Err(DbError::new(
                "UnsupportedQueryError",
                "A non-empty read-only SQL query is required",
            ));
        }
        let statement = sql.trim().trim_end_matches(';').trim();
        let upper = statement.to_ascii_uppercase();
        let allowed = ["SELECT", "WITH", "EXPLAIN"].iter().any(|w| {
            upper
                .strip_prefix(w)
                .is_some_and(|s| s.is_empty() || s.chars().next().is_some_and(char::is_whitespace))
        });
        if !allowed {
            return Err(DbError::new(
                "UnsupportedQueryError",
                "Only SELECT, WITH and EXPLAIN statements are allowed here",
            ));
        }
        ensure_single(statement)?;
        let conn = self.connect()?;
        conn.authorizer(Some(|ctx: AuthContext<'_>| match ctx.action {
            AuthAction::Insert { .. }
            | AuthAction::Update { .. }
            | AuthAction::Delete { .. }
            | AuthAction::CreateIndex { .. }
            | AuthAction::CreateTable { .. }
            | AuthAction::CreateTempIndex { .. }
            | AuthAction::CreateTempTable { .. }
            | AuthAction::CreateTempTrigger { .. }
            | AuthAction::CreateTempView { .. }
            | AuthAction::CreateTrigger { .. }
            | AuthAction::CreateView { .. }
            | AuthAction::DropIndex { .. }
            | AuthAction::DropTable { .. }
            | AuthAction::DropTempIndex { .. }
            | AuthAction::DropTempTable { .. }
            | AuthAction::DropTempTrigger { .. }
            | AuthAction::DropTempView { .. }
            | AuthAction::DropTrigger { .. }
            | AuthAction::DropView { .. }
            | AuthAction::AlterTable { .. }
            | AuthAction::Reindex { .. }
            | AuthAction::Analyze { .. }
            | AuthAction::Attach { .. }
            | AuthAction::Detach { .. } => Authorization::Deny,
            _ => Authorization::Allow,
        }));
        let result = if upper.starts_with("EXPLAIN") {
            let mut stmt = conn.prepare(statement).map_err(|e| {
                DbError::adapter(format!(
                    "SQLite read-only query failed: {}",
                    clean_sqlite_error(&e)
                ))
            })?;
            let headers = column_headers(&stmt);
            self.record(statement, params);
            let rows = query_window(&mut stmt, params, offset, limit).map_err(|e| {
                DbError::adapter(format!(
                    "SQLite read-only query failed: {}",
                    e.message
                        .strip_prefix("SQLite error: ")
                        .unwrap_or(&e.message)
                ))
            })?;
            QueryResult { headers, rows }
        } else {
            let wrapped = format!("SELECT * FROM ({statement}) AS _dbcli_query LIMIT ? OFFSET ?");
            let mut all_params = params.to_vec();
            all_params.push(json!(limit));
            all_params.push(json!(offset));
            self.record(&wrapped, &all_params);
            let mut stmt = conn.prepare(&wrapped).map_err(|e| {
                DbError::adapter(format!(
                    "SQLite read-only query failed: {}",
                    clean_sqlite_error(&e)
                ))
            })?;
            let headers = column_headers(&stmt);
            let rows = query_all(&mut stmt, &all_params).map_err(|e| {
                DbError::adapter(format!(
                    "SQLite read-only query failed: {}",
                    e.message
                        .strip_prefix("SQLite error: ")
                        .unwrap_or(&e.message)
                ))
            })?;
            QueryResult { headers, rows }
        };
        Ok(result)
    }
    /// Return an interactive inspector view of table indexes, foreign keys and triggers.
    pub fn indexes(&self, table: &str) -> Result<QueryResult, DbError> {
        if self.postgres {
            return self.pg(json!({"op":"inspector","table":table}));
        }

        let conn = self.connect()?;
        self.table_info_conn(&conn, table)?;
        let mut rows = Vec::new();
        rows.push(vec![
            json!(format!("INDEXES, CONSTRAINTS & TRIGGERS FOR '{table}'")),
            Value::Null,
        ]);
        rows.push(vec![json!("[INDEXES]"), Value::Null]);
        let index_sql = format!("PRAGMA index_list({})", quote_identifier(table)?);
        self.record(&index_sql, &[]);
        let indexes = {
            let mut stmt = conn.prepare(&index_sql).map_err(sqlite_error)?;
            let mapped = stmt
                .query_map([], |r| {
                    Ok((r.get::<_, String>(1)?, r.get::<_, i64>(2)? != 0))
                })
                .map_err(sqlite_error)?;
            mapped
                .collect::<Result<Vec<_>, _>>()
                .map_err(sqlite_error)?
        };
        if indexes.is_empty() {
            rows.push(vec![json!("(No explicit indexes found)"), Value::Null]);
        }
        for (name, unique) in indexes {
            let info_sql = format!("PRAGMA index_info({})", quote_identifier(&name)?);
            self.record(&info_sql, &[]);
            let columns = {
                let mut stmt = conn.prepare(&info_sql).map_err(sqlite_error)?;
                let mapped = stmt
                    .query_map([], |r| r.get::<_, Option<String>>(2))
                    .map_err(sqlite_error)?;
                mapped
                    .collect::<Result<Vec<_>, _>>()
                    .map_err(sqlite_error)?
            };
            rows.push(vec![
                json!(format!(
                    "{name} ({}) ➔ Columns: {}",
                    if unique { "UNIQUE" } else { "NON-UNIQUE" },
                    columns.into_iter().flatten().collect::<Vec<_>>().join(", ")
                )),
                Value::Null,
            ]);
        }
        rows.push(vec![json!("[FOREIGN KEYS]"), Value::Null]);
        let fk_sql = format!("PRAGMA foreign_key_list({})", quote_identifier(table)?);
        self.record(&fk_sql, &[]);
        let fks = {
            let mut stmt = conn.prepare(&fk_sql).map_err(sqlite_error)?;
            let mapped = stmt
                .query_map([], |r| {
                    Ok((
                        r.get::<_, String>(3)?,
                        r.get::<_, String>(2)?,
                        r.get::<_, Option<String>>(4)?.unwrap_or_default(),
                    ))
                })
                .map_err(sqlite_error)?;
            mapped
                .collect::<Result<Vec<_>, _>>()
                .map_err(sqlite_error)?
        };
        if fks.is_empty() {
            rows.push(vec![json!("(No foreign keys found)"), Value::Null]);
        } else {
            for (from, to_table, to_col) in fks {
                rows.push(vec![
                    json!(format!("{from} ➔ {to_table}.{to_col}")),
                    Value::Null,
                ]);
            }
        }
        rows.push(vec![json!("[TRIGGERS]"), Value::Null]);
        let trigger_sql="SELECT name, sql FROM sqlite_master WHERE type='trigger' AND tbl_name = ? ORDER BY name";
        self.record(trigger_sql, &[json!(table)]);
        let triggers = {
            let mut stmt = conn.prepare(trigger_sql).map_err(sqlite_error)?;
            let mapped = stmt
                .query_map([table], |r| {
                    Ok((r.get::<_, String>(0)?, r.get::<_, Option<String>>(1)?))
                })
                .map_err(sqlite_error)?;
            mapped
                .collect::<Result<Vec<_>, _>>()
                .map_err(sqlite_error)?
        };
        if triggers.is_empty() {
            rows.push(vec![json!("(No triggers found)"), Value::Null]);
        } else {
            for (name, sql) in triggers {
                rows.push(vec![
                    json!(format!("{name}: {}", sql.unwrap_or_default())),
                    Value::Null,
                ]);
            }
        }
        Ok(QueryResult {
            headers: vec!["Inspector".into(), "".into()],
            rows,
        })
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ForeignKey {
    pub from_col: String,
    pub to_table: String,
    pub to_col: String,
    #[serde(default, skip_serializing)]
    pub pairs: Vec<Value>,
}
#[derive(Debug, Clone)]
struct TableColumn {
    cid: i64,
    name: String,
    typ: String,
    notnull: bool,
    default: Option<String>,
    pk: i64,
}
fn default_value_to_string(value: rusqlite::types::Value) -> Option<String> {
    use rusqlite::types::Value as V;
    match value {
        V::Null => None,
        V::Integer(n) => Some(n.to_string()),
        V::Real(n) => Some(n.to_string()),
        V::Text(s) => Some(s),
        V::Blob(bytes) => Some(format!("b{:?}", String::from_utf8_lossy(&bytes))),
    }
}
fn clean_sqlite_error(error: &rusqlite::Error) -> String {
    let message = error.to_string();
    if let Some(pos) = message.rfind(" at offset ") {
        if message[pos + 11..].chars().all(|c| c.is_ascii_digit()) {
            if let Some(context) = message[..pos].rfind(" in ") {
                return message[..context].to_owned();
            }
        }
    }
    message
}
fn sqlite_error(error: rusqlite::Error) -> DbError {
    DbError::adapter(format!("SQLite error: {}", clean_sqlite_error(&error)))
}
pub fn validate_page(limit: i64, offset: i64) -> Result<(), DbError> {
    if limit < 1 {
        return Err(DbError::new(
            "ValueError",
            "limit must be greater than zero",
        ));
    }
    if offset < 0 {
        return Err(DbError::new("ValueError", "offset cannot be negative"));
    }
    Ok(())
}
pub fn quote_identifier(identifier: &str) -> Result<String, DbError> {
    quote_identifier_for_dialect(identifier, "sqlite")
}
pub fn quote_identifier_for_dialect(identifier: &str, dialect: &str) -> Result<String, DbError> {
    if identifier.is_empty() || identifier.contains('\0') {
        return Err(DbError::new(
            "InvalidIdentifierError",
            format!("Invalid {dialect} identifier: {}", python_repr(identifier)),
        ));
    }
    if dialect == "mysql" {
        Ok(format!("`{}`", identifier.replace('`', "``")))
    } else {
        Ok(format!("\"{}\"", identifier.replace('"', "\"\"")))
    }
}
fn has_expression_marker(s: &str) -> bool {
    let lower = s.to_ascii_lowercase();
    ["!=", "<=", ">=", "<>", "=", "<", ">", "::", "->>", "->"]
        .iter()
        .any(|m| lower.contains(m))
        || ["and", "between", "ilike", "in", "is", "like", "not", "or"]
            .iter()
            .any(|w| {
                lower.match_indices(w).any(|(i, _)| {
                    let before = lower[..i].chars().last();
                    let after = lower[i + w.len()..].chars().next();
                    !before.is_some_and(|c| c.is_ascii_alphanumeric() || c == '_')
                        && !after.is_some_and(|c| c.is_ascii_alphanumeric() || c == '_')
                })
            })
}
fn sanitize_filter(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut i = 0;
    while i < text.len() {
        let rest = &text[i..];
        let len = if rest.starts_with("->>") {
            3
        } else if rest.starts_with("->") {
            2
        } else {
            0
        };
        if len > 0 {
            let after = i + len;
            let tail = text[after..].trim_start_matches(char::is_whitespace);
            let skipped = text[after..].len() - tail.len();
            if let Some(body) = tail.strip_prefix("''") {
                if let Some(close) = body.find("''") {
                    if close > 0 {
                        out.push_str(&text[i..i + len]);
                        out.push('\'');
                        out.push_str(&body[..close]);
                        out.push('\'');
                        i = after + skipped + 2 + close + 2;
                        continue;
                    }
                }
            }
            out.push_str(&text[i..i + len]);
            i += len;
        } else {
            let ch = rest.chars().next().unwrap();
            out.push(ch);
            i += ch.len_utf8();
        }
    }
    out
}
pub fn build_filter_clause(
    filter: Option<&str>,
    columns: &[String],
) -> Result<(String, Vec<Value>), DbError> {
    build_filter_clause_for_dialect(filter, columns, "sqlite")
}
pub fn build_filter_clause_for_dialect(
    filter: Option<&str>,
    columns: &[String],
    dialect: &str,
) -> Result<(String, Vec<Value>), DbError> {
    let Some(text) = filter.filter(|s| !s.is_empty()) else {
        return Ok((String::new(), vec![]));
    };
    let clean = sanitize_filter(text);
    if has_expression_marker(&clean) {
        return Ok((format!("WHERE ({clean})"), vec![]));
    }
    if columns.is_empty() {
        return Ok(("WHERE 1 = 0".into(), vec![]));
    }
    let cast_type = if dialect == "mysql" { "CHAR" } else { "TEXT" };
    let clauses = columns
        .iter()
        .map(|c| {
            Ok(format!(
                "CAST({} AS {cast_type}) LIKE ?",
                quote_identifier_for_dialect(c, dialect)?
            ))
        })
        .collect::<Result<Vec<_>, DbError>>()?;
    Ok((
        format!("WHERE {}", clauses.join(" OR ")),
        vec![Value::String(format!("%{clean}%")); columns.len()],
    ))
}
pub fn build_order_clause(sort: Option<&[String]>, columns: &[String]) -> Result<String, DbError> {
    build_order_clause_for_dialect(sort, columns, "sqlite")
}
pub fn build_order_clause_for_dialect(
    sort: Option<&[String]>,
    columns: &[String],
    dialect: &str,
) -> Result<String, DbError> {
    let Some(s) = sort else {
        return Ok(String::new());
    };
    if s.len() != 2 {
        return Err(DbError::new(
            "ValueError",
            "order_by must contain (column, direction)",
        ));
    }
    if !columns.iter().any(|c| c == &s[0]) {
        return Err(DbError::new(
            "InvalidIdentifierError",
            format!("Unknown sort column: {}", python_repr(&s[0])),
        ));
    }
    let direction = s[1].to_ascii_uppercase();
    if direction != "ASC" && direction != "DESC" {
        return Err(DbError::new(
            "ValueError",
            "sort direction must be ASC or DESC",
        ));
    }
    Ok(format!(
        "ORDER BY {} {direction}",
        quote_identifier_for_dialect(&s[0], dialect)?
    ))
}
fn build_pk_where(
    original: &Map<String, Value>,
    pks: &[String],
) -> Result<(String, Vec<Value>), DbError> {
    build_pk_where_for_dialect(original, pks, "sqlite")
}
pub fn build_pk_where_for_dialect(
    original: &Map<String, Value>,
    pks: &[String],
    dialect: &str,
) -> Result<(String, Vec<Value>), DbError> {
    if pks.is_empty() {
        return Err(DbError::new(
            "RowIdentityError",
            "This table has no primary key; refusing an ambiguous row mutation",
        ));
    }
    let mut clauses = Vec::new();
    let mut params = Vec::new();
    for col in pks {
        let Some(value) = original.get(col) else {
            return Err(DbError::new(
                "RowIdentityError",
                format!(
                    "Original row does not contain primary-key column {}",
                    python_repr(col)
                ),
            ));
        };
        if value.is_null() {
            clauses.push(format!(
                "{} IS NULL",
                quote_identifier_for_dialect(col, dialect)?
            ))
        } else {
            clauses.push(format!(
                "{} = ?",
                quote_identifier_for_dialect(col, dialect)?
            ));
            params.push(value.clone())
        }
    }
    Ok((clauses.join(" AND "), params))
}
pub fn normalize_json_value(v: &Value) -> Value {
    if v.as_object()
        .is_some_and(|m| m.len() == 1 && m.get("$blob").is_some())
    {
        v.clone()
    } else if v.is_object() || v.is_array() {
        Value::String(json_value_to_text(v))
    } else {
        v.clone()
    }
}
pub fn json_value_to_text(value: &Value) -> String {
    let compact = serde_json::to_string(value).unwrap_or_default();
    let mut out = String::with_capacity(compact.len() + compact.len() / 8);
    let mut in_string = false;
    let mut escaped = false;
    for ch in compact.chars() {
        if in_string {
            out.push(ch);
            if escaped {
                escaped = false
            } else if ch == '\\' {
                escaped = true
            } else if ch == '"' {
                in_string = false
            }
        } else {
            match ch {
                '"' => {
                    in_string = true;
                    out.push(ch)
                }
                ':' | ',' => {
                    out.push(ch);
                    out.push(' ')
                }
                _ => out.push(ch),
            }
        }
    }
    out
}
fn python_repr(value: &str) -> String {
    let quote = if value.contains('\'') && !value.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut e = String::new();
    for ch in value.chars() {
        match ch {
            '\\' => e.push_str("\\\\"),
            '\n' => e.push_str("\\n"),
            '\r' => e.push_str("\\r"),
            '\t' => e.push_str("\\t"),
            '\0' => e.push_str("\\x00"),
            c if c == quote => {
                e.push('\\');
                e.push(c)
            }
            c if (c as u32) < 32 || c == '\u{7f}' => e.push_str(&format!("\\x{:02x}", c as u32)),
            c => e.push(c),
        }
    }
    format!("{quote}{e}{quote}")
}
fn python_repr_list(v: &[String]) -> String {
    format!(
        "[{}]",
        v.iter()
            .map(|x| python_repr(x))
            .collect::<Vec<_>>()
            .join(", ")
    )
}
fn trace_value(v: &Value) -> Value {
    if v.as_object()
        .is_some_and(|m| m.len() == 1 && m.get("$blob").is_some())
    {
        v.clone()
    } else {
        normalize_json_value(v)
    }
}
fn value_to_sql(v: &Value) -> Result<rusqlite::types::Value, DbError> {
    use rusqlite::types::Value as V;
    Ok(match v {
        Value::Null => V::Null,
        Value::Bool(b) => V::Integer(i64::from(*b)),
        Value::Number(n) if n.as_i64().is_some() => V::Integer(n.as_i64().unwrap()),
        Value::Number(n) if n.as_u64().is_some() => {
            return Err(DbError::adapter(
                "integer exceeds SQLite signed 64-bit range",
            ))
        }
        Value::Number(n) => V::Real(
            n.as_f64()
                .ok_or_else(|| DbError::adapter("invalid numeric value"))?,
        ),
        Value::String(s) => V::Text(s.clone()),
        Value::Object(m) if m.len() == 1 && m.contains_key("$blob") => V::Blob(
            BASE64
                .decode(
                    m["$blob"]
                        .as_str()
                        .ok_or_else(|| DbError::adapter("blob tag requires base64 string"))?,
                )
                .map_err(|_| DbError::adapter("invalid blob base64"))?,
        ),
        _ => V::Text(json_value_to_text(v)),
    })
}
fn params_from_values(v: &[Value]) -> Result<Vec<rusqlite::types::Value>, DbError> {
    v.iter().map(value_to_sql).collect()
}
fn sql_value_to_json(v: rusqlite::types::Value) -> Value {
    use rusqlite::types::Value as V;
    match v {
        V::Null => Value::Null,
        V::Integer(i) => json!(i),
        V::Real(f) => Number::from_f64(f)
            .map(Value::Number)
            .unwrap_or(Value::Null),
        V::Text(s) => Value::String(s),
        V::Blob(b) => json!({"$blob":BASE64.encode(b)}),
    }
}
fn column_headers(stmt: &Statement<'_>) -> Vec<String> {
    stmt.column_names().iter().map(|s| s.to_string()).collect()
}
fn row_values(row: &Row<'_>) -> Result<Vec<Value>, rusqlite::Error> {
    (0..row.as_ref().column_count())
        .map(|i| {
            row.get::<_, rusqlite::types::Value>(i)
                .map(sql_value_to_json)
        })
        .collect()
}
fn query_all(stmt: &mut Statement<'_>, params: &[Value]) -> Result<Vec<Vec<Value>>, DbError> {
    let bind = params_from_values(params)?;
    let mut result = Vec::new();
    let mut rows = stmt
        .query(rusqlite::params_from_iter(bind.iter()))
        .map_err(sqlite_error)?;
    while let Some(row) = rows.next().map_err(sqlite_error)? {
        result.push(row_values(row).map_err(sqlite_error)?)
    }
    Ok(result)
}
fn query_window(
    stmt: &mut Statement<'_>,
    params: &[Value],
    offset: i64,
    limit: i64,
) -> Result<Vec<Vec<Value>>, DbError> {
    let bind = params_from_values(params)?;
    let mut result = Vec::new();
    let mut rows = stmt
        .query(rusqlite::params_from_iter(bind.iter()))
        .map_err(sqlite_error)?;
    for _ in 0..offset {
        if rows.next().map_err(sqlite_error)?.is_none() {
            return Ok(vec![]);
        }
    }
    for _ in 0..limit {
        match rows.next().map_err(sqlite_error)? {
            Some(row) => result.push(row_values(row).map_err(sqlite_error)?),
            None => break,
        }
    }
    Ok(result)
}

#[derive(Debug, Serialize, Deserialize)]
pub struct ProtocolRequest {
    pub op: String,
    #[serde(default)]
    pub actions: Vec<Value>,
    #[serde(default)]
    pub table: Option<String>,
    #[serde(default)]
    pub include_views: bool,
    #[serde(default)]
    pub limit: Option<i64>,
    #[serde(default)]
    pub offset: Option<i64>,
    #[serde(default)]
    pub filter: Option<String>,
    #[serde(default)]
    pub sort: Option<Vec<String>>,
    #[serde(default)]
    pub row: Option<Map<String, Value>>,
    #[serde(default)]
    pub original: Option<Map<String, Value>>,
    #[serde(default)]
    pub updated: Option<Map<String, Value>>,
    #[serde(default)]
    pub sql: Option<String>,
    #[serde(default)]
    pub params: Option<Vec<Value>>,
    #[serde(default)]
    pub width: Option<u16>,
    #[serde(default)]
    pub height: Option<u16>,
}
#[derive(Serialize)]
struct ProtocolResponse<T: Serialize> {
    ok: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    result: Option<T>,
    #[serde(skip_serializing_if = "Option::is_none")]
    error: Option<DbError>,
    trace: Vec<TraceEntry>,
}
impl SQLiteAdapter {
    pub fn protocol(&self, req: ProtocolRequest) -> Value {
        if self.postgres {
            return match pg_read::Reader::new(pg_read::BlobStyle::Base64)
                .with_read_only(self.read_only)
                .operation(&serde_json::to_value(&req).unwrap())
            {
                Ok(v) => json!({"ok":true,"result":v,"trace":[]}),
                Err(e) => {
                    json!({"ok":false,"error":{"class":e.kind,"message":e.message},"trace":[]})
                }
            };
        }

        self.clear_trace();
        let result: Result<Value, DbError> = (|| {
            let table = || {
                req.table
                    .as_deref()
                    .ok_or_else(|| DbError::adapter("Missing table"))
            };
            match req.op.as_str() {
                "list_tables" => {
                    Ok(serde_json::to_value(self.list_tables(req.include_views)?).unwrap())
                }
                "schema" => Ok(serde_json::to_value(self.schema_info(table()?)?).unwrap()),
                "foreign_keys" => Ok(serde_json::to_value(self.foreign_keys(table()?)?).unwrap()),
                "page" => Ok(serde_json::to_value(self.fetch_page(
                    table()?,
                    req.limit.unwrap_or(50),
                    req.offset.unwrap_or(0),
                    req.filter.as_deref(),
                    req.sort.as_deref(),
                )?)
                .unwrap()),
                "apply_batch" => Ok(json!(self.apply_batch(table()?, &req.actions)?)),
                "count" => Ok(json!(self.count_rows(table()?, req.filter.as_deref())?)),
                "insert" => Ok(json!(self.insert_row(
                    table()?,
                    req.row
                        .as_ref()
                        .ok_or_else(|| DbError::adapter("Missing row"))?
                )?)),
                "update" => Ok(json!(self.update_row(
                    table()?,
                    req.original
                        .as_ref()
                        .ok_or_else(|| DbError::adapter("Missing original row"))?,
                    req.updated
                        .as_ref()
                        .ok_or_else(|| DbError::adapter("Missing updated row"))?
                )?)),
                "delete" => Ok(json!(self.delete_row(
                    table()?,
                    req.original
                        .as_ref()
                        .ok_or_else(|| DbError::adapter("Missing original row"))?
                )?)),
                "raw" => Ok(serde_json::to_value(self.execute_readonly(
                    req.sql.as_deref().unwrap_or(""),
                    req.params.as_deref().unwrap_or(&[]),
                    req.limit.unwrap_or(50),
                    req.offset.unwrap_or(0),
                )?)
                .unwrap()),
                "render" => {
                    let page = self.fetch_page(
                        table()?,
                        req.limit.unwrap_or(50),
                        req.offset.unwrap_or(0),
                        req.filter.as_deref(),
                        req.sort.as_deref(),
                    )?;
                    let count = self.count_rows(table()?, req.filter.as_deref())?;
                    let screen = render::render_browser(
                        &page,
                        table()?,
                        count,
                        req.offset.unwrap_or(0),
                        req.width.unwrap_or(100) as usize,
                        req.height.unwrap_or(24) as usize,
                        req.filter.as_deref(),
                        req.sort.as_deref(),
                        self.read_only,
                    );
                    Ok(json!({"screen":screen}))
                }
                other => Err(DbError::new(
                    "UnsupportedOperationError",
                    format!("Unknown protocol operation: {other}"),
                )),
            }
        })();
        let trace = self.take_trace();
        match result {
            Ok(result) => serde_json::to_value(ProtocolResponse::<Value> {
                ok: true,
                result: Some(result),
                error: None,
                trace,
            })
            .unwrap(),
            Err(error) => serde_json::to_value(ProtocolResponse::<Value> {
                ok: false,
                result: None,
                error: Some(error),
                trace,
            })
            .unwrap(),
        }
    }
}

fn write_error(s: String) -> DbError {
    DbError::new(
        if s.contains("primary key") {
            "RowIdentityError"
        } else if s.contains("64-bit") {
            "OverflowError"
        } else {
            "AdapterError"
        },
        s,
    )
}

fn ensure_single(sql: &str) -> Result<(), DbError> {
    let mut chars = sql.chars().peekable();
    let mut quote = None;
    let mut line = false;
    let mut block = false;
    let mut ended = false;
    while let Some(c) = chars.next() {
        if line {
            if c == '\n' {
                line = false;
            }
            continue;
        }
        if block {
            if c == '*' && chars.peek() == Some(&'/') {
                chars.next();
                block = false;
            }
            continue;
        }
        if let Some(q) = quote {
            if c == q {
                if chars.peek() == Some(&q) {
                    chars.next();
                } else {
                    quote = None;
                }
            }
            continue;
        }
        if c == '-' && chars.peek() == Some(&'-') {
            chars.next();
            line = true;
            continue;
        }
        if c == '/' && chars.peek() == Some(&'*') {
            chars.next();
            block = true;
            continue;
        }
        if c.is_whitespace() {
            continue;
        }
        if ended {
            return Err(DbError::new(
                "AdapterError",
                "You can only execute one statement at a time",
            ));
        }
        match c {
            '\'' | '"' | '`' => quote = Some(c),
            '[' => quote = Some(']'),
            ';' => ended = true,
            _ => {}
        }
    }
    Ok(())
}

pub mod bulk_editor;

pub mod export_file;
