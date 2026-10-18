"""
XTernal for Mac -- weather detection.

SPDX-License-Identifier: AGPL-3.0-only
Derivative work of OpenMacro XTernal (c) 2026 (@anorexc). See LICENSE / NOTICE.

Fisch publishes its weather in ReplicatedStorage.world as several independent
layers, all readable straight from memory:

    world.weather                 base: Clear / Foggy / Windy / Rain / Stormy
                                  (also Eclipse, Starfall, ... on rare rolls)
          .meteorological         Aurora Borealis, Rainbow, Starfall, ...
          .squall                 Raging Squall, Tropical Squall
          .astral                 Abyssal Alignment, Celestial Congregation,
                                  Evershifting Eclipse, Lunar Eclipse,
                                  Meteoric Outburst
          .sovereign              Sovereign Surge / Storm / Reckoning
    world.cycle                   Day / Night
    world.season                  Spring / Summer / Autumn / Winter
    world.event                   the running server event, if any
    world.meteor_active           bool
    world.blizzard_active         bool

Each layer reads "None" when inactive. They stack -- a night can be Clear with
Aurora Borealis and a Tropical Squall at once -- so the detector reports the
set of active effects rather than one name.
"""

from __future__ import annotations

from dataclasses import dataclass, field

LAYERS = ("meteorological", "squall", "astral", "sovereign")

#: substring of a weather name -> effect tag the scene knows how to draw.
_TAGS = [
    ("aurora", "aurora"),
    ("rainbow", "rainbow"),
    ("starfall", "starfall"),
    ("lunar eclipse", "blood_moon"),
    ("evershifting", "blood_moon"),
    ("eclipse", "eclipse"),
    ("raging squall", "raging_squall"),
    ("tropical squall", "tropical_squall"),
    ("frost moon", "frost_moon"),
    ("tropical sun", "tropical_sun"),
    ("sovereign", "sovereign"),
    ("abyssal", "abyssal"),
    ("celestial", "celestial"),
    ("meteor", "meteor"),
    ("blizzard", "blizzard"),
    ("storm", "storm"),
    ("thunder", "storm"),
    ("rain", "rain"),
    ("fog", "fog"),
    ("mist", "fog"),
    ("wind", "wind"),
    ("snow", "snow"),
]


def _clean(value: str) -> str:
    value = (value or "").strip()
    return "" if value.lower() in ("none", "nil", "") else value


@dataclass
class WeatherState:
    cycle: str = ""
    base: str = ""
    layers: dict = field(default_factory=dict)
    season: str = ""
    event: str = ""
    meteor: bool = False
    blizzard: bool = False

    @property
    def names(self) -> list[str]:
        """Every active weather, base first, in display order."""
        out = [self.base] if self.base and self.base.lower() != "clear" else []
        out += [v for v in (self.layers.get(k, "") for k in LAYERS) if v]
        if self.blizzard and not any("blizzard" in n.lower() for n in out):
            out.append("Blizzard")
        if self.meteor and not any("meteor" in n.lower() for n in out):
            out.append("Meteor Shower")
        return out

    @property
    def tags(self) -> frozenset:
        tags = set()
        for name in self.names:
            low = name.lower()
            for needle, tag in _TAGS:
                # "Rainbow" is a clear-sky event; it must not also read as rain.
                if needle == "rain" and "rainbow" in low:
                    continue
                if needle in low:
                    tags.add(tag)
                    if tag in ("eclipse", "blood_moon", "storm", "rain"):
                        break  # "Lunar Eclipse" is not also a plain eclipse, etc.
        if self.season.lower() == "winter":
            tags.add("snow")
        return frozenset(tags)

    @property
    def night(self) -> bool:
        return "night" in self.cycle.lower()

    def label(self) -> str:
        names = self.names
        return " · ".join(names) if names else (self.base or "Clear")


def read_weather(game) -> WeatherState:
    """One pass over ReplicatedStorage.world. Cheap: ~a dozen reads."""
    r = game.r
    world = r.get_world_config()
    if not world:
        return WeatherState()
    children = dict((name, inst) for inst, name in r.named_children(world))

    def text(inst: int) -> str:
        return _clean(game.read_world_string_value(inst)) if inst else ""

    def flag(name: str) -> bool:
        inst = children.get(name)
        return bool(inst and r.u8(inst + r.offsets.get("Value", 0xB8)))

    weather = children.get("weather", 0)
    layers = {}
    if weather:
        sub = dict((name, inst) for inst, name in r.named_children(weather))
        layers = {k: text(sub.get(k, 0)) for k in LAYERS}
    return WeatherState(
        cycle=text(children.get("cycle", 0)),
        base=text(weather),
        layers=layers,
        season=text(children.get("season", 0)),
        event=text(children.get("event", 0)),
        meteor=flag("meteor_active"),
        blizzard=flag("blizzard_active"),
    )
