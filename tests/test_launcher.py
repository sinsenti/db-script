"""Offline startup and Rust handoff tests; no Docker access or application data."""
import contextlib
import io
import json
import os
from pathlib import Path
import signal
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import unquote, urlsplit

import db
from dbcli import launcher
from dbcli.postgres_target import database_url


class LauncherTests(unittest.TestCase):
    def args(self, target=None, read_only=False):
        return SimpleNamespace(target=target, read_only=read_only, python=False, cli=False)

    def test_db_entry_defaults_to_rust_and_explicit_python_or_cli_preserve_reference(self):
        with patch.object(launcher, 'run', return_value=0) as native, patch.object(db, 'python_main') as reference:
            self.assertEqual(db.main([]), 0)
            self.assertFalse(native.call_args.args[0].python)
            reference.assert_not_called()
        for flag in ['--python', '--cli']:
            with patch.object(launcher, 'run') as native, patch.object(db, 'python_main', return_value=0) as reference:
                self.assertEqual(db.main([flag]), 0)
                reference.assert_called_once()
                native.assert_not_called()

    def test_discovery_labels_container_backends_and_offers_manual_open(self):
        containers = [{'name':'pg','engine':'postgres'}, {'name':'mysql','engine':'mysql'}]
        with patch.object(launcher, 'find_sqlite_files', return_value=[Path('/tmp/example.db')]), patch.object(launcher, 'find_docker_databases', return_value=containers), patch.dict(os.environ, {}, clear=True):
            items = launcher.discover_items()
        self.assertEqual([item['type'] for item in items], ['sqlite', 'docker', 'docker', 'open_sqlite', 'refresh'])
        self.assertIn('Rust', items[1]['label'])
        self.assertIn('Python', items[2]['label'])

    def test_empty_discovery_still_shows_requested_picker_title(self):
        with patch.object(launcher, 'discover_items', return_value=[{'type':'open_sqlite','label':'Open a SQLite file…'}]), patch.object(launcher, 'select_menu_item', return_value=(None,0)) as menu, patch.object(launcher, 'enter_alt_screen'), patch.object(launcher, 'exit_cleanup'), patch.object(launcher, 'require_rust') as require:
            self.assertEqual(launcher.run(self.args(), Mock()), 0)
            self.assertEqual(menu.call_args.args[0], 'Interactive Database Browser & Explorer')
            require.assert_not_called()

    def test_environment_label_does_not_disclose_password_or_options(self):
        url = 'postgresql://user:secret-sentinel@localhost:5432/app?password=secret-sentinel'
        with patch.object(launcher, 'find_sqlite_files', return_value=[]), patch.object(launcher, 'find_docker_databases', return_value=[]), patch.dict(os.environ, {'DBCLI_POSTGRES_URL':url}, clear=True):
            item = launcher.discover_items()[0]
        self.assertNotIn('secret-sentinel', item['label'])
        self.assertNotIn('password=', item['label'])
        self.assertIn('localhost:5432/app', item['label'])
        self.assertEqual(item['target'], url)

    def test_sqlite_handoff_uses_local_rust_and_readonly_mode(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'space in name.sqlite'
            with sqlite3.connect(path) as con:
                con.execute('CREATE TABLE rows(id INTEGER PRIMARY KEY)')
            item = launcher.explicit_target(str(path))
            with patch.object(launcher, 'require_rust'), patch.object(launcher, 'run_native', return_value=0) as run:
                self.assertEqual(launcher.launch_selected(item, self.args(read_only=True), Mock()), 0)
            argv, env = run.call_args.args
            self.assertEqual(argv[0], str(launcher.FRONTEND))
            self.assertIn(str(path), argv)
            self.assertIn('--read-only', argv)
            self.assertEqual(argv[argv.index('--backend')+1], 'original-rust')

    def test_sqlite_validation_rejects_non_databases_and_handles_file_uri(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'space file.sqlite'
            path.touch()  # An empty existing SQLite file is valid.
            self.assertEqual(launcher.explicit_target(path.as_uri().replace('file:', 'sqlite:'))['target'], path)
            path.write_text('ordinary text')
            with self.assertRaises(launcher.LaunchError):
                launcher.sqlite_path(path)
        with self.assertRaises(launcher.LaunchError):
            launcher.sqlite_path('/does/not/exist.db')

    def test_relative_sqlite_url_shorthand_remains_supported(self):
        with patch.object(launcher,'sqlite_path',return_value=Path('/tmp/relative.db')) as validate:
            launcher.explicit_target('sqlite://relative.db')
            validate.assert_called_once_with('relative.db')

    def test_postgres_database_discovery_uses_native_readonly_and_env_credentials(self):
        url = 'postgresql://user:secret-sentinel@localhost/app?sslmode=disable'
        result = SimpleNamespace(returncode=0, stdout=json.dumps({'headers':['datname'],'rows':[['app'],['other']]}))
        with patch.object(launcher.subprocess, 'run', return_value=result) as run:
            self.assertEqual(launcher.postgres_databases(url), ['app','other'])
        argv = run.call_args.args[0]
        self.assertIn('--read-only', argv)
        self.assertNotIn('secret-sentinel', ' '.join(argv))
        self.assertIn('secret-sentinel', run.call_args.kwargs['env']['DBCLI_POSTGRES_URL'])
        self.assertEqual(run.call_args.kwargs['timeout'], 10)
        with patch.object(launcher.subprocess, 'run', return_value=SimpleNamespace(returncode=1,stdout='private detail')):
            with self.assertRaisesRegex(launcher.LaunchError, 'Could not list'):
                launcher.postgres_databases(url)

    def test_container_selection_chooses_database_before_rust_and_preserves_mode(self):
        item = {'type':'docker','target':{'name':'pg','engine':'postgres'}}
        url = 'postgresql://u:secret-sentinel@localhost/default?sslmode=disable'
        with patch.object(launcher, 'require_rust'), patch.object(launcher, 'docker_target', return_value=(url,{})) as resolve, patch.object(launcher, 'postgres_databases', return_value=['default','db / name']), patch.object(launcher, 'select_menu_item', return_value=('db / name',1)), patch.object(launcher, 'enter_alt_screen'), patch.object(launcher, 'run_native', return_value=0) as run:
            self.assertEqual(launcher.launch_selected(item,self.args(),Mock()),0)
        resolve.assert_called_once_with('pg',read_only=True)
        argv, env = run.call_args.args
        self.assertNotIn('--read-only', argv)
        self.assertNotIn('secret-sentinel',' '.join(argv))
        self.assertEqual(unquote(urlsplit(env['DBCLI_POSTGRES_URL']).path), '/db / name')
        self.assertIn('default_transaction_read_only=off', unquote(env['DBCLI_POSTGRES_URL']))

    def test_cancel_database_submenu_never_launches_browser(self):
        item = {'type':'docker','target':{'name':'pg','engine':'postgres'}}
        with patch.object(launcher,'require_rust'), patch.object(launcher,'docker_target',return_value=('postgresql://u@localhost/db',{})), patch.object(launcher,'postgres_databases',return_value=['db']), patch.object(launcher,'select_menu_item',return_value=(None,0)), patch.object(launcher,'enter_alt_screen'), patch.object(launcher,'run_native') as run:
            self.assertEqual(launcher.launch_selected(item,self.args(),Mock()),0)
            run.assert_not_called()

    def test_return_from_browser_reopens_picker(self):
        item = {'type':'sqlite','target':'fixture'}
        with patch.object(launcher,'discover_items',return_value=[item]), patch.object(launcher,'select_menu_item',side_effect=[(item,0),(None,0)]) as menu, patch.object(launcher,'launch_selected',return_value=0) as launch, patch.object(launcher,'enter_alt_screen'), patch.object(launcher,'exit_cleanup'):
            self.assertEqual(launcher.run(self.args(),Mock()),0)
            self.assertEqual(menu.call_count,2)
            launch.assert_called_once()

    def test_manual_path_and_refresh_actions(self):
        manual = {'type':'open_sqlite'}; refresh = {'type':'refresh'}; sqlite = {'type':'sqlite','target':'fixture'}
        with patch.object(launcher,'discover_items',return_value=[manual,refresh]) as discover, patch.object(launcher,'select_menu_item',side_effect=[(refresh,1),(manual,0),(None,0)]), patch.object(launcher,'open_sqlite_item',return_value=sqlite), patch.object(launcher,'launch_selected',return_value=0) as launch, patch.object(launcher,'enter_alt_screen'), patch.object(launcher,'exit_cleanup'):
            self.assertEqual(launcher.run(self.args(),Mock()),0)
            self.assertEqual(discover.call_count,3)
            self.assertEqual(launch.call_args.args[0],sqlite)

    def test_errors_remain_in_picker_without_silently_falling_back(self):
        item = {'type':'sqlite','target':'fixture'}
        reference = Mock()
        with patch.object(launcher,'discover_items',return_value=[item]), patch.object(launcher,'select_menu_item',side_effect=[(item,0),(None,0)]) as menu, patch.object(launcher,'launch_selected',side_effect=launcher.LaunchError('Build Rust once')), patch.object(launcher,'enter_alt_screen'), patch.object(launcher,'exit_cleanup'):
            self.assertEqual(launcher.run(self.args(),reference),0)
            self.assertIn('Build Rust once',menu.call_args.kwargs['subtext'])
            reference.assert_not_called()

    def test_mysql_uses_reference_at_exact_target_and_then_returns(self):
        for item in [{'type':'docker','target':{'name':'mysql','engine':'mysql'}},{'type':'direct_mysql','target':'mysql://localhost/app'}]:
            reference = Mock()
            args = self.args()
            with patch.object(launcher,'require_rust') as require, patch.object(launcher,'exit_cleanup'):
                self.assertEqual(launcher.launch_selected(item,args,reference),0)
                require.assert_not_called()
            reference.assert_called_once_with(args,initial_item=item)

    def test_explicit_target_skips_discovery_and_failure_is_redacted(self):
        with patch.object(launcher,'explicit_target',return_value={'type':'fixture'}) as target, patch.object(launcher,'launch_selected',return_value=0), patch.object(launcher,'discover_items') as discover, patch.object(launcher,'exit_cleanup'):
            self.assertEqual(launcher.run(self.args(target='fixture'),Mock()),0)
            target.assert_called_once_with('fixture')
            discover.assert_not_called()
        with patch.object(launcher,'explicit_target',side_effect=launcher.LaunchError('Missing database')), patch.object(launcher,'exit_cleanup'), contextlib.redirect_stderr(io.StringIO()) as output:
            self.assertEqual(launcher.run(self.args(target='missing'),Mock()),1)
            self.assertIn('Missing database',output.getvalue())

    def test_reference_mysql_handoff_does_not_rediscover_or_show_second_picker(self):
        item = {'type':'docker','target':{'name':'mysql','engine':'mysql'}}
        with patch.object(db,'find_sqlite_files',side_effect=AssertionError('rediscovery')), patch.object(db,'find_docker_databases',side_effect=AssertionError('rediscovery')), patch.object(db,'enter_alt_screen'), patch.object(db,'prompt_select_database',return_value=(None,0)) as databases, patch.object(db,'select_menu_item') as menu:
            db.python_main(self.args(),initial_item=item)
            databases.assert_called_once_with('mysql','mysql',initial_idx=0)
            menu.assert_not_called()

    def test_owned_child_terminated_and_reaped_on_interrupt(self):
        child = Mock()
        child.wait.side_effect = [KeyboardInterrupt(), 0]
        child.poll.return_value = None
        with patch.object(launcher.subprocess,'Popen',return_value=child), patch.object(launcher,'exit_cleanup'), patch.object(launcher,'restored_terminal',return_value=contextlib.nullcontext()), self.assertRaises(KeyboardInterrupt):
            launcher.run_native(['fixture'],{})
        child.terminate.assert_called_once()
        self.assertEqual(child.wait.call_count,2)

    def test_database_name_override_encodes_name_without_touching_credentials(self):
        url = database_url('postgresql://u:secret-sentinel@localhost/original?sslmode=disable','db/name',read_only=False)
        self.assertEqual(urlsplit(url).path,'/db%2Fname')
        self.assertEqual(urlsplit(url).password,'secret-sentinel')

if __name__ == '__main__': unittest.main()
