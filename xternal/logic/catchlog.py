"""
XTernal for Mac -- catch log.

SPDX-License-Identifier: AGPL-3.0-only
Derivative work of OpenMacro XTernal (c) 2026 (@anorexc). See LICENSE / NOTICE.

No counterpart upstream: the Windows build counts catches but never records what
was caught.

Fisch announces every landed fish in a HUD banner at

    PlayerGui/hud/safezone/announcements/catch/Main

whose rich text carries the species, its mutations, the weight and the rarity,
e.g.

    You just caught a <b><font color='#fcfeff'>Albino</font>
    <font color='#776cb5'>Halibut</font></b> at
    <font color='#b3b3b3'><b>113.2</b></font>kg! (<b><font ...>1/8,803</font></b>)

The species is worth extracting separately from its mutations, and the markup
makes that reliable: each is wrapped in its own coloured <font> span, with the
species last. That beats matching against a hardcoded species list, which would
rot every time Fisch adds a fish.

The banner is only sampled once per catch. Walking the HUD costs several hundred
memory reads, and doing that on every tick would compete with the reel control
loop for exactly the frames where tracking accuracy matters most.
"""

from __future__ import annotations

import html
import re
import time
from dataclasses import dataclass, field

_TAG = re.compile(r"<[^>]+>")
_FONT_SPAN = re.compile(r"<font[^>]*>(.*?)</font>", re.IGNORECASE | re.DOTALL)
_CAUGHT = re.compile(
    r"caught\s+(?:an?\s+)?(?P<name>.+?)\s+at\s+(?P<weight>[\d.,]+)\s*kg",
    re.IGNORECASE | re.DOTALL,
)
_RARITY = re.compile(r"\(\s*(?P<rarity>1\s*/\s*[\d,]+)\s*\)")


def _strip(text: str) -> str:
    return re.sub(r"\s+", " ", _TAG.sub("", text)).strip()


@dataclass
class Catch:
    species: str = ""
    mutations: list[str] = field(default_factory=list)
    weight: float = 0.0
    rarity: str = ""
    at: float = field(default_factory=time.time)

    @property
    def display(self) -> str:
        return " ".join(self.mutations + [self.species]) if self.species else "?"

    @property
    def rarity_denominator(self) -> int:
        """8803 for '1/8,803'; 0 when absent. Lets rarity sort numerically."""
        if not self.rarity:
            return 0
        digits = re.sub(r"[^\d]", "", self.rarity.split("/")[-1])
        return int(digits) if digits else 0


def parse_announcement(raw: str) -> Catch | None:
    """Parse the HUD catch banner. Returns None if it isn't a catch line.

    The banner arrives with its markup partly HTML-escaped and partly not
    (single-quoted spans come through as &lt;font ...&gt; while double-quoted
    ones stay literal), so entities are unescaped before anything else.
    """
    if not raw:
        return None
    text = html.unescape(raw)
    flat = _strip(text)
    match = _CAUGHT.search(flat)
    if not match:
        return None

    catch = Catch()
    try:
        catch.weight = float(match.group("weight").replace(",", ""))
    except ValueError:
        catch.weight = 0.0

    rarity = _RARITY.search(flat)
    if rarity:
        catch.rarity = re.sub(r"\s+", "", rarity.group("rarity"))

    # Each mutation and the species sit in their own <font> span; species last.
    spans = [_strip(s) for s in _FONT_SPAN.findall(text)]
    spans = [s for s in spans if s and "/" not in s and not s.replace(".", "").isdigit()]
    if spans:
        catch.species = spans[-1]
        catch.mutations = spans[:-1]
    else:
        # No usable markup: keep the whole phrase rather than inventing a split.
        catch.species = match.group("name").strip()
    return catch


class CatchLog:
    """Rolling record of landed fish, with per-species tallies."""

    MAX_ENTRIES = 500

    #: How long after a fight ends to keep watching for the banner to update.
    #: The announcement lands a beat after the reel GUI disappears.
    POLL_WINDOW_S = 2.0

    def __init__(self):
        self.entries: list[Catch] = []
        self._baseline = ""
        self._deadline = 0.0

    def arm(self, game) -> None:
        """Record the banner's current text at the start of a fight.

        Comparing against a per-fight baseline rather than de-duplicating on the
        last logged string matters: land the same species twice at the same
        weight and the banner text is byte-identical, and a dedupe would silently
        drop the second fish.
        """
        self._baseline = self._read_banner(game)
        self._deadline = 0.0

    def begin_poll(self) -> None:
        """Called when a fight ends; opens the window in which to expect a banner."""
        self._deadline = time.time() + self.POLL_WINDOW_S

    @property
    def polling(self) -> bool:
        return self._deadline > 0.0 and time.time() < self._deadline

    def poll(self, game) -> Catch | None:
        """Check for a new announcement. Returns the Catch once one appears.

        A lost fish produces no banner at all -- verified live, where fights that
        ended without a catch left the text untouched while bait still ticked
        down -- so the window simply expires and nothing is recorded.
        """
        if not self.polling:
            return None
        raw = self._read_banner(game)
        if not raw or raw == self._baseline:
            return None
        self._deadline = 0.0
        self._baseline = raw
        catch = parse_announcement(raw)
        if catch is None:
            return None
        self.entries.append(catch)
        del self.entries[: -self.MAX_ENTRIES]
        return catch

    @staticmethod
    def _read_banner(game) -> str:
        r = game.r
        pg = r.find_player_gui()
        if not pg:
            return ""
        hud = r.find_child_by_name(pg, "hud")
        safezone = r.find_child_by_name(hud, "safezone") if hud else 0
        announcements = r.find_child_by_name(safezone, "announcements") if safezone else 0
        catch = r.find_child_by_name(announcements, "catch") if announcements else 0
        main = r.find_child_by_name(catch, "Main") if catch else 0
        return r.read_gui_text(main) if main else ""

    # -- reporting --------------------------------------------------------

    def species_counts(self) -> list[tuple[str, int]]:
        counts: dict[str, int] = {}
        for entry in self.entries:
            counts[entry.species] = counts.get(entry.species, 0) + 1
        return sorted(counts.items(), key=lambda kv: -kv[1])

    def mutation_counts(self) -> list[tuple[str, int]]:
        counts: dict[str, int] = {}
        for entry in self.entries:
            for mutation in entry.mutations:
                counts[mutation] = counts.get(mutation, 0) + 1
        return sorted(counts.items(), key=lambda kv: -kv[1])

    def rarest(self, limit: int = 5) -> list[Catch]:
        ranked = [e for e in self.entries if e.rarity_denominator]
        ranked.sort(key=lambda e: -e.rarity_denominator)
        return ranked[:limit]

    def recent(self, limit: int = 20) -> list[Catch]:
        return list(reversed(self.entries[-limit:]))
