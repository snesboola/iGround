"""Terminal output helpers shared by the guided flow and the commands."""

from __future__ import annotations

import sys
import threading
import time
from typing import Callable, List, Optional, Sequence, TextIO


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} TB"


def plural(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 90:
        return f"{minutes} min"
    return f"{minutes // 60}h {minutes % 60:02d}m"


class ProgressPrinter:
    """A live progress bar on a terminal, plain log lines otherwise. Works for files and photos."""

    WIDTH = 24

    def __init__(self, total_items: int, total_bytes: int = 0, stream: TextIO = sys.stderr,
                 verbose: bool = False, indent: str = "    "):
        self.total_items = total_items
        self.total_bytes = total_bytes
        self.stream = stream
        self.verbose = verbose
        self.indent = indent
        self.tty = stream.isatty()
        self.done_items = 0
        self.done_bytes = 0
        self.start = time.monotonic()
        self._last = 0.0
        self._lock = threading.Lock()

    def __call__(self, event: str, item, detail: str) -> None:
        name = getattr(item, "rel_path", None) or getattr(item, "filename", None) or getattr(item, "id", "?")
        size = getattr(item, "size", 0)
        with self._lock:
            if event in ("copied", "skipped", "failed", "exported"):
                self.done_items += 1
                self.done_bytes += size
            if event == "failed":
                self._line(f"{self.indent}couldn't copy {name}: {detail}")
            elif self.verbose and event in ("copied", "download", "exported", "planned"):
                self._line(f"{self.indent}{event:8} {name}")
            self._status()

    def _line(self, text: str) -> None:
        if self.tty:
            self.stream.write("\r\033[K")
        self.stream.write(text + "\n")

    def _status(self) -> None:
        if not self.tty:
            return
        now = time.monotonic()
        if now - self._last < 0.2:
            return
        self._last = now
        if self.total_bytes:
            frac = min(1.0, self.done_bytes / self.total_bytes)
        elif self.total_items:
            frac = min(1.0, self.done_items / self.total_items)
        else:
            frac = 0.0
        filled = int(frac * self.WIDTH)
        bar = "█" * filled + "░" * (self.WIDTH - filled)
        line = f"\r\033[K{self.indent}{bar} {frac * 100:3.0f}%  {self.done_items:,}/{self.total_items:,}"
        elapsed = now - self.start
        if 0.02 < frac < 1 and elapsed > 10:
            line += f"  about {duration(elapsed / frac - elapsed)} left"
        self.stream.write(line)
        self.stream.flush()

    def finish(self) -> None:
        if self.tty:
            self.stream.write("\r\033[K")
            self.stream.flush()


class Console:
    """Questions and answers for the guided flow. `ask` is swappable for tests."""

    def __init__(self, out: TextIO = sys.stdout, ask: Optional[Callable[[str], str]] = None):
        self.out = out
        self._ask = ask or input

    def say(self, text: str = "") -> None:
        self.out.write(text + "\n")
        self.out.flush()

    def heading(self, text: str) -> None:
        self.say()
        self.say(f"\033[1m{text}\033[0m" if self.out.isatty() else text)

    def ask(self, prompt: str) -> str:
        self.out.flush()
        try:
            return self._ask(prompt).strip()
        except EOFError:
            return ""

    def yes(self, question: str, default: bool = True) -> bool:
        hint = "[Y/n]" if default else "[y/N]"
        while True:
            answer = self.ask(f"{question} {hint} ").lower()
            if not answer:
                return default
            if answer in ("y", "yes"):
                return True
            if answer in ("n", "no"):
                return False
            self.say("Please answer y or n.")

    def choose(self, question: str, options: Sequence[str], default: int = 1) -> int:
        """Numbered choice; returns a 0-based index."""
        for i, text in enumerate(options, 1):
            self.say(f"   {i}) {text}")
        while True:
            answer = self.ask(f"{question} [{default}] ")
            if not answer:
                return default - 1
            if answer.isdigit() and 1 <= int(answer) <= len(options):
                return int(answer) - 1
            self.say(f"Please type a number from 1 to {len(options)}.")


def table(rows: List[tuple]) -> List[str]:
    widths = [max(len(str(r[i])) for r in rows) for i in range(len(rows[0]))]
    return ["   " + "  ".join(str(c).ljust(w) for c, w in zip(r, widths)).rstrip() for r in rows]
