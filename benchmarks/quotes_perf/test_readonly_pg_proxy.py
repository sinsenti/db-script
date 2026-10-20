"""Offline lexer/packet guard regressions. No database or credentials required."""
import socket
import struct
import threading
import unittest
from readonly_pg_proxy import Guard,allowed_sql,exact

class GuardTests(unittest.TestCase):
    def test_proxy_url_preserves_optional_authentication_without_inventing_a_user(self):
        from urllib.parse import urlsplit
        for url,username in [('postgresql://127.0.0.1:55432/fixture?sslmode=disable',None),('postgresql://name:secret@127.0.0.1:55432/fixture?sslmode=disable','name')]:
            with Guard(url) as guard:
                parsed=urlsplit(guard.url)
                self.assertEqual(parsed.username,username)
                self.assertEqual(parsed.path,'/fixture')
                self.assertEqual(parsed.hostname,'127.0.0.1')
                self.assertEqual(parsed.port,guard.server.server_address[1])

    def test_controlled_reads_and_connection_setup(self):
        for query in ["SELECT * FROM public.quotes ORDER BY id LIMIT $1",'BEGIN READ ONLY',
                      'SET default_transaction_read_only=off','SET TRANSACTION READ WRITE',
                      'SELECT current_setting(\'transaction_read_only\')',
                      'WITH q AS(SELECT 1)SELECT *FROM q',"SELECT 'DELETE; UPDATE INTO' AS value",
                      'SELECT $$DROP TABLE; $$',"SELECT $body$COPY; INTO$body$",
                      '/* outer /* inner */ still outer */ SELECT 1;',
                      "SELECT 'it''s ordinary'; SELECT 2",'SELECT "update" FROM "into"',
                      'BEGIN; SET TIME ZONE \'UTC\'; SELECT 1; COMMIT']:
            with self.subTest(query=query):self.assertTrue(allowed_sql(query.encode()))
    def test_writes_into_functions_malformed_and_tail_rejected(self):
        for query in ['UPDATE public.quotes SET id=1','INSERT INTO x VALUES(1)',
                      'WITH x AS(DELETE FROM quotes RETURNING *)SELECT *FROM x',
                      'SELECT * INTO new_table FROM quotes','EXPLAIN ANALYZE DELETE FROM quotes',
                      'SELECT 1; DROP TABLE quotes','SELECT 1; RESET ALL','SELECT nextval(\'seq\')',
                      'SELECT pg_advisory_lock(1)',"SELECT set_config('transaction_read_only','off',false)",
                      'SELECT lo_export(1,\'/tmp/file\')','SELECT dblink_exec(\'x\',\'UPDATE y\')',
                      'VACUUM quotes','REINDEX TABLE quotes','SELECT 1 /* unclosed',
                      "SELECT 'unclosed",'SELECT $$unclosed',"SELECT '\\'; DELETE FROM quotes; --'",
                      'SET standard_conforming_strings=off','PREPARE s AS SELECT 1','EXECUTE s']:
            with self.subTest(query=query):self.assertFalse(allowed_sql(query.encode()))
        self.assertFalse(allowed_sql(b'SELECT \xff'))
    def test_simple_and_extended_write_packets_never_forwarded(self):
        for kind in [b'Q',b'P']:
            with self.subTest(kind=kind):
                upstream=socket.socket();upstream.bind(('127.0.0.1',0));upstream.listen();seen=[]
                def serve():
                    con,_=upstream.accept()
                    with con:
                        size=struct.unpack('!I',exact(con,4))[0];exact(con,size-4)
                        try:
                            while True:seen.append(con.recv(1024));break
                        except OSError:pass
                thread=threading.Thread(target=serve);thread.start()
                try:
                    with Guard('postgresql://unused@127.0.0.1:'+str(upstream.getsockname()[1])+'/fixture?sslmode=disable')as guard:
                        sock=socket.create_connection(guard.server.server_address,timeout=3)
                        with sock:
                            startup=struct.pack('!I',196608)+b'user\0unused\0database\0fixture\0\0'
                            sock.sendall(struct.pack('!I',len(startup)+4)+startup)
                            sql=b'SELECT * INTO forbidden FROM quotes'
                            body=sql+b'\0'if kind==b'Q'else b'stmt\0'+sql+b'\0'+struct.pack('!H',0)
                            sock.sendall(kind+struct.pack('!I',len(body)+4)+body)
                            self.assertEqual(sock.recv(1),b'')
                        thread.join(3);self.assertFalse(thread.is_alive())
                        self.assertEqual(guard.blocked,1);self.assertEqual(guard.statements,1)
                    self.assertEqual(seen,[b''])
                finally:upstream.close()

if __name__=='__main__':unittest.main()
