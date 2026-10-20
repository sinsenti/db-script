"""Read-only PostgreSQL benchmark helpers; no row values in reports."""
from __future__ import annotations
import datetime as dt
import decimal
import hashlib
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit,urlunsplit,parse_qsl,urlencode,quote
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools'))
from postgres_target import docker_target,target_environment,connection_factory,configured_url
TABLE = 'public.quotes'
class JsonNull:pass
JSON_NULL=JsonNull()
def exact_loads(value):
    return json.loads(value,parse_float=decimal.Decimal)

def exact_dumps(value,*,sort_keys=False):
    """JSON transport retaining Decimal numeric tokens, without quoted coercion."""
    if isinstance(value,decimal.Decimal):
        if not value.is_finite():raise ValueError('Nonfinite JSON decimal')
        return str(value)
    if isinstance(value,dict):
        keys=sorted(value)if sort_keys else value
        return '{'+','.join(json.dumps(k,ensure_ascii=True)+':'+exact_dumps(value[k],sort_keys=sort_keys)for k in keys)+'}'
    if isinstance(value,(list,tuple)):return '['+','.join(exact_dumps(v,sort_keys=sort_keys)for v in value)+']'
    return json.dumps(value,ensure_ascii=True,separators=(',',':'),allow_nan=False)

def decimal_key(value):
    v=decimal.Decimal(str(value))
    if not v.is_finite():return str(v)
    sign,digits,exponent=v.as_tuple();digits=list(digits)
    while digits and digits[-1]==0:digits.pop();exponent+=1
    return [sign if digits else 0,digits,exponent if digits else 0]

def canon_json(value):
    if value is None:return ['null']
    if isinstance(value,bool):return ['bool',value]
    if isinstance(value,(int,float,decimal.Decimal)):
        key=decimal_key(value)
        if isinstance(key,str):raise ValueError('Nonfinite JSON number')
        return ['number',key]
    if isinstance(value,str):return ['text',value]
    if isinstance(value,list):return ['array',[canon_json(v)for v in value]]
    if isinstance(value,dict):return ['object',[[k,canon_json(value[k])]for k in sorted(value)]]
    raise ValueError('Unsupported JSON cell type')

def loads(value):
    parsed=exact_loads(value);return JSON_NULL if parsed is None else parsed

def connect(url):
    import psycopg
    from psycopg.types.json import set_json_loads
    con=connection_factory(url)();set_json_loads(loads,con);return con

def table_parts(table):
    import re
    identifier = r'(?:"(?:[^"\x00]|"")+"|[A-Za-z_][A-Za-z_0-9$]*)'
    if not re.fullmatch(identifier + r'\.' + identifier, table):
        raise ValueError('Use a schema-qualified table name, for example public.quotes')
    tokens = re.findall(identifier, table)
    return tuple(token[1:-1].replace('""', '"') if token.startswith('"') else token.lower() for token in tokens)

def table_ref(table):
    return '.'.join('"' + token.replace('"', '""') + '"' for token in table_parts(table))

def target(args):
    if args.from_env:
        url = configured_url(os.environ.get('DBCLI_POSTGRES_URL', ''), read_only=True)
        parsed = urlsplit(url)
        if parsed.hostname not in {'127.0.0.1', 'localhost', '::1'}:
            raise ValueError('Benchmarks currently require a loopback NoTLS PostgreSQL endpoint')
        safe = {'source': 'environment', 'endpoint': parsed.hostname + ':' + str(parsed.port or 5432)}
    else:
        url, safe = docker_target(args.container, read_only=True)
    # The benchmark packet guard operates on PostgreSQL NoTLS v3 packets only.
    params = dict(parse_qsl(urlsplit(url).query))
    if params.get('sslmode', 'prefer') != 'disable':
        raise ValueError('Benchmarks require sslmode=disable on a local endpoint')
    return url, safe

def metadata(url, table=TABLE):
    schema, name = table_parts(table)
    reference = table_ref(table)
    with connect(url) as con:
        info = con.execute("SELECT c.reltuples::bigint,pg_table_size(c.oid),pg_indexes_size(c.oid) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=%s AND c.relname=%s AND c.relkind IN('r','p')", (schema,name)).fetchone()
        if not info: raise RuntimeError('benchmark_table_unavailable')
        columns = con.execute("SELECT a.attname,a.atttypid,format_type(a.atttypid,a.atttypmod) FROM pg_attribute a WHERE a.attrelid=to_regclass(%s) AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum", (reference,)).fetchall()
        keys = con.execute("SELECT a.attname FROM pg_index i CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY k(num,ord) JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.num WHERE i.indrelid=to_regclass(%s) AND i.indisprimary ORDER BY k.ord", (reference,)).fetchall()
        server_version = con.execute('SHOW server_version').fetchone()[0]
        stats = con.execute('SELECT count(*),COALESCE(sum(pg_column_size(q)),0),COALESCE(max(pg_column_size(q)),0) FROM ' + reference + ' q').fetchone()
    if not keys: raise RuntimeError('benchmark_requires_primary_key_for_deterministic_order')
    return {'table':table,'server_version':server_version,'estimated_rows':info[0],'table_bytes':info[1],'index_bytes':info[2], 'columns':[{'name':c[0],'oid':c[1],'type':c[2]}for c in columns], 'primary_key':[r[0]for r in keys], 'exact_rows':stats[0],'logical_row_bytes':int(stats[1]),'largest_row_bytes':stats[2]}


