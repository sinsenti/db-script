//! Native PostgreSQL/MySQL adapters for the shared JSONL remote protocol.
//! Connections are opened per operation, just like the Python DB-API adapters.
use crate::{
    build_filter_clause_for_dialect, build_order_clause_for_dialect, build_pk_where_for_dialect,
    json_value_to_text, quote_identifier_for_dialect, validate_page, DbError, ProtocolRequest,
    QueryResult, TraceEntry,
};
use base64::{engine::general_purpose::STANDARD as BASE64, Engine};
use bytes::BytesMut;
use mysql::{
    consts::ColumnType as MyColumnType, prelude::Queryable, Conn as MyConn, Opts,
    Params as MyParams, Value as MyValue,
};
use postgres::{
    types::{IsNull, ToSql, Type as PgType},
    Client as PgClient, NoTls,
};
use serde_json::{json, Map, Number, Value};
use std::{env, error::Error};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RemoteEngine {
    Postgres,
    Mysql,
}
impl RemoteEngine {
    pub fn parse(value: &str) -> Result<Self, DbError> {
        match value {
            "postgres" => Ok(Self::Postgres),
            "mysql" => Ok(Self::Mysql),
            _ => Err(DbError::new(
                "ValueError",
                "remote engine must be postgres or mysql",
            )),
        }
    }
    fn name(self) -> &'static str {
        match self {
            Self::Postgres => "postgres",
            Self::Mysql => "mysql",
        }
    }
    fn schema_env(self) -> &'static str {
        match self {
            Self::Postgres => "public",
            Self::Mysql => "test",
        }
    }
    fn url_env(self) -> &'static str {
        match self {
            Self::Postgres => "DBCLI_POSTGRES_URL",
            Self::Mysql => "DBCLI_MYSQL_URL",
        }
    }

    fn validate_fixture_url(self, value: &str) -> Result<(), DbError> {
        let variable = self.url_env();
        let approved = url::Url::parse(value).ok().is_some_and(|url| {
            let (schemes, username, password, port, database) = match self {
                Self::Postgres => (
                    &["postgres", "postgresql"][..],
                    "postgres",
                    "postgres",
                    55432,
                    "postgres",
                ),
                Self::Mysql => (&["mysql"][..], "root", "root", 53306, "test"),
            };
            schemes.contains(&url.scheme())
                && matches!(url.host_str(), Some("localhost" | "127.0.0.1"))
                && url.port() == Some(port)
                && url.username() == username
                && url.password() == Some(password)
                && url.path().trim_start_matches('/') == database
        });
        if approved {
            Ok(())
        } else {
            Err(DbError::adapter(format!(
                "{variable} is not the approved localhost disposable Compose fixture endpoint"
            )))
        }
    }
}

pub struct RemoteAdapter {
    engine: RemoteEngine,
    read_only: bool,
}
impl RemoteAdapter {
    pub fn new(engine: RemoteEngine, read_only: bool) -> Self {
        Self { engine, read_only }
    }

    pub fn protocol(&self, request: ProtocolRequest) -> Value {
        if self.engine == RemoteEngine::Postgres {
            return crate::SQLiteAdapter::postgres().protocol(request);
        }
        let mut trace = Vec::new();
        let result = self.run(request, &mut trace);
        match result {
            Ok(value) => json!({"ok":true,"result":value,"trace":trace}),
            Err(error) => json!({"ok":false,"error":error,"trace":trace}),
        }
    }

