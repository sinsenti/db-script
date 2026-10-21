"""Opt-in real picker → Rust → picker checks on disposable fixtures only.

DBCLI_RUN_PICKER_PTY=1 enables SQLite checks (requires pyte).
DBCLI_RUN_PICKER_POSTGRES_PTY=1 additionally uses the root manager's already
started, marked disposable PostgreSQL fixture (requires psycopg too).
"""
import hashlib
import os
from pathlib import Path
import signal
import sqlite3
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get('DBCLI_RUN_PICKER_PTY') == '1', 'opt-in real picker PTY checks')
class PickerPtyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT/'benchmarks/quotes_perf'))
        from terminal import Terminal
        cls.Terminal = Terminal

    def open_picker(self, folder, *args):
        env = dict(os.environ, TERM='xterm-256color')
        env.pop('DBCLI_POSTGRES_URL', None)
        executable = os.environ.get('DBCLI_PICKER_PYTHON', sys.executable)
        terminal = self.Terminal([executable,str(ROOT/'db.py'),*args],folder,env)
        self.addCleanup(terminal.close)
        self.assertTrue(terminal.wait(lambda:'Interactive Database Browser & Explorer' in terminal.display,12), 'Picker did not appear')
        return terminal

    def ready(self, terminal, mode):
        self.assertTrue(terminal.wait(lambda:'DBCLI' in terminal.display and 'original-rust' in terminal.display and mode in terminal.display and 'READY' in terminal.display,12), 'Rust browser did not appear')

    def back_and_quit(self, terminal):
        terminal.send('q')
        self.assertTrue(terminal.wait(lambda:'Interactive Database Browser & Explorer' in terminal.display,12), 'Browser did not return to picker')
        terminal.send('q')
        self.assertTrue(terminal.wait(lambda:terminal.process.poll() is not None,5))
        lifecycle = terminal.close()
        self.assertEqual(lifecycle['exit_code'],0)
        self.assertFalse(lifecycle['forced_exit'])
        self.assertTrue(lifecycle['termios_restored'])
        self.assertTrue(lifecycle['alternate_screen_restored'])

    def sqlite(self, read_only=False, manual=False, terminate=False):
        with tempfile.TemporaryDirectory(prefix='dbcli-picker-') as folder:
            folder = Path(folder)
            nested = folder/'nested' if manual else folder
            nested.mkdir(exist_ok=True)
            database = nested/'picker.sqlite'
            with sqlite3.connect(database) as con:
                con.execute('CREATE TABLE items(id INTEGER PRIMARY KEY,label TEXT)')
                con.execute("INSERT INTO items VALUES(1,'synthetic picker row')")
            before = hashlib.sha256(database.read_bytes()).digest()
            terminal = self.open_picker(folder,*(['--read-only'] if read_only else []))
            if manual:
                terminal.send('G');terminal.send('k');terminal.send('\r')
                self.assertTrue(terminal.wait(lambda:'SQLite file:' in terminal.display,5))
                terminal.send(str(database)+'\r')
            else:
                terminal.send('1')  # Shallow SQLite entries precede containers.
            self.ready(terminal,'READ ONLY' if read_only else 'WRITABLE')
            if terminate:
                terminal.process.send_signal(signal.SIGTERM)
                self.assertTrue(terminal.wait(lambda:terminal.process.poll() is not None,8))
                lifecycle = terminal.close()
                self.assertEqual(lifecycle['exit_code'],128+signal.SIGTERM)
                self.assertTrue(lifecycle['termios_restored'])
                self.assertTrue(lifecycle['alternate_screen_restored'])
                self.assertFalse(lifecycle['forced_exit'])
            else:
                self.back_and_quit(terminal)
            self.assertEqual(before,hashlib.sha256(database.read_bytes()).digest())

    def test_sqlite_writable(self): self.sqlite()
    def test_sqlite_readonly(self): self.sqlite(read_only=True)
    def test_sqlite_manual_path(self): self.sqlite(read_only=True,manual=True)
    def test_sigterm_reaps_browser_and_restores_terminal(self): self.sqlite(read_only=True,terminate=True)

    def postgres(self, read_only):
        from postgres_write_fixture import verified_descriptor
        from urllib.parse import urlsplit
        import psycopg
        from psycopg import sql
        import uuid
        data = verified_descriptor()
        parsed = urlsplit(data['admin_url'])
        self.assertEqual(parsed.hostname,'127.0.0.1')
        self.assertEqual(parsed.port,data['port'])
        self.assertNotEqual(parsed.port,5432)
        self.assertEqual(parsed.path,'/dbcli_postgres_write_fixture')
        schema = 'picker_pty_'+uuid.uuid4().hex
        with psycopg.connect(data['admin_url'],autocommit=True) as con:
            self.assertEqual(con.execute('SELECT current_database()').fetchone()[0],'dbcli_postgres_write_fixture')
            con.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
            try:
                con.execute(sql.SQL("CREATE TABLE {}.items(id INTEGER PRIMARY KEY,label TEXT);INSERT INTO {}.items VALUES(1,'synthetic picker row')").format(sql.Identifier(schema),sql.Identifier(schema)))
                with tempfile.TemporaryDirectory(prefix='dbcli-picker-pg-') as folder:
                    terminal = self.open_picker(Path(folder),*(['--read-only'] if read_only else []))
                    terminal.send('/')
                    self.assertTrue(terminal.wait(lambda:'Search:' in terminal.display,5))
                    terminal.send(data['container']+'\r');terminal.send('1')
                    self.assertTrue(terminal.wait(lambda:"Databases in '"+data['container']+"'" in terminal.display,12), 'Container database menu did not appear')
                    # The fixture DB sorts before postgres; use its displayed name.
                    terminal.send('1')
                    self.ready(terminal,'READ ONLY' if read_only else 'WRITABLE')
                    self.back_and_quit(terminal)
                count = con.execute(sql.SQL('SELECT count(*) FROM {}.items').format(sql.Identifier(schema))).fetchone()[0]
                self.assertEqual(count,1)
            finally:
                con.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))

    @unittest.skipUnless(os.environ.get('DBCLI_RUN_PICKER_POSTGRES_PTY') == '1', 'opt-in disposable PostgreSQL picker check')
    def test_postgres_writable(self): self.postgres(False)
    @unittest.skipUnless(os.environ.get('DBCLI_RUN_PICKER_POSTGRES_PTY') == '1', 'opt-in disposable PostgreSQL picker check')
    def test_postgres_readonly(self): self.postgres(True)

if __name__ == '__main__': unittest.main()
