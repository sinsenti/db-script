"""Benchmark-only SQL write guard for the local, authorized NoTLS target.

Forwards PostgreSQL packets without retaining payloads. Rejects generated CRUD,
and DDL before forwarding them. Connection-mode setup is passed through; the
applications still enforce server read-only transactions for their read APIs. This is a guard
for these controlled application workloads, not an untrusted-SQL sandbox.
"""
import re,socket,socketserver,struct,threading
from urllib.parse import urlsplit,urlunsplit

def exact(sock,size):
    chunks=[]
    while size:
        data=sock.recv(size)
        if not data:raise EOFError()
        chunks.append(data);size-=len(data)
    return b''.join(chunks)

def masked_sql(sql):
    """Mask quoted text and nested comments before checking controlled SQL."""
    out=list(sql);i=0
    while i<len(sql):
        start=i
        if sql.startswith('--',i):
            end=sql.find('\n',i);i=len(sql)if end<0 else end
        elif sql.startswith('/*',i):
            depth=1;i+=2
            while i<len(sql)and depth:
                if sql.startswith('/*',i):depth+=1;i+=2
                elif sql.startswith('*/',i):depth-=1;i+=2
                else:i+=1
            if depth:raise ValueError('unclosed_comment')
        elif sql[i]in ["'",'"']:
            quote=sql[i];escaped=quote=="'"and i>0 and sql[i-1]in 'Ee'and(i<2 or not(sql[i-2].isalnum()or sql[i-2]=='_'))
            i+=1;closed=False
            while i<len(sql):
                if escaped and sql[i]=='\\':i+=2
                elif sql[i]==quote:
                    if i+1<len(sql)and sql[i+1]==quote:i+=2
                    else:i+=1;closed=True;break
                else:i+=1
            if not closed:raise ValueError('unclosed_quote')
        elif sql[i]=='$':
            match=re.match(r'\$(?:[A-Za-z_][A-Za-z_0-9]*)?\$',sql[i:])
            if not match:i+=1;continue
            tag=match.group();end=sql.find(tag,i+len(tag))
            if end<0:raise ValueError('unclosed_dollar_quote')
            i=end+len(tag)
        else:i+=1;continue
        out[start:i]=' '*(i-start)
    return ''.join(out)


def allowed_sql(raw):
    try:masked=masked_sql(raw.decode('utf-8'))
    except (UnicodeDecodeError,ValueError):return False
    # Connection setup may be writable: baseline UIs override URL defaults even
    # for ordinary reads. The guard blocks generated mutation SQL regardless.
    # This finite controlled workload guard is not an untrusted-SQL sandbox;
    # arbitrary user-defined SELECT functions cannot be made safe by this lexer.
    forbidden=r'INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|TRUNCATE|GRANT|REVOKE|COPY|CALL|DO|INTO|VACUUM|REINDEX|REFRESH|CLUSTER|COMMENT|SECURITY|REASSIGN|DISCARD|LISTEN|NOTIFY|UNLISTEN|PREPARE|EXECUTE|LOCK|AUTHORIZATION|ROLE'
    if re.search(r'(?i)\b(?:'+forbidden+r')\b',masked):return False
    if re.search(r'(?i)\b(?:nextval|setval|set_config|lo_[A-Za-z_0-9]*|dblink[A-Za-z_0-9]*|pg_(?:advisory[A-Za-z_0-9]*|cancel_backend|terminate_backend|reload_conf|rotate_logfile|logical_emit_message|write_file))\s*\(',masked):return False
    if re.search(r'(?i)\bstandard_conforming_strings\b',masked):return False
    statements=[part.strip()for part in masked.split(';')if part.strip()]
    return bool(statements)and all(re.match(r'(?i)^(SELECT|WITH|EXPLAIN|SHOW|SET|BEGIN|START|COMMIT|END|ROLLBACK|DEALLOCATE)\b',part)for part in statements)

class Guard:
    def __init__(self,url):
        parsed=urlsplit(url);self.upstream=(parsed.hostname,parsed.port or 5432);self.blocked=0;self.statements=0;self.connections=0;self.lock=threading.Lock();self.sockets=set();guard=self
        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                remote=None
                try:
                    self.request.settimeout(30);self.request.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1);head=exact(self.request,4);length=struct.unpack('!I',head)[0]
                    if not 8<=length<=65536:raise ValueError('startup_length')
                    startup=exact(self.request,length-4)
                    if struct.unpack('!I',startup[:4])[0]!=196608:raise ValueError('only_postgresql_NoTLS_v3')
                    remote=socket.create_connection(guard.upstream,timeout=3);remote.settimeout(30);remote.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
                    with guard.lock:guard.connections+=1;guard.sockets.update([self.request,remote])
                    remote.sendall(head+startup)
                    def reverse():
                        try:
                            while True:
                                data=remote.recv(262144)
                                if not data:break
                                self.request.sendall(data)
                        except OSError:pass
                        finally:
                            try:self.request.shutdown(socket.SHUT_RDWR)
                            except OSError:pass
                    thread=threading.Thread(target=reverse,daemon=True);thread.start()
                    while True:
                        kind=exact(self.request,1);size=struct.unpack('!I',exact(self.request,4))[0]
                        if not 4<=size<=128*1024*1024:raise ValueError('packet_length')
                        body=exact(self.request,size-4)
                        query=body.split(b'\0',1)[0]if kind==b'Q'else body.split(b'\0',2)[1]if kind==b'P'else None
                        if query is not None:
                            with guard.lock:guard.statements+=1
                            if not allowed_sql(query):
                                with guard.lock:guard.blocked+=1
                                break
                        remote.sendall(kind+struct.pack('!I',size)+body)
                        if kind==b'X':break
                except (OSError,EOFError,ValueError,IndexError):pass
                finally:
                    if remote:
                        try:remote.shutdown(socket.SHUT_RDWR)
                        except OSError:pass
                        remote.close()
                    with guard.lock:guard.sockets.discard(self.request);guard.sockets.discard(remote)
        class Server(socketserver.ThreadingTCPServer):allow_reuse_address=True;daemon_threads=True
        self.server=Server(('127.0.0.1',0),Handler);self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        userinfo, separator, _ = parsed.netloc.rpartition('@')
        authority=(userinfo+'@' if separator else '')+'127.0.0.1:'+str(self.server.server_address[1])
        self.url=urlunsplit((parsed.scheme,authority,parsed.path,parsed.query,''))
    def stats(self):return {'connections':self.connections,'statements':self.statements,'blocked_write_attempts':self.blocked}
    def close(self):
        self.server.shutdown();self.server.server_close()
        with self.lock:sockets=list(self.sockets)
        for sock in sockets:
            try:sock.shutdown(socket.SHUT_RDWR);sock.close()
            except OSError:pass
    def __enter__(self):return self
    def __exit__(self,*args):self.close()
