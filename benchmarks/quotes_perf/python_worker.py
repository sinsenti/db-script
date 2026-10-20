#!/usr/bin/env python3
"""Reference Python adapter reads; exact JSON serialization and server read-only."""
import os
import sys
from common import ROOT, connect, encode, json_wire, exact_loads, exact_dumps, table_parts
sys.path.insert(0,str(ROOT))
from dbcli.adapters import PostgresAdapter
schema, table = table_parts(os.environ['DBCLI_PERF_TABLE'])
url = os.environ['DBCLI_POSTGRES_URL']
backend = PostgresAdapter(url,schema=schema,read_only=True,connection_factory=lambda:connect(url))
json_names = set(exact_loads(os.environ['DBCLI_QUOTES_JSON_COLUMNS']))
for line in sys.stdin:
    try:
        op = exact_loads(line);kind = op['op']
        if kind == 'tables':
            value = [schema+'.'+name for name in backend.list_tables()]
        elif kind == 'page': value = backend.fetch_page(table,op['limit'],op['offset'],op.get('filter'),op.get('order_by'))
        elif kind == 'count': value = backend.count_rows(table)
        elif kind == 'schema': value = backend.schema_info(table)
        elif kind == 'raw': value = backend.execute_readonly(op['sql'],limit=op['limit'],offset=op.get('offset',0))
        else: raise ValueError('unsupported_read')
        if hasattr(value,'headers'):
            value = {'headers':value.headers,'rows':[[json_wire(v) if k in json_names else encode(v) for k,v in zip(value.headers,row)] for row in value.rows]}
        response = {'ok':True,'value':value}
    except Exception as exc: response = {'ok':False,'error':type(exc).__name__}
    print(exact_dumps(response),flush=True)
