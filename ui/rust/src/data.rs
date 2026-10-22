use crate::bridge::Worker;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::io::Write;
use std::{
    collections::{BTreeMap, HashMap},
    path::PathBuf,
    sync::mpsc,
};
pub type Result<T> = std::result::Result<T, String>;
#[derive(Clone, Debug)]
pub struct Config {
    pub engine: String,
    pub database: String,
    pub table: Option<String>,
    pub backend: String,
    pub worker: PathBuf,
    pub read_only: bool,
}
impl Default for Config {
    fn default() -> Self {
        Self {
            engine: "sqlite".into(),
            database: String::new(),
            table: None,
            backend: "codex-rust".into(),
            worker: PathBuf::new(),
            read_only: false,
        }
    }
}
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct Page {
    pub headers: Vec<String>,
    pub rows: Vec<Vec<Value>>,
    #[serde(skip)]
    pub sql_nulls: Vec<std::collections::HashSet<String>>,
}
#[derive(Clone, Debug, Default)]
pub struct Meta {
    pub postgres: bool,
    pub insert_omit: std::collections::HashSet<String>,
    pub generated: std::collections::HashSet<String>,
    pub identity_always: std::collections::HashSet<String>,
    pub schema: Page,
    pub keys: Vec<String>,
    pub types: HashMap<String, String>,
    pub relations: Vec<Relation>,
}
#[derive(Clone, Debug)]
pub struct Relation {
    pub table: String,
    pub pairs: Vec<(String, String)>,
}
#[derive(Clone, Debug, Default)]
pub struct Spec {
    pub table: String,
    pub raw: Option<String>,
    pub base: Option<(String, Vec<Value>)>,
    pub filter: String,
    pub sort: Option<(String, String)>,
    pub offset: usize,
    pub limit: usize,
}
#[derive(Clone, Debug)]
pub struct Bundle {
    pub spec: Spec,
    pub page: Page,
    pub total: usize,
    pub meta: Meta,
}
#[derive(Clone, Debug)]
pub enum Job {
    Init,
    Load(Spec),
    Inspect(String),
    Diagram(String),
    Fetch(Spec, String),
    Apply(String, Vec<Value>),
}
#[derive(Debug)]
pub enum Reply {
    Init(Vec<String>),
    Loaded(Bundle),
    Inspected(Page),
    Diagram(crate::diagram::Diagram, String),
    Fetched(Page, Meta, String),
    Applied(Value),
    Error(String),
}
#[derive(Debug)]
pub struct Completion {
    pub id: u64,
    pub reply: Reply,
}
pub struct Request {
    id: u64,
    job: Job,
}
pub struct Sender {
    tx: mpsc::Sender<Request>,
    control: std::sync::Arc<crate::bridge::Control>,
}
impl Sender {
    /// Every submission supersedes older reads; confirmed writes are never superseded.
    pub fn send(&self, job: Job) -> Result<u64> {
        use std::sync::atomic::Ordering;
        let _guard = self
            .control
            .submitting
            .lock()
            .unwrap_or_else(|e| e.into_inner());
        if self.control.stopped.load(Ordering::Acquire) {
            return Err("Worker stopped".into());
        }
        if self.control.writing.load(Ordering::Acquire) {
            return Err("Transaction pending: wait for its outcome".into());
        }
        let write = matches!(&job, Job::Apply(..));
        let id = self.control.generation.fetch_add(1, Ordering::AcqRel) + 1;
        if write {
            self.control.writing.store(true, Ordering::Release);
        }
        if self.tx.send(Request { id, job }).is_err() {
            self.control.writing.store(false, Ordering::Release);
            return Err("Worker stopped".into());
        }
        Ok(id)
    }
}
pub struct Async {
    pub send: Sender,
    pub receive: mpsc::Receiver<Completion>,
    pub pid: std::sync::Arc<std::sync::atomic::AtomicU32>,
    pub control: std::sync::Arc<crate::bridge::Control>,
}
impl Async {
    pub fn start(config: Config) -> Self {
        use std::sync::atomic::Ordering;
        let (tx, rx) = mpsc::channel::<Request>();
        let (out, input) = mpsc::channel();
        let pid = std::sync::Arc::new(std::sync::atomic::AtomicU32::new(0));
        let shared = pid.clone();
        let control = std::sync::Arc::new(crate::bridge::Control::default());
        let child_control = control.clone();
        std::thread::spawn(move || {
            let mut active: Option<Worker> = None;
            let mut cache = HashMap::new();
            for Request { id, job } in rx {
                if child_control.stopped.load(Ordering::Acquire) {
                    break;
                }
                let write = matches!(&job, Job::Apply(..));
                if !write && child_control.generation.load(Ordering::Acquire) != id {
                    continue;
                }
                let result = (|| -> Result<Reply> {
                    if active.is_none() {
                        active = Some(Worker::spawn_request(
                            config.clone(),
                            Some(&child_control),
                            id,
                            !write,
                        )?);
                        shared.store(active.as_ref().unwrap().pid(), Ordering::Release);
                    }
                    let worker = active.as_mut().unwrap();
                    worker.context(id, !write);

                    Ok(match job {
                        Job::Init => Reply::Init(
                            serde_json::from_value(
                                worker.request(json!({"op":"tables","include_views":true}))?,
                            )
                            .map_err(|_| "Invalid table list")?,
                        ),
                        Job::Load(spec) => {
                            let meta = if spec.table.is_empty() && spec.raw.is_some() {
                                Meta::default()
                            } else {
                                metadata(worker, &config, &spec.table, &mut cache)?
                            };
                            let (page, total, meta) = fetch(worker, &config, &spec, &meta)?;
                            Reply::Loaded(Bundle {
                                spec,
                                page,
                                total,
                                meta,
                            })
                        }
                        Job::Inspect(table) => Reply::Inspected(inspect(worker, &config, &table)?),
                        Job::Diagram(selected) => {
                            Reply::Diagram(crate::diagram::discover(worker, &config)?, selected)
                        }
                        Job::Fetch(mut spec, purpose) => {
                            spec.offset = 0;
                            spec.limit = 100000;
                            let m = if spec.table.is_empty() && spec.raw.is_some() {
                                Meta::default()
                            } else {
                                metadata(worker, &config, &spec.table, &mut cache)?
                            };
                            let (p, _, m) = fetch(worker, &config, &spec, &m)?;
                            Reply::Fetched(p, m, purpose)
                        }
                        Job::Apply(table, actions) => Reply::Applied(worker.request(
                            json!({"op":"apply_batch","table":table,"actions":actions}),
                        )?),
                    })
                })();
                if active.as_ref().is_some_and(Worker::failed) {
                    active.take();
                    shared.store(0, Ordering::Release);
                    cache.clear();
                }
                if !write && child_control.generation.load(Ordering::Acquire) != id {
                    // Also discard workers cancelled during post-response composition.
                    active.take();
                    shared.store(0, Ordering::Release);
                    cache.clear();
                    continue;
                }
                // Retain the write gate until its completion is acknowledged by the UI.
                if out
                    .send(Completion {
                        id,
                        reply: result.unwrap_or_else(Reply::Error),
                    })
                    .is_err()
                {
                    break;
                }
            }
        });
        Self {
            send: Sender {
                tx,
                control: control.clone(),
            },
            receive: input,
            pid,
            control,
        }
    }
    pub fn cancel_read(&self) -> bool {
        use std::sync::atomic::Ordering;
        let _guard = self
            .control
            .submitting
            .lock()
            .unwrap_or_else(|e| e.into_inner());
        if self.control.writing.load(Ordering::Acquire) {
            return false;
        }
        self.control.generation.fetch_add(1, Ordering::AcqRel);
        true
    }
    pub(crate) fn acknowledge_write(&self, id: u64) {
        use std::sync::atomic::Ordering;
        if self.control.generation.load(Ordering::Acquire) == id {
            self.control.writing.store(false, Ordering::Release);
        }
    }
    pub fn terminate(&self) {
        self.control.shutdown();
    }
    #[cfg(test)]
    pub(crate) fn disconnected() -> Self {
        let (tx, _) = mpsc::channel();
        let (_, receive) = mpsc::channel();
        let control = std::sync::Arc::new(crate::bridge::Control::default());
        Self {
            send: Sender {
                tx,
                control: control.clone(),
            },
            receive,
            pid: Default::default(),
            control,
        }
    }
}
impl Drop for Async {
    fn drop(&mut self) {
        self.terminate();
    }
}

