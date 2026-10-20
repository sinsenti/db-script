#!/usr/bin/env python3
"""JSONL runner using the production PostgreSQLAdapter/MySQLAdapter."""
from __future__ import annotations
import argparse, base64, json, os, re, sys
from pathlib import Path
from typing import Any
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from dbcli.adapters import MySQLAdapter, PostgresAdapter  # noqa:E402
TRACE=[]
PLACEHOLDER=re.compile(r"\$[1-9][0-9]*|%s|\?")


def safe(value:Any)->Any:
    if isinstance(value,(bytes,bytearray,memoryview)):
        return {"$blob":base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value,dict): return {str(k):safe(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)): return [safe(v) for v in value]
    if value is None or isinstance(value,(str,int,float,bool)): return value
    return str(value)

def normalize_sql(sql:str)->str:
    return PLACEHOLDER.sub("?",sql)

def decode(value:Any)->Any:
    if isinstance(value,dict):
        if set(value)=={"$blob"}:return base64.b64decode(value["$blob"],validate=True)
        return {k:decode(v) for k,v in value.items()}
    if isinstance(value,list):return [decode(v) for v in value]
    return value

class TrackedCursor:
    def __init__(self,cursor):self._cursor=cursor
    def execute(self,sql,params=None):
        bound=() if params is None else params
        TRACE.append({"sql":normalize_sql(str(sql)),"params":safe(list(bound))})
        if params is None:return self._cursor.execute(sql)
        return self._cursor.execute(sql,params)
    def __getattr__(self,name):return getattr(self._cursor,name)
    def close(self):return self._cursor.close()
class TrackedConnection:
    def __init__(self,connection):self._connection=connection
    def cursor(self,*args,**kwargs):return TrackedCursor(self._connection.cursor(*args,**kwargs))
    def __getattr__(self,name):return getattr(self._connection,name)
    def close(self):return self._connection.close()

def result_object(result):return {"headers":list(result.headers),"rows":safe(result.rows)}
def run(adapter,engine,request):
    op=request["op"];table=request.get("table")
    if op=="list_databases":return adapter.list_databases()
    if op=="list_tables":return adapter.list_tables(bool(request.get("include_views",False)))
    if op=="schema":return result_object(adapter.schema_info(table))
    if op=="foreign_keys":return adapter.foreign_keys(table)
    if op=="page":
        order=request.get("sort");return result_object(adapter.fetch_page(table,request["limit"],request["offset"],request.get("filter"),tuple(order) if order else None))
    if op=="count":return adapter.count_rows(table,request.get("filter"))
    if op=="insert":return adapter.insert_row(table,decode(request["row"]))
    if op=="update":return adapter.update_row(table,decode(request["original"]),decode(request["updated"]))
    if op=="delete":return adapter.delete_row(table,decode(request["original"]))
    if op=="raw":
        sql=request["sql"].replace("?","%s")
        result=adapter.execute_readonly(sql,decode(request.get("params",[])),request.get("limit",50),request.get("offset",0))
        return result_object(result)
    raise ValueError(f"Unknown operation: {op}")

def main():
    parser=argparse.ArgumentParser();parser.add_argument("--engine",choices=("postgres","mysql"),required=True);parser.add_argument("--read-only",action="store_true");args=parser.parse_args()
    variable="DBCLI_POSTGRES_URL" if args.engine=="postgres" else "DBCLI_MYSQL_URL"
    url=os.environ.get(variable)
    if not url:raise SystemExit(f"missing required environment variable {variable}")
    def factory():
        if args.engine=="postgres":
            import psycopg
            return TrackedConnection(psycopg.connect(url))
        from urllib.parse import urlsplit
        import mysql.connector
        parsed=urlsplit(url)
        return TrackedConnection(mysql.connector.connect(host=parsed.hostname or "localhost",port=parsed.port or 3306,
            user=parsed.username or "",password=parsed.password or "",database=parsed.path.lstrip("/"),autocommit=False))
    # Credentials stay in the connection factory and never enter stdout/argv.
    adapter=(PostgresAdapter(url,schema="public",read_only=args.read_only,connection_factory=factory)
             if args.engine=="postgres" else MySQLAdapter(url,read_only=args.read_only,connection_factory=factory))
    for line in sys.stdin:
        TRACE.clear()
        try:
            request=json.loads(line);value=run(adapter,args.engine,request)
            response={"ok":True,"result":safe(value),"trace":list(TRACE)}
        except Exception as exc:
            response={"ok":False,"error":{"class":type(exc).__name__,"message":str(exc).replace(url,"<configured fixture URL>")},"trace":list(TRACE)}
        sys.stdout.write(json.dumps(response,ensure_ascii=False,separators=(",",":"))+"\n");sys.stdout.flush()
if __name__=="__main__":main()
