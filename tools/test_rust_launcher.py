"""Offline selected-Rust launcher checks; no Docker or database access."""
import contextlib,io,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import rust
from postgres_target import TargetError

class RustLauncherTests(unittest.TestCase):
 def test_selected_local_backend(self):
  command=rust.command(postgres=True,table='public.quotes')
  self.assertEqual(command[command.index('--backend')+1],'original-rust')
  self.assertEqual(command[command.index('--worker')+1],str(rust.ROOT/'rust/target/release/dbcli-rust'))
  self.assertEqual(Path(command[0]).parents[4],rust.ROOT)
  self.assertEqual(Path(command[command.index('--worker')+1]).parents[3],rust.ROOT)
 def test_writable_and_readonly_native_modes(self):
  for legacy in [False,True]:
   for readonly in [False,True]:
    with self.subTest(legacy=legacy,readonly=readonly):
     command=rust.command(postgres=True,legacy=legacy,read_only=readonly)
     self.assertEqual('--read-only'in command,readonly)
     self.assertEqual('--postgres'in command,legacy)
 def test_sqlite_path_and_spaces(self):
  with tempfile.TemporaryDirectory()as folder:
   path=Path(folder)/'space and quote.sqlite';path.touch()
   for legacy in [False,True]:self.assertIn(str(path),rust.command(database=str(path),legacy=legacy))
 def test_missing_or_conflicting_targets_rejected(self):
  for kwargs in [{},{'database':'/does/not/exist.sqlite'},{'database':'x','postgres':True}]:
   with self.assertRaises(ValueError):rust.command(**kwargs)
 def test_print_and_list_do_not_discover_docker(self):
  for args in [['--postgres','--print-command'],['--list']]:
   with patch.object(rust,'docker_target',side_effect=AssertionError('Docker accessed')),contextlib.redirect_stdout(io.StringIO())as output:
    self.assertEqual(rust.main(args),0)
    self.assertNotIn('postgresql://',output.getvalue())
 def test_url_only_in_environment_and_explicit_mode(self):
  for readonly in [False,True]:
   with patch.object(Path,'is_file',return_value=True),patch.dict(os.environ,{'DBCLI_POSTGRES_URL':'postgresql://u:secret-sentinel@localhost/db'}),patch.object(rust.os,'execve',side_effect=OSError)as execute,contextlib.redirect_stderr(io.StringIO()):
    self.assertEqual(rust.main(['--postgres','--from-env']+(['--read-only']if readonly else[])),1)
    executable,command,env=execute.call_args.args
    self.assertFalse(any('secret-sentinel'in arg for arg in command))
    self.assertIn('secret-sentinel',env['DBCLI_POSTGRES_URL'])
    self.assertEqual('--read-only'in command,readonly)
 def test_unavailable_credentials_redacted(self):
  with patch.object(Path,'is_file',return_value=True),patch.dict(os.environ,{'DBCLI_POSTGRES_URL':'bad-secret-sentinel'}),contextlib.redirect_stderr(io.StringIO())as output:
   self.assertEqual(rust.main(['--postgres','--from-env']),1)
   self.assertNotIn('secret-sentinel',output.getvalue())
 def test_container_requires_postgres(self):
  with contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):rust.main(['--container','example'])
if __name__=='__main__':unittest.main()
