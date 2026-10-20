"""Synthetic /proc regressions for the all-thread memory collector."""
import tempfile
from pathlib import Path
from unittest import TestCase,main
from unittest.mock import patch
import memory_sidecar

class ChildrenTests(TestCase):
    def test_children_from_every_os_thread(self):
        with tempfile.TemporaryDirectory()as folder:
            root=Path(folder)
            for tid,text in [('10','20 21'),('11','22 20')]:
                p=root/'proc/10/task'/tid/'children';p.parent.mkdir(parents=True);p.write_text(text)
            def fake_path(*parts):return root.joinpath(*(str(p).lstrip('/')for p in parts))
            with patch.object(memory_sidecar,'Path',side_effect=fake_path):
                self.assertEqual(memory_sidecar.children(10),{20,21,22})
    def test_tree_deduplicates_shared_descendant(self):
        graph={10:{20,21},20:{30},21:{30},30:set()}
        with patch.object(memory_sidecar,'children',side_effect=lambda pid:graph[pid]):
            self.assertEqual(memory_sidecar.tree(10),{10,20,21,30})
if __name__=='__main__':main()
