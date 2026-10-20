"""Real PTY with a terminal emulator: assertions inspect the displayed screen."""
from __future__ import annotations

import codecs
import fcntl
import os
from pathlib import Path
import pty
import select
import signal
import struct
import subprocess
import termios
import time

import pyte


class Terminal:
    def __init__(self, command: list[str], folder: Path, environment: dict[str, str],
                 rows: int = 36, columns: int = 140):
        self.master, self.slave = pty.openpty()
        self.rows, self.columns = rows, columns
        self.screen = pyte.Screen(columns, rows)
        self.stream = pyte.Stream(self.screen)
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.content = bytearray()
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
        self.before = termios.tcgetattr(self.slave)

        def setup():
            os.setsid()
            fcntl.ioctl(self.slave, termios.TIOCSCTTY, 0)

        try:
            self.process = subprocess.Popen(command, stdin=self.slave, stdout=self.slave, stderr=self.slave,
                                            cwd=folder, env=environment, preexec_fn=setup)
        except Exception:
            os.close(self.master)
            os.close(self.slave)
            raise
        self.closed = False

    @property
    def display(self) -> str:
        return "\n".join(self.screen.display)

    def drain(self, seconds: float = .1):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            ready, _, _ = select.select([self.master], [], [], max(0, min(.05, deadline - time.monotonic())))
            if not ready:
                continue
            try:
                chunk = os.read(self.master, 131072)
            except OSError:
                break
            if not chunk:
                break
            self.content.extend(chunk)
            self.stream.feed(self.decoder.decode(chunk))
            # Cursor-position probes and terminal attributes used by the UI frameworks.
            if b"\x1b[6n" in chunk:
                os.write(self.master, b"\x1b[1;1R")
            if b"\x1b[c" in chunk or b"\x1b[>c" in chunk:
                os.write(self.master, b"\x1b[?1;2c")
            if b"\x1b]11;?" in chunk:
                os.write(self.master, b"\x1b]11;rgb:1010/1414/1c1c\x1b\\")

    def wait(self, predicate, seconds: float = 12) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if predicate():
                return True
            if self.process.poll() is not None:
                self.drain(.05)
                return bool(predicate())
            self.drain(.1)
        return bool(predicate())

    def send(self, text: str | bytes, seconds: float = .1):
        if self.process.poll() is None:
            os.write(self.master, text.encode() if isinstance(text, str) else text)
        self.drain(seconds)

    def resize(self, rows: int, columns: int):
        self.rows, self.columns = rows, columns
        self.screen.resize(lines=rows, columns=columns)
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
        self.process.send_signal(signal.SIGWINCH)
        self.drain(.2)

    def close(self) -> dict:
        if self.closed:
            return self.lifecycle
        for key in [b"\x1b", b"q", b"q", b"\x11", b"\x03"]:
            if self.process.poll() is not None:
                break
            self.send(key, .2)
        forced = self.process.poll() is None
        if forced:
            self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.drain(.1)
        self.lifecycle = {"exit_code": self.process.returncode, "forced_exit": forced,
                          "termios_restored": termios.tcgetattr(self.slave) == self.before,
                          "alternate_screen_restored": b"\x1b[?1049l" in self.content}
        os.close(self.master)
        os.close(self.slave)
        self.closed = True
        return self.lifecycle
