//! Bound PostgreSQL row mutations. All row actions share one verified transaction.
use super::*;
use std::collections::{HashMap, HashSet};
#[derive(Clone)]
pub(super) struct Column {
    pub name: String,
    pub ty: Type,
    pub generated: bool,
    pub identity: String,
    pub default: bool,
}
pub(super) fn columns(c: &mut Client, table: &str) -> Result<Vec<Column>> {
    let reference = qualified(table)?.2;
    let stmt = c
        .prepare(&format!("SELECT * FROM {reference} LIMIT 0"))
        .map_err(db)?;
    let types = stmt
        .columns()
        .iter()
        .map(|col| (col.name().to_string(), col.type_().clone()))
        .collect::<HashMap<_, _>>();
    let rows=c.query("SELECT attname,attgenerated::text,attidentity::text,atthasdef FROM pg_catalog.pg_attribute WHERE attrelid=to_regclass($1) AND attnum>0 AND NOT attisdropped ORDER BY attnum",&[&reference]).map_err(db)?;
    if rows.is_empty() {
        return Err(error("AdapterError", "Unknown PostgreSQL table"));
    }
    rows.into_iter()
        .map(|r| {
            let name = r.get::<_, String>(0);
            Ok(Column {
                name: name.clone(),
                ty: types.get(&name).cloned().ok_or_else(|| {
                    error(
                        "AdapterError",
                        "Column metadata changed; refresh before retry",
                    )
                })?,
                generated: !r.get::<_, String>(1).is_empty(),
                identity: r.get(2),
                default: r.get(3),
            })
        })
        .collect()
}
fn keys(c: &mut Client, table: &str) -> Result<Vec<String>> {
    let reference = qualified(table)?.2;
    let rows=c.query("SELECT a.attname FROM pg_catalog.pg_index i CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY k(attnum,ord) JOIN pg_catalog.pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.attnum WHERE i.indrelid=to_regclass($1) AND i.indisprimary ORDER BY k.ord",&[&reference]).map_err(db)?;
    Ok(rows.iter().map(|r| r.get(0)).collect())
}
fn row<'a>(v: &'a Value, label: &str) -> Result<&'a serde_json::Map<String, Value>> {
    v.as_object()
        .ok_or_else(|| error("ValueError", format!("{label} must be an object")))
}
fn validate(v: &Value, col: &Column) -> Result<()> {
    let mut bytes = postgres::types::private::BytesMut::new();
    Param(v.clone()).to_sql(&col.ty, &mut bytes).map_err(|_| {
        error(
            "ValueError",
            format!(
                "Invalid value for column {} ({}); preserve its native type",
                col.name,
                col.ty.name()
            ),
        )
    })?;
    Ok(())
}
fn validate_row(v: &Value, cols: &[Column], label: &str) -> Result<()> {
    for (k, v) in row(v, label)? {
        let col = cols
            .iter()
            .find(|c| &c.name == k)
            .ok_or_else(|| error("InvalidIdentifierError", format!("Unknown column {k}")))?;
        validate(v, col)?;
    }
    Ok(())
}
fn predicates(
    original: &Value,
    pk: &[String],
    cols: &[Column],
) -> Result<(String, Vec<Value>, Vec<Type>)> {
    if pk.is_empty() {
        return Err(error(
            "RowIdentityError",
            "This table has no primary key; edit/delete require complete original keys",
        ));
    }
    let o = row(original, "original")?;
    let mut clauses = vec![];
    let mut values = vec![];
    let mut types = vec![];
    for k in pk {
        let v = o.get(k).filter(|v| !v.is_null()).ok_or_else(|| {
            error(
                "RowIdentityError",
                format!("Missing/non-NULL original primary key: {k}"),
            )
        })?;
        let col = cols
            .iter()
            .find(|c| &c.name == k)
            .ok_or_else(|| error("AdapterError", "Primary key metadata changed"))?;
        validate(v, col)?;
        values.push(v.clone());
        types.push(col.ty.clone());
        clauses.push(format!("{}=${}", quote(k)?, values.len()));
    }
    Ok((clauses.join(" AND "), values, types))
}
fn typed(
    c: &mut Client,
    sql: &str,
    values: &[Value],
    types: &[Type],
    blob: BlobStyle,
) -> Result<Value> {
    let params = values.iter().cloned().map(Param).collect::<Vec<_>>();
    let stmt = c.prepare_typed(sql, types).map_err(db)?;
    let refs = params
        .iter()
        .map(|p| p as &(dyn ToSql + Sync))
        .collect::<Vec<_>>();
    let rows = c.query(&stmt, &refs).map_err(db)?;
    let headers = stmt
        .columns()
        .iter()
        .map(|c| c.name().to_string())
        .collect::<Vec<_>>();
    let mut result = vec![];
    for r in rows {
        let mut cells = vec![];
        for (i, col) in r.columns().iter().enumerate() {
            let v = r
                .try_get::<_, Option<Cell>>(i)
                .map_err(db)?
                .map(|cell| wire_blob(cell.0, blob, col.type_()))
                .unwrap_or(Value::Null);
            cells.push(v);
        }
        result.push(cells);
    }
    Ok(json!({"headers":headers,"rows":result}))
}
fn execute(c: &mut Client, sql: &str, values: &[Value], types: &[Type]) -> Result<u64> {
    let params = values.iter().cloned().map(Param).collect::<Vec<_>>();
    let stmt = c.prepare_typed(sql, types).map_err(db)?;
    let refs = params
        .iter()
        .map(|p| p as &(dyn ToSql + Sync))
        .collect::<Vec<_>>();
    c.execute(&stmt, &refs).map_err(db)
}
fn compare(
    c: &mut Client,
    table: &str,
    original: &Value,
    values_to_check: &Value,
    pk: &[String],
    cols: &[Column],
    blob: BlobStyle,
) -> Result<Vec<(String, bool)>> {
    let reference = qualified(table)?.2;
    let (where_sql, mut params, mut types) = predicates(original, pk, cols)?;
    let mut names = vec![];
    let mut select = vec![];
    for (k, v) in row(values_to_check, "row")? {
        let col = cols
            .iter()
            .find(|c| &c.name == k)
            .ok_or_else(|| error("InvalidIdentifierError", format!("Unknown column {k}")))?;
        validate(v, col)?;
        params.push(v.clone());
        types.push(col.ty.clone());
        names.push(k.clone());
        select.push(format!(
            "to_jsonb({}) IS NOT DISTINCT FROM to_jsonb(${})",
            quote(k)?,
            params.len()
        ));
    }
    if select.is_empty() {
        return Ok(vec![]);
    }
    let out = typed(
        c,
        &format!(
            "SELECT {} FROM {reference} WHERE {where_sql} FOR UPDATE",
            select.join(",")
        ),
        &params,
        &types,
        blob,
    )?;
    let rows = out["rows"].as_array().unwrap();
    if rows.len() != 1 {
        return Err(error(
            "RowIdentityError",
            "Original identity must match exactly one existing row",
        ));
    }
    Ok(names
        .into_iter()
        .zip(
            rows[0]
                .as_array()
                .unwrap()
                .iter()
                .map(|v| v == &Value::Bool(true)),
        )
        .collect())
}
#[derive(Clone)]
struct Prepared {
    kind: String,
    original: Value,
    row: Value,
    changed: Vec<String>,
}
pub(super) fn apply(reader: &Reader, table: &str, actions: &[Value]) -> Result<usize> {
    if reader.read_only {
        return Err(error("ReadOnlyError", "PostgreSQL session is read-only"));
    }
    if actions.len() > 100000 {
        return Err(error("ValueError", "Atomic batch exceeds 100000 actions"));
    }
    let mut c = reader.connect_mode(false)?;
    let outcome = (|| -> Result<usize> {
        let cols = columns(&mut c, table)?;
        let pk = keys(&mut c, table)?;
        let reference = qualified(table)?.2;
        let mut prepared = vec![];
        let mut identities = HashSet::new();
        // Validate every shape/type and lock all existing identities before any writes.
        for action in actions {
            let kind = action["kind"]
                .as_str()
                .or_else(|| action["op"].as_str())
                .ok_or_else(|| {
                    error("ValueError", "Each action needs kind insert/update/delete")
                })?;
            if !["insert", "update", "delete"].contains(&kind) {
                return Err(error("ValueError", "Unknown batch action"));
            }
            let original = action.get("original").cloned().unwrap_or(Value::Null);
            let updated = action
                .get("row")
                .or_else(|| action.get("updated"))
                .cloned()
                .unwrap_or_else(|| json!({}));
            let mut changed = vec![];
            if kind != "delete" {
                validate_row(&updated, &cols, "row")?;
            }
            if kind == "insert" {
                for name in row(&updated, "row")?.keys() {
                    let col = cols.iter().find(|c| &c.name == name).unwrap();
                    if col.generated {
                        return Err(error(
                            "ValueError",
                            format!("Generated column {name} must be omitted from insert"),
                        ));
                    }
                    if col.identity == "a" {
                        return Err(error("ValueError",format!("GENERATED ALWAYS identity column {name} must be omitted; its default is generated")));
                    }
                }
            } else {
                validate_row(&original, &cols, "original")?;
                let (where_sql, params, types) = predicates(&original, &pk, &cols)?;
                let locked = typed(
                    &mut c,
                    &format!(
                        "SELECT {} FROM {reference} WHERE {where_sql} LIMIT 2 FOR UPDATE",
                        pk.iter()
                            .map(|k| quote(k))
                            .collect::<Result<Vec<_>>>()?
                            .join(",")
                    ),
                    &params,
                    &types,
                    reader.blob,
                )?;
                let rows = locked["rows"].as_array().unwrap();
                if rows.len() != 1 {
                    return Err(error(
                        "RowIdentityError",
                        "Original identity must match exactly one existing row",
                    ));
                }
                let token = rows[0].to_string();
                if !identities.insert(token) {
                    return Err(error(
                        "RowIdentityError",
                        "Duplicate original row identity in batch",
                    ));
                }
                if compare(&mut c, table, &original, &original, &pk, &cols, reader.blob)?
                    .iter()
                    .any(|(_, same)| !*same)
                {
                    return Err(error(
                        "RowIdentityError",
                        "Original row is stale; refresh before editing",
                    ));
                }
                if kind == "update" {
                    for (name, same) in
                        compare(&mut c, table, &original, &updated, &pk, &cols, reader.blob)?
                    {
                        let col = cols.iter().find(|c| c.name == name).unwrap();
                        if col.generated || col.identity == "a" {
                            if !same {
                                return Err(error(
                                    "ValueError",
                                    format!(
                                        "Generated/ALWAYS identity column {name} cannot be updated"
                                    ),
                                ));
                            }
                        } else if !same {
                            changed.push(name);
                        }
                    }
                }
            }
            prepared.push(Prepared {
                kind: kind.into(),
                original,
                row: updated,
                changed,
            });
        }
        let mut total = 0;
        for p in prepared {
            let affected = match p.kind.as_str() {
                "insert" => {
                    let values = row(&p.row, "row")?;
                    if values.is_empty() {
                        execute(
                            &mut c,
                            &format!("INSERT INTO {reference} DEFAULT VALUES"),
                            &[],
                            &[],
                        )?
                    } else {
                        let names = values
                            .keys()
                            .map(|k| quote(k))
                            .collect::<Result<Vec<_>>>()?;
                        let vals = values.values().cloned().collect::<Vec<_>>();
                        let types = values
                            .keys()
                            .map(|k| cols.iter().find(|c| &c.name == k).unwrap().ty.clone())
                            .collect::<Vec<_>>();
                        let binds = (1..=vals.len())
                            .map(|i| format!("${i}"))
                            .collect::<Vec<_>>()
                            .join(",");
                        execute(
                            &mut c,
                            &format!(
                                "INSERT INTO {reference} ({}) VALUES ({binds})",
                                names.join(",")
                            ),
                            &vals,
                            &types,
                        )?
                    }
                }
                "delete" => {
                    let (w, p, t) = predicates(&p.original, &pk, &cols)?;
                    execute(
                        &mut c,
                        &format!("DELETE FROM {reference} WHERE {w}"),
                        &p,
                        &t,
                    )?
                }
                "update" => {
                    if p.changed.is_empty() {
                        1
                    } else {
                        let (w, mut values, mut types) = predicates(&p.original, &pk, &cols)?;
                        let mut assigns = vec![];
                        for name in p.changed {
                            let col = cols.iter().find(|c| c.name == name).unwrap();
                            values.push(p.row[&name].clone());
                            types.push(col.ty.clone());
                            assigns.push(format!("{}=${}", quote(&name)?, values.len()));
                        }
                        execute(
                            &mut c,
                            &format!("UPDATE {reference} SET {} WHERE {w}", assigns.join(",")),
                            &values,
                            &types,
                        )?
                    }
                }
                _ => unreachable!(),
            };
            if affected != 1 {
                return Err(error(
                    "RowIdentityError",
                    "Expected exactly one affected row; atomic batch rolled back",
                ));
            }
            total += 1;
        }
        Ok(total)
    })();
    match outcome {
        Ok(n) => {
            if let Err(e) = c.batch_execute("COMMIT") {
                let err = if e.code().is_none() {
                    error(
                        "AdapterError",
                        "Commit outcome could not be confirmed; refresh before retry",
                    )
                } else {
                    db(e)
                };
                let _ = c.batch_execute("ROLLBACK");
                Err(err)
            } else {
                Ok(n)
            }
        }
        Err(e) => {
            let _ = c.batch_execute("ROLLBACK");
            Err(e)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn bytes(value: Value, ty: &Type) -> Vec<u8> {
        let mut out = postgres::types::private::BytesMut::new();
        assert!(matches!(
            Param(value).to_sql(ty, &mut out).unwrap(),
            IsNull::No
        ));
        out.to_vec()
    }
    #[test]
    fn json_context_preserves_null_scalars_and_reserved_tags() {
        for payload in [
            Value::Null,
            json!("00123"),
            json!(9007199254740993u64),
            json!({"$bytes":"00ff"}),
            json!({"$type":"decimal","value":"00123"}),
            json!({"$type":"json","value":null}),
        ] {
            let envelope = json!({"$type":"json","value":payload});
            let encoded = bytes(envelope, &Type::JSONB);
            assert_eq!(encoded[0], 1);
            assert_eq!(
                serde_json::from_slice::<Value>(&encoded[1..]).unwrap(),
                payload
            );
            let read = decode(&Type::JSONB, &encoded).unwrap();
            assert_eq!(json_payload(&read), &payload);
        }
        let literal = json!({"$type":"decimal","value":"00123"});
        assert_eq!(
            String::from_utf8(bytes(literal.clone(), &Type::TEXT)).unwrap(),
            literal.to_string()
        );
    }
    #[test]
    fn typed_validation_rejects_overflow_and_bad_uuid() {
        for (v, t) in [
            (json!(u64::MAX), Type::INT8),
            (json!(32768), Type::INT2),
            (json!("not-a-uuid"), Type::UUID),
        ] {
            assert!(Param(v)
                .to_sql(&t, &mut postgres::types::private::BytesMut::new())
                .is_err());
        }
        assert_eq!(bytes(json!("00001"), &Type::TEXT), b"00001");
    }
    #[test]
    fn native_arrays_preserve_json_null_and_tag_payloads() {
        let value = json!([null,{"$type":"json","value":null},{"$type":"json","value":{"$type":"json","value":null}},{"$bytes":"00ff"},[1,null],9007199254740993u64]);
        let encoded = bytes(value.clone(), &Type::JSONB_ARRAY);
        assert_eq!(decode(&Type::JSONB_ARRAY, &encoded).unwrap(), value);
        let uuids = json!([{"$type":"uuid","value":"00000000-0000-0000-0000-000000000001"},null]);
        assert_eq!(
            decode(&Type::UUID_ARRAY, &bytes(uuids.clone(), &Type::UUID_ARRAY)).unwrap(),
            uuids
        );
        let numbers = json!([{"$type":"decimal","value":"123456789.12345678"},null]);
        assert_eq!(
            decode(
                &Type::NUMERIC_ARRAY,
                &bytes(numbers.clone(), &Type::NUMERIC_ARRAY)
            )
            .unwrap(),
            numbers
        );
        let time = json!({"$type":"time","value":"12:34:56.123456+05:30"});
        assert_eq!(
            decode(&Type::TIMETZ, &bytes(time.clone(), &Type::TIMETZ)).unwrap(),
            time
        );
    }
    #[test]
    fn explicit_readonly_rejects_before_connection() {
        let reader = Reader::new(BlobStyle::Hex).with_read_only(true);
        assert_eq!(
            apply(&reader, "items", &[json!({"kind":"insert","row":{}})])
                .unwrap_err()
                .kind,
            "ReadOnlyError"
        );
        assert!(configuration_mode("postgresql://localhost/test", false)
            .unwrap()
            .get_options()
            .unwrap()
            .contains("default_transaction_read_only=off"));
    }
}
