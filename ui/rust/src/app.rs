use crate::data::*;
use ratatui::crossterm::event::{KeyCode as K, KeyEvent, KeyModifiers as M};
use serde_json::{json, Value};
use std::collections::{BTreeMap, HashSet};
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum View {
    Data,
    Sql,
    Schema,
    Inspect,
}
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Focus {
    Sidebar,
    Grid,
}
#[derive(Clone, Debug)]
pub enum Edit {
    Filter,
    Search,
    Sql,
    Row {
        original: Value,
    },
    Insert,
    Bulk {
        original: Vec<Value>,
    },
    Export {
        format: String,
        page: Page,
        meta: Meta,
    },
    Palette,
}
#[derive(Clone, Debug)]
pub enum Modal {
    Help {
        scroll: usize,
    },
    Text {
        title: String,
        text: String,
        scroll: usize,
        detail: bool,
    },
    Input {
        kind: Edit,
        text: String,
        cursor: usize,
    },
    BulkConfirm {
        actions: Vec<Value>,
        confirmation: String,
    },
    Preview {
        actions: Vec<Value>,
        scroll: usize,
        confirmation: String,
    },
    Choices {
        title: String,
        items: Vec<String>,
        selected: usize,
        kind: String,
    },
}
#[derive(Clone, Debug)]
pub struct Marked {
    pub row: Value,
    pub sql_nulls: HashSet<String>,
}
pub struct App {
    pub config: Config,
    pub tables: Vec<String>,
    pub table_filter: String,
    pub table_cursor: usize,
    pub spec: Spec,
    pub page: Page,
    pub meta: Meta,
    pub total: usize,
    pub cursor: usize,
    pub column: usize,
    pub column_start: usize,
    pub hidden: HashSet<String>,
    pub marks: BTreeMap<String, Marked>,
    pub view: View,
    pub focus: Focus,
    pub modal: Option<Modal>,
    pub status: String,
    pub busy: bool,
    pub write_pending: bool,
    pub quit: bool,
    pub draft: String,
    pub inspector: Page,
    pub scroll: u16,
    pub external: bool,
    pub external_review: bool,
    pub filter_suggestions: Option<crate::filter_completion::Suggestions>,
    pub json_fields: Vec<crate::json_completion::Field>,
    pub bulk_external: Option<(Vec<Value>, Value)>,
    pub width: u16,
    pub height: u16,
    history: Vec<Spec>,
    last: Option<char>,
    pending: Option<u64>,
    tables_loaded: bool,
}
impl App {
    pub fn new(c: Config) -> Self {
        Self {
            spec: Spec {
                table: c.table.clone().unwrap_or_default(),
                limit: 24,
                ..Spec::default()
            },
            config: c,
            tables: vec![],
            table_filter: String::new(),
            table_cursor: 0,
            page: Page::default(),
            meta: Meta::default(),
            total: 0,
            cursor: 0,
            column: 0,
            column_start: 0,
            hidden: HashSet::new(),
            marks: BTreeMap::new(),
            view: View::Data,
            focus: Focus::Sidebar,
            modal: None,
            status: "Starting native worker…".into(),
            busy: true,
            write_pending: false,
            quit: false,
            draft: String::new(),
            inspector: Page::default(),
            scroll: 0,
            external: false,
            external_review: false,
            filter_suggestions: None,
            json_fields: vec![],
            bulk_external: None,
            width: 120,
            height: 35,
            history: vec![],
            last: None,
            pending: None,
            tables_loaded: false,
        }
    }
    pub fn read_only(&self) -> bool {
        self.config.read_only || self.spec.raw.is_some()
    }
    pub fn current(&self) -> Option<Value> {
        self.page
            .rows
            .get(self.cursor)
            .map(|r| object(&self.page.headers, r))
    }
    pub fn table_names(&self) -> Vec<String> {
        self.tables
            .iter()
            .filter(|t| fuzzy(&self.table_filter, t))
            .cloned()
            .collect()
    }
    fn selected_id(&self, row: &Value) -> String {
        identity(row, &self.meta.keys).unwrap_or_else(|_| row.to_string())
    }
    pub fn submit(&mut self, job: Job, db: &Async) {
        if self.write_pending {
            self.status = "Transaction pending: wait for its outcome".into();
            return;
        }
        let write = matches!(&job, Job::Apply(..));
        match db.send.send(job) {
            Ok(id) => {
                self.pending = Some(id);
                self.write_pending = write;
                self.busy = true;
                self.status = if write {
                    "Transaction pending…"
                } else {
                    "Working… Esc cancels read; / or : starts a newer search"
                }
                .into();
            }
            Err(e) => {
                self.pending = None;
                self.busy = false;
                self.status = e;
            }
        }
    }
    fn cancel_read(&mut self, db: &Async) {
        if db.cancel_read() {
            self.pending = None;
            self.busy = false;
            self.status = "Read cancelled; browser remains open. Ctrl+P → refresh to retry".into();
        }
    }
    fn refresh(&mut self, db: &Async) {
        if !self.tables_loaded {
            self.submit(Job::Init, db);
        } else {
            self.load(self.spec.clone(), db);
        }
    }
    pub fn load(&mut self, spec: Spec, db: &Async) {
        if self.write_pending {
            self.status = "Transaction pending: wait for its outcome".into();
            return;
        }
        if spec.table != self.spec.table || spec.raw != self.spec.raw {
            self.hidden.clear();
            self.marks.clear();
            self.cursor = 0;
            self.column = 0;
            self.column_start = 0;
        }
        self.table_cursor = self
            .table_names()
            .iter()
            .position(|t| t == &spec.table)
            .unwrap_or(self.table_cursor);
        self.spec = spec.clone();
        self.page = Page::default();
        self.json_fields.clear();
        self.submit(Job::Load(spec), db);
    }
    pub fn receive(&mut self, completion: Completion, db: &Async) {
        // Check before changing busy/status, opening editors, or acknowledging writes.
        if self.pending != Some(completion.id) {
            return;
        }
        self.pending = None;
        if self.write_pending {
            db.acknowledge_write(completion.id);
        }
        self.busy = false;
        let r = completion.reply;
        match r {
            Reply::Init(t) => {
                self.tables_loaded = true;
                self.tables = t;
                if self.spec.table.is_empty() {
                    if let Some(t) = self.tables.first() {
                        self.spec.table = t.clone();
                    }
                }
                if self.spec.table.is_empty() {
                    self.status = "No accessible tables".into();
                } else {
                    self.load(self.spec.clone(), db);
                }
            }
            Reply::Loaded(b) => {
                self.spec = b.spec;
                self.page = b.page;
                self.meta = b.meta;
                self.json_fields =
                    crate::json_completion::discover(&self.page, &self.meta, &self.config.engine);
                self.total = b.total;
                self.cursor = self.cursor.min(self.page.rows.len().saturating_sub(1));
                self.status = format!("{} rows · {} marked", self.total, self.marks.len());
            }
            Reply::Inspected(p) => {
                self.inspector = p;
                self.view = View::Inspect;
                self.scroll = 0;
                self.status = "Index / constraint / trigger inspector".into();
            }
            Reply::Fetched(p, m, purpose) => {
                if purpose == "bulk" {
                    let rows = p
                        .rows
                        .iter()
                        .map(|r| object(&p.headers, r))
                        .collect::<Vec<_>>();
                    self.bulk_editor(rows);
                } else {
                    self.input(
                        Edit::Export {
                            format: purpose.clone(),
                            page: p,
                            meta: m,
                        },
                        format!("export.{purpose}"),
                    );
                }
            }
            Reply::Applied(v) => {
                self.write_pending = false;
                self.marks.clear();
                self.status = format!("Atomic changes applied ({v})");
                self.load(self.spec.clone(), db);
            }
            Reply::Error(e) => {
                if self.write_pending {
                    self.status = format!(
                        "Write not confirmed: {e}. Refresh before retry; do not assume rollback."
                    );
                    self.write_pending = false;
                } else {
                    self.status = e;
                }
            }
        }
    }
    fn input(&mut self, kind: Edit, text: String) {
        let cursor = text.chars().count();
        self.modal = Some(Modal::Input { kind, text, cursor });
        self.refresh_filter_suggestions();
    }
    fn refresh_filter_suggestions(&mut self) {
        self.filter_suggestions = match &self.modal {
            Some(Modal::Input {
                kind: Edit::Filter,
                text,
                cursor,
            }) => {
                let suggestions = crate::filter_completion::suggest(
                    text,
                    *cursor,
                    &self.page,
                    &self.meta,
                    &self.config.engine,
                    &self.json_fields,
                );
                (!suggestions.items.is_empty()).then_some(suggestions)
            }
            _ => None,
        };
    }
    fn writable(&mut self) -> bool {
        if self.read_only() {
            self.status = "Read only: mutations disabled".into();
            false
        } else if self.spec.table.is_empty() {
            self.status = "Select a table first".into();
            false
        } else {
            true
        }
    }
    fn edit(&mut self) {
        if !self.writable() {
            return;
        }
        if let Some(row) = self.current() {
            if let Err(e) = identity(&row, &self.meta.keys) {
                self.status = e;
                return;
            }
            self.input(
                Edit::Row {
                    original: row.clone(),
                },
                serde_json::to_string_pretty(&editable(&row)).unwrap(),
            );
            self.external = true;
            self.external_review = true;
        }
    }
    fn insert(&mut self) {
        if !self.writable() {
            return;
        }
        let row = Value::Object(
            self.page
                .headers
                .iter()
                .filter(|h| !self.meta.insert_omit.contains(*h))
                .map(|h| (h.clone(), Value::Null))
                .collect(),
        );
        self.input(Edit::Insert, serde_json::to_string_pretty(&row).unwrap());
    }
    fn bulk_editor(&mut self, rows: Vec<Value>) {
        let document = match crate::bulk_editor::compact_document(&rows, &self.meta.keys, editable)
        {
            Ok(document) => document,
            Err(error) => {
                self.status = error;
                return;
            }
        };
        self.bulk_external = Some((rows, document));
        self.modal = None;
        self.status = "Opening Neovim diff; save+exit to review action counts, :cq cancels".into();
    }
    pub fn finish_bulk(
        &mut self,
        original: Vec<Value>,
        initial: Value,
        text: String,
    ) -> Result<()> {
        let edited: Value =
            serde_json::from_str(&text).map_err(|_| "Invalid bulk JSON; nothing written")?;
        let changed = crate::bulk_editor::changed_plan(edited, &initial, &self.meta.keys)?;
        let expanded = crate::bulk_editor::expand_plan(changed, &original, &self.meta.keys)?;
        let actions = bulk_actions(&expanded, &original, &self.meta, &self.page.headers)?;
        if actions.is_empty() {
            self.status = "No changes — no writes".into();
            self.modal = None;
        } else {
            self.modal = Some(Modal::BulkConfirm {
                actions,
                confirmation: String::new(),
            });
        }
        Ok(())
    }
    fn bulk(&mut self, db: &Async) {
        if !self.writable() {
            return;
        }
        if self.meta.keys.is_empty() {
            self.status = "Bulk editing requires a primary key".into();
            return;
        }
        if !self.marks.is_empty() {
            self.bulk_editor(self.marks.values().map(|r| r.row.clone()).collect());
        } else {
            self.submit(Job::Fetch(self.spec.clone(), "bulk".into()), db);
        }
    }
    fn delete(&mut self) {
        if !self.writable() {
            return;
        }
        let rows = if self.marks.is_empty() {
            self.current().into_iter().collect::<Vec<_>>()
        } else {
            self.marks.values().map(|r| r.row.clone()).collect()
        };
        let mut actions = vec![];
        for row in rows {
            if let Err(e) = identity(&row, &self.meta.keys) {
                self.status = e;
                return;
            }
            actions.push(json!({"kind":"delete","original":row}));
        }
        self.preview(actions);
    }
    fn preview(&mut self, actions: Vec<Value>) {
        if actions.is_empty() {
            self.status = "No changes".into();
            self.modal = None;
        } else {
            self.modal = Some(Modal::Preview {
                actions,
                scroll: 0,
                confirmation: String::new(),
            });
        }
    }
    fn export(&mut self, format: String, db: &Async) {
        if self.marks.is_empty() {
            self.submit(Job::Fetch(self.spec.clone(), format), db)
        } else {
            let page = Page {
                headers: self.page.headers.clone(),
                sql_nulls: self.marks.values().map(|r| r.sql_nulls.clone()).collect(),
                rows: self
                    .marks
                    .values()
                    .map(|r| self.page.headers.iter().map(|h| r.row[h].clone()).collect())
                    .collect(),
            };
            self.input(
                Edit::Export {
                    format: format.clone(),
                    page,
                    meta: self.meta.clone(),
                },
                format!("export.{format}"),
            );
        }
    }
    fn fk(&mut self) {
        if self.meta.relations.is_empty() {
            self.status = "No foreign keys on this table".into();
            return;
        }
        let items = self
            .meta
            .relations
            .iter()
            .map(|r| {
                format!(
                    "{} → {} ({})",
                    r.pairs
                        .iter()
                        .map(|(s, _)| s.clone())
                        .collect::<Vec<_>>()
                        .join(", "),
                    r.table,
                    r.pairs
                        .iter()
                        .map(|(_, t)| t.clone())
                        .collect::<Vec<_>>()
                        .join(", ")
                )
            })
            .collect();
        self.modal = Some(Modal::Choices {
            title: "Follow foreign key".into(),
            items,
            selected: 0,
            kind: "fk".into(),
        });
    }
    fn choose(&mut self, kind: &str, index: usize, db: &Async) {
        match kind {
            "fk" => {
                if let (Some(r), Some(row)) =
                    (self.meta.relations.get(index).cloned(), self.current())
                {
                    let mut terms = vec![];
                    let mut params = vec![];
                    for (from, to) in &r.pairs {
                        let Some(v) = row.get(from).filter(|v| !v.is_null()) else {
                            self.status = "Foreign-key columns are NULL".into();
                            return;
                        };
                        terms.push(format!(
                            "{} = {}",
                            quote(to).unwrap(),
                            if self.config.engine == "postgres" {
                                "%s"
                            } else {
                                "?"
                            }
                        ));
                        params.push(v.clone());
                    }
                    self.history.push(self.spec.clone());
                    let s = Spec {
                        table: r.table,
                        base: Some((terms.join(" AND "), params)),
                        limit: self.spec.limit,
                        ..Spec::default()
                    };
                    self.view = View::Data;
                    self.load(s, db);
                }
            }
            "export" => self.export(["json", "csv", "sql"][index.min(2)].into(), db),
            "columns" => {
                if index == self.page.headers.len() {
                    self.hidden.clear();
                } else if let Some(h) = self.page.headers.get(index) {
                    if !self.hidden.insert(h.clone()) {
                        self.hidden.remove(h);
                    }
                }
            }
            _ => {}
        }
    }
    fn back(&mut self, db: &Async) {
        if self.modal.is_some() {
            self.modal = None;
        } else if self.view != View::Data {
            self.view = View::Data;
        } else if let Some(s) = self.history.pop() {
            self.load(s, db);
        } else {
            self.quit = true;
        }
    }
    pub fn external_result(&mut self, text: String) {
        if let Some(Modal::Input {
            text: t, cursor, ..
        }) = &mut self.modal
        {
            *t = text;
            *cursor = t.chars().count();
        }
    }
    pub fn external_finished(&mut self, result: Result<String>, db: &Async) {
        let direct = std::mem::take(&mut self.external_review);
        match result {
            Ok(text) => {
                self.external_result(text.clone());
                if direct {
                    let kind = match &self.modal {
                        Some(Modal::Input {
                            kind: kind @ Edit::Row { .. },
                            ..
                        }) => kind.clone(),
                        _ => return,
                    };
                    match self.accept_input(kind, text, db) {
                        Ok(()) => {
                            if matches!(self.modal, Some(Modal::Input { .. })) {
                                self.modal = None;
                            }
                        }
                        Err(error) => self.status = error,
                    }
                } else {
                    self.refresh_filter_suggestions();
                }
            }
            Err(error) => {
                if direct {
                    self.modal = None;
                }
                self.status = format!("{error}; nothing written");
            }
        }
    }
    pub fn key(&mut self, key: KeyEvent, db: &Async) {
        let ctrl = key.modifiers.contains(M::CONTROL);
        if self.write_pending && matches!(key.code, K::Char('q') | K::Char('h') | K::Esc) {
            self.status="Transaction pending: wait for its outcome before leaving. Ctrl+C forces exit; outcome may be unknown.".into();
            return;
        }
        if ctrl && key.code == K::Char('c') {
            self.quit = true;
            return;
        }
        if self.busy && !self.write_pending && key.code == K::Esc && self.modal.is_none() {
            self.cancel_read(db);
            return;
        }
        if self.write_pending {
            self.status = "Transaction pending: wait for its outcome; Ctrl+C forces exit".into();
            return;
        }
        if self.modal.is_some() {
            self.modal_key(key, db);
            return;
        }
        if key.code == K::Char('q') || key.code == K::Char('h') || key.code == K::Esc {
            self.back(db);
            return;
        }
        if ctrl && key.code == K::Char('p') {
            self.input(Edit::Palette, String::new());
            return;
        }
        if key.code == K::Char('?') {
            self.modal = Some(Modal::Help { scroll: 0 });
            return;
        }
        if matches!(key.code, K::Tab | K::BackTab) {
            self.focus = match self.focus {
                Focus::Sidebar => Focus::Grid,
                Focus::Grid => Focus::Sidebar,
            };
            return;
        }
        if self.busy {
            // Only read controls are available until a current result is installed.
            match key.code {
                K::Char('/') => {
                    let (kind, text) = if self.focus == Focus::Sidebar {
                        (Edit::Search, self.table_filter.clone())
                    } else {
                        (Edit::Filter, self.spec.filter.clone())
                    };
                    self.input(kind, text);
                }
                K::Char(':') | K::Char('2') => self.input(Edit::Sql, self.draft.clone()),
                K::Char('r') if ctrl => self.run_sql(self.draft.clone(), db),
                K::Char('j') | K::Down if self.focus == Focus::Sidebar => {
                    self.table_cursor =
                        (self.table_cursor + 1).min(self.table_names().len().saturating_sub(1));
                }
                K::Char('k') | K::Up if self.focus == Focus::Sidebar => {
                    self.table_cursor = self.table_cursor.saturating_sub(1);
                }
                K::Enter | K::Char('l') if self.focus == Focus::Sidebar => {
                    if let Some(table) = self.table_names().get(self.table_cursor).cloned() {
                        self.history.clear();
                        self.view = View::Data;
                        self.focus = Focus::Grid;
                        self.load(
                            Spec {
                                table,
                                limit: self.spec.limit,
                                ..Spec::default()
                            },
                            db,
                        );
                    }
                }
                _ => {
                    self.status =
                        "Read pending: Esc cancels; / filter or : SQL supersedes it".into()
                }
            }
            return;
        }
        if self.focus == Focus::Sidebar {
            match key.code {
                K::Char('/') => self.input(Edit::Search, self.table_filter.clone()),
                K::Char('j') | K::Down => {
                    self.table_cursor =
                        (self.table_cursor + 1).min(self.table_names().len().saturating_sub(1))
                }
                K::Char('k') | K::Up => self.table_cursor = self.table_cursor.saturating_sub(1),
                K::Enter | K::Char('l') => {
                    if let Some(t) = self.table_names().get(self.table_cursor).cloned() {
                        self.history.clear();
                        self.view = View::Data;
                        self.focus = Focus::Grid;
                        self.load(
                            Spec {
                                table: t,
                                limit: self.spec.limit,
                                ..Spec::default()
                            },
                            db,
                        );
                    }
                }
                _ => {}
            }
            return;
        }
        if self.view == View::Schema || self.view == View::Inspect {
            let rows = if self.view == View::Schema {
                self.meta.schema.rows.len()
            } else {
                self.inspector.rows.len()
            };
            let last = rows.saturating_sub(1).min(u16::MAX as usize) as u16;
            let step = self.height.saturating_sub(12).max(1);
            match key.code {
                K::Char('j') | K::Down => self.scroll = self.scroll.saturating_add(1).min(last),
                K::Char('k') | K::Up => self.scroll = self.scroll.saturating_sub(1),
                K::Char('d') if ctrl => self.scroll = self.scroll.saturating_add(step).min(last),
                K::Char('u') if ctrl => self.scroll = self.scroll.saturating_sub(step),
                K::Char('g') if !ctrl => self.scroll = 0,
                K::Home => self.scroll = 0,
                K::Char('G') | K::End => self.scroll = last,
                _ => { /* View switches and other commands are handled below. */ }
            }
            if matches!(
                key.code,
                K::Char('j' | 'k' | 'G') | K::Down | K::Up | K::Home | K::End
            ) || !ctrl && key.code == K::Char('g')
                || ctrl && matches!(key.code, K::Char('d' | 'u'))
            {
                return;
            }
        }
        let previous = self.last.take();
        match key.code {
            K::Char('1') => self.view = View::Data,
            K::Char('2') => {
                self.view = View::Sql;
                self.input(Edit::Sql, self.draft.clone());
            }
            K::Char('3') | K::Char('S') => {
                self.view = View::Schema;
                self.scroll = 0;
            }
            K::Char('4') | K::Char('I') => self.submit(Job::Inspect(self.spec.table.clone()), db),
            K::Char('j') | K::Down if self.view == View::Data => {
                if self.cursor + 1 < self.page.rows.len() {
                    self.cursor += 1;
                } else if self.spec.offset + self.page.rows.len() < self.total {
                    let mut s = self.spec.clone();
                    s.offset += s.limit;
                    self.cursor = 0;
                    self.load(s, db);
                }
            }
            K::Char('k') | K::Up if self.view == View::Data => {
                if self.cursor > 0 {
                    self.cursor -= 1
                } else if self.spec.offset > 0 {
                    let mut s = self.spec.clone();
                    s.offset = s.offset.saturating_sub(s.limit);
                    self.load(s, db);
                }
            }
            K::Char('H') | K::Left => self.column = self.column.saturating_sub(1),
            K::Char('L') | K::Right => {
                self.column = (self.column + 1).min(self.page.headers.len().saturating_sub(1))
            }
            K::Enter | K::Char('l') => {
                if let Some(row) = self.current() {
                    self.modal = Some(Modal::Text {
                        title: "Row detail · e edit / dd delete / f follow / h back".into(),
                        text: serde_json::to_string_pretty(&editable(&row)).unwrap(),
                        scroll: 0,
                        detail: true,
                    });
                }
            }
            K::Char('g') if !ctrl => {
                if previous == Some('g') {
                    let mut s = self.spec.clone();
                    s.offset = 0;
                    self.cursor = 0;
                    self.load(s, db);
                } else {
                    self.last = Some('g');
                }
            }
            K::Char('G') => {
                let mut s = self.spec.clone();
                s.offset = self.total.saturating_sub(1) / s.limit.max(1) * s.limit.max(1);
                self.cursor = self.total.saturating_sub(1) % s.limit.max(1);
                self.load(s, db);
            }
            K::Char('d') if !ctrl => {
                if previous == Some('d') {
                    self.delete();
                } else {
                    self.last = Some('d');
                }
            }
            K::Char('d') if ctrl => {
                let mut s = self.spec.clone();
                s.offset = (s.offset + s.limit / 2).min(self.total.saturating_sub(1));
                self.load(s, db);
            }
            K::Char('u') if ctrl => {
                let mut s = self.spec.clone();
                s.offset = s.offset.saturating_sub(s.limit / 2);
                self.load(s, db);
            }
            K::Char('/') => self.input(Edit::Filter, self.spec.filter.clone()),
            K::Char(':') => {
                self.view = View::Sql;
                self.input(Edit::Sql, self.draft.clone());
            }
            K::Char('r') if ctrl => self.run_sql(self.draft.clone(), db),
            K::Char('g') if ctrl => {
                self.input(Edit::Filter, self.spec.filter.clone());
                self.external = true;
            }
            K::Char('o') | K::Char('O') => {
                if let Some(h) = self.page.headers.get(self.column) {
                    let order = (
                        h.clone(),
                        if key.code == K::Char('o') {
                            "ASC".into()
                        } else {
                            "DESC".into()
                        },
                    );
                    let mut s = self.spec.clone();
                    s.sort = if s.sort.as_ref() == Some(&order) {
                        None
                    } else {
                        Some(order)
                    };
                    s.offset = 0;
                    self.load(s, db);
                }
            }
            K::Char('v') => {
                let mut items = self
                    .page
                    .headers
                    .iter()
                    .map(|h| {
                        format!(
                            "{} {h}",
                            if self.hidden.contains(h) {
                                "[ ]"
                            } else {
                                "[x]"
                            }
                        )
                    })
                    .collect::<Vec<_>>();
                items.push("Restore all columns".into());
                self.modal = Some(Modal::Choices {
                    title: "Column visibility".into(),
                    items,
                    selected: self.column,
                    kind: "columns".into(),
                });
            }
            K::Char(' ') => {
                if let Some(row) = self.current() {
                    let id = self.selected_id(&row);
                    if self.marks.remove(&id).is_none() {
                        self.marks.insert(
                            id,
                            Marked {
                                row,
                                sql_nulls: self
                                    .page
                                    .sql_nulls
                                    .get(self.cursor)
                                    .cloned()
                                    .unwrap_or_default(),
                            },
                        );
                    }
                }
            }
            K::Char('f') => self.fk(),
            K::Char('a') => self.insert(),
            K::Char('e') => self.edit(),
            K::Char('E') => self.bulk(db),
            K::Char('x') => self.export("json".into(), db),
            K::Char('X') => {
                self.modal = Some(Modal::Choices {
                    title: "Export format".into(),
                    items: vec!["JSON".into(), "CSV".into(), "SQL INSERT".into()],
                    selected: 0,
                    kind: "export".into(),
                })
            }
            _ => {}
        }
    }
    fn run_sql(&mut self, text: String, db: &Async) {
        self.draft = text.clone();
        if let Ok(n) = text.trim().parse::<usize>() {
            if n > 0 {
                let mut s = self.spec.clone();
                s.offset = (n - 1) / s.limit * s.limit;
                self.cursor = (n - 1) % s.limit;
                self.view = View::Data;
                self.load(s, db);
            }
            return;
        }
        if text.trim().is_empty() {
            self.status = "Enter a bounded SELECT/WITH/EXPLAIN query".into();
            return;
        }
        self.history.push(self.spec.clone());
        let s = Spec {
            table: self.spec.table.clone(),
            raw: Some(text),
            limit: self.spec.limit,
            ..Spec::default()
        };
        self.view = View::Data;
        self.load(s, db);
    }
    fn modal_key(&mut self, key: KeyEvent, db: &Async) {
        let ctrl = key.modifiers.contains(M::CONTROL);
        let Some(mut modal) = self.modal.take() else {
            return;
        };
        if let Modal::Input {
            kind: Edit::Filter,
            text,
            cursor,
        } = &mut modal
        {
            if key.code == K::Tab && self.filter_suggestions.is_none() {
                let suggestions = crate::filter_completion::suggest(
                    text,
                    *cursor,
                    &self.page,
                    &self.meta,
                    &self.config.engine,
                    &self.json_fields,
                );
                self.filter_suggestions = (!suggestions.items.is_empty()).then_some(suggestions);
                self.modal = Some(modal);
                return;
            }
            if let Some(suggestions) = &mut self.filter_suggestions {
                match key.code {
                    K::Up | K::BackTab => {
                        suggestions.move_selection(false);
                        self.modal = Some(modal);
                        return;
                    }
                    K::Down => {
                        suggestions.move_selection(true);
                        self.modal = Some(modal);
                        return;
                    }
                    K::Tab => {
                        suggestions.apply(text, cursor);
                        self.modal = Some(modal);
                        self.refresh_filter_suggestions();
                        return;
                    }
                    K::Esc => {
                        self.filter_suggestions = None;
                        self.modal = Some(modal);
                        return;
                    }
                    _ => {}
                }
            }
        }
        if key.code == K::Esc {
            if let Modal::Input {
                kind: Edit::Sql,
                text,
                ..
            } = modal
            {
                self.draft = text;
            }
            return;
        }
        match &mut modal {
            Modal::Help { scroll } => {
                let (count, visible) = crate::view::help_dimensions(self.width, self.height);
                let last = count.saturating_sub(visible);
                let step = visible.saturating_sub(1).max(1);
                match key.code {
                    K::Char('q') | K::Char('h') => return,
                    K::Char('j') | K::Down => *scroll = scroll.saturating_add(1).min(last),
                    K::Char('k') | K::Up => *scroll = scroll.saturating_sub(1),
                    K::Char('d') if ctrl => *scroll = scroll.saturating_add(step).min(last),
                    K::Char('u') if ctrl => *scroll = scroll.saturating_sub(step),
                    K::Char('g') | K::Home => *scroll = 0,
                    K::Char('G') | K::End => *scroll = last,
                    _ => {}
                }
            }
            Modal::Text { scroll, detail, .. } => match key.code {
                K::Char('q') | K::Char('h') => return,
                K::Char('j') | K::Down => *scroll = scroll.saturating_add(1),
                K::Char('k') | K::Up => *scroll = scroll.saturating_sub(1),
                K::Char('d') if ctrl => *scroll = scroll.saturating_add(12),
                K::Char('u') if ctrl => *scroll = scroll.saturating_sub(12),
                K::Char('e') if *detail => {
                    self.edit();
                    return;
                }
                K::Char('f') if *detail => {
                    self.fk();
                    return;
                }
                K::Char('d') if *detail => {
                    if self.last == Some('d') {
                        self.last = None;
                        self.delete();
                        return;
                    } else {
                        self.last = Some('d');
                    }
                }
                _ => {}
            },
            Modal::Choices {
                items,
                selected,
                kind,
                ..
            } => match key.code {
                K::Char('q') | K::Char('h') => return,
                K::Char('j') | K::Down => {
                    *selected = (*selected + 1).min(items.len().saturating_sub(1))
                }
                K::Char('k') | K::Up => *selected = selected.saturating_sub(1),
                K::Enter | K::Char('l') => {
                    self.choose(kind, *selected, db);
                    return;
                }
                _ => {}
            },
            Modal::BulkConfirm {
                actions,
                confirmation,
            } => match key.code {
                K::Char('q') | K::Char('n') => return,
                K::Backspace => {
                    confirmation.pop();
                }
                K::Char('y') => {
                    if actions.iter().any(|a| a["kind"] == "delete") && confirmation != "DELETE" {
                        self.status = "Type DELETE, then y to confirm deletions".into();
                    } else {
                        if self.writable() {
                            self.submit(Job::Apply(self.spec.table.clone(), actions.clone()), db);
                        }
                        return;
                    }
                }
                K::Char(c) => confirmation.push(c),
                _ => {}
            },
            Modal::Preview {
                actions,
                scroll,
                confirmation,
            } => match key.code {
                K::Char('q') | K::Char('n') => return,
                K::Char('j') | K::Down => *scroll = scroll.saturating_add(1),
                K::Char('k') | K::Up => *scroll = scroll.saturating_sub(1),
                K::Char('d') if ctrl => *scroll = scroll.saturating_add(15),
                K::Char('u') if ctrl => *scroll = scroll.saturating_sub(15),
                K::Char('G') => {
                    *scroll = crate::view::wrapped_lines(
                        &serde_json::to_string_pretty(actions).unwrap(),
                        self.width.saturating_sub(8).min(110).saturating_sub(2) as usize,
                    )
                    .len()
                    .saturating_sub(self.height.saturating_sub(6) as usize)
                }
                K::Char('g') => *scroll = 0,
                K::Backspace => {
                    confirmation.pop();
                }
                K::Char('y') => {
                    if actions.iter().any(|a| a["kind"] == "delete") && confirmation != "DELETE" {
                        self.status = "Type DELETE, then y to confirm deletions".into();
                    } else {
                        if self.writable() {
                            self.submit(Job::Apply(self.spec.table.clone(), actions.clone()), db);
                        }
                        return;
                    }
                }
                K::Char(c) => confirmation.push(c),
                _ => {}
            },
            Modal::Input { kind, text, cursor } => {
                if ctrl && key.code == K::Char('g') {
                    self.external = true;
                } else if ctrl && key.code == K::Char('r')
                    || key.code == K::Enter
                        && !matches!(
                            kind,
                            Edit::Sql | Edit::Row { .. } | Edit::Insert | Edit::Bulk { .. }
                        )
                {
                    let kind = kind.clone();
                    let text = text.clone();
                    if let Err(e) = self.accept_input(kind, text, db) {
                        self.status = e;
                    } else {
                        return;
                    }
                } else {
                    match key.code {
                        K::Left => *cursor = cursor.saturating_sub(1),
                        K::Right => *cursor = (*cursor + 1).min(text.chars().count()),
                        K::Home | K::Char('a') if key.code == K::Home || ctrl => {
                            *cursor = line_start(text, *cursor)
                        }
                        K::End | K::Char('e') if key.code == K::End || ctrl => {
                            *cursor = line_end(text, *cursor)
                        }
                        K::Up => *cursor = vertical_cursor(text, *cursor, -1),
                        K::Down => *cursor = vertical_cursor(text, *cursor, 1),
                        K::Char('k') if ctrl => {
                            let end = line_end(text, *cursor);
                            let mut chars = text.chars().collect::<Vec<_>>();
                            chars.drain(*cursor..end);
                            *text = chars.into_iter().collect();
                        }
                        K::Char('w') if ctrl => {
                            let mut chars = text.chars().collect::<Vec<_>>();
                            let end = (*cursor).min(chars.len());
                            let mut start = end;
                            while start > 0 && chars[start - 1].is_whitespace() {
                                start -= 1;
                            }
                            while start > 0 && !chars[start - 1].is_whitespace() {
                                start -= 1;
                            }
                            chars.drain(start..end);
                            *cursor = start;
                            *text = chars.into_iter().collect();
                        }
                        K::Backspace => {
                            if *cursor > 0 {
                                let mut chars = text.chars().collect::<Vec<_>>();
                                chars.remove(*cursor - 1);
                                *cursor -= 1;
                                *text = chars.into_iter().collect();
                            }
                        }
                        K::Delete => {
                            let mut chars = text.chars().collect::<Vec<_>>();
                            if *cursor < chars.len() {
                                chars.remove(*cursor);
                                *text = chars.into_iter().collect();
                            }
                        }
                        K::Char('u') if ctrl => {
                            text.clear();
                            *cursor = 0;
                        }
                        K::Enter => {
                            let mut chars = text.chars().collect::<Vec<_>>();
                            chars.insert(*cursor, '\n');
                            *cursor += 1;
                            *text = chars.into_iter().collect();
                        }
                        K::Tab => {
                            let token = text.split_whitespace().last().unwrap_or("");
                            let mut candidates = if matches!(kind, Edit::Palette) {
                                COMMANDS.iter().map(|s| s.to_string()).collect()
                            } else {
                                self.page.headers.clone()
                            };
                            candidates.extend(self.tables.clone());
                            candidates.extend(
                                ["SELECT", "FROM", "WHERE", "ORDER BY", "LIMIT"]
                                    .iter()
                                    .map(|s| s.to_string()),
                            );
                            if let Some(c) = candidates.iter().find(|c| fuzzy(token, c)) {
                                let new = format!("{}{}", text.trim_end_matches(token), c);
                                *text = new;
                                *cursor = text.chars().count();
                            }
                        }
                        K::Char(c) if !ctrl => {
                            let mut chars = text.chars().collect::<Vec<_>>();
                            chars.insert(*cursor, c);
                            *cursor += 1;
                            *text = chars.into_iter().collect();
                        }
                        _ => {}
                    }
                }
            }
        }
        if self.modal.is_none() {
            self.modal = Some(modal);
        }
        self.refresh_filter_suggestions();
    }
    fn accept_input(&mut self, kind: Edit, text: String, db: &Async) -> Result<()> {
        match kind {
            Edit::Filter => {
                let mut s = self.spec.clone();
                s.filter = text;
                s.offset = 0;
                self.cursor = 0;
                self.load(s, db);
            }
            Edit::Search => {
                self.table_filter = text;
                self.table_cursor = 0;
            }
            Edit::Sql => self.run_sql(text, db),
            Edit::Export { format, page, meta } => {
                let target = if self.spec.raw.is_some() {
                    "query_result"
                } else {
                    &self.spec.table
                };
                crate::data::export(
                    std::path::Path::new(&text),
                    &format,
                    &self.config.engine,
                    target,
                    &page,
                    &meta,
                )?;
                self.status = format!("Exported {} rows to {text}", page.rows.len());
            }
            Edit::Row { original } => {
                let v: Value = serde_json::from_str(&text)
                    .map_err(|_| "Invalid JSON; correct the editor text")?;
                if v == editable(&original) {
                    self.modal = None;
                    self.status = "No changes — no writes".into();
                    return Ok(());
                }
                let mut row = normalize_row(&v, Some(&original), &self.meta, &self.page.headers)?;
                // Preserve exact original storage for fields unchanged in the editor.
                // In particular, editing tags must not rewrite JSON TEXT formatting.
                let before = editable(&original);
                for (column, value) in row.as_object_mut().unwrap() {
                    if v.get(column) == before.get(column) {
                        if let Some(original_value) = original.get(column) {
                            *value = original_value.clone();
                        }
                    }
                }
                for h in &self.page.headers {
                    if row.get(h).is_none() {
                        return Err(format!("Missing field {h}; preserve complete row"));
                    }
                }
                identity(&original, &self.meta.keys)?;
                self.preview(vec![json!({"kind":"update","original":original,"row":row})]);
            }
            Edit::Insert => {
                let v: Value = serde_json::from_str(&text).map_err(|_| "Invalid insert JSON")?;
                let rows = if let Some(a) = v.as_array() {
                    a.clone()
                } else {
                    vec![v]
                };
                let mut a = vec![];
                for row in rows {
                    a.push(json!({"kind":"insert","row":normalize_row(&row,None,&self.meta,&self.page.headers)?}));
                }
                self.preview(a);
            }
            Edit::Bulk { original } => {
                let v: Value = serde_json::from_str(&text).map_err(|_| "Invalid bulk JSON")?;
                self.preview(bulk_actions(&v, &original, &self.meta, &self.page.headers)?);
            }
            Edit::Palette => {
                let mut action = text.trim().to_ascii_lowercase();
                if !COMMANDS.contains(&action.as_str()) {
                    let matching = COMMANDS
                        .iter()
                        .filter(|c| fuzzy(&action, c))
                        .collect::<Vec<_>>();
                    if matching.len() == 1 {
                        action = matching[0].to_string();
                    }
                }
                match action.as_str(){"help"=>self.modal=Some(Modal::Help{scroll:0}),"data"=>self.view=View::Data,"sql"=>self.input(Edit::Sql,self.draft.clone()),"schema"=>self.view=View::Schema,"inspect"=>self.submit(Job::Inspect(self.spec.table.clone()),db),"filter"=>self.input(Edit::Filter,self.spec.filter.clone()),"insert"=>self.insert(),"edit"=>self.edit(),"bulk"=>self.bulk(db),"delete"=>self.delete(),"export"=>self.export("json".into(),db),"tables"=>self.focus=Focus::Sidebar,"refresh"=>self.refresh(db),"quit"=>self.quit=true,_=>return Err("Commands: data sql schema inspect filter insert edit bulk delete export tables refresh quit".into())}
            }
        }
        Ok(())
    }
}
fn line_start(text: &str, cursor: usize) -> usize {
    text.chars()
        .take(cursor)
        .enumerate()
        .filter(|(_, c)| *c == '\n')
        .map(|(i, _)| i + 1)
        .last()
        .unwrap_or(0)
}
fn line_end(text: &str, cursor: usize) -> usize {
    cursor + text.chars().skip(cursor).take_while(|c| *c != '\n').count()
}
fn vertical_cursor(text: &str, cursor: usize, direction: isize) -> usize {
    let start = line_start(text, cursor);
    let col = cursor - start;
    if direction < 0 {
        if start == 0 {
            0
        } else {
            let previous = line_start(text, start - 1);
            previous + col.min(start - 1 - previous)
        }
    } else {
        let end = line_end(text, cursor);
        if end == text.chars().count() {
            end
        } else {
            let next = end + 1;
            next + col.min(line_end(text, next) - next)
        }
    }
}