pub fn page(v: Value) -> Result<Page> {
    serde_json::from_value(v).map_err(|_| "Worker returned invalid result shape".into())
}
pub fn raw(
    w: &mut Worker,
    sql: &str,
    params: Vec<Value>,
    limit: usize,
    offset: usize,
) -> Result<Page> {
    page(w.request(json!({"op":"raw","sql":sql,"params":params,"limit":limit,"offset":offset}))?)
}
pub fn quote(s: &str) -> Result<String> {
    if s.is_empty() || s.contains('\0') {
        return Err("Empty/NUL identifier".into());
    }
    Ok(format!("\"{}\"", s.replace('"', "\"\"")))
}
pub fn table_ref(engine: &str, table: &str) -> Result<String> {
    if engine == "sqlite" {
        return quote(table);
    }
    let mut parts = vec![];
    let mut part = String::new();
    let mut quoted = false;
    let mut it = table.chars().peekable();
    while let Some(c) = it.next() {
        match c {
            '"' => {
                if quoted && it.peek() == Some(&'"') {
                    part.push('"');
                    it.next();
                } else {
                    quoted = !quoted
                }
            }
            '.' if !quoted => {
                parts.push(part.clone());
                part.clear()
            }
            _ => part.push(c),
        }
    }
    if quoted {
        return Err("Unclosed identifier quote".into());
    }
    parts.push(part);
    if parts.len() == 1 {
        parts.insert(0, "public".into());
    }
    if parts.len() != 2 {
        return Err("Expected schema.table".into());
    }
    Ok(format!("{}.{}", quote(&parts[0])?, quote(&parts[1])?))
}
fn metadata(
    w: &mut Worker,
    c: &Config,
    table: &str,
    cache: &mut HashMap<String, Meta>,
) -> Result<Meta> {
    if let Some(m) = cache.get(table) {
        return Ok(m.clone());
    }
    let schema = page(w.request(json!({"op":"schema","table":table}))?)?;
    let mut m = Meta {
        schema: schema.clone(),
        postgres: c.engine == "postgres",
        ..Meta::default()
    };
    let name = schema
        .headers
        .iter()
        .position(|s| s == "Column Name")
        .unwrap_or(if c.engine == "sqlite" { 1 } else { 0 });
    let typ = schema
        .headers
        .iter()
        .position(|s| s == "Type")
        .unwrap_or(if c.engine == "sqlite" { 2 } else { 1 });
    for r in &schema.rows {
        if let Some(col) = r.get(name).and_then(Value::as_str) {
            m.types.insert(
                col.into(),
                r.get(typ).and_then(Value::as_str).unwrap_or("TEXT").into(),
            );
        }
    }
    if c.engine == "sqlite" {
        let info = raw(
            w,
            "SELECT name,pk FROM pragma_table_info(?) ORDER BY pk",
            vec![json!(table)],
            100000,
            0,
        )?;
        m.keys = info
            .rows
            .iter()
            .filter(|r| r[1].as_i64().unwrap_or(0) > 0)
            .map(|r| r[0].as_str().unwrap().into())
            .collect();
        let fk=raw(w,"SELECT id,seq,\"table\",\"from\",\"to\" FROM pragma_foreign_key_list(?) ORDER BY id,seq",vec![json!(table)],100000,0)?;
        let mut groups: BTreeMap<i64, Relation> = BTreeMap::new();
        for r in fk.rows {
            let target = r[2].as_str().unwrap().to_string();
            let column = if r[4].is_null() {
                let p = raw(
                    w,
                    "SELECT name FROM pragma_table_info(?) WHERE pk>0 ORDER BY pk",
                    vec![json!(target)],
                    100000,
                    0,
                )?;
                p.rows
                    .get(r[1].as_u64().unwrap_or(0) as usize)
                    .and_then(|r| r[0].as_str())
                    .ok_or("Implicit FK has no paired parent key")?
                    .to_string()
            } else {
                r[4].as_str().unwrap().into()
            };
            groups
                .entry(r[0].as_i64().unwrap())
                .or_insert_with(|| Relation {
                    table: target,
                    pairs: vec![],
                })
                .pairs
                .push((r[3].as_str().unwrap().into(), column));
        }
        m.relations = groups.into_values().collect();
    } else {
        let reference = table_ref("postgres", table)?;
        let native_types=raw(w,"SELECT attname,pg_catalog.format_type(atttypid,NULL) FROM pg_catalog.pg_attribute WHERE attrelid=to_regclass(%s) AND attnum>0 AND NOT attisdropped ORDER BY attnum",vec![json!(reference)],100000,0)?;
        for row in native_types.rows {
            m.types.insert(text(&row[0]), text(&row[1]));
        }
        let defaults=raw(w,"SELECT attname,attgenerated::text,attidentity::text FROM pg_catalog.pg_attribute WHERE attrelid=to_regclass(%s) AND attnum>0 AND NOT attisdropped AND (attgenerated<>'' OR attidentity<>'' OR atthasdef) ORDER BY attnum",vec![json!(reference)],100000,0)?;
        for row in &defaults.rows {
            let name = row[0].as_str().ok_or("Invalid column metadata")?;
            if row[1].as_str().is_some_and(|value| !value.is_empty()) {
                m.generated.insert(name.to_string());
            }
            if row[2].as_str() == Some("a") {
                m.identity_always.insert(name.to_string());
            }
        }
        m.insert_omit = defaults
            .rows
            .iter()
            .filter_map(|r| r[0].as_str().map(str::to_string))
            .collect();
        let pk=raw(w,"SELECT a.attname FROM pg_catalog.pg_index i CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY k(attnum,ord) JOIN pg_catalog.pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.attnum WHERE i.indrelid=to_regclass(%s) AND i.indisprimary ORDER BY k.ord",vec![json!(reference)],100000,0)?;
        m.keys = pk
            .rows
            .iter()
            .map(|r| r[0].as_str().unwrap().to_string())
            .collect();
        let f=raw(w,"SELECT con.oid::bigint,src.attname,ns.nspname,rel.relname,dst.attname FROM pg_catalog.pg_constraint con JOIN pg_catalog.pg_class rel ON rel.oid=con.confrelid JOIN pg_catalog.pg_namespace ns ON ns.oid=rel.relnamespace CROSS JOIN LATERAL unnest(con.conkey) WITH ORDINALITY sk(attnum,ord) CROSS JOIN LATERAL unnest(con.confkey) WITH ORDINALITY dk(attnum,ord) JOIN pg_catalog.pg_attribute src ON src.attrelid=con.conrelid AND src.attnum=sk.attnum JOIN pg_catalog.pg_attribute dst ON dst.attrelid=con.confrelid AND dst.attnum=dk.attnum WHERE con.contype='f' AND con.conrelid=to_regclass(%s) AND sk.ord=dk.ord ORDER BY con.oid,sk.ord",vec![json!(reference)],100000,0)?;
        let mut groups: BTreeMap<i64, Relation> = BTreeMap::new();
        for r in f.rows {
            let target = format!(
                "{}.{}",
                quote(r[2].as_str().unwrap())?,
                quote(r[3].as_str().unwrap())?
            );
            groups
                .entry(r[0].as_i64().unwrap())
                .or_insert_with(|| Relation {
                    table: target,
                    pairs: vec![],
                })
                .pairs
                .push((r[1].as_str().unwrap().into(), r[4].as_str().unwrap().into()));
        }
        m.relations = groups.into_values().collect();
    }

    cache.insert(table.into(), m.clone());
    Ok(m)
}
pub fn expression(s: &str) -> bool {
    static E: std::sync::LazyLock<regex::Regex> = std::sync::LazyLock::new(|| {
        regex::Regex::new(
            r"(?i)(!=|<=|>=|<>|=|<|>|::|->|\b(AND|BETWEEN|ILIKE|IN|IS|LIKE|NOT|OR)\b)",
        )
        .unwrap()
    });
    E.is_match(s)
}
fn fetch(w: &mut Worker, c: &Config, s: &Spec, m: &Meta) -> Result<(Page, usize, Meta)> {
    let mut m = m.clone();
    let mut p;
    let count;
    let mut source: Option<(String, Vec<Value>)> = None;
    if s.raw.is_none() && s.base.is_none() {
        p=page(w.request(json!({"op":"page","table":s.table,"filter":s.filter,"order_by":s.sort.as_ref().map(|(a,b)|json!([a,b])),"limit":s.limit,"offset":s.offset}))?)?;
        count = w
            .request(json!({"op":"count","table":s.table,"filter":s.filter}))?
            .as_u64()
            .ok_or("Invalid count")? as usize;
        if c.engine == "postgres" {
            let mut params = vec![];
            let w = if s.filter.is_empty() {
                String::new()
            } else if expression(&s.filter) {
                format!("WHERE ({})", s.filter)
            } else {
                params = vec![json!(format!("%{}%", s.filter)); p.headers.len()];
                format!(
                    "WHERE {}",
                    p.headers
                        .iter()
                        .map(|h| Ok(format!("CAST({} AS TEXT) LIKE %s", quote(h)?)))
                        .collect::<Result<Vec<_>>>()?
                        .join(" OR ")
                )
            };
            let order = if let Some((col, d)) = &s.sort {
                if !p.headers.contains(col) || !["ASC", "DESC"].contains(&d.as_str()) {
                    return Err("Invalid sort".into());
                }
                format!("ORDER BY {} {d}", quote(col)?)
            } else {
                String::new()
            };
            source = Some((
                format!(
                    "SELECT * FROM {} {w} {order}",
                    table_ref("postgres", &s.table)?
                ),
                params,
            ));
        }
    } else {
        let (sql, mut params) = if let Some(raw) = &s.raw {
            (raw.trim().trim_end_matches(';').to_string(), vec![])
        } else {
            let (base, params) = s.base.clone().unwrap();
            (
                format!(
                    "SELECT * FROM {} WHERE {base}",
                    table_ref(&c.engine, &s.table)?
                ),
                params,
            )
        };
        if sql.to_ascii_uppercase().starts_with("EXPLAIN") {
            if !s.filter.is_empty() || s.sort.is_some() {
                return Err("EXPLAIN supports paging, not filter/sort".into());
            }
            p = raw(w, &sql, params, s.limit, s.offset)?;
            count = raw(
                w,
                &sql,
                if let Some((_, p)) = &s.base {
                    p.clone()
                } else {
                    vec![]
                },
                100000,
                0,
            )?
            .rows
            .len();
        } else {
            let columns = raw(w, &sql, params.clone(), 1, 0)?.headers;
            let mut where_sql = String::new();
            if !s.filter.is_empty() {
                if expression(&s.filter) {
                    where_sql = format!("WHERE ({})", s.filter);
                } else {
                    let placeholder = if c.engine == "postgres" { "%s" } else { "?" };
                    where_sql = format!(
                        "WHERE {}",
                        columns
                            .iter()
                            .map(|h| Ok(format!("CAST({} AS TEXT) LIKE {placeholder}", quote(h)?)))
                            .collect::<Result<Vec<_>>>()?
                            .join(" OR ")
                    );
                    params.extend(vec![json!(format!("%{}%", s.filter)); columns.len()]);
                }
            }
            let order = if let Some((col, dir)) = &s.sort {
                if !columns.contains(col) || !["ASC", "DESC"].contains(&dir.as_str()) {
                    return Err("Invalid sort column/direction".into());
                }
                format!("ORDER BY {} {dir}", quote(col)?)
            } else {
                String::new()
            };
            let full = format!("SELECT * FROM ({sql}) _ui_rows {where_sql} {order}");
            source = Some((full.clone(), params.clone()));
            p = raw(w, &full, params.clone(), s.limit, s.offset)?;
            let n = raw(
                w,
                &format!("SELECT COUNT(*) FROM ({full}) _ui_count"),
                params,
                1,
                0,
            )?;
            count = n.rows[0][0].as_u64().ok_or("Invalid raw count")? as usize;
        }
        if c.engine == "postgres"
            && !p.rows.is_empty()
            && !sql.to_ascii_uppercase().starts_with("EXPLAIN")
        {
            let types = raw(
                w,
                &format!(
                    "SELECT {} FROM ({sql}) _types LIMIT 1",
                    p.headers
                        .iter()
                        .map(|h| Ok(format!("pg_typeof({})::text", quote(h)?)))
                        .collect::<Result<Vec<_>>>()?
                        .join(",")
                ),
                if let Some((_, p)) = &s.base {
                    p.clone()
                } else {
                    vec![]
                },
                1,
                0,
            )?;
            for (h, t) in p.headers.iter().zip(&types.rows[0]) {
                m.types
                    .insert(h.clone(), t.as_str().unwrap_or("text").into());
            }
        }
    }
    if c.engine == "postgres" {
        if let Some((sql, params)) = source {
            let json_columns = p
                .headers
                .iter()
                .filter(|h| {
                    m.types.get(*h).is_some_and(|t| {
                        t.eq_ignore_ascii_case("json") || t.eq_ignore_ascii_case("jsonb")
                    })
                })
                .cloned()
                .collect::<Vec<_>>();
            if !json_columns.is_empty() {
                let original = p.headers.clone();
                let mut aliases = original.clone();
                let mut projections = vec![];
                for (h, i) in json_columns.iter().zip(0usize..) {
                    let mut alias = format!("__dbcli_sql_null_{i}");
                    while aliases.contains(&alias) {
                        alias.push('_');
                    }
                    aliases.push(alias.clone());
                    projections.push(format!(
                        "(_export_rows.{} IS NULL) AS {}",
                        quote(h)?,
                        quote(&alias)?
                    ));
                }
                let projected = raw(
                    w,
                    &format!(
                        "SELECT _export_rows.*,{} FROM ({sql}) _export_rows",
                        projections.join(",")
                    ),
                    params,
                    s.limit,
                    s.offset,
                )?;
                let n = original.len();
                p = Page {
                    headers: original,
                    rows: vec![],
                    sql_nulls: vec![],
                };
                for mut row in projected.rows {
                    let mut flags = std::collections::HashSet::new();
                    for (i, h) in json_columns.iter().enumerate() {
                        if row.get(n + i) == Some(&Value::Bool(true)) {
                            flags.insert(h.clone());
                        }
                    }
                    row.truncate(n);
                    p.rows.push(row);
                    p.sql_nulls.push(flags);
                }
            }
        }
    }
    for (index, row) in p.rows.iter_mut().enumerate() {
        for (h, v) in p.headers.iter().zip(row) {
            let typ = m.types.get(h).map(|s| s.as_str()).unwrap_or("");
            if c.engine == "postgres"
                && is_json_type(typ)
                && !p
                    .sql_nulls
                    .get(index)
                    .is_some_and(|flags| flags.contains(h))
            {
                *v = json!({"$type":"json","value":json_payload(v)});
            } else if c.engine == "postgres"
                && typ.ends_with("[]")
                && is_json_type(typ.trim_end_matches("[]"))
            {
                *v = wrap_json_array(v);
            } else {
                *v = canonical(v.clone(), typ);
            }
        }
    }
    Ok((p, count, m))
}
fn inspect(w: &mut Worker, c: &Config, t: &str) -> Result<Page> {
    if c.engine == "postgres" {
        return raw(w,"SELECT 'index'::text AS kind,indexname::text AS name,indexdef::text AS definition FROM pg_catalog.pg_indexes WHERE to_regclass(format('%I.%I',schemaname,tablename))=to_regclass(%s) UNION ALL SELECT 'constraint',con.conname,pg_get_constraintdef(con.oid,true) FROM pg_catalog.pg_constraint con WHERE con.conrelid=to_regclass(%s) UNION ALL SELECT 'trigger',tr.tgname,pg_get_triggerdef(tr.oid,true) FROM pg_catalog.pg_trigger tr WHERE tr.tgrelid=to_regclass(%s) AND NOT tr.tgisinternal ORDER BY kind,name",vec![json!(table_ref("postgres",t)?);3],100000,0);
    }
    let mut p=raw(w,"SELECT type AS kind,name,sql AS definition FROM sqlite_master WHERE tbl_name=? AND type IN ('table','index','trigger') ORDER BY type,name",vec![json!(t)],100000,0)?;
    let indexes=raw(w,"SELECT il.name,il.\"unique\",ii.name FROM pragma_index_list(?) il LEFT JOIN pragma_index_info(il.name) ii ORDER BY il.seq,ii.seqno",vec![json!(t)],100000,0)?;
    for r in indexes.rows {
        p.rows.push(vec![
            json!("index column"),
            r[0].clone(),
            json!(format!("unique={} column={}", r[1], text(&r[2]))),
        ]);
    }
    let fks = raw(
        w,
        "SELECT id,seq,\"table\",\"from\",\"to\" FROM pragma_foreign_key_list(?)",
        vec![json!(t)],
        100000,
        0,
    )?;
    for r in fks.rows {
        p.rows.push(vec![
            json!("foreign key"),
            r[3].clone(),
            json!(format!(
                "{} → {} (constraint {} pair {})",
                text(&r[2]),
                text(&r[4]),
                r[0],
                r[1]
            )),
        ]);
    }
    Ok(p)
}
pub fn object(headers: &[String], row: &[Value]) -> Value {
    Value::Object(headers.iter().cloned().zip(row.iter().cloned()).collect())
}
pub fn identity(row: &Value, keys: &[String]) -> Result<String> {
    if keys.is_empty() {
        return Err("This table has no primary key; browsing/export/insert work, edit/delete require a complete key".into());
    }
    let mut p = vec![];
    for k in keys {
        let v = row
            .get(k)
            .filter(|v| !v.is_null())
            .ok_or_else(|| format!("Missing/non-null primary key column {k}"))?;
        p.push((k, v));
    }
    serde_json::to_string(&p).map_err(|e| e.to_string())
}
pub fn hex(s: &str) -> Result<Vec<u8>> {
    if s.len() % 2 != 0 || !s.bytes().all(|b| b.is_ascii_hexdigit()) {
        return Err("Malformed bytes tag".into());
    }
    s.as_bytes()
        .chunks_exact(2)
        .map(|b| {
            u8::from_str_radix(std::str::from_utf8(b).unwrap(), 16)
                .map_err(|_| "Malformed bytes tag".into())
        })
        .collect()
}
pub fn blob(v: &Value) -> Option<Result<Vec<u8>>> {
    use base64::Engine;
    if let Some(s) = v.get("$bytes").and_then(Value::as_str) {
        return Some(hex(s));
    }
    let b = if v.as_object().is_some_and(|m| m.len() == 1) {
        v.get("$blob").and_then(Value::as_str)
    } else if v["$type"] == "blob" {
        v["base64"].as_str()
    } else {
        None
    };
    b.map(|s| {
        base64::engine::general_purpose::STANDARD
            .decode(s)
            .map_err(|_| "Malformed base64 bytes".into())
    })
}
pub fn canonical(v: Value, typ: &str) -> Value {
    if typ.eq_ignore_ascii_case("json") || typ.eq_ignore_ascii_case("jsonb") {
        return v;
    }
    if let Some(Ok(b)) = blob(&v) {
        return json!({"$bytes":b.iter().map(|b|format!("{b:02x}")).collect::<String>()});
    }
    if let Value::Array(a) = v {
        Value::Array(
            a.into_iter()
                .map(|v| canonical(v, typ.trim_end_matches("[]")))
                .collect(),
        )
    } else {
        v
    }
}
fn wrap_json_array(v: &Value) -> Value {
    if let Some(values) = v.as_array() {
        Value::Array(
            values
                .iter()
                .map(|v| {
                    if v.is_null() {
                        Value::Null
                    } else {
                        json!({"$type":"json","value":json_payload(v)})
                    }
                })
                .collect(),
        )
    } else {
        v.clone()
    }
}
fn export_json(v: &Value, typ: &str) -> Value {
    if is_json_type(typ) {
        return json_payload(v).clone();
    }
    if typ.ends_with("[]") && is_json_type(typ.trim_end_matches("[]")) {
        return Value::Array(
            v.as_array()
                .unwrap_or(&vec![])
                .iter()
                .map(|v| json_payload(v).clone())
                .collect(),
        );
    }
    v.clone()
}
pub fn is_json_type(t: &str) -> bool {
    t.eq_ignore_ascii_case("json") || t.eq_ignore_ascii_case("jsonb")
}
pub fn json_envelope(v: &Value) -> bool {
    v.as_object().is_some_and(|m| {
        m.len() == 2 && m.get("$type") == Some(&json!("json")) && m.contains_key("value")
    })
}
pub fn json_payload(v: &Value) -> &Value {
    if json_envelope(v) {
        &v["value"]
    } else {
        v
    }
}
pub fn text(v: &Value) -> String {
    if json_envelope(v) {
        return json_payload(v).to_string();
    }
    if v.is_null() {
        "NULL".into()
    } else if let Some(s) = v.as_str() {
        s.into()
    } else if let Some(s) = v
        .get("value")
        .and_then(Value::as_str)
        .filter(|_| v.get("$type").is_some())
    {
        s.into()
    } else if let Some(Ok(b)) = blob(v) {
        format!("<{} bytes>", b.len())
    } else {
        v.to_string()
    }
}
pub fn editable(row: &Value) -> Value {
    let mut row = row.clone();
    if let Some(o) = row.as_object_mut() {
        for v in o.values_mut() {
            if let Some(j) = v
                .as_str()
                .and_then(|s| serde_json::from_str::<Value>(s).ok())
                .filter(|j| j.is_array() || j.is_object())
            {
                *v = j
            }
        }
    }
    row
}
pub fn normalize_row(
    edited: &Value,
    original: Option<&Value>,
    m: &Meta,
    headers: &[String],
) -> Result<Value> {
    let mut o = edited
        .as_object()
        .cloned()
        .ok_or("Each edited row must be a JSON object")?;
    for (k, v) in &mut o {
        if !headers.contains(k) {
            return Err(format!("Unknown column {k}"));
        }
        let original_json = original
            .and_then(|o| o.get(k))
            .and_then(Value::as_str)
            .and_then(|s| serde_json::from_str::<Value>(s).ok())
            .is_some_and(|j| j.is_object() || j.is_array());
        let declared_json = m
            .types
            .get(k)
            .is_some_and(|s| s.to_ascii_lowercase().contains("json"));
        if m.postgres
            && m.types
                .get(k)
                .is_some_and(|t| t.ends_with("[]") && is_json_type(t.trim_end_matches("[]")))
        {
            *v = wrap_json_array(v);
        } else if m.postgres && m.types.get(k).is_some_and(|t| is_json_type(t)) {
            if !v.is_null() && !json_envelope(v) {
                *v = json!({"$type":"json","value":v.clone()});
            }
        } else if original_json || declared_json {
            if v.is_array() || v.is_object() {
                *v = json!(python_json(v));
            }
        } else if let Some(b) = blob(v) {
            b?;
        }
    }
    Ok(Value::Object(o))
}
pub fn python_json(v: &Value) -> String {
    match v {
        Value::Array(a) => format!(
            "[{}]",
            a.iter().map(python_json).collect::<Vec<_>>().join(", ")
        ),
        Value::Object(o) => format!(
            "{{{}}}",
            o.iter()
                .map(|(k, v)| format!("{}: {}", serde_json::to_string(k).unwrap(), python_json(v)))
                .collect::<Vec<_>>()
                .join(", ")
        ),
        _ => v.to_string(),
    }
}
pub fn bulk_actions(
    value: &Value,
    original: &[Value],
    meta: &Meta,
    headers: &[String],
) -> Result<Vec<Value>> {
    let mut before = HashMap::new();
    for r in original {
        let id = identity(r, &meta.keys)?;
        if before.insert(id, r).is_some() {
            return Err("Duplicate original primary key".into());
        }
    }
    let o = value
        .as_object()
        .ok_or("Bulk editor requires {updates:[],insert:[],delete:[]}")?;
    if o.keys()
        .any(|k| !["updates", "insert", "inserts", "delete"].contains(&k.as_str()))
    {
        return Err("Unknown bulk plan field".into());
    }
    let mut used = std::collections::HashSet::new();
    let mut out = vec![];
    for kind in ["updates", "delete"] {
        let entries = o
            .get(kind)
            .and_then(Value::as_array)
            .ok_or_else(|| format!("{kind} must be an array"))?;
        for entry in entries {
            let raw = if kind == "updates" {
                &entry["original"]
            } else {
                entry
            };
            let id = identity(raw, &meta.keys)?;
            let original = *before
                .get(&id)
                .ok_or("Identity is not in the immutable original selection")?;
            if raw != original {
                return Err("Original snapshot was changed in editor".into());
            }
            if !used.insert(id) {
                return Err("Duplicate update/delete identity".into());
            }
            if kind == "updates" {
                let row = normalize_row(&entry["row"], Some(original), meta, headers)?;
                if identity(&row, &meta.keys)? != identity(original, &meta.keys)? {
                    return Err("Bulk key changes require single-row editor".into());
                }
                if row != *original {
                    out.push(json!({"kind":"update","original":original,"row":row}));
                }
            } else {
                out.push(json!({"kind":"delete","original":original}));
            }
        }
    }
    let inserts = o
        .get("insert")
        .or_else(|| o.get("inserts"))
        .and_then(Value::as_array)
        .ok_or("insert must be an array")?;
    for r in inserts {
        out.push(json!({"kind":"insert","row":normalize_row(r,None,meta,headers)?}));
    }
    Ok(out)
}
pub fn export(
    path: &std::path::Path,
    format: &str,
    engine: &str,
    table: &str,
    p: &Page,
    m: &Meta,
) -> Result<()> {
    if !["json", "csv", "sql"].contains(&format) {
        return Err("Format must be json/csv/sql".into());
    }
    crate::export_file::write_new(path, |file| {
        write_export(file, format, engine, table, p, m).map_err(std::io::Error::other)
    })
    .map_err(|error| {
        if error.kind() == std::io::ErrorKind::AlreadyExists {
            "Export destination already exists; choose another filename".into()
        } else {
            format!("Could not export: {error}")
        }
    })
}

