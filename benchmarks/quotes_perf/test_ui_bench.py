"""Offline startup/lifecycle checks; synthetic screen strings, no DB access."""
from types import SimpleNamespace
import unittest
try:
    from ui_bench import InitialFrame,graceful_close,lifecycle_ok
except ModuleNotFoundError:
    InitialFrame=None

@unittest.skipIf(InitialFrame is None,'UI benchmark terminal dependencies unavailable')
class ReadinessTests(unittest.TestCase):
    def settled(self,terminal,kind):
        probe=InitialFrame(terminal,kind)
        self.assertFalse(probe())
        probe.changed-=1
        return probe
    def test_legacy_overflow_uses_metadata_and_reports_invisible_header(self):
        terminal=SimpleNamespace(display='synthetic grid fragment',content=bytearray(b'\x1b[2J\x1b[HData Browser: public.quotes [Rows 1-29 of 771]\r\n'))
        probe=self.settled(terminal,'legacy')
        self.assertTrue(probe());self.assertTrue(probe.diagnostics()['legacy_header_scrolled_out'])
    def test_direct_go_header_has_no_data_browser_prefix(self):
        terminal=SimpleNamespace(display='public.quotes Rows 1-25 of 771',content=bytearray(b'\x1b[2J\x1b[Hpublic.quotes  Rows 1-25 of 771\r\n'))
        self.assertTrue(self.settled(terminal,'legacy')())
    def test_modern_row_loading_word_does_not_block_ready(self):
        terminal=SimpleNamespace(display='public.quotes WRITABLE\nheader\ntabs\nLoading synthetic row value\nmore grid\n1–25 / 771\nReady\nfooter',content=bytearray(b'frame'))
        self.assertTrue(self.settled(terminal,'new')())
        terminal.display=terminal.display.replace('WRITABLE','WRITABLE BUSY')
        self.assertFalse(self.settled(terminal,'new')())
    def test_modern_identical_frame_control_updates_do_not_reset_readiness(self):
        terminal=SimpleNamespace(display='public.quotes WRITABLE\nReady\n1–25 / 771',content=bytearray(b'frame'))
        probe=self.settled(terminal,'new')
        terminal.content.extend(b'\x1b[?25l')
        self.assertTrue(probe())
    def test_cleanup_requires_last_alternate_leave_and_all_flags(self):
        flags={'exit_code':0,'forced_exit':False,'termios_restored':True,'alternate_screen_restored':True}
        terminal=SimpleNamespace(process=SimpleNamespace(poll=lambda:0),content=bytearray(b'\x1b[?1049l\x1b[?1049h'),close=lambda:dict(flags))
        result=graceful_close(terminal,'new');self.assertFalse(lifecycle_ok(result))
        terminal.content.extend(b'\x1b[?1049l')
        self.assertTrue(lifecycle_ok(graceful_close(terminal,'new')))
        for field in ['termios_restored','alternate_screen_restored']:
            bad=dict(flags);bad[field]=False;self.assertFalse(lifecycle_ok(bad))
        self.assertFalse(lifecycle_ok(dict(flags,forced_exit=True)))
        self.assertFalse(lifecycle_ok(dict(flags,exit_code=-9)))

if __name__=='__main__':unittest.main()
