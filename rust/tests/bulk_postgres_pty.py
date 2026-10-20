#!/usr/bin/env python3
"""Neovim E on a strictly verified disposable PostgreSQL fixture, own schema only."""
import argparse,json,os,signal,subprocess,tempfile
from pathlib import Path
from urllib.parse import urlsplit
import psycopg
from psycopg.types.json import Json,Jsonb
from bulk_neovim_pty import Terminal,nvim_child

def main():
 p=argparse.ArgumentParser();p.add_argument('binary',type=Path);p.add_argument('--kind',choices=['ratatui','original','direct','pi'],required=True);p.add_argument('--worker',type=Path);p.add_argument('--fixture',type=Path,required=True);args=p.parse_args()
 assert args.fixture.stat().st_mode&0o077==0
 data=json.loads(args.fixture.read_text());assert data['fixture_marker']=='dbcli-postgres-write-disposable-v1'and data['database']=='dbcli_postgres_write_fixture'
 for key in ['url','admin_url']:
  u=urlsplit(data[key]);assert u.hostname in ['127.0.0.1','localhost']and u.port==data['port']and u.port!=5432 and u.path=='/dbcli_postgres_write_fixture'
 raw=subprocess.run(['docker','inspect','--format','{{json .Id}} {{json .Config.Labels}}',data['container']],capture_output=True,text=True,check=True).stdout
 ident,labels=raw.split(' ',1);assert json.loads(ident)==data['container_id']and json.loads(labels).get('dbcli.fixture')=='postgres-write'
 con=psycopg.connect(data['admin_url']);assert con.execute('SELECT current_database()').fetchone()==('dbcli_postgres_write_fixture',)
 con.execute('CREATE SCHEMA rust_quotes_test');con.commit();passed=[]
 def snapshot():con.rollback();return con.execute('SELECT id,name,code,payload,plain_json,data,payload IS NULL,plain_json IS NULL FROM rust_quotes_test.items ORDER BY id').fetchall()
 try:
  for mode in ['unchanged','cancel','changed','signal']:
   con.rollback();con.execute('DROP TABLE IF EXISTS rust_quotes_test.items');con.execute('CREATE TABLE rust_quotes_test.items(id INTEGER PRIMARY KEY,name TEXT,code TEXT,payload JSONB,plain_json JSON,data BYTEA)')
   con.execute('INSERT INTO rust_quotes_test.items VALUES(%s,%s,%s,%s,%s,%s)',(1,'alpha','00123',Jsonb({'$bytes':'not-hex','nested':['synthetic '*1000,9007199254740993]}),Json(None),bytes.fromhex('00ff')))
   con.execute('INSERT INTO rust_quotes_test.items VALUES(%s,%s,%s,%s,%s,%s)',(2,'beta','00234',Jsonb({'$type':'json','value':None}),None,b''));con.commit();before=snapshot();con.rollback()
   with tempfile.TemporaryDirectory(prefix='dbcli-nvim-pg-test-')as folder:
    cmd=[str(args.binary.resolve())]
    if args.kind=='ratatui':cmd+=['--engine','postgres','--backend','codex-rust','--worker',str(args.worker.resolve()),'--table','rust_quotes_test.items']
    else:cmd+=['--postgres','--table','rust_quotes_test.items']
    previous=os.environ.get('DBCLI_POSTGRES_URL');os.environ['DBCLI_POSTGRES_URL']=data['url']
    try:t=Terminal(cmd,folder)
    finally:
     if previous is None:os.environ.pop('DBCLI_POSTGRES_URL',None)
     else:os.environ['DBCLI_POSTGRES_URL']=previous
    t.kind=args.kind;paths=[];child=None
    try:
     assert t.wait(lambda:b'alpha'in t.output),'initial PG rows missing';t.send('E');assert t.wait(lambda:nvim_child(t.proc.pid)is not None),'Neovim not launched';child,paths=nvim_child(t.proc.pid);assert len(paths)==2
     assert t.wait(lambda:b'original.json'in t.output and b'edited.json'in t.output),'diff not ready'
     if mode=='signal':os.kill(t.proc.pid,signal.SIGTERM);assert t.wait(lambda:t.proc.poll()is not None),'signal exit timed out'
     else:
      if mode in ['cancel','changed']:t.send(":lua local lines=vim.api.nvim_buf_get_lines(0,0,-1,false);local row=false;for i,l in ipairs(lines)do if l:find('\"row\"')then row=true end;if row and l:find('\"name\": \"alpha\"')then lines[i]=l:gsub('alpha','PG Neovim edited');break end end;vim.api.nvim_buf_set_lines(0,0,-1,false,lines)\r")
      t.output=b'';t.send(':cq\r'if mode=='cancel'else':wqa\r');assert t.wait(lambda:nvim_child(t.proc.pid)is None),'editor not exited'
      if mode=='changed':
       assert t.wait(lambda:b'1 updates'in t.output),'count confirmation missing';t.send('y'if args.kind=='ratatui'else'y\r')
       assert t.wait(lambda:snapshot()[0][1]=='PG Neovim edited'),'PG write not committed'
       expected=list(before);row=list(expected[0]);row[1]='PG Neovim edited';expected[0]=tuple(row);assert snapshot()==expected,'typed JSON/blob/null/TEXT state changed'
      else:assert t.wait(lambda:b'no writes'in t.output),'no-write result missing'
     assert all(not path.exists()for path in paths)and not paths[0].parent.exists(),'temporary files leaked'
     if mode!='changed':assert snapshot()==before,'cancel/noop changed PG data'
    finally:t.close()
    if child:
     try:os.kill(child,0)
     except ProcessLookupError:pass
     else:raise AssertionError('editor child leaked')
   passed.append(mode)
 finally:con.rollback();con.execute('DROP SCHEMA rust_quotes_test CASCADE');con.commit();con.close()
 print(json.dumps({'kind':args.kind,'disposable_postgres_neovim_cases':passed,'typed_rows_preserved':True,'owned_schema_removed':True}))
if __name__=='__main__':main()
