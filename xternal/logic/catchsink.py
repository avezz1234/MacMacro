"""
XTernal for Mac -- catch feed for external tools.

SPDX-License-Identifier: AGPL-3.0-only
Derivative work of OpenMacro XTernal (c) 2026 (@anorexc). See LICENSE / NOTICE.

No counterpart upstream. CatchLog keeps landed fish in memory for the UI's
session view, which means nothing outside the process can see them and a
restart discards the lot. This appends one JSON object per line to a file
instead, so a separate process -- the Discord bot in the companion project --
can follow it the way `tail -F` does.

A file rather than a socket or an HTTP callback, because the two processes have
no reason to be started together: the macro should fish whether or not the bot
is running, and the bot should be able to start late and still pick up from
where it left off. A file is the only one of those three that survives either
side being down.

Writing is best-effort and never raises. A full disk or a read-only home is a
reason for the feed to stop, not for fishing to stop -- the same rule the
webhook reporter follows.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from ..settings import catch_feed_path

#: Rotate past this size. A catch line is roughly 150 bytes, so this holds well
#: over ten thousand fish -- far more history than anything downstream reads,
#: while still bounding a feed nobody ever prunes by hand.
MAX_BYTES = 4 * 1024 * 1024


class CatchSink:
    """Appends landed fish to a JSONL feed."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else catch_feed_path()
        self.enabled = True
        self._warned = False

    def write(self, catch) -> None:
        if not self.enabled:
            return
        try:
            self._append(self._record(catch))
        except OSError as exc:
            # Complain once, then stay quiet: this runs on every catch, and a
            # persistent failure would otherwise bury the console in identical
            # lines while the macro is still fishing perfectly well.
            if not self._warned:
                print(f"[catchsink] disabled after write error: {exc}")
                self._warned = True
            self.enabled = False

    @staticmethod
    def _record(catch) -> dict:
        return {
            "species": catch.species,
            "mutations": list(catch.mutations),
            "weight": catch.weight,
            "rarity": catch.rarity,
            "rarity_denominator": catch.rarity_denominator,
            "at": catch.at or time.time(),
        }

    def _append(self, record: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._rotate_if_large()

        # One write of one line, to a descriptor opened O_APPEND. Under the size
        # of a pipe buffer that lands atomically, so a reader polling the file
        # never sees half a record -- which is what lets the follower parse a
        # line the moment it sees the newline.
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()

    def _rotate_if_large(self) -> None:
        try:
            if self.path.stat().st_size < MAX_BYTES:
                return
        except FileNotFoundError:
            return
        # Keep exactly one generation. Followers detect the shrink by re-stating
        # the path and simply resume from the new file's start.
        os.replace(self.path, self.path.with_suffix(".jsonl.1"))