    fn run(&self, request: ProtocolRequest, trace: &mut Vec<TraceEntry>) -> Result<Value, DbError> {
        let table = || {
            request
                .table
                .as_deref()
                .ok_or_else(|| DbError::adapter("Missing table"))
        };
        match request.op.as_str() {
            "list_databases" => {
                let mut session = self.connect()?;
                let sql = match self.engine {
                    RemoteEngine::Postgres => "SELECT current_database()",
                    RemoteEngine::Mysql => "SELECT DATABASE()",
                };
                let result = session.query(trace, sql, &[])?;
                Ok(Value::Array(
                    result
                        .rows
                        .first()
                        .and_then(|row| row.first())
                        .filter(|value| !value.is_null())
                        .map(|value| vec![value.clone()])
                        .unwrap_or_default(),
                ))
            }
            "list_tables" => {
                let table_types = if request.include_views {
                    vec!["BASE TABLE", "VIEW"]
                } else {
                    vec!["BASE TABLE"]
                };
                let placeholders = vec!["?"; table_types.len()].join(", ");
                let sql = format!(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = ? AND table_type IN ({placeholders}) ORDER BY table_name"
                );
                let mut params = vec![Value::String(self.engine.schema_env().to_owned())];
                params.extend(
                    table_types
                        .into_iter()
                        .map(|value| Value::String(value.to_owned())),
                );
                let mut session = self.connect()?;
                let result = session.query(trace, &sql, &params)?;
                Ok(Value::Array(
                    result
                        .rows
                        .into_iter()
                        .filter_map(|row| row.first().cloned())
                        .collect(),
                ))
            }
            "schema" => {
                let table = table()?;
                let sql = "SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns WHERE table_schema = ? AND table_name = ? ORDER BY ordinal_position";
                let mut session = self.connect()?;
                let result = session.query(
                    trace,
                    sql,
                    &[
                        Value::String(self.engine.schema_env().to_owned()),
                        Value::String(table.to_owned()),
                    ],
                )?;
                if result.rows.is_empty() {
                    return Err(DbError::adapter(format!(
                        "Unknown {} table: {table}",
                        self.engine.name()
                    )));
                }
                Ok(serde_json::to_value(QueryResult {
                    headers: vec![
                        "Column Name".into(),
                        "Type".into(),
                        "Nullable".into(),
                        "Default".into(),
                    ],
                    rows: result.rows,
                })
                .unwrap())
            }
            "foreign_keys" => {
                let table = table()?;
                let mut session = self.connect()?;
                self.metadata_columns(&mut session, trace, table)?;
                let (sql, params) = match self.engine {
                    RemoteEngine::Postgres => (
                        "SELECT kcu.column_name, ccu.table_name, ccu.column_name FROM information_schema.table_constraints tc JOIN information_schema.key_column_usage kcu ON kcu.constraint_schema = tc.constraint_schema AND kcu.constraint_name = tc.constraint_name AND kcu.table_schema = tc.table_schema AND kcu.table_name = tc.table_name JOIN information_schema.constraint_column_usage ccu ON ccu.constraint_schema = tc.constraint_schema AND ccu.constraint_name = tc.constraint_name AND ccu.table_schema = tc.table_schema WHERE tc.constraint_schema = ? AND tc.table_schema = ? AND tc.table_name = ? AND tc.constraint_type = 'FOREIGN KEY' ORDER BY kcu.ordinal_position",
                        vec![
                            Value::String(self.engine.schema_env().into()),
                            Value::String(self.engine.schema_env().into()),
                            Value::String(table.into()),
                        ],
                    ),
                    RemoteEngine::Mysql => (
                        "SELECT column_name, referenced_table_name, referenced_column_name FROM information_schema.key_column_usage WHERE table_schema = ? AND table_name = ? AND referenced_table_name IS NOT NULL ORDER BY ordinal_position",
                        vec![
                            Value::String(self.engine.schema_env().into()),
                            Value::String(table.into()),
                        ],
                    ),
                };
                let result = session.query(trace, sql, &params)?;
                let keys = result
                    .rows
                    .into_iter()
                    .filter_map(|row| {
                        Some(json!({
                            "from_col": row.first()?.clone(),
                            "to_table": row.get(1)?.clone(),
                            "to_col": row.get(2)?.clone(),
                        }))
                    })
                    .collect::<Vec<_>>();
                Ok(Value::Array(keys))
            }
            "page" => {
                let table = table()?;
                let limit = request.limit.unwrap_or(50);
                let offset = request.offset.unwrap_or(0);
                validate_page(limit, offset)?;
                let mut session = self.connect()?;
                let columns = self.metadata_columns(&mut session, trace, table)?;
                let (where_sql, mut params) = build_filter_clause_for_dialect(
                    request.filter.as_deref(),
                    &columns,
                    self.engine.name(),
                )?;
                let order = build_order_clause_for_dialect(
                    request.sort.as_deref(),
                    &columns,
                    self.engine.name(),
                )
                .map_err(|error| self.filter_order_error(error))?;
                let sql = format!(
                    "SELECT * FROM {} {} {} LIMIT ? OFFSET ?",
                    self.table_reference(table)?,
                    where_sql,
                    order
                );
                params.push(json!(limit));
                params.push(json!(offset));
                let result = session.query(trace, &sql, &params)?;
                Ok(serde_json::to_value(result).unwrap())
            }
            "count" => {
                let table = table()?;
                let mut session = self.connect()?;
                let columns = self.metadata_columns(&mut session, trace, table)?;
                let (where_sql, params) = build_filter_clause_for_dialect(
                    request.filter.as_deref(),
                    &columns,
                    self.engine.name(),
                )?;
                let sql = format!(
                    "SELECT COUNT(*) FROM {} {}",
                    self.table_reference(table)?,
                    where_sql
                );
                let result = session.query(trace, &sql, &params)?;
                Ok(json!(result
                    .rows
                    .first()
                    .and_then(|row| row.first())
                    .and_then(Value::as_i64)
                    .unwrap_or(0)))
            }
            "insert" => {
                self.ensure_writable()?;
                let table = table()?;
                let row = request
                    .row
                    .as_ref()
                    .ok_or_else(|| DbError::adapter("Missing row"))?;
                let mut session = self.connect()?;
                session.begin()?;
                let columns = self.metadata_columns(&mut session, trace, table)?;
                self.check_columns(table, row.keys(), &columns)?;
                let (sql, params) = self.insert_sql(table, row)?;
                let count = session.execute(trace, &sql, &params)?;
                session.commit()?;
                Ok(json!(count))
            }
            "update" => {
                self.ensure_writable()?;
                let table = table()?;
                let original = request
                    .original
                    .as_ref()
                    .ok_or_else(|| DbError::adapter("Missing original row"))?;
                let updated = request
                    .updated
                    .as_ref()
                    .ok_or_else(|| DbError::adapter("Missing updated row"))?;
                let mut session = self.connect()?;
                session.begin()?;
                let columns = self.metadata_columns(&mut session, trace, table)?;
                let pks = self.primary_key_columns(&mut session, trace, table)?;
                self.check_columns(table, updated.keys(), &columns)?;
                let (where_sql, where_params) =
                    build_pk_where_for_dialect(original, &pks, self.engine.name())?;
                let changed = updated
                    .iter()
                    .filter(|(key, value)| original.get(*key) != Some(*value))
                    .map(|(key, value)| (key.clone(), normalize_write_value(value)))
                    .collect::<Vec<_>>();
                if changed.is_empty() {
                    return Ok(json!(0));
                }
                let assignments = changed
                    .iter()
                    .map(|(key, _)| {
                        Ok(format!(
                            "{} = ?",
                            quote_identifier_for_dialect(key, self.engine.name())?
                        ))
                    })
                    .collect::<Result<Vec<_>, DbError>>()?
                    .join(", ");
                let mut params = changed
                    .into_iter()
                    .map(|(_, value)| value)
                    .collect::<Vec<_>>();
                params.extend(where_params);
                let sql = format!(
                    "UPDATE {} SET {assignments} WHERE {where_sql}",
                    self.table_reference(table)?
                );
                let count = session.execute(trace, &sql, &params)?;
                session.commit()?;
                Ok(json!(count))
            }
            "delete" => {
                self.ensure_writable()?;
                let table = table()?;
                let original = request
                    .original
                    .as_ref()
                    .ok_or_else(|| DbError::adapter("Missing original row"))?;
                let mut session = self.connect()?;
                session.begin()?;
                self.metadata_columns(&mut session, trace, table)?;
                let pks = self.primary_key_columns(&mut session, trace, table)?;
                let (where_sql, params) =
                    build_pk_where_for_dialect(original, &pks, self.engine.name())?;
                let sql = format!(
                    "DELETE FROM {} WHERE {where_sql}",
                    self.table_reference(table)?
                );
                let count = session.execute(trace, &sql, &params)?;
                session.commit()?;
                Ok(json!(count))
            }
            "raw" => self.raw(&request, trace),
            other => Err(DbError::new(
                "UnsupportedOperationError",
                format!("Unknown protocol operation: {other}"),
            )),
        }
    }

