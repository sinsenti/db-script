#!/usr/bin/env python3
"""Real Neovim diff workflows against disposable on-disk SQLite only."""
import argparse,fcntl,json,os,pty,select,signal,sqlite3,struct,subprocess,tempfile,termios,time
from pathlib import Path

class Terminal:
 def __init__(self,cmd,folder):
  self.master,self.slave=pty.openpty();fcntl.ioctl(self.slave,termios.TIOCSWINSZ,struct.pack('HHHH',40,140,0,0));self.before=termios.tcgetattr(self.slave);self.output=b''
  def setup():os.setsid();fcntl.ioctl(self.slave,termios.TIOCSCTTY,0)
  self.proc=subprocess.Popen(cmd,stdin=self.slave,stdout=self.slave,stderr=self.slave,cwd=folder,env=dict(os.environ,TERM='xterm-256color',DBCLI_BULK_EDITOR='nvim -u NONE'),preexec_fn=setup)
 def drain(self,seconds=.15):
  end=time.monotonic()+seconds
  while time.monotonic()<end:
   if select.select([self.master],[],[],.03)[0]:
    try:
     chunk=os.read(self.master,65536);self.output+=chunk
     if b'\x1b[6n'in chunk:os.write(self.master,b'\x1b[1;1R')
     if b'\x1b[c'in chunk or b'\x1b[>c'in chunk:os.write(self.master,b'\x1b[?1;2c')
     if b'\x1b]11;?'in chunk:os.write(self.master,b'\x1b]11;rgb:1010/1414/1c1c\x1b\\')
    except OSError:break
 def send(self,text):os.write(self.master,text.encode());self.drain()
 def wait(self,predicate,seconds=15):
  end=time.monotonic()+seconds
  while time.monotonic()<end:
   self.drain(.1)
   if predicate():return True
  return False
 def close(self):
  if self.proc.poll()is None and getattr(self,"kind",None)=="pi":self.send("\r")
  for _ in range(4):
   if self.proc.poll()is not None:break
   self.send('q');self.drain(.2)
  try:self.proc.wait(timeout=3)
  except subprocess.TimeoutExpired:self.proc.kill();self.proc.wait();raise AssertionError('UI failed to quit')
  assert termios.tcgetattr(self.slave)==self.before,'terminal settings not restored'
  os.close(self.master);os.close(self.slave)

def nvim_child(parent):
 for p in Path('/proc').iterdir():
  if not p.name.isdigit():continue
  try:
   status=(p/'status').read_text();ppid=int(next(l for l in status.splitlines()if l.startswith('PPid:')).split()[1]);args=(p/'cmdline').read_bytes().split(b'\0')
   if ppid==parent and args and Path(args[0].decode()).name=='nvim':return int(p.name),[Path(a.decode())for a in args if a.endswith(b'.json')]
  except(FileNotFoundError,PermissionError,StopIteration,ValueError):pass
 return None

