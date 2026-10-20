#!/usr/bin/env python3
"""Reset only the fixed dbcli_cmp_* fixture objects on Compose loopback DBs."""
from __future__ import annotations
import argparse, hashlib, json, os, random
from pathlib import Path
from urllib.parse import urlsplit

SEED=20250308
ROWS=128
PARENT="dbcli_cmp_people"
PAIRS="dbcli_cmp_pairs"
LOOSE="dbcli_cmp_loose"
EDIT="dbcli_cmp_edit_targets"
VIEW="dbcli_cmp_people_view"


def configured_url(engine:str)->str:
    variable="DBCLI_POSTGRES_URL" if engine=="postgres" else "DBCLI_MYSQL_URL"
    value=os.environ.get(variable,"" )
    if not value: raise RuntimeError(f"required fixture environment variable {variable} is unset")
    parsed=urlsplit(value)
    if engine=="postgres": expected=("postgresql", "postgres", "postgres", 55432, "postgres")
    else: expected=("mysql", "root", "root", 53306, "test")
    scheme,user,password,port,database=expected
    if (parsed.scheme not in (("postgres", "postgresql") if engine=="postgres" else ("mysql",))
        or parsed.hostname not in ("localhost","127.0.0.1") or parsed.port!=port
        or (parsed.username or "")!=user or (parsed.password or "")!=password
        or parsed.path.lstrip("/")!=database):
        raise RuntimeError(f"{variable} is not the approved localhost disposable Compose fixture endpoint")
    return value


def connection(engine:str):
    value=configured_url(engine)
    if engine=="postgres":
        import psycopg
        return psycopg.connect(value)
    import mysql.connector
    u=urlsplit(value)
    return mysql.connector.connect(host=u.hostname,port=u.port,user=u.username,password=u.password,
                                   database=u.path.lstrip("/"),autocommit=False)


def fixture_rows():
    rng=random.Random(SEED)
    names=[("Ada Lovelace","München","O'Brien · 雪"),("Grace Hopper","New York","quoted 'value'"),
           ("李小龍","香港","naïve café"),("Zoë Saldaña","Bogotá","line\nbreak")]
    people=[]
    for ident in range(1,ROWS+1):
        if ident<=len(names): name,city,note=names[ident-1]
        else:
            name=f"Person {ident:05d}";city=("New York","München","Bogotá","東京","Nairobi")[ident%5];note=f"note {rng.randrange(1_000_000)}"
        profile=json.dumps({"index":ident,"role":"engineer" if ident%2 else "analyst",
                            "tags":["seeded","雪"] if ident==1 else [f"g{ident%7}"]},ensure_ascii=False,separators=(",",":"))
        payload=bytes((0,255,39,10,128)) if ident==1 else bytes(rng.randrange(256) for _ in range(5+ident%8))
        people.append((ident,name,city,note,profile,payload,(ident*37)%1000,ident%2,
                       None if ident%9==0 else f"optional-{ident%13}",f"2025-03-{ident%28+1:02d}T12:00:00Z"))
    pairs=[]
    for ident in range(1,ROWS+1):
        for line in (1,2):
            sku="Café-雪" if ident==1 and line==1 else ("O'Brien" if ident==2 else f"SKU-{(ident*3+line)%997:03d}")
            attrs=json.dumps({"gift":bool(ident%2),"line":line,"label":"naïve" if ident==1 else "standard"},ensure_ascii=False,separators=(",",":"))
            pairs.append((ident,line,ident,sku,(ident+line)%11+1,attrs))
    return people,pairs


def reset(engine:str,drop_only:bool=False):
    con=connection(engine);cur=con.cursor()
    try:
        cur.execute(f"DROP VIEW IF EXISTS {VIEW}")
        for table in (PAIRS,LOOSE,EDIT,PARENT): cur.execute(f"DROP TABLE IF EXISTS {table}")
        if drop_only:
            con.commit();return {"engine":engine,"action":"drop","objects_removed":5}
        blob_type="BYTEA" if engine=="postgres" else "LONGBLOB"
        cur.execute(f"CREATE TABLE {PARENT} (id INTEGER PRIMARY KEY, full_name TEXT NOT NULL, city VARCHAR(255), note TEXT, profile_json TEXT, payload {blob_type}, score INTEGER NOT NULL, active INTEGER, nullable TEXT, created_at TEXT NOT NULL)")
        cur.execute(f"CREATE INDEX idx_dbcli_cmp_city_score ON {PARENT}(city,score)")
        cur.execute(f"CREATE TABLE {PAIRS} (order_id INTEGER NOT NULL, line_id INTEGER NOT NULL, person_id INTEGER NOT NULL REFERENCES {PARENT}(id), sku VARCHAR(255) NOT NULL, quantity INTEGER NOT NULL, attributes TEXT, PRIMARY KEY(order_id,line_id))")
        cur.execute(f"CREATE INDEX idx_dbcli_cmp_person ON {PAIRS}(person_id)")
        cur.execute(f"CREATE TABLE {LOOSE} (tag TEXT, person_id INTEGER REFERENCES {PARENT}(id), value TEXT)")
        cur.execute(f"CREATE TABLE {EDIT} (id INTEGER PRIMARY KEY, label TEXT NOT NULL)")
        cur.execute(f"CREATE VIEW {VIEW} AS SELECT id,full_name FROM {PARENT}")
        people,pairs=fixture_rows()
        marks=", ".join("%s" for _ in people[0])
        for row in people: cur.execute(f"INSERT INTO {PARENT} VALUES ({marks})",row)
        marks=", ".join("%s" for _ in pairs[0])
        for row in pairs: cur.execute(f"INSERT INTO {PAIRS} VALUES ({marks})",row)
        cur.executemany(f"INSERT INTO {LOOSE} VALUES (%s,%s,%s)",[("priority",1,"O'Brien"),("雪",2,None),(None,None,"no identity")])
        cur.executemany(f"INSERT INTO {EDIT} VALUES (%s,%s)",[(1,"original key"),(2,"stable row"),(3,"delete target")])
        con.commit()
        return {"engine":engine,"action":"reset","seed":SEED,"people_rows":ROWS,"pairs_rows":ROWS*2,
                "fixture_spec_sha256":hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    except Exception:
        con.rollback();raise
    finally:
        cur.close();con.close()


def main():
    parser=argparse.ArgumentParser();parser.add_argument("--engine",choices=("postgres","mysql"),required=True);parser.add_argument("--action",choices=("reset","drop"),default="reset");args=parser.parse_args()
    try: print(json.dumps(reset(args.engine,args.action=="drop"),ensure_ascii=False))
    except Exception as exc:
        message=str(exc)
        for variable in ("DBCLI_POSTGRES_URL","DBCLI_MYSQL_URL"):
            url=os.environ.get(variable,"")
            if url:
                from urllib.parse import urlsplit
                parsed=urlsplit(url)
                for secret in (url,parsed.password or "",parsed.username or ""):
                    if secret: message=message.replace(secret,"<redacted>")
        raise SystemExit(f"remote fixture failed ({type(exc).__name__}): {message}; verify the disposable Compose service and driver") from None

if __name__=="__main__":main()