    fn raw(
        &self,
        request: &ProtocolRequest,
        trace: &mut Vec<TraceEntry>,
    ) -> Result<Value, DbError> {
        let limit = request.limit.unwrap_or(50);
        let offset = request.offset.unwrap_or(0);
        validate_page(limit, offset)?;
        let raw_sql = request.sql.as_deref().unwrap_or("");
        let statement = validate_readonly_sql(raw_sql)?;
        let params = request.params.as_deref().unwrap_or(&[]);
        let mut session = self.connect()?;
        if statement.to_ascii_uppercase().starts_with("EXPLAIN") {
            let result = session.query(trace, statement, params)?;
            let rows = result
                .rows
                .into_iter()
                .skip(offset as usize)
                .take(limit as usize)
                .collect();
            Ok(serde_json::to_value(QueryResult {
                headers: result.headers,
                rows,
            })
            .unwrap())
        } else {
            let sql = format!("SELECT * FROM ({statement}) AS _dbcli_query LIMIT ? OFFSET ?");
            let mut all_params = params.to_vec();
            all_params.push(json!(limit));
            all_params.push(json!(offset));
            let result = session.query(trace, &sql, &all_params)?;
            Ok(serde_json::to_value(result).unwrap())
        }
    }

    fn connect(&self) -> Result<RemoteSession, DbError> {
        let variable = self.engine.url_env();
        let url = env::var(variable).map_err(|_| {
            DbError::adapter(format!(
                "Unable to connect to {}: required environment variable {variable} is unset",
                self.engine.name()
            ))
        })?;
        self.engine.validate_fixture_url(&url)?;
        let connection = match self.engine {
            RemoteEngine::Postgres => PgClient::connect(&url, NoTls)
                .map(RemoteConnection::Postgres)
                .map_err(|error| self.connection_error(&url, error.to_string()))?,
            RemoteEngine::Mysql => {
                let options = Opts::from_url(&url)
                    .map_err(|error| self.connection_error(&url, error.to_string()))?;
                MyConn::new(options)
                    .map(RemoteConnection::Mysql)
                    .map_err(|error| self.connection_error(&url, error.to_string()))?
            }
        };
        Ok(RemoteSession {
            engine: self.engine,
            connection,
            url,
        })
    }

    fn connection_error(&self, url: &str, error: String) -> DbError {
        DbError::adapter(format!(
            "Unable to connect to {}: {}",
            self.engine.name(),
            redact_url(&error, url)
        ))
    }

