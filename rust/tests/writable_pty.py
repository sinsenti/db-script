"""Disposable synthetic writable TUI smoke; output contains statuses only."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import sqlite3
import struct
import subprocess
import tempfile
import termios
import time


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('binary',type=Path)
    parser.add_argument('--kind',choices=['original','direct','pi'],required=True)
    args=parser.parse_args()
    binary=args.binary.resolve()
    with tempfile.TemporaryDirectory(prefix='dbcli-writable-pty-') as folder:
        folder=Path(folder);database=folder/'synthetic.sqlite';control=folder/'edit.json';editor=folder/'editor.py'
        blob={'original':{'$blob':'AP8='},'direct':{'$bytes':'00ff'},'pi':{'$type':'blob','base64':'AP8='}}[args.kind]
        with sqlite3.connect(database) as c:
            c.executescript('CREATE TABLE tasks(id INTEGER PRIMARY KEY,code TEXT UNIQUE,payload TEXT,bdata BLOB); CREATE VIRTUAL TABLE task_search USING fts5(code); CREATE TRIGGER tasks_i AFTER INSERT ON tasks BEGIN INSERT INTO task_search(rowid,code) VALUES(new.id,new.code); END; CREATE TRIGGER tasks_u AFTER UPDATE ON tasks BEGIN UPDATE task_search SET code=new.code WHERE rowid=old.id; END; CREATE TRIGGER tasks_d AFTER DELETE ON tasks BEGIN DELETE FROM task_search WHERE rowid=old.id; END;')
            c.execute('INSERT INTO tasks VALUES(?,?,?,?)',(1,'0001',json.dumps(blob),bytes.fromhex('00ff')))
        editor.write_text('''import json,os,stat,sys\np=sys.argv[-1]\nassert stat.S_IMODE(os.stat(p).st_mode)==0o600\na=json.load(open(os.environ['DBCLI_PTY_CONTROL']))\nif a.get('fail'): sys.exit(7)\nv=json.load(open(p))\nif a['mode']=='insert': v=a['value']\nelif a['mode']=='edit': v['code']=a['code']\nelif a['mode']=='bulk':\n v['updates'][0]['row']['code']=a['code']\n v['insert']=a['insert']\njson.dump(v,open(p,'w'),ensure_ascii=False)\n''')
        command=[str(binary)]
        if args.kind=='original':command+=['--db',str(database),'--table','tasks']
        elif args.kind=='direct':command +=[str(database),'tasks']
        else:command+=['--database='+str(database),'--table','tasks']
        master,slave=pty.openpty();saved=termios.tcgetattr(slave)
        fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',28,120,0,0))
        env=os.environ.copy();env.update(TERM='xterm-256color',EDITOR='python3 '+str(editor),DBCLI_PTY_CONTROL=str(control))
        process=subprocess.Popen(command,stdin=slave,stdout=slave,stderr=slave,cwd=folder,env=env)
        captured=bytearray()
        def drain(seconds=.15):
            end=time.monotonic()+seconds
            while time.monotonic()<end:
                if select.select([master],[],[],max(0,end-time.monotonic()))[0]:
                    try: captured.extend(os.read(master,65536))
                    except OSError: break
        def send(value):os.write(master,value);drain()
        def data(sql):
            with sqlite3.connect(database) as c:return c.execute(sql).fetchall()
        def until(predicate):
            end=time.monotonic()+8
            while time.monotonic()<end:
                if predicate():return
                drain(.05)
                if process.poll() is not None:raise AssertionError('unexpected process exit')
            raise AssertionError('workflow timed out')
        def dismiss():
            if args.kind=='pi':send(b'\r')
        checks=[]
        try:
            drain(.25)
            control.write_text(json.dumps({'mode':'insert','value':[{'id':2,'code':'0002','payload':{'array':[1,None,'名']},'bdata':blob}]}))
            send(b'a');until(lambda:len(data('SELECT id FROM tasks'))==2);dismiss();checks.append('atomic multi-insert')
            control.write_text(json.dumps({'mode':'edit','code':'0009'}));send(b'e');until(lambda:data('SELECT code FROM tasks WHERE id=1')==[('0009',)]);dismiss();checks.append('row edit preserves JSON/BLOB')
            assert data("SELECT typeof(payload),typeof(bdata),hex(bdata) FROM tasks WHERE id=1")==[('text','blob','00FF')]
            control.write_text(json.dumps({'mode':'edit','code':'0010'}));send(b'\r');send(b'e');until(lambda:data('SELECT code FROM tasks WHERE id=1')==[('0010',)]);dismiss();checks.append('detail edit')
            send(b'dd');send(b'y\r');until(lambda:data('SELECT id FROM tasks WHERE id=1')==[]);dismiss();checks.append('confirmed delete')
            control.write_text(json.dumps({'mode':'bulk','code':'0004','insert':[{'id':3,'code':'0003','payload':{'ok':True},'bdata':blob}]}));send(b'E');drain(.2);send(b'q');send(b'y\r');until(lambda:data('SELECT id,code FROM tasks ORDER BY id')==[(2,'0004'),(3,'0003')]);dismiss();checks.append('atomic explicit bulk update+insert')
            send(b':');send(b'SELECT * FROM tasks ORDER BY id\r');send(b'/');send(b'code = \'0004\'\r');drain();send(b'q');checks.append('nested raw paging/filter')
            send(b' ');send(b'j ');send(b'dd');drain(.2);send(b'q');send(b'y\r');until(lambda:data('SELECT id FROM tasks')==[]);dismiss();checks.append('atomic marked deletion')
            send(b'q');until(lambda:process.poll() is not None)
            drain(.1)
            assert termios.tcgetattr(slave)==saved,'terminal settings not restored'
            assert b'\x1b[?1049l' in captured,'alternate screen not restored'
            with sqlite3.connect(database) as c:
                c.execute("INSERT INTO task_search(task_search) VALUES('integrity-check')")
                assert c.execute('PRAGMA integrity_check').fetchone()==('ok',)
            checks.append('terminal restoration and FTS integrity')
        finally:
            if process.poll() is None:process.terminate();process.wait(timeout=5)
            os.close(master);os.close(slave)
        print(json.dumps({'kind':args.kind,'passed':checks,'count':len(checks)}))

if __name__=='__main__':main()