def main():
 parser=argparse.ArgumentParser();parser.add_argument('binary',type=Path);parser.add_argument('--kind',choices=['ratatui','original','direct','pi'],required=True);parser.add_argument('--worker',type=Path);args=parser.parse_args();binary=args.binary.resolve();passed=[]
 for mode in ['unchanged','cancel','changed','signal']:
  with tempfile.TemporaryDirectory(prefix='dbcli-neovim-test-')as tmp:
   folder=Path(tmp);db=folder/'fixture.sqlite'
   with sqlite3.connect(db)as c:c.executescript('CREATE TABLE items(id INTEGER PRIMARY KEY,name TEXT,code TEXT,payload TEXT,data BLOB);INSERT INTO items VALUES(1,\'alpha\',\'00123\',\'{"nested":[1,2]}\',X\'00FF\'),(2,\'beta\',\'00234\',\'{"unchanged":true}\',X\'\');')
   before=db.read_bytes()
   cmd=[str(binary)]
   if args.kind=='ratatui':cmd+=['--engine','sqlite','--database',str(db),'--table','items','--backend','original-rust','--worker',str(args.worker.resolve())]
   elif args.kind=='original':cmd+=['--db',str(db),'--table','items']
   elif args.kind=='pi':cmd+=['--database',str(db),'--table','items']
   else:cmd+=[str(db),'items']
   t=Terminal(cmd,folder);t.kind=args.kind;paths=[];child=None
   try:
    assert t.wait(lambda:b'alpha'in t.output),'initial data missing';t.send('\t')if args.kind=='ratatui'and b'Tables focused'in t.output else None;t.send('E');assert t.wait(lambda:nvim_child(t.proc.pid)is not None),'Neovim did not launch';child,paths=nvim_child(t.proc.pid);assert len(paths)==2
    for p in paths:assert p.stat().st_mode&0o777==0o600
    assert paths[0].parent==paths[1].parent and paths[0].parent.stat().st_mode&0o777==0o700
    assert t.wait(lambda:b'original.json'in t.output and b'edited.json'in t.output),'Neovim diff not displayed'
    document=json.loads(paths[0].read_text());assert all(set(entry['original'])=={'id'}for entry in document['updates']),'full original snapshot duplicated in editor file'
    assert all('name'in entry['row']for entry in document['updates']),'editable rows missing'
    proof=folder/"nvim-proof";proof.write_text("");proof.chmod(0o600)
    check="local b=vim.fn.bufnr(vim.fn.argv(0));assert(not vim.bo[b].modifiable and vim.bo[b].readonly);assert(vim.fn.expand('%:t')=='edited.json');assert(not vim.o.backup and not vim.o.writebackup and not vim.bo.swapfile and not vim.bo.undofile and vim.o.shada=='');vim.fn.writefile({'ready'},"+json.dumps(str(proof))+")"
    t.send(':lua '+check+'\r');assert t.wait(lambda:proof.read_text().strip()=='ready'),'diff buffer safety check failed'
    if mode=='signal':os.kill(t.proc.pid,signal.SIGTERM);assert t.wait(lambda:t.proc.poll()is not None),'signal exit timed out'
    else:
     if mode in ['cancel','changed']:
      t.send(":lua local v=vim.json.decode(table.concat(vim.api.nvim_buf_get_lines(0,0,-1,false),'\\n'));v.updates[1].row.name='changed through nvim';vim.api.nvim_buf_set_lines(0,0,-1,false,{vim.json.encode(v)})\r")
     t.output=b'';t.send(':cq\r'if mode=='cancel'else':wqa\r')
     assert t.wait(lambda:nvim_child(t.proc.pid)is None),'Neovim did not exit'
     if mode=='changed':
      assert t.wait(lambda:b'1 updates'in t.output and (b'Apply atomic'in t.output or b'count confirmation'in t.output or b'atomic transaction'in t.output)),'minimal count confirmation missing'
      assert b'"original"'not in t.output,'full JSON preview rendered in TUI'
      t.send('y'if args.kind=='ratatui'else'y\r')
      def changed():
       with sqlite3.connect(db)as c:return c.execute('SELECT name FROM items WHERE id=1').fetchone()==('changed through nvim',)
      assert t.wait(changed),'confirmed change not applied'
      with sqlite3.connect(db)as c:
       assert c.execute('SELECT code,data FROM items WHERE id=1').fetchone()==('00123',bytes.fromhex('00ff'))
       assert c.execute('SELECT payload FROM items WHERE id=2').fetchone()==('{"unchanged":true}',),'untouched JSON TEXT was rewritten'
     else:assert t.wait(lambda:b'no writes'in t.output),'no-write result missing'
    assert all(not p.exists()for p in paths),'bulk files leaked'
    assert not paths[0].parent.exists(),'bulk directory leaked'
    if mode!='changed':assert db.read_bytes()==before,'cancel/no-op changed database bytes'
   except Exception:
    debug=Path(__file__).resolve().parent.parent/'target'/('bulk-'+args.kind+'-'+mode+'.ansi');debug.write_bytes(t.output);print('Failed workflow',args.kind,mode,'capture',debug)
    raise
   finally:
    try:t.close()
    except Exception:
     debug=Path(__file__).resolve().parent.parent/'target'/('bulk-'+args.kind+'-'+mode+'-close.ansi');debug.write_bytes(t.output);print('Failed close',args.kind,mode,'capture',debug);raise
   if child:
    try:os.kill(child,0)
    except ProcessLookupError:pass
    else:raise AssertionError('Neovim child leaked')
   passed.append(mode)
 print(json.dumps({'kind':args.kind,'real_neovim_cases':passed,'terminal_restored':True,'temporary_files_removed':True}))
if __name__=='__main__':main()