    fn table_reference(&self, table: &str) -> Result<String, DbError> {
        Ok(format!(
            "{}.{}",
            quote_identifier_for_dialect(self.engine.schema_env(), self.engine.name())?,
            quote_identifier_for_dialect(table, self.engine.name())?
        ))
    }

    fn metadata_columns(
        &self,
        session: &mut RemoteSession,
        trace: &mut Vec<TraceEntry>,
        table: &str,
    ) -> Result<Vec<String>, DbError> {
        let sql = "SELECT column_name FROM information_schema.columns WHERE table_schema = ? AND table_name = ? ORDER BY ordinal_position";
        let result = session.query(
            trace,
            sql,
            &[
                Value::String(self.engine.schema_env().to_owned()),
                Value::String(table.to_owned()),
            ],
        )?;
        let columns = result
            .rows
            .into_iter()
            .filter_map(|row| row.first().and_then(Value::as_str).map(str::to_owned))
            .collect::<Vec<_>>();
        if columns.is_empty() {
            return Err(DbError::adapter(format!(
                "Unknown {} table: {table}",
                self.engine.name()
            )));
        }
        Ok(columns)
    }

    fn primary_key_columns(
        &self,
        session: &mut RemoteSession,
        trace: &mut Vec<TraceEntry>,
        table: &str,
    ) -> Result<Vec<String>, DbError> {
        let sql = "SELECT kcu.column_name FROM information_schema.table_constraints tc JOIN information_schema.key_column_usage kcu ON kcu.constraint_schema = tc.constraint_schema AND kcu.constraint_name = tc.constraint_name AND kcu.table_schema = tc.table_schema AND kcu.table_name = tc.table_name WHERE tc.constraint_schema = ? AND tc.table_schema = ? AND tc.table_name = ? AND tc.constraint_type = 'PRIMARY KEY' ORDER BY kcu.ordinal_position";
        let result = session.query(
            trace,
            sql,
            &[
                Value::String(self.engine.schema_env().to_owned()),
                Value::String(self.engine.schema_env().to_owned()),
                Value::String(table.to_owned()),
            ],
        )?;
        Ok(result
            .rows
            .into_iter()
            .filter_map(|row| row.first().and_then(Value::as_str).map(str::to_owned))
            .collect())
    }

    fn check_columns<'a>(
        &self,
        table: &str,
        keys: impl Iterator<Item = &'a String>,
        columns: &[String],
    ) -> Result<(), DbError> {
        let mut unknown = keys
            .filter(|key| !columns.contains(key))
            .cloned()
            .collect::<Vec<_>>();
        unknown.sort();
        if unknown.is_empty() {
            return Ok(());
        }
        let repr = unknown
            .iter()
            .map(|value| python_repr(value))
            .collect::<Vec<_>>()
            .join(", ");
        Err(DbError::adapter(format!(
            "Unknown column(s) for {table}: [{repr}]"
        )))
    }

    fn insert_sql(
        &self,
        table: &str,
        row: &Map<String, Value>,
    ) -> Result<(String, Vec<Value>), DbError> {
        if row.is_empty() {
            return Ok(match self.engine {
                RemoteEngine::Postgres => (
                    format!(
                        "INSERT INTO {} DEFAULT VALUES",
                        self.table_reference(table)?
                    ),
                    Vec::new(),
                ),
                RemoteEngine::Mysql => (
                    format!("INSERT INTO {} () VALUES ()", self.table_reference(table)?),
                    Vec::new(),
                ),
            });
        }
        let names = row.keys().cloned().collect::<Vec<_>>();
        let quoted = names
            .iter()
            .map(|name| quote_identifier_for_dialect(name, self.engine.name()))
            .collect::<Result<Vec<_>, _>>()?
            .join(", ");
        let placeholders = vec!["?"; names.len()].join(", ");
        let params = names
            .iter()
            .map(|name| normalize_write_value(&row[name]))
            .collect();
        Ok((
            format!(
                "INSERT INTO {} ({quoted}) VALUES ({placeholders})",
                self.table_reference(table)?
            ),
            params,
        ))
    }

    fn filter_order_error(&self, error: DbError) -> DbError {
        if error.class_name == "ValueError" {
            DbError::adapter(format!(
                "{} operation failed: {}",
                self.engine.name(),
                error.message
            ))
        } else {
            error
        }
    }

    fn ensure_writable(&self) -> Result<(), DbError> {
        if self.read_only {
            Err(DbError::new(
                "ReadOnlyError",
                format!("This {} connection is read-only", self.engine.name()),
            ))
        } else {
            Ok(())
        }
    }
}

struct RemoteSession {
    engine: RemoteEngine,
    connection: RemoteConnection,
    url: String,
}
enum RemoteConnection {
    Postgres(PgClient),
    Mysql(MyConn),
}
impl RemoteSession {
    fn begin(&mut self) -> Result<(), DbError> {
        let engine = self.engine;
        let url = self.url.clone();
        let result = match &mut self.connection {
            RemoteConnection::Postgres(client) => {
                client.batch_execute("BEGIN").map_err(|e| e.to_string())
            }
            RemoteConnection::Mysql(conn) => conn
                .query_drop("START TRANSACTION")
                .map_err(|e| e.to_string()),
        };
        result.map_err(|error| operation_error(engine, &url, error))
    }

