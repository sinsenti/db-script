//! Read-only schema snapshots and a private, self-contained browser viewer.
use crate::{bridge::Worker, data::*};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    collections::BTreeMap,
    io::Write,
    path::{Path, PathBuf},
    process::{Command, Stdio},
};

#[derive(Debug, Default, Serialize, Deserialize)]
pub struct Diagram {
    pub engine: String,
    pub tables: Vec<Table>,
    pub relationships: Vec<ForeignKey>,
    pub warnings: Vec<String>,
}
#[derive(Debug, Serialize, Deserialize)]
pub struct Table {
    pub id: String,
    pub schema: String,
    pub name: String,
    pub kind: String,
    pub columns: Vec<Column>,
    pub unique_keys: Vec<Vec<String>>,
    #[serde(default)]
    pub external: bool,
}
#[derive(Debug, Serialize, Deserialize)]
pub struct Column {
    pub name: String,
    pub data_type: String,
    pub nullable: bool,
    pub primary: bool,
    pub generated: bool,
}
#[derive(Debug, Serialize, Deserialize)]
pub struct ForeignKey {
    pub name: String,
    pub from: String,
    pub to: String,
    pub columns: Vec<String>,
    pub target_columns: Vec<String>,
    pub on_update: String,
    pub on_delete: String,
    pub match_type: String,
    pub validated: bool,
    #[serde(default)]
    pub unique: bool,
    #[serde(default)]
    pub optional: bool,
}

// One PostgreSQL catalog statement gives the whole snapshot a consistent view.
// Names and types are metadata; no user rows or connection information are read.
const POSTGRES: &str = r#"
WITH objects AS (
 SELECT c.oid,n.nspname,c.relname,c.relkind,format('%I.%I',n.nspname,c.relname) AS id
 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
 WHERE c.relkind IN ('r','p','v','m','f') AND n.nspname !~ '^pg_'
 AND n.nspname <> 'information_schema' AND has_schema_privilege(n.oid,'USAGE')
 AND has_table_privilege(c.oid,'SELECT')
), entities AS (
 SELECT o.id,jsonb_build_object('id',o.id,'schema',o.nspname,'name',o.relname,
 'kind',CASE o.relkind WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view'
 WHEN 'p' THEN 'partitioned table' WHEN 'f' THEN 'foreign table' ELSE 'table' END,
 'columns',COALESCE((SELECT jsonb_agg(jsonb_build_object(
 'name',a.attname,'data_type',pg_catalog.format_type(a.atttypid,a.atttypmod),
 'nullable',NOT a.attnotnull,'generated',a.attgenerated<>'',
 'primary',EXISTS(SELECT 1 FROM pg_catalog.pg_constraint p WHERE p.conrelid=o.oid
 AND p.contype='p' AND a.attnum=ANY(p.conkey))) ORDER BY a.attnum)
 FROM pg_catalog.pg_attribute a WHERE a.attrelid=o.oid AND a.attnum>0 AND NOT a.attisdropped),'[]'::jsonb),
 'unique_keys',COALESCE((SELECT jsonb_agg(k.cols) FROM (
 SELECT (SELECT jsonb_agg(a.attname ORDER BY u.ord)
 FROM unnest(i.indkey) WITH ORDINALITY u(attnum,ord)
 JOIN pg_catalog.pg_attribute a ON a.attrelid=o.oid AND a.attnum=u.attnum
 WHERE u.ord<=i.indnkeyatts) AS cols
 FROM pg_catalog.pg_index i WHERE i.indrelid=o.oid AND i.indisunique
 AND i.indisvalid AND i.indpred IS NULL AND i.indexprs IS NULL
 ) k WHERE k.cols IS NOT NULL),'[]'::jsonb)) AS entity FROM objects o
), links AS (
 SELECT con.oid,jsonb_build_object('name',con.conname,'from',src.id,
 'to',format('%I.%I',ns.nspname,dst.relname),
 'columns',(SELECT jsonb_agg(a.attname ORDER BY k.ord)
 FROM unnest(con.conkey) WITH ORDINALITY k(attnum,ord)
 JOIN pg_catalog.pg_attribute a ON a.attrelid=con.conrelid AND a.attnum=k.attnum),
 'target_columns',(SELECT jsonb_agg(a.attname ORDER BY k.ord)
 FROM unnest(con.confkey) WITH ORDINALITY k(attnum,ord)
 JOIN pg_catalog.pg_attribute a ON a.attrelid=con.confrelid AND a.attnum=k.attnum),
 'on_update',CASE con.confupdtype WHEN 'a' THEN 'NO ACTION' WHEN 'r' THEN 'RESTRICT'
 WHEN 'c' THEN 'CASCADE' WHEN 'n' THEN 'SET NULL' ELSE 'SET DEFAULT' END,
 'on_delete',CASE con.confdeltype WHEN 'a' THEN 'NO ACTION' WHEN 'r' THEN 'RESTRICT'
 WHEN 'c' THEN 'CASCADE' WHEN 'n' THEN 'SET NULL' ELSE 'SET DEFAULT' END,
 'match_type',CASE con.confmatchtype WHEN 'f' THEN 'FULL' ELSE 'SIMPLE' END,
 'validated',con.convalidated) AS link
 FROM pg_catalog.pg_constraint con JOIN objects src ON src.oid=con.conrelid
 JOIN pg_catalog.pg_class dst ON dst.oid=con.confrelid
 JOIN pg_catalog.pg_namespace ns ON ns.oid=dst.relnamespace WHERE con.contype='f'
)
SELECT jsonb_build_object('engine','postgres',
 'tables',COALESCE((SELECT jsonb_agg(entity ORDER BY id) FROM entities),'[]'::jsonb),
 'relationships',COALESCE((SELECT jsonb_agg(link ORDER BY oid) FROM links),'[]'::jsonb),
 'warnings','[]'::jsonb) AS diagram
