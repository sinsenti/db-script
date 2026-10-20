"""Validation failures must prevent benchmark timings from being collected."""
import argparse
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import backend_bench


class ValidationGateTests(unittest.TestCase):
    def test_any_candidate_mismatch_prevents_every_timing_round(self):
        class FakeRunner:
            calls=[]
            def __init__(self,candidate,env): self.name=candidate['name'];self.proc=argparse.Namespace(pid=123)
            def request(self,op):
                self.calls.append((self.name,op['op']))
                return {'ok':True,'value':1 if self.name=='good' else 2}
            def close(self): pass
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'report.json'
            selected={name:{'name':name} for name in ['good','bad']}
            meta={'columns':[],'primary_key':['id']}
            with patch.object(backend_bench,'target',lambda args:('unused',{})), patch.multiple(backend_bench,
                candidates=lambda args:selected,
                metadata=lambda url,table:meta,workloads=lambda *args:{'count':{'op':'count'}},
                oracle=lambda *args:1,provenance=lambda *args:[],Runner=FakeRunner,
                target_environment=lambda *args,**kwargs:{}):
                status=backend_bench.main(['--output',str(path),'--repetitions','3'])
            self.assertEqual(status,1)
            self.assertEqual(len(FakeRunner.calls),2)
            report=__import__('json').loads(path.read_text())
            self.assertFalse(report['validation_passed'])
            self.assertEqual(report['status'],'FAIL')
            self.assertEqual(report['candidates']['good']['workloads']['count']['samples_ms'],[])

if __name__=='__main__':unittest.main()