    fn commit(&mut self) -> Result<(), DbError> {
        let url = self.url.clone();
        let result = match &mut self.connection {
            RemoteConnection::Postgres(client) => {
                client.batch_execute("COMMIT").map_err(|e| e.to_string())
            }
            RemoteConnection::Mysql(conn) => conn.query_drop("COMMIT").map_err(|e| e.to_string()),
        };
        result.map_err(|error| {
            DbError::adapter(format!(
                "Database commit failed: {}",
                redact_url(&error, &url)
            ))
        })
    }

    fn query(
        &mut self,
        trace: &mut Vec<TraceEntry>,
        sql: &str,
        params: &[Value],
    ) -> Result<QueryResult, DbError> {
        trace.push(TraceEntry {
            sql: sql.to_owned(),
            params: params.iter().map(trace_value).collect(),
        });
        let engine = self.engine;
        let url = self.url.clone();
        match &mut self.connection {
            RemoteConnection::Postgres(client) => {
                let actual_sql = postgres_placeholders(sql);
                let statement = client
                    .prepare(&actual_sql)
                    .map_err(|error| operation_error(engine, &url, error.to_string()))?;
                let headers = statement
                    .columns()
                    .iter()
                    .map(|column| column.name().to_owned())
                    .collect();
                let binds = params.iter().map(PgBind).collect::<Vec<_>>();
                let references = binds
                    .iter()
                    .map(|bind| bind as &(dyn ToSql + Sync))
                    .collect::<Vec<_>>();
                let rows = client
                    .query(&statement, &references)
                    .map_err(|error| operation_error(engine, &url, error.to_string()))?;
                postgres_result(&rows, headers)
                    .map_err(|error| operation_error(engine, &url, error))
            }
            RemoteConnection::Mysql(conn) => {
                let values = params.iter().map(mysql_bind).collect::<Vec<_>>();
                let mut result = conn
                    .exec_iter(sql, MyParams::Positional(values))
                    .map_err(|error| operation_error(engine, &url, error.to_string()))?;
                let columns = result
                    .columns()
                    .as_ref()
                    .iter()
                    .map(|column| {
                        (
                            column.name_str().to_string(),
                            column.column_type(),
                            column.character_set(),
                        )
                    })
                    .collect::<Vec<_>>();
                let headers = columns.iter().map(|(name, _, _)| name.clone()).collect();
                let mut rows = Vec::new();
                while let Some(row) = result.next() {
                    let row =
                        row.map_err(|error| operation_error(engine, &url, error.to_string()))?;
                    rows.push(mysql_row(&row, &columns));
                }
                Ok(QueryResult { headers, rows })
            }
        }
    }

    fn execute(
        &mut self,
        trace: &mut Vec<TraceEntry>,
        sql: &str,
        params: &[Value],
    ) -> Result<u64, DbError> {
        trace.push(TraceEntry {
            sql: sql.to_owned(),
            params: params.iter().map(trace_value).collect(),
        });
        let engine = self.engine;
        let url = self.url.clone();
        match &mut self.connection {
            RemoteConnection::Postgres(client) => {
                let actual_sql = postgres_placeholders(sql);
                let statement = client
                    .prepare(&actual_sql)
                    .map_err(|error| operation_error(engine, &url, error.to_string()))?;
                let binds = params.iter().map(PgBind).collect::<Vec<_>>();
                let references = binds
                    .iter()
                    .map(|bind| bind as &(dyn ToSql + Sync))
                    .collect::<Vec<_>>();
                client
                    .execute(&statement, &references)
                    .map_err(|error| operation_error(engine, &url, error.to_string()))
            }
            RemoteConnection::Mysql(conn) => {
                let values = params.iter().map(mysql_bind).collect::<Vec<_>>();
                let result = conn
                    .exec_iter(sql, MyParams::Positional(values))
                    .map_err(|error| operation_error(engine, &url, error.to_string()))?;
                let affected = result.affected_rows();
                drop(result);
                Ok(affected)
            }
        }
    }
}

fn operation_error(engine: RemoteEngine, url: &str, error: String) -> DbError {
    DbError::adapter(format!(
        "{} operation failed: {}",
        engine.name(),
        redact_url(&error, url)
    ))
}