def encode(value):
    import uuid
    if isinstance(value,JsonNull):return {'$type':'json','value':None}
    if isinstance(value,uuid.UUID):return {'$type':'uuid','value':str(value)}
    if isinstance(value,decimal.Decimal):return {'$type':'decimal','value':str(value)}
    if isinstance(value,(dt.datetime,dt.date,dt.time)):return {'$type':type(value).__name__,'value':value.isoformat()}
    if isinstance(value,(bytes,bytearray,memoryview)):return {'$bytes':bytes(value).hex()}
    if isinstance(value,(tuple,list)):return [encode(v)for v in value]
    if isinstance(value,dict):return {k:encode(v)for k,v in value.items()}
    return value

def json_wire(value):
    if isinstance(value,JsonNull):return {'$type':'json','value':None}
    if isinstance(value,dict)and set(value)=={'$type','value'}and value.get('$type')=='json':return {'$type':'json','value':value}
    return value

def canon_cell(value,oid):
    if oid in (114,3802):
        if value is None:return ['sql-null']
        if isinstance(value,JsonNull):value=None
        elif isinstance(value,dict)and set(value)=={'$type','value'}and value.get('$type')=='json':value=value['value']
        return ['json',canon_json(value)]
    if value is None:return ['sql-null']
    arrays={1000:16,1001:17,1005:21,1007:23,1016:20,1009:25,1015:1043,1021:700,1022:701,1115:1114,1182:1082,1183:1083,1185:1184,1231:1700,1270:1266,199:114,3807:3802,2951:2950}
    if oid in arrays:
        def elements(v):
            return ['array',[elements(item) for item in v]] if isinstance(v,list) else canon_cell(v,arrays[oid])
        if not isinstance(value,list): raise ValueError('Invalid PostgreSQL array')
        return elements(value)
    if oid==17:
        import base64
        if isinstance(value,dict) and set(value)=={'$bytes'}: raw=bytes.fromhex(value['$bytes'])
        elif isinstance(value,dict) and set(value)=={'$blob'}: raw=base64.b64decode(value['$blob'],validate=True)
        elif isinstance(value,dict) and value.get('$type')=='blob' and set(value)=={'$type','base64'}: raw=base64.b64decode(value['base64'],validate=True)
        else: raise ValueError('Invalid bytea envelope')
        return ['bytes',raw.hex()]
    tags={1700:{'decimal'},2950:{'uuid'},1082:{'date'},1083:{'time'},1266:{'time'},1114:{'datetime'},1184:{'datetime'},700:{'float'},701:{'float'}}
    if isinstance(value,dict)and set(value)=={'$type','value'}and value.get('$type')in tags.get(oid,set()):value=value['value']
    if oid==1700:
        return ['numeric',decimal_key(value)]
    if oid in (1114,1184):
        v=dt.datetime.fromisoformat(str(value).replace('Z','+00:00'))
        if v.tzinfo:v=v.astimezone(dt.timezone.utc)
        return ['datetime',v.isoformat(timespec='microseconds')]
    if oid==1082:return ['date',dt.date.fromisoformat(str(value)).isoformat()]
    if oid in (1083,1266):return ['time',dt.time.fromisoformat(str(value)).isoformat(timespec='microseconds')]
    if oid==2950:return ['uuid',str(value).lower()]
    if oid==16:return ['bool',value]if isinstance(value,bool)else['invalid-bool',str(value)]
    return [type(value).__name__,value]

def digest_result(value,columns):
    types={c['name']:c['oid']for c in columns}
    if not isinstance(value,dict)or'headers'not in value:return hashlib.sha256(exact_dumps(value,sort_keys=True).encode()).hexdigest()
    if not isinstance(value['headers'],list)or not all(isinstance(h,str)for h in value['headers']):raise ValueError('Invalid result headers')
    if not isinstance(value.get('rows'),list)or any(not isinstance(row,(list,tuple))or len(row)!=len(value['headers'])for row in value['rows']):raise ValueError('Result row/header arity mismatch')
    data={'headers':value['headers'],'rows':[[canon_cell(v,types.get(k))for k,v in zip(value['headers'],row)]for row in value['rows']]}
    return hashlib.sha256(exact_dumps(data,sort_keys=True).encode()).hexdigest()

def proc_usage(pid):
    try:
        stat=Path('/proc',str(pid),'stat').read_text().rsplit(')',1)[1].split();status=Path('/proc',str(pid),'status').read_text();ticks=os.sysconf('SC_CLK_TCK')
        memory={line.split(':',1)[0]:int(line.split()[1])for line in status.splitlines()if line.startswith(('VmRSS:','VmHWM:'))}
        return {'cpu_seconds':(int(stat[11])+int(stat[12]))/ticks,'rss_kib':memory.get('VmRSS',0),'peak_rss_kib':memory.get('VmHWM',0)}
    except (OSError,ValueError,IndexError):return {'cpu_seconds':0,'rss_kib':0,'peak_rss_kib':0}
