#!/usr/bin/env python3
"""Opt-in disposable PostgreSQL regression; never accepts a bare database URL.

Requires psycopg. A private descriptor, dedicated Docker label and marker database
are all verified before mutation. Only rust_write_test is created/dropped.
"""
import argparse, base64, json, os, selectors, subprocess
from pathlib import Path
from urllib.parse import urlsplit
import psycopg

def run():
    p=argparse.ArgumentParser();p.add_argument('--fixture',required=True);p.add_argument('--worker',required=True);p.add_argument('--family',choices=['dispatch','batch','rewrite'],required=True);a=p.parse_args()
    path=Path(a.fixture);assert path.stat().st_mode&0o077==0,'descriptor permissions'
    data=json.loads(path.read_text());assert data['fixture_marker']=='dbcli-postgres-write-disposable-v1'and data['database']=='dbcli_postgres_write_fixture'
    u=urlsplit(data['url']);assert u.hostname in ['localhost','127.0.0.1']and u.port==data['port']and u.port!=5432 and u.path=='/dbcli_postgres_write_fixture'
    actual=subprocess.run(['docker','inspect','--format','{{json .Id}} {{json .Config.Labels}}',data['container']],capture_output=True,check=True,text=True).stdout
    ident,labels=actual.split(' ',1);assert json.loads(ident)==data['container_id']and json.loads(labels).get('dbcli.fixture')=='postgres-write'
    con=psycopg.connect(data['admin_url']);assert con.execute('SELECT current_database()').fetchone()==('dbcli_postgres_write_fixture',)
    # Fail if another test owns the schema; never remove somebody else's test state.
    con.execute('CREATE SCHEMA rust_write_test');con.commit()
    proc=None;checks=0
    def wire(v):
        if isinstance(v,dict):
            if set(v)=={'$type','value'}and v['$type']=='json':return v
            if set(v)=={'$bytes'}and a.family!='batch':
                b=base64.b64encode(bytes.fromhex(v['$bytes'])).decode();return {'$blob':b}if a.family=='dispatch'else {'$type':'blob','base64':b}
            return {k:wire(x)for k,x in v.items()}
        return [wire(x)for x in v]if isinstance(v,list)else v
    serial=0
    def request(op,ro=False):
        nonlocal serial
        serial+=1;op=dict(op)
        if a.family=='batch':req={'engine':'postgres','read_only':ro,'operations':[op]}
        elif a.family=='dispatch':
            if op['op']=='update':op['updated']=op.pop('row')
            req=op
        else:
            kind=op.pop('op')
            if kind=='update':op['updated']=op.pop('row')
            req={'id':serial,'op':kind,'args':op}
        proc.stdin.write(json.dumps(wire(req))+'\n');proc.stdin.flush();assert selector.select(15),'worker timeout'
        res=json.loads(proc.stdout.readline())
        if a.family=='batch':res=res['results'][0]
        return res
    def ok(op):
        nonlocal checks
        res=request(op);assert res['ok'],op['op']+' rejected'
        checks+=1;return res.get('value',res.get('result'))
    def snapshot():
        con.rollback()
        return con.execute('SELECT to_jsonb(t) FROM rust_write_test.items t ORDER BY id').fetchall()
    def reject(op):
        nonlocal checks
        before=snapshot();assert not request(op)['ok'];assert before==snapshot();checks+=1
    try:
        con.execute('CREATE TABLE rust_write_test.items(LIKE public.items INCLUDING ALL)');con.execute('INSERT INTO rust_write_test.items SELECT * FROM public.items');con.execute('CREATE TABLE rust_write_test.gen(id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,name TEXT NOT NULL UNIQUE,base INTEGER DEFAULT 3,derived INTEGER GENERATED ALWAYS AS(base*2)STORED)');con.execute("CREATE TABLE rust_write_test.composite(a TEXT,b INTEGER,value TEXT,PRIMARY KEY(a,b));INSERT INTO rust_write_test.composite VALUES('001',1,'old')");con.execute('CREATE TABLE rust_write_test.arrays(id INTEGER PRIMARY KEY,uuids UUID[],numbers NUMERIC[],jsons JSONB[],clock TIMETZ)');con.execute("CREATE TABLE rust_write_test.numeric_keys(id NUMERIC(8,2) PRIMARY KEY,name TEXT);INSERT INTO rust_write_test.numeric_keys VALUES(1.00,'original')");con.commit()
        cmd=[a.worker,'--postgres',{'dispatch':'--protocol','batch':'--runner','rewrite':'--protocol=jsonl'}[a.family]]
        proc=subprocess.Popen(cmd,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,env=dict(os.environ,DBCLI_POSTGRES_URL=data['url']))
        selector=selectors.DefaultSelector();selector.register(proc.stdout,selectors.EVENT_READ)
        if a.family=='rewrite':assert json.loads(proc.stdout.readline())['ready']==1
        policy=ok({'op':'raw','sql':"SELECT current_setting('transaction_read_only'),current_setting('default_transaction_read_only')",'limit':1});assert policy['rows'][0]==['on','on']
        arrays={'id':1,'uuids':[{'$type':'uuid','value':'00000000-0000-0000-0000-000000000001'},None],'numbers':[{'$type':'decimal','value':'123456789.12345678'},None],'jsons':[None,{'$type':'json','value':None},{'$type':'json','value':{'$type':'json','value':None}},{'$type':'json','value':{'$bytes':'00ff'}},{'$type':'json','value':[1,None]}],'clock':{'$type':'time','value':'12:34:56.123456+05:30'}}
        ok({'op':'insert','table':'rust_write_test.arrays','row':arrays})
        arraypage=ok({'op':'page','table':'rust_write_test.arrays','limit':1});ar=dict(zip(arraypage['headers'],arraypage['rows'][0]));assert ar['uuids']==arrays['uuids'] and ar['numbers']==arrays['numbers'] and ar['clock']==arrays['clock'];assert ar['jsons']==[None,{'$type':'json','value':None},{'$type':'json','value':{'$type':'json','value':None}},{'$bytes':'00ff'},[1,None]]
        ar['jsons']=[None if v is None else v if isinstance(v,dict)and set(v)=={'$type','value'}and v.get('$type')=='json'else {'$type':'json','value':v}for v in ar['jsons']]
        ok({'op':'update','table':'rust_write_test.arrays','original':ar,'row':ar})
        alias=request({'op':'apply_batch','table':'rust_write_test.numeric_keys','actions':[{'kind':'update','original':{'id':{'$type':'decimal','value':'1.0'}},'row':{'name':'must rollback'}},{'kind':'delete','original':{'id':{'$type':'decimal','value':'1.00'}}}]});assert not alias['ok'];checks+=1
        con.rollback();assert con.execute('SELECT name FROM rust_write_test.numeric_keys').fetchall()==[('original',)]
        table='rust_write_test.items' 
        for i in range(1,13):
            r=ok({'op':'page','table':table,'limit':1,'filter':'id = '+str(i)})
            original=dict(zip(r['headers'],r['rows'][0]));original={k:({'$type':'json','value':v}if k in ['payload','plain_json']and v is not None and not(isinstance(v,dict)and set(v)=={'$type','value'}and v['$type']=='json')else v)for k,v in original.items()}
            before=snapshot();ok({'op':'update','table':table,'original':original,'row':dict(original,name='edited '+str(i))});after=snapshot()
            for aa,bb in zip(before,after):aa[0].pop('name');bb[0].pop('name')
            assert before==after,'typed snapshot changed'
        for j in [None,{'$type':'json','value':None},{'$type':'json','value':{'$type':'json','value':None}},{'$type':'json','value':{'$bytes':'00ff'}}]:
            ok({'op':'update','table':table,'original':{'id':1},'row':{'payload':j,'plain_json':j}})
            con.rollback();v=con.execute('SELECT payload,plain_json,payload IS NULL FROM rust_write_test.items WHERE id=1').fetchone();assert v==(None,None,True)if j is None else v==(j['value'],j['value'],False)
        ok({'op':'insert','table':'rust_write_test.gen','row':{'name':'first'}})
        g=ok({'op':'page','table':'rust_write_test.gen','limit':1});o=dict(zip(g['headers'],g['rows'][0]));ok({'op':'update','table':'rust_write_test.gen','original':o,'row':dict(o,base=8)})
        reject({'op':'update','table':'rust_write_test.gen','original':{'id':1},'row':{'id':90}})
        reject({'op':'update','table':'rust_write_test.gen','original':{'id':1},'row':{'derived':90}})
        ok({'op':'update','table':'rust_write_test.composite','original':{'a':'001','b':1},'row':{'a':'002','value':'new'}})
        reject({'op':'delete','table':'rust_write_test.composite','original':{'a':'002'}})
        reject({'op':'apply_batch','table':table,'actions':[{'kind':'update','original':{'id':1},'row':{'name':'rollback'}},{'kind':'insert','row':{'id':1,'name':'duplicate'}}]})
        reject({'op':'apply_batch','table':table,'actions':[{'kind':'update','original':{'id':1},'row':{'name':'rollback'}},{'kind':'insert','row':{'id':900,'uuid_value':'not uuid','name':'bad uuid'}}]})
        reject({'op':'apply_batch','table':table,'actions':[{'kind':'delete','original':{'id':1}},{'kind':'delete','original':{'id':1}}]})
        reject({'op':'delete','table':table,'original':{'id':1,'name':'stale'}})
        reject({'op':'raw','sql':"WITH x AS(DELETE FROM rust_write_test.items RETURNING *)SELECT * FROM x",'limit':1})
        print('PASS',checks,'disposable PostgreSQL checks; no row data persisted')
    finally:
        if proc:
            proc.stdin.close();proc.wait(timeout=5)
        con.rollback();con.execute('DROP SCHEMA rust_write_test CASCADE');con.commit();con.close()
if __name__=='__main__':run()