#[derive(Debug)]
struct PgBind<'a>(&'a Value);
impl ToSql for PgBind<'_> {
    fn to_sql(
        &self,
        ty: &PgType,
        out: &mut BytesMut,
    ) -> Result<IsNull, Box<dyn Error + Sync + Send>> {
        match self.0 {
            Value::Null => Ok(IsNull::Yes),
            Value::Bool(value) => value.to_sql(ty, out),
            Value::Number(number) => {
                if let Some(value) = number.as_i64() {
                    if *ty == PgType::INT2 {
                        i16::try_from(value)?.to_sql(ty, out)
                    } else if *ty == PgType::INT4 {
                        i32::try_from(value)?.to_sql(ty, out)
                    } else if *ty == PgType::OID {
                        u32::try_from(value)?.to_sql(ty, out)
                    } else {
                        value.to_sql(ty, out)
                    }
                } else if let Some(value) = number.as_u64() {
                    if *ty == PgType::INT2 {
                        i16::try_from(value)?.to_sql(ty, out)
                    } else if *ty == PgType::INT4 {
                        i32::try_from(value)?.to_sql(ty, out)
                    } else if *ty == PgType::OID {
                        u32::try_from(value)?.to_sql(ty, out)
                    } else {
                        i64::try_from(value)?.to_sql(ty, out)
                    }
                } else {
                    let value = number.as_f64().ok_or_else(|| {
                        std::io::Error::new(
                            std::io::ErrorKind::InvalidInput,
                            "invalid numeric bind",
                        )
                    })?;
                    value.to_sql(ty, out)
                }
            }
            Value::String(value) => value.to_sql(ty, out),
            Value::Array(_) | Value::Object(_) => json_value_to_text(self.0).to_sql(ty, out),
        }
    }
    fn accepts(_ty: &PgType) -> bool {
        true
    }
    postgres::types::to_sql_checked!();
}

fn postgres_result(rows: &[postgres::Row], headers: Vec<String>) -> Result<QueryResult, String> {
    let mut values = Vec::with_capacity(rows.len());
    for row in rows {
        let mut result = Vec::with_capacity(row.len());
        for (index, column) in row.columns().iter().enumerate() {
            let value = match *column.type_() {
                PgType::BOOL => row
                    .try_get::<_, Option<bool>>(index)
                    .map(|v| v.map(Value::Bool)),
                PgType::INT2 => row
                    .try_get::<_, Option<i16>>(index)
                    .map(|v| v.map(|x| json!(x))),
                PgType::INT4 => row
                    .try_get::<_, Option<i32>>(index)
                    .map(|v| v.map(|x| json!(x))),
                PgType::INT8 => row
                    .try_get::<_, Option<i64>>(index)
                    .map(|v| v.map(|x| json!(x))),
                PgType::OID => row
                    .try_get::<_, Option<u32>>(index)
                    .map(|v| v.map(|x| json!(x))),
                PgType::FLOAT4 => row
                    .try_get::<_, Option<f32>>(index)
                    .map(|v| v.and_then(|x| Number::from_f64(x as f64).map(Value::Number))),
                PgType::FLOAT8 => row
                    .try_get::<_, Option<f64>>(index)
                    .map(|v| v.and_then(|x| Number::from_f64(x).map(Value::Number))),
                PgType::BYTEA => row
                    .try_get::<_, Option<Vec<u8>>>(index)
                    .map(|v| v.map(|x| json!({"$blob":BASE64.encode(x)}))),
                _ => row
                    .try_get::<_, Option<String>>(index)
                    .map(|v| v.map(Value::String)),
            }
            .map_err(|error| error.to_string())?
            .unwrap_or(Value::Null);
            result.push(value);
        }
        values.push(result);
    }
    Ok(QueryResult {
        headers,
        rows: values,
    })
}

fn mysql_bind(value: &Value) -> MyValue {
    if let Some(object) = value.as_object() {
        if object.len() == 1 {
            if let Some(Value::String(encoded)) = object.get("$blob") {
                if let Ok(bytes) = BASE64.decode(encoded) {
                    return MyValue::Bytes(bytes);
                }
            }
        }
        return MyValue::Bytes(json_value_to_text(value).into_bytes());
    }
    match value {
        Value::Null => MyValue::NULL,
        Value::Bool(value) => MyValue::Int(i64::from(*value)),
        Value::Number(number) => {
            if let Some(value) = number.as_i64() {
                MyValue::Int(value)
            } else if let Some(value) = number.as_u64() {
                MyValue::UInt(value)
            } else {
                MyValue::Double(number.as_f64().unwrap_or_default())
            }
        }
        Value::String(value) => MyValue::Bytes(value.as_bytes().to_vec()),
        Value::Array(_) | Value::Object(_) => {
            MyValue::Bytes(json_value_to_text(value).into_bytes())
        }
    }
}