"#;

pub fn discover(w: &mut Worker, config: &Config) -> Result<Diagram> {
    if !matches!(config.engine.as_str(), "postgres" | "sqlite") {
        return Err("Schema diagrams support SQLite and PostgreSQL".into());
    }
    let mut diagram = if config.engine == "postgres" {
        let page = raw(w, POSTGRES, vec![], 1, 0)?;
        let value = page
            .rows
            .first()
            .and_then(|r| r.first())
            .ok_or("Missing schema snapshot")?;
        serde_json::from_value(json_payload(value).clone())
            .map_err(|_| "Invalid schema snapshot")?
    } else {
        sqlite(w)?
    };
    if diagram.tables.len() > 1000
        || diagram.relationships.len() > 10000
        || diagram
            .tables
            .iter()
            .map(|t| t.columns.len())
            .sum::<usize>()
            > 20000
    {
        return Err(
            "Schema exceeds diagram limits (1,000 tables / 20,000 columns / 10,000 foreign keys)"
                .into(),
        );
    }
    for fk in &mut diagram.relationships {
        let source = diagram
            .tables
            .iter()
            .find(|t| t.id == fk.from)
            .ok_or("Missing FK source")?;
        fk.unique = source
            .unique_keys
            .iter()
            .any(|key| !key.is_empty() && key.iter().all(|c| fk.columns.contains(c)));
        let nullable: Vec<_> = fk
            .columns
            .iter()
            .map(|name| {
                source
                    .columns
                    .iter()
                    .find(|c| &c.name == name)
                    .is_none_or(|c| c.nullable)
            })
            .collect();
        fk.optional = if fk.match_type == "FULL" {
            nullable.iter().all(|v| *v)
        } else {
            nullable.iter().any(|v| *v)
        };
    }
    // Preserve links to inaccessible/missing targets as explicit placeholders.
    for fk in &diagram.relationships {
        if let Some(target) = diagram.tables.iter_mut().find(|t| t.id == fk.to) {
            if target.external {
                for name in &fk.target_columns {
                    if !target.columns.iter().any(|c| &c.name == name) {
                        target.columns.push(Column {
                            name: name.clone(),
                            data_type: "unknown".into(),
                            nullable: true,
                            primary: false,
                            generated: false,
                        });
                    }
                }
            }
        } else {
            diagram.tables.push(Table {
                id: fk.to.clone(),
                schema: "Unavailable targets".into(),
                name: fk.to.clone(),
                kind: "unavailable referenced table".into(),
                columns: fk
                    .target_columns
                    .iter()
                    .map(|name| Column {
                        name: name.clone(),
                        data_type: "unknown".into(),
                        nullable: true,
                        primary: false,
                        generated: false,
                    })
                    .collect(),
                unique_keys: vec![],
                external: true,
            });
        }
    }
    if diagram.tables.len() > 1000
        || diagram
            .tables
            .iter()
            .map(|t| t.columns.len())
            .sum::<usize>()
            > 20000
    {
        return Err("Schema including unavailable targets exceeds diagram limits".into());
    }
    if diagram.engine == "sqlite" {
        diagram.warnings.push("SQLite foreign keys are declared relationships; enforcement depends on each connection. Schema changes during discovery require a new snapshot.".into());
    }
    diagram.warnings.push("Only declared foreign keys are shown. Views have no inferred links; JSON fields and application-only relationships are not inferred. Parent participation is 0..N (or 0..1 with a unique child key).".into());
    Ok(diagram)
}