pub fn fuzzy(needle: &str, haystack: &str) -> bool {
    let mut chars = needle
        .to_lowercase()
        .chars()
        .collect::<Vec<_>>()
        .into_iter();
    let mut next = chars.next();
    for c in haystack.to_lowercase().chars() {
        if next == Some(c) {
            next = chars.next();
        }
    }
    next.is_none()
}
pub const COMMANDS: [&str; 14] = [
    "data", "sql", "schema", "inspect", "filter", "insert", "edit", "bulk", "delete", "export",
    "tables", "refresh", "quit", "help",
];

// One shortcut (or related key group) per row; tabs separate keys and descriptions.
pub const HELP: &str = "NAVIGATION
j/k ↑/↓\tMove through rows, menus or help
H/L ←/→\tFocus a data column
gg/G\tJump to the first / last row
Ctrl+D/U\tPage down / up
Enter/l\tOpen row details or choose an item
Tab/Shift+Tab\tSwitch between the table list and table rows
1/2/3/4\tShow Data / SQL / Schema / Inspect
h/q\tGo back or quit
Esc\tClose a modal, or cancel a pending read
Ctrl+C\tForce quit; a write outcome may be unknown

SEARCH & QUERY
/\tFilter rows; search tables in the sidebar
: or 2\tOpen the multiline SQL editor
Ctrl+R\tRun SQL or accept JSON in an editor
Ctrl+G\tOpen the active prompt in your external editor
Tab (filter)\tAccept a suggestion; reopen hidden suggestions
↑/↓ (filter)\tChoose a column, JSON path, predicate or value
Esc (filter)\tHide suggestions first, then close the prompt
Tab (editor)\tComplete columns, tables or SQL keywords
o/O\tToggle ascending / descending sort
v\tChoose visible columns
Space\tMark or unmark a complete row
f\tChoose a foreign-key relation to follow
S/3\tShow the full table schema
I/4\tInspect indexes, constraints and triggers