fn mysql_row(row: &mysql::Row, columns: &[(String, MyColumnType, u16)]) -> Vec<Value> {
    columns
        .iter()
        .enumerate()
        .map(|(index, (_, column_type, character_set))| match row.as_ref(index) {
            None | Some(MyValue::NULL) => Value::Null,
            Some(MyValue::Bytes(bytes)) if is_mysql_blob(*column_type, *character_set) => {
                json!({"$blob":BASE64.encode(bytes)})
            }
            Some(MyValue::Bytes(bytes)) => {
                Value::String(String::from_utf8_lossy(bytes).into_owned())
            }
            Some(MyValue::Int(value)) => json!(value),
            Some(MyValue::UInt(value)) => json!(value),
            Some(MyValue::Float(value)) => Number::from_f64(*value as f64)
                .map(Value::Number)
                .unwrap_or(Value::Null),
            Some(MyValue::Double(value)) => Number::from_f64(*value)
                .map(Value::Number)
                .unwrap_or(Value::Null),
            Some(MyValue::Date(year, month, day, hour, minute, second, micros)) => {
                if *hour == 0 && *minute == 0 && *second == 0 && *micros == 0 {
                    Value::String(format!("{year:04}-{month:02}-{day:02}"))
                } else if *micros == 0 {
                    Value::String(format!("{year:04}-{month:02}-{day:02} {hour:02}:{minute:02}:{second:02}"))
                } else {
                    Value::String(format!("{year:04}-{month:02}-{day:02} {hour:02}:{minute:02}:{second:02}.{micros:06}"))
                }
            }
            Some(MyValue::Time(negative, days, hour, minute, second, micros)) => {
                let total_hours = *days * 24 + u32::from(*hour);
                let sign = if *negative { "-" } else { "" };
                if *micros == 0 {
                    Value::String(format!("{sign}{total_hours:02}:{minute:02}:{second:02}"))
                } else {
                    Value::String(format!("{sign}{total_hours:02}:{minute:02}:{second:02}.{micros:06}"))
                }
            }
        })
        .collect()
}

fn is_mysql_blob(column_type: MyColumnType, character_set: u16) -> bool {
    character_set == 63
        && matches!(
            column_type,
            MyColumnType::MYSQL_TYPE_TINY_BLOB
                | MyColumnType::MYSQL_TYPE_MEDIUM_BLOB
                | MyColumnType::MYSQL_TYPE_LONG_BLOB
                | MyColumnType::MYSQL_TYPE_BLOB
        )
}

fn normalize_write_value(value: &Value) -> Value {
    if value
        .as_object()
        .is_some_and(|object| object.len() == 1 && object.get("$blob").is_some())
    {
        value.clone()
    } else if value.is_object() || value.is_array() {
        Value::String(json_value_to_text(value))
    } else {
        value.clone()
    }
}

fn trace_value(value: &Value) -> Value {
    value.clone()
}

fn postgres_placeholders(sql: &str) -> String {
    let mut output = String::with_capacity(sql.len() + 8);
    let chars = sql.char_indices().collect::<Vec<_>>();
    let mut index = 0;
    let mut bind = 0usize;
    let mut quote = None;
    let mut line_comment = false;
    let mut block_comment = false;
    while index < chars.len() {
        let (byte, ch) = chars[index];
        let next = chars.get(index + 1).map(|(_, value)| *value);
        if line_comment {
            output.push(ch);
            if ch == '\n' {
                line_comment = false;
            }
            index += 1;
            continue;
        }
        if block_comment {
            output.push(ch);
            if ch == '*' && next == Some('/') {
                output.push('/');
                index += 2;
                block_comment = false;
            } else {
                index += 1;
            }
            continue;
        }
        if let Some(q) = quote {
            output.push(ch);
            if ch == q {
                if next == Some(q) {
                    output.push(q);
                    index += 2;
                } else {
                    quote = None;
                    index += 1;
                }
            } else if ch == '\\' && q == '\'' && next.is_some() {
                output.push(next.unwrap());
                index += 2;
            } else {
                index += 1;
            }
            continue;
        }
        if ch == '-' && next == Some('-') {
            output.push_str("--");
            index += 2;
            line_comment = true;
            continue;
        }
        if ch == '/' && next == Some('*') {
            output.push_str("/*");
            index += 2;
            block_comment = true;
            continue;
        }
        if ch == '\'' || ch == '"' || ch == '`' {
            quote = Some(ch);
            output.push(ch);
            index += 1;
            continue;
        }
        if ch == '?' {
            bind += 1;
            output.push('$');
            output.push_str(&bind.to_string());
        } else {
            output.push(ch);
        }
        let _ = byte;
        index += 1;
    }
    output
}

fn validate_readonly_sql(sql: &str) -> Result<&str, DbError> {
    let empty = DbError::new(
        "UnsupportedQueryError",
        "A non-empty read-only SQL query is required",
    );
    if sql.trim().is_empty() {
        return Err(empty);
    }
    let statement = sql.trim().trim_end_matches(';').trim();
    let upper = statement.to_ascii_uppercase();
    let allowed = ["SELECT", "WITH", "EXPLAIN"].iter().any(|word| {
        upper.strip_prefix(word).is_some_and(|rest| {
            rest.is_empty()
                || rest
                    .chars()
                    .next()
                    .is_some_and(|ch| !ch.is_ascii_alphanumeric() && ch != '_')
        })
    });
    if !allowed || statement.contains(';') || contains_mutating_keyword(statement) {
        return Err(DbError::new(
            "UnsupportedQueryError",
            "Only single-statement SELECT, read-only WITH and EXPLAIN queries are allowed here",
        ));
    }
    Ok(statement)
}