fn sqlite(w: &mut Worker) -> Result<Diagram> {
    let entities = raw(w,"SELECT name,type FROM sqlite_schema WHERE type IN ('table','view') AND substr(name,1,7)<>'sqlite_' ORDER BY name",vec![],1001,0)?;
    if entities.rows.len() > 1000 {
        return Err("Schema exceeds diagram limit (1,000 tables)".into());
    }
    let mut graph = Diagram {
        engine: "sqlite".into(),
        ..Diagram::default()
    };
    let mut primary = BTreeMap::<String, Vec<String>>::new();
    let mut column_count = 0;
    for row in entities.rows {
        let name = text(&row[0]);
        let info = raw(
            w,
            "SELECT name,type,\"notnull\",pk,hidden FROM pragma_table_xinfo(?) ORDER BY cid",
            vec![json!(name)],
            100000,
            0,
        )?;
        if info.rows.len() >= 100000 {
            return Err("Too many columns for schema diagram".into());
        }
        column_count += info.rows.len();
        if column_count > 20000 {
            return Err("Schema exceeds diagram limit (20,000 columns)".into());
        }
        let mut keys: Vec<_> = info
            .rows
            .iter()
            .filter_map(|r| r[3].as_i64().filter(|n| *n > 0).map(|n| (n, text(&r[0]))))
            .collect();
        keys.sort_by_key(|(n, _)| *n);
        let pk: Vec<_> = keys.into_iter().map(|(_, name)| name).collect();
        primary.insert(name.clone(), pk.clone());
        let mut unique_keys = if pk.is_empty() { vec![] } else { vec![pk] };
        let indices = raw(
            w,
            "SELECT name,origin FROM pragma_index_list(?) WHERE \"unique\"=1 AND partial=0",
            vec![json!(name)],
            100000,
            0,
        )?;
        if indices.rows.len() >= 100000 {
            return Err("Too many schema indexes".into());
        }
        let has_pk_index = indices.rows.iter().any(|r| r[1].as_str() == Some("pk"));
        for index in indices.rows {
            let cols = raw(
                w,
                "SELECT name FROM pragma_index_info(?) ORDER BY seqno",
                vec![index[0].clone()],
                100000,
                0,
            )?;
            if cols.rows.len() >= 100000 {
                return Err("Too many index columns".into());
            }
            if !cols.rows.is_empty() && cols.rows.iter().all(|r| r[0].is_string()) {
                unique_keys.push(cols.rows.iter().map(|r| text(&r[0])).collect());
            }
        }
        // SQLite rowid-table composite/text primary keys may still permit NULL.
        // An INTEGER PRIMARY KEY or WITHOUT ROWID/STRICT primary key is required.
        let flags = raw(
            w,
            "SELECT wr,strict FROM pragma_table_list WHERE schema='main' AND name=?",
            vec![json!(name)],
            1,
            0,
        )?;
        let strict_pk = flags
            .rows
            .first()
            .is_some_and(|r| r[0].as_i64() == Some(1) || r[1].as_i64() == Some(1));
        let integer_pk = primary[&name].len() == 1
            && info
                .rows
                .iter()
                .any(|r| r[3].as_i64() == Some(1) && text(&r[1]).eq_ignore_ascii_case("INTEGER"))
            && !has_pk_index;
        let columns = info
            .rows
            .iter()
            .filter(|r| r[4].as_i64() != Some(1))
            .map(|r| {
                let pk = r[3].as_i64().unwrap_or(0) > 0;
                Column {
                    name: text(&r[0]),
                    data_type: text(&r[1]),
                    primary: pk,
                    nullable: r[2].as_i64() != Some(1) && !(pk && (strict_pk || integer_pk)),
                    generated: r[4].as_i64().unwrap_or(0) > 1,
                }
            })
            .collect();
        graph.tables.push(Table {
            id: name.clone(),
            schema: "main".into(),
            name,
            kind: text(&row[1]),
            columns,
            unique_keys,
            external: false,
        });
    }
    for table in &graph.tables {
        let fks=raw(w,"SELECT id,seq,\"table\",\"from\",\"to\",on_update,on_delete,\"match\" FROM pragma_foreign_key_list(?) ORDER BY id,seq",vec![json!(table.name)],100000,0)?;
        if fks.rows.len() >= 100000 {
            return Err("Too many foreign keys for schema diagram".into());
        }
        let mut groups = BTreeMap::<i64, ForeignKey>::new();
        for r in fks.rows {
            let id = r[0].as_i64().ok_or("Invalid FK metadata")?;
            let declared_target = text(&r[2]);
            let target = graph
                .tables
                .iter()
                .find(|t| t.name.eq_ignore_ascii_case(&declared_target))
                .map(|t| t.id.clone())
                .unwrap_or(declared_target);
            let target_col = if r[4].is_null() {
                primary
                    .get(&target)
                    .and_then(|p| p.get(r[1].as_u64().unwrap_or(0) as usize))
                    .cloned()
                    .unwrap_or_else(|| "(unresolved primary key)".into())
            } else {
                text(&r[4])
            };
            let fk = groups.entry(id).or_insert_with(|| ForeignKey {
                name: format!("fk_{}_{}", table.name, id),
                from: table.id.clone(),
                to: target,
                columns: vec![],
                target_columns: vec![],
                on_update: text(&r[5]),
                on_delete: text(&r[6]),
                match_type: "SIMPLE".into(),
                validated: true,
                unique: false,
                optional: false,
            });
            fk.columns.push(text(&r[3]));
            fk.target_columns.push(target_col);
        }
        graph.relationships.extend(groups.into_values());
        if graph.relationships.len() > 10000 {
            return Err("Schema exceeds diagram limit (10,000 foreign keys)".into());
        }
    }
    Ok(graph)
}