TEXT EDITING
Ctrl+W\tDelete the preceding word and whitespace
Ctrl+A/E\tMove to the start / end of the line
Ctrl+K\tDelete from the cursor to the end of the line
Ctrl+U\tClear the prompt or editor
Backspace\tDelete the character before the cursor
Delete\tDelete the character after the cursor

EXPORT
x\tExport JSON
X\tChoose JSON, CSV or SQL export

WRITES · WRITABLE TABLES ONLY
a\tInsert a JSON object or array
e\tEdit the current row in your external editor
E\tOpen bulk edits as a private Neovim diff
dd\tReview deletion of current or marked rows
y (review)\tConfirm one atomic transaction
DELETE then y\tAcknowledge and confirm explicit deletions
Esc/q (review)\tCancel without applying the plan

NEOVIM BULK DIFF
:wqa\tSave and return to operation-count confirmation
:cq\tCancel bulk editing
]c/[c\tJump to the next / previous change
zR\tExpand all diff folds

HELP & COMMANDS
?\tOpen this keyboard guide
Ctrl+P\tOpen the command palette
j/k ↑/↓\tScroll this guide one line
Ctrl+D/U\tScroll this guide one page
g/G\tJump to the start / end of this guide
Esc/q\tClose this guide

Read operations can be cancelled; a newer submitted query supersedes an older read.
Confirmed writes cannot be cancelled or superseded.
Exports use marked rows, or at most 100,000 matching rows, and require a new filename.
Raw SQL screens are read only; --read-only disables all mutations.
";
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn text_modals_support_prompt_line_editing() {
        let db = Async::disconnected();
        let mut a = App::new(Config::default());
        a.input(Edit::Filter, "export.json".into());
        a.key(KeyEvent::new(K::Char('a'), M::CONTROL), &db);
        a.key(KeyEvent::new(K::Char('k'), M::CONTROL), &db);
        for c in "混合/path".chars() {
            a.key(KeyEvent::new(K::Char(c), M::NONE), &db);
        }
        if let Some(Modal::Input { text, cursor, .. }) = &a.modal {
            assert_eq!(text, "混合/path");
            assert_eq!(*cursor, 7);
        } else {
            panic!("missing input")
        }
        a.key(KeyEvent::new(K::Left, M::NONE), &db);
        a.key(KeyEvent::new(K::Delete, M::NONE), &db);
        a.key(KeyEvent::new(K::Char('e'), M::CONTROL), &db);
        a.key(KeyEvent::new(K::Backspace, M::NONE), &db);
        if let Some(Modal::Input { text, .. }) = &a.modal {
            assert_eq!(text, "混合/pa");
        }
        a.key(KeyEvent::new(K::Char('u'), M::CONTROL), &db);
        if let Some(Modal::Input { text, cursor, .. }) = &a.modal {
            assert_eq!(text, "");
            assert_eq!(*cursor, 0);
        }
        assert_eq!(vertical_cursor("abc\n混合\ndef", 6, -1), 2);
        assert_eq!(vertical_cursor("abc\n混合\ndef", 2, 1), 6);
    }
    #[test]
    fn pending_write_cannot_be_silently_quit() {
        let db = Async::start(Config::default());
        let mut app = App::new(Config::default());
        app.write_pending = true;
        app.key(KeyEvent::new(K::Char('q'), M::NONE), &db);
        assert!(!app.quit);
        assert!(app.status.contains("Transaction pending"));
        app.key(KeyEvent::new(K::Char('c'), M::CONTROL), &db);
        assert!(app.quit);
        assert!(app.write_pending);
    }
    #[test]
    fn unicode_fuzzy_and_readonly_state() {
        assert!(fuzzy("混表", "混合表"));
        let mut c = Config::default();
        c.engine = "postgres".into();
        let mut a = App::new(c);
        assert!(!a.read_only());
        a.config.read_only = true;
        assert!(a.read_only());
        a.config.read_only = false;
        a.config.engine = "sqlite".into();
        assert!(!a.read_only());
        a.spec.raw = Some("SELECT 1".into());
        assert!(a.read_only());
    }
    #[test]
    fn bulk_opens_external_diff_and_never_full_row_preview() {
        let mut app = App::new(Config::default());
        app.meta.keys = vec!["id".into()];
        app.page.headers = vec!["id".into(), "value".into()];
        let rows = vec![json!({"id":1,"value":"private original value"})];
        app.bulk_editor(rows.clone());
        assert!(app.modal.is_none());
        let (original, initial) = app.bulk_external.take().unwrap();
        assert_eq!(initial["updates"][0]["original"], json!({"id":1}));
        assert_eq!(original, rows);
        app.finish_bulk(original.clone(), initial.clone(), initial.to_string())
            .unwrap();
        assert!(app.modal.is_none());
        assert!(app.status.contains("No changes"));
        let mut changed = initial.clone();
        changed["updates"][0]["row"]["value"] = json!("private edited value");
        app.finish_bulk(original, initial, changed.to_string())
            .unwrap();
        if let Some(Modal::BulkConfirm { actions, .. }) = &app.modal {
            assert_eq!(actions[0]["original"], rows[0]);
        } else {
            panic!("Missing bulk count confirmation");
        }
    }
    #[test]
    fn immutable_edit_preview() {
        let mut a = App::new(Config::default());
        a.meta.keys = vec!["id".into()];
        a.spec.table = "items".into();
        a.page = Page {
            sql_nulls: vec![],
            headers: vec!["id".into(), "value".into()],
            rows: vec![vec![json!(1), json!("old")]],
        };
        a.edit();
        if let Some(Modal::Input {
            kind: Edit::Row { original },
            ..
        }) = a.modal
        {
            assert_eq!(original["value"], "old")
        } else {
            panic!("row editor missing")
        }
    }
}