fn write_export(
    file: &mut std::fs::File,
    format: &str,
    engine: &str,
    table: &str,
    p: &Page,
    m: &Meta,
) -> Result<()> {
    match format {
        "json" => {
            let rows = p
                .rows
                .iter()
                .map(|r| {
                    let mut row = object(&p.headers, r);
                    if engine == "postgres" {
                        for (h, v) in row.as_object_mut().unwrap() {
                            if let Some(t) = m.types.get(h) {
                                *v = export_json(v, t);
                            }
                        }
                    }
                    row
                })
                .collect::<Vec<_>>();
            serde_json::to_writer_pretty(&mut *file, &rows)
                .map_err(|_| "Could not write export file".into())
        }
        "csv" => {
            let mut w = csv::WriterBuilder::new()
                .terminator(csv::Terminator::CRLF)
                .from_writer(&mut *file);
            w.write_record(&p.headers).map_err(|_| "CSV write failed")?;
            for row in &p.rows {
                w.write_record(row.iter().map(|v| {
                    if v.is_null() {
                        String::new()
                    } else if blob(v).is_some() {
                        v.to_string()
                    } else {
                        text(v)
                    }
                }))
                .map_err(|_| "CSV write failed")?;
            }
            w.flush().map_err(|_| "CSV flush failed".into())
        }
        "sql" => {
            let target = table_ref(engine, table)?;
            let indices = p
                .headers
                .iter()
                .enumerate()
                .filter(|(_, h)| engine != "postgres" || !m.generated.contains(*h))
                .map(|(index, _)| index)
                .collect::<Vec<_>>();
            let cols = indices
                .iter()
                .map(|&index| quote(&p.headers[index]))
                .collect::<Result<Vec<_>>>()?
                .join(", ");
            let overriding = if engine == "postgres"
                && indices
                    .iter()
                    .any(|&index| m.identity_always.contains(&p.headers[index]))
            {
                " OVERRIDING SYSTEM VALUE"
            } else {
                ""
            };
            let mut out = String::new();
            for (row_index, r) in p.rows.iter().enumerate() {
                if r.len() != p.headers.len() {
                    return Err("Export row width mismatch".into());
                }
                if indices.is_empty() {
                    out += &format!("INSERT INTO {target} DEFAULT VALUES;\n");
                    continue;
                }
                let values = indices
                    .iter()
                    .map(|&index| {
                        let (h, v) = (&p.headers[index], &r[index]);
                        let typ = m.types.get(h).map(String::as_str).unwrap_or("");
                        if v.is_null()
                            && engine == "postgres"
                            && (typ == "json" || typ == "jsonb")
                            && p.sql_nulls
                                .get(row_index)
                                .is_some_and(|flags| !flags.contains(h))
                        {
                            Ok("'null'".to_string())
                        } else {
                            sql_literal(v, engine, typ)
                        }
                    })
                    .collect::<Result<Vec<_>>>()?
                    .join(", ");
                out += &format!("INSERT INTO {target} ({cols}){overriding} VALUES ({values});\n");
            }
            file.write_all(out.as_bytes())
                .map_err(|_| "Could not write SQL export".into())
        }
        _ => Err("Format must be json/csv/sql".into()),
    }
}
pub fn sql_literal(v: &Value, engine: &str, typ: &str) -> Result<String> {
    if v.is_null() {
        return Ok("NULL".into());
    }
    let quoted = |s: &str| format!("'{}'", s.replace('\'', "''"));
    if engine == "postgres" && (typ == "json" || typ == "jsonb") {
        return Ok(quoted(&json_payload(v).to_string()));
    }
    if let Some(b) = blob(v) {
        let h = b?.iter().map(|b| format!("{b:02x}")).collect::<String>();
        return Ok(if engine == "sqlite" {
            format!("X'{h}'")
        } else {
            format!("decode('{h}','hex')")
        });
    }
    if v.is_number() {
        return Ok(v.to_string());
    }
    if let Some(b) = v.as_bool() {
        return Ok(if b { "TRUE" } else { "FALSE" }.into());
    }
    if let Some(s) = v
        .get("value")
        .and_then(Value::as_str)
        .filter(|_| v.get("$type").is_some())
    {
        return Ok(quoted(s));
    }
    if engine == "postgres" && (typ == "ARRAY" || typ.ends_with("[]")) {
        fn arr(v: &Value) -> String {
            if let Some(a) = v.as_array() {
                format!("{{{}}}", a.iter().map(arr).collect::<Vec<_>>().join(","))
            } else if v.is_null() {
                "NULL".into()
            } else {
                format!("\"{}\"", text(v).replace('\\', "\\\\").replace('"', "\\\""))
            }
        }
        return Ok(quoted(&arr(v)));
    }
    Ok(quoted(
        &v.as_str()
            .map(str::to_owned)
            .unwrap_or_else(|| python_json(v)),
    ))
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn every_export_format_refuses_existing_paths_and_leaves_original_bytes() {
        use std::os::unix::fs::{symlink, PermissionsExt};
        let directory = tempfile::tempdir().unwrap();
        let page = Page {
            headers: vec!["id".into(), "label".into()],
            rows: vec![vec![json!(1), json!("synthetic")]],
            ..Page::default()
        };
        for format in ["json", "csv", "sql"] {
            let path = directory.path().join(format!("rows.{format}"));
            export(&path, format, "sqlite", "items", &page, &Meta::default()).unwrap();
            let original = std::fs::read(&path).unwrap();
            assert_eq!(
                std::fs::metadata(&path).unwrap().permissions().mode() & 0o777,
                0o600
            );
            assert!(
                export(&path, format, "sqlite", "items", &page, &Meta::default())
                    .unwrap_err()
                    .contains("already exists")
            );
            assert_eq!(std::fs::read(&path).unwrap(), original);
            let link = directory.path().join(format!("link.{format}"));
            symlink(&path, &link).unwrap();
            assert!(export(&link, format, "sqlite", "items", &page, &Meta::default()).is_err());
            assert_eq!(std::fs::read(&path).unwrap(), original);
        }
        let invalid = directory.path().join("invalid.sql");
        assert!(export(
            &invalid,
            "unknown",
            "sqlite",
            "items",
            &page,
            &Meta::default()
        )
        .is_err());
        assert!(!invalid.exists());
    }

    #[test]
    fn postgres_sql_export_generated_identity_and_defaults() {
        let folder = tempfile::tempdir().unwrap();
        let path = folder.path().join("rows.sql");
        let mut m = Meta::default();
        m.generated.insert("computed".into());
        m.identity_always.insert("id".into());
        let p = Page {
            headers: vec!["computed".into(), "id".into(), "label".into()],
            rows: vec![vec![json!(6), json!(42), json!("it's fine")]],
            ..Page::default()
        };
        export(&path, "sql", "postgres", "public.items", &p, &m).unwrap();
        assert_eq!(std::fs::read_to_string(&path).unwrap(),
            "INSERT INTO \"public\".\"items\" (\"id\", \"label\") OVERRIDING SYSTEM VALUE VALUES (42, 'it''s fine');\n");
        // JSON/CSV retain all projected columns, including computed ones.
        std::fs::remove_file(&path).unwrap();
        export(&path, "json", "postgres", "public.items", &p, &m).unwrap();
        assert!(std::fs::read_to_string(&path).unwrap().contains("computed"));
        let generated_only = Page {
            headers: vec!["computed".into()],
            rows: vec![vec![json!(6)], vec![json!(8)]],
            ..Page::default()
        };
        std::fs::remove_file(&path).unwrap();
        export(
            &path,
            "sql",
            "postgres",
            "public.items",
            &generated_only,
            &m,
        )
        .unwrap();
        assert_eq!(std::fs::read_to_string(&path).unwrap(),
            "INSERT INTO \"public\".\"items\" DEFAULT VALUES;\nINSERT INTO \"public\".\"items\" DEFAULT VALUES;\n");
        let without_id = Page {
            headers: vec!["label".into()],
            rows: vec![vec![json!("text")]],
            ..Page::default()
        };
        std::fs::remove_file(&path).unwrap();
        export(&path, "sql", "postgres", "public.items", &without_id, &m).unwrap();
        assert!(!std::fs::read_to_string(&path)
            .unwrap()
            .contains("OVERRIDING"));
        let by_default = Meta::default();
        std::fs::remove_file(&path).unwrap();
        export(&path, "sql", "postgres", "public.items", &p, &by_default).unwrap();
        assert!(!std::fs::read_to_string(&path)
            .unwrap()
            .contains("OVERRIDING"));
        let malformed = Page {
            headers: vec!["id".into()],
            rows: vec![vec![]],
            ..Page::default()
        };
        std::fs::remove_file(&path).unwrap();
        assert!(export(&path, "sql", "postgres", "public.items", &malformed, &m).is_err());
        assert!(
            !path.exists(),
            "failed export must not leave a partial destination"
        );
    }

    #[test]
    fn complete_composite_identity_and_atomic_plan() {
        let m = Meta {
            keys: vec!["a".into(), "b".into()],
            ..Meta::default()
        };
        let row = json!({"a":1,"b":"001","v":"old"});
        assert!(identity(&json!({"a":1}), &m.keys).is_err());
        let h = vec!["a".into(), "b".into(), "v".into()];
        let plan = json!({"updates":[{"original":row,"row":{"a":1,"b":"001","v":"new"}}],"delete":[],"insert":[]});
        assert_eq!(
            bulk_actions(&plan, &[row.clone()], &m, &h).unwrap().len(),
            1
        );
        assert!(bulk_actions(
            &json!({"updates":[],"delete":[row.clone(),row.clone()],"insert":[]}),
            &[row],
            &m,
            &h
        )
        .is_err());
    }
    #[test]
    fn json_array_elements_are_wrapped_without_wire_reinterpretation() {
        let value = json!([null,{"$type":"json","value":null},{"$type":"json","value":{"$type":"json","value":null}},{"$bytes":"00ff"},[1,null]]);
        let wrapped = wrap_json_array(&value);
        assert_eq!(
            wrapped[3],
            json!({"$type":"json","value":{"$bytes":"00ff"}})
        );
        assert_eq!(wrapped[4], json!({"$type":"json","value":[1,null]}));
        assert_eq!(wrap_json_array(&wrapped), wrapped);
        assert_eq!(
            export_json(&wrapped, "jsonb[]"),
            json!([null,null,{"$type":"json","value":null},{"$bytes":"00ff"},[1,null]])
        );
    }
    #[test]
    fn postgres_editor_json_is_context_typed_and_null_lossless() {
        let mut m = Meta {
            postgres: true,
            ..Meta::default()
        };
        m.types.insert("payload".into(), "jsonb".into());
        let h = vec!["payload".into(), "code".into()];
        for value in [
            json!({"$bytes":"00ff"}),
            json!({"$type":"json","value":null}),
            json!("00123"),
            json!(9007199254740993u64),
        ] {
            let e = json!({"payload":{"$type":"json","value":value},"code":"00123"});
            let result = normalize_row(&editable(&e), Some(&e), &m, &h).unwrap();
            assert_eq!(result, e);
            assert_eq!(
                sql_literal(&result["payload"], "postgres", "jsonb").unwrap(),
                format!("'{}'", value.to_string().replace('\'', "''"))
            );
        }
        let sql_null = json!({"payload":null});
        assert_eq!(normalize_row(&sql_null, None, &m, &h).unwrap(), sql_null);
        let json_null = json!({"payload":{"$type":"json","value":null}});
        assert_eq!(normalize_row(&json_null, None, &m, &h).unwrap(), json_null);
    }
    #[test]
    fn json_blob_tags_and_text_are_preserved() {
        let row = json!({"json":"{\"$bytes\":\"00ff\"}","blob":{"$bytes":"00ff"},"code":"00123"});
        let h = vec!["json".into(), "blob".into(), "code".into()];
        let n = normalize_row(&editable(&row), Some(&row), &Meta::default(), &h).unwrap();
        assert!(n["json"].is_string());
        assert_eq!(n["blob"], row["blob"]);
        assert_eq!(n["code"], "00123");
        assert!(hex("0z").is_err());
    }
    #[test]
    fn json_null_and_sql_null_exports_retain_text_and_scalar_types() {
        let dir = tempfile::tempdir().unwrap();
        let mut meta = Meta::default();
        meta.types.insert("j".into(), "jsonb".into());
        let page = Page {
            headers: vec!["j".into(), "text".into()],
            rows: vec![
                vec![Value::Null, json!("{\"looks\":true}")],
                vec![Value::Null, json!("00123")],
            ],
            sql_nulls: vec![
                std::collections::HashSet::new(),
                ["j".to_string()].into_iter().collect(),
            ],
        };
        export(
            &dir.path().join("rows.sql"),
            "sql",
            "postgres",
            "public.items",
            &page,
            &meta,
        )
        .unwrap();
        let sql = std::fs::read_to_string(dir.path().join("rows.sql")).unwrap();
        assert!(sql.contains("VALUES ('null',"));
        assert!(sql.contains("VALUES (NULL,"));
        export(
            &dir.path().join("rows.json"),
            "json",
            "postgres",
            "public.items",
            &page,
            &meta,
        )
        .unwrap();
        let json: Value =
            serde_json::from_slice(&std::fs::read(dir.path().join("rows.json")).unwrap()).unwrap();
        assert_eq!(json[0]["text"], "{\"looks\":true}");
        assert_eq!(json[1]["text"], "00123");
        assert_eq!(
            sql_literal(&serde_json::json!("hello"), "postgres", "jsonb").unwrap(),
            "'\"hello\"'"
        );
    }
    #[test]
    fn json_array_and_pg_array_export_differ() {
        assert_eq!(
            sql_literal(&json!([1, null]), "postgres", "jsonb").unwrap(),
            "'[1,null]'"
        );
        assert_eq!(
            sql_literal(&json!([1, null]), "postgres", "ARRAY").unwrap(),
            "'{\"1\",NULL}'"
        );
        assert_eq!(
            table_ref("postgres", "x.\"Mixed Table\"").unwrap(),
            "\"x\".\"Mixed Table\""
        );
    }
}