pub struct Viewer {
    pub path: PathBuf,
}
impl Viewer {
    pub fn create(graph: &Diagram, focus: &str) -> Result<Self> {
        let home_path = std::env::var_os("HOME")
            .map(PathBuf::from)
            .filter(|p| p.is_absolute())
            .ok_or("Cannot find your home directory for browser-accessible diagrams")?;
        Self::create_in(graph, focus, &home_path.join("dbcli-diagrams"))
    }
    fn create_in(graph: &Diagram, focus: &str, root: &Path) -> Result<Self> {
        use std::os::unix::fs::{DirBuilderExt, MetadataExt};
        use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
        // Snap browsers have their own /tmp and cannot read hidden home paths.
        // Keep complete snapshots in a non-hidden, owner-only home directory.
        match std::fs::DirBuilder::new().mode(0o700).create(root) {
            Ok(()) => {}
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {}
            Err(_) => return Err("Cannot create ~/dbcli-diagrams".into()),
        }
        let metadata =
            std::fs::symlink_metadata(root).map_err(|_| "Cannot inspect diagram directory")?;
        if !metadata.is_dir()
            || metadata.file_type().is_symlink()
            || metadata.uid() != unsafe { libc::geteuid() }
            || metadata.mode() & 0o077 != 0
        {
            return Err(
                "Diagram directory must be an owner-only directory (0700), not a symlink".into(),
            );
        }
        let directory = tempfile::Builder::new()
            .prefix("schema-")
            .tempdir_in(root)
            .map_err(|_| "Cannot create private diagram directory")?;
        std::fs::set_permissions(directory.path(), std::fs::Permissions::from_mode(0o700))
            .map_err(|_| "Cannot protect diagram directory")?;
        let path = directory.path().join("index.html");
        // Use internal IDs in the document and Mermaid exports. Database names
        // remain plain text, never DOM IDs, CSS selectors or executable markup.
        let ids: BTreeMap<_, _> = graph
            .tables
            .iter()
            .enumerate()
            .map(|(i, t)| (t.id.as_str(), format!("t{i}")))
            .collect();
        let selected_reference = table_ref(&graph.engine, focus).ok();
        let focus = graph
            .tables
            .iter()
            .find(|t| {
                selected_reference.is_some()
                    && table_ref(&graph.engine, &t.id).ok() == selected_reference
            })
            .and_then(|t| ids.get(t.id.as_str()));
        let tables: Vec<Value> = graph.tables.iter().map(|t| {
            let primary: Vec<_> = t.columns.iter().filter(|c| c.primary).map(|c| c.name.clone()).collect();
            let pk = t.unique_keys.iter().find(|k| k.len()==primary.len() && k.iter().all(|c| primary.contains(c))).cloned().unwrap_or(primary);
            let mut keys = vec![];
            if !pk.is_empty() { keys.push(json!({"name":"PRIMARY KEY","primary":true,"columns":pk})); }
            for (i,k) in t.unique_keys.iter().enumerate() {
                if k != &pk { keys.push(json!({"name":format!("UNIQUE {}",i+1),"primary":false,"columns":k})); }
            }
            json!({"id":ids[t.id.as_str()],"schema":t.schema,"name":t.name,"kind":t.kind,"external":t.external,"reference":t.id,"keys":keys,
                "columns":t.columns.iter().map(|c| json!({"name":c.name,"data_type":c.data_type,"nullable":c.nullable,"generated":c.generated,"default":Value::Null,"identity":""})).collect::<Vec<_>>()})
        }).collect();
        let foreign_keys: Vec<_> = graph.relationships.iter().enumerate().map(|(i,f)| json!({
            "id":format!("f{i}"),"name":f.name,"source":ids[f.from.as_str()],"target":ids[f.to.as_str()],
            "pairs":f.columns.iter().zip(&f.target_columns).collect::<Vec<_>>(),"on_update":f.on_update,"on_delete":f.on_delete,
            "match_type":f.match_type,"validated":f.validated,"optional":f.optional,"unique":f.unique
        })).collect();
        let data = json!({"engine":graph.engine,"focus":focus,"tables":tables,"foreign_keys":foreign_keys,"warnings":graph.warnings})
            .to_string()
            .replace('&', "\\u0026")
            .replace('<', "\\u003c")
            .replace('>', "\\u003e")
            .replace('\u{2028}', "\\u2028")
            .replace('\u{2029}', "\\u2029");
        let html = include_str!("diagram/viewer.html")
            .replace("@@STYLE@@", include_str!("diagram/viewer.css"))
            .replace("@@SCRIPT@@", include_str!("diagram/viewer.js"))
            .replace("@@DATA@@", &data);
        let mut file = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&path)
            .map_err(|_| "Cannot create private diagram file")?;
        file.write_all(html.as_bytes())
            .map_err(|_| "Cannot write schema diagram")?;
        // Keep the snapshot after UI exit and after later D requests: graphical
        // openers are asynchronous, and browser reloads must remain valid.
        file.sync_all()
            .map_err(|_| "Cannot finish schema diagram")?;
        let _ = directory.keep();
        Ok(Self { path })
    }
    pub fn open(&self) -> bool {
        let opener = if cfg!(target_os = "macos") {
            "open"
        } else {
            "xdg-open"
        };
        // Give the graphical opener no DB credentials and no terminal handles.
        let mut cmd = Command::new(opener);
        cmd.arg(&self.path)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        for (key, _) in std::env::vars_os() {
            let name = key.to_string_lossy();
            if name.starts_with("DBCLI_")
                || name.starts_with("PG")
                || name == "MYSQL_PWD"
                || name == "DATABASE_URL"
            {
                cmd.env_remove(key);
            }
        }
        match cmd.spawn() {
            Ok(mut child) => {
                std::thread::spawn(move || {
                    let _ = child.wait();
                });
                true
            }
            Err(_) => false,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::{symlink, PermissionsExt};

    fn graph() -> Diagram {
        Diagram {
            engine: "sqlite".into(),
            tables: vec![Table {
                id: "example".into(),
                schema: "main".into(),
                name: "</script><script>bad()</script>".into(),
                kind: "table".into(),
                columns: vec![],
                unique_keys: vec![],
                external: false,
            }],
            ..Diagram::default()
        }
    }

    #[test]
    fn private_snapshots_survive_viewer_drop_and_later_diagrams() {
        let folder = tempfile::tempdir().unwrap();
        let root = folder.path().join("dbcli-diagrams");
        let first = Viewer::create_in(&graph(), "example", &root).unwrap();
        let first_path = first.path.clone();
        let source = std::fs::read_to_string(&first_path).unwrap();
        assert!(!source.contains("</script><script>bad()"));
        assert!(source.contains("\\u003c/script\\u003e"));
        assert_eq!(
            std::fs::metadata(&root).unwrap().permissions().mode() & 0o777,
            0o700
        );
        assert_eq!(
            std::fs::metadata(first_path.parent().unwrap())
                .unwrap()
                .permissions()
                .mode()
                & 0o777,
            0o700
        );
        assert_eq!(
            std::fs::metadata(&first_path).unwrap().permissions().mode() & 0o777,
            0o600
        );
        drop(first);
        assert!(first_path.is_file());
        let next = Viewer::create_in(&graph(), "example", &root).unwrap();
        assert_ne!(first_path, next.path);
        drop(next);
        assert_eq!(std::fs::read_to_string(first_path).unwrap(), source);
    }

    #[test]
    fn diagram_output_rejects_symlinks_and_public_directories() {
        let folder = tempfile::tempdir().unwrap();
        let target = folder.path().join("target");
        std::fs::create_dir(&target).unwrap();
        let link = folder.path().join("link");
        symlink(&target, &link).unwrap();
        assert!(Viewer::create_in(&graph(), "example", &link).is_err());
        std::fs::set_permissions(&target, std::fs::Permissions::from_mode(0o755)).unwrap();
        assert!(Viewer::create_in(&graph(), "example", &target).is_err());
        assert!(std::fs::read_dir(target).unwrap().next().is_none());
    }
}