fn contains_mutating_keyword(sql: &str) -> bool {
    let chars = sql.char_indices().collect::<Vec<_>>();
    let mut index = 0;
    let mut quote = None;
    let mut line_comment = false;
    let mut block_comment = false;
    let mut token = String::new();
    let flush = |token: &mut String| {
        let found = matches!(
            token.as_str(),
            "INSERT"
                | "UPDATE"
                | "DELETE"
                | "ALTER"
                | "DROP"
                | "CREATE"
                | "TRUNCATE"
                | "GRANT"
                | "REVOKE"
                | "CALL"
                | "DO"
        );
        token.clear();
        found
    };
    while index < chars.len() {
        let (_, ch) = chars[index];
        let next = chars.get(index + 1).map(|(_, value)| *value);
        if line_comment {
            if ch == '\n' {
                line_comment = false;
            }
            index += 1;
            continue;
        }
        if block_comment {
            if ch == '*' && next == Some('/') {
                index += 2;
                block_comment = false;
            } else {
                index += 1;
            }
            continue;
        }
        if let Some(q) = quote {
            if ch == q && next == Some(q) {
                index += 2;
            } else if ch == q {
                quote = None;
                index += 1;
            } else if ch == '\\' && q == '\'' && next.is_some() {
                index += 2;
            } else {
                index += 1;
            }
            continue;
        }
        if ch == '-' && next == Some('-') {
            if flush(&mut token) {
                return true;
            }
            line_comment = true;
            index += 2;
            continue;
        }
        if ch == '/' && next == Some('*') {
            if flush(&mut token) {
                return true;
            }
            block_comment = true;
            index += 2;
            continue;
        }
        if ch == '\'' || ch == '"' || ch == '`' {
            if flush(&mut token) {
                return true;
            }
            quote = Some(ch);
            index += 1;
            continue;
        }
        if ch.is_ascii_alphanumeric() || ch == '_' {
            token.push(ch.to_ascii_uppercase());
        } else if flush(&mut token) {
            return true;
        }
        index += 1;
    }
    flush(&mut token)
}

fn python_repr(value: &str) -> String {
    let quote = if value.contains('\'') && !value.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut escaped = String::new();
    for ch in value.chars() {
        match ch {
            '\\' => escaped.push_str("\\\\"),
            '\n' => escaped.push_str("\\n"),
            '\r' => escaped.push_str("\\r"),
            '\t' => escaped.push_str("\\t"),
            '\0' => escaped.push_str("\\x00"),
            c if c == quote => {
                escaped.push('\\');
                escaped.push(c);
            }
            c if (c as u32) < 32 || c == '\u{7f}' => {
                escaped.push_str(&format!("\\x{:02x}", c as u32))
            }
            c => escaped.push(c),
        }
    }
    format!("{quote}{escaped}{quote}")
}

fn redact_url(message: &str, url: &str) -> String {
    let mut redacted = message.replace(url, "<configured fixture URL>");
    if let Some((_, authority)) = url.split_once("://") {
        if let Some((userinfo, _)) = authority.split_once('@') {
            for secret in userinfo.split([':', '%']).filter(|part| part.len() > 2) {
                redacted = redacted.replace(secret, "<redacted>");
            }
        }
    }
    redacted
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fixture_url_guard_accepts_only_the_approved_disposable_targets() {
        assert!(RemoteEngine::Postgres
            .validate_fixture_url("postgresql://postgres:postgres@127.0.0.1:55432/postgres")
            .is_ok());
        assert!(RemoteEngine::Mysql
            .validate_fixture_url("mysql://root:root@localhost:53306/test")
            .is_ok());
        let error = RemoteEngine::Postgres
            .validate_fixture_url("postgresql://user:secret@example.com:5432/app")
            .unwrap_err();
        assert!(!error.message.contains("secret"));
        assert!(error.message.contains("DBCLI_POSTGRES_URL"));
    }

    #[test]
    fn postgres_placeholder_translation_ignores_quoted_question_marks() {
        assert_eq!(
            postgres_placeholders(
                "SELECT '?', \"?\", `?`, col FROM t WHERE a = ? AND b = ? -- ?\n/* ? */"
            ),
            "SELECT '?', \"?\", `?`, col FROM t WHERE a = $1 AND b = $2 -- ?\n/* ? */"
        );
    }

    #[test]
    fn raw_query_guard_matches_remote_contract() {
        for sql in [
            "DELETE FROM t",
            "WITH x AS (SELECT 1) DELETE FROM t",
            "SELECT 1; DELETE FROM t",
        ] {
            assert_eq!(
                validate_readonly_sql(sql).unwrap_err().class_name,
                "UnsupportedQueryError"
            );
        }
        assert_eq!(
            validate_readonly_sql("SELECT 'DELETE'").unwrap(),
            "SELECT 'DELETE'"
        );
        assert_eq!(
            validate_readonly_sql("SELECT 1 -- DELETE\n").unwrap(),
            "SELECT 1 -- DELETE"
        );
    }
}
