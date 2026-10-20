"""Main-path, quoting, candidate selection and readonly transport regressions."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from common import table_parts,table_ref,canon_cell,ROOT
from config import candidates,parser,save
from client import Runner
from ui_bench import command


class ConfigurationTests(unittest.TestCase):
    def test_table_identifiers_are_quoted_and_bound_separately(self):
        self.assertEqual(table_parts('Public.Quotes'),('public','quotes'))
        self.assertEqual(table_parts('"a.b"."odd""name"'),('a.b','odd"name'))
        self.assertEqual(table_ref('"a.b"."odd""name"'),'"a.b"."odd""name"')
        for value in ['quotes','public.quotes;DROP TABLE x','a.b.c','a.','a."bad','a.\x00']:
            with self.assertRaises(ValueError):table_parts(value)

    def test_default_commands_use_this_checkout(self):
        args=parser('test',ui=True).parse_args([])
        with tempfile.TemporaryDirectory() as folder:
            args.output=Path(folder)/'report.json'
            with patch('config.os.access',return_value=True): result=candidates(args,ui=True)
            record=result['original-rust']
            self.assertEqual(record['worker'],str(ROOT/'rust/target/release/dbcli-rust'))
            cmd=command(record,'modern',args)
            self.assertIn(str(ROOT/'ui/rust/target/release/dbcli-ui-rust'),cmd)
            self.assertNotIn('--read-only',cmd) # E stays available; proxy rejects writes.
            self.assertFalse(any('postgresql://' in arg for arg in cmd))

    def test_explicit_manifest_paths_and_duplicate_names(self):
        with tempfile.TemporaryDirectory() as folder:
            folder=Path(folder);worker=folder/'worker';worker.write_text('#!/bin/sh\n');worker.chmod(0o700)
            manifest=folder/'candidates.json'
            manifest.write_text(json.dumps([{'name':'before','protocol':'dispatch','worker':'worker'}]))
            args=parser('test').parse_args(['--manifest',str(manifest),'--candidates','before','--output',str(folder/'report.json')])
            self.assertEqual(candidates(args)['before']['worker'],str(worker))
            manifest.write_text(json.dumps([{'name':'before','protocol':'dispatch','worker':'worker'}]*2))
            with self.assertRaises(ValueError):candidates(args)
            args.repetitions=0
            with self.assertRaises(ValueError):candidates(args)

    def test_reports_private_and_existing_output_requires_explicit_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            output=Path(folder)/'out.json';save(output,{'status':'PASS'})
            self.assertEqual(output.stat().st_mode & 0o777,0o600)
            args=parser('test').parse_args(['--output',str(output)])
            with self.assertRaises(ValueError):candidates(args)

    def test_binary_and_typed_array_results_canonicalize_by_catalog_type(self):
        self.assertEqual(canon_cell({'$bytes':'00ff'},17),canon_cell({'$blob':'AP8='},17))
        self.assertEqual(canon_cell([{'$type':'decimal','value':'1.00'},None],1231),canon_cell([{'$type':'decimal','value':'1.0'},None],1231))
        self.assertNotEqual(canon_cell([None],3807),canon_cell([{'$type':'json','value':None}],3807))
        self.assertNotEqual(canon_cell({'$bytes':'00ff'},3802),canon_cell({'$bytes':'00ff'},17))

    def test_dispatch_transport_reads_partial_frames_and_rejects_mutations(self):
        with tempfile.TemporaryDirectory() as folder:
            script=Path(folder)/'worker'
            script.write_text('#!'+sys.executable+'\nimport sys,time\nfor line in sys.stdin:\n sys.stdout.write(\'{"ok":true,"result":\');sys.stdout.flush();time.sleep(.01);sys.stdout.write(\'7}\\n\');sys.stdout.flush()\n')
            script.chmod(0o700)
            runner=Runner({'protocol':'dispatch','worker':str(script)},dict(os.environ,DBCLI_PERF_TABLE='public.quotes'))
            try:
                self.assertEqual(runner.request({'op':'count','table':'public.quotes'}),{'ok':True,'value':7})
                with self.assertRaises(ValueError):runner.request({'op':'delete'})
            finally:runner.close()

if __name__=='__main__':unittest.main()
