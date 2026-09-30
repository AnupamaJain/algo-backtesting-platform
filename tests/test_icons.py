"""Icons are drawn, not typed.

Emoji were doing the work of an icon set across the terminal: a colour
sticker at roughly 1.4x the line height, rendered differently by every
operating system, sitting on a different baseline in every font, and
impossible to tint -- so a warning triangle and a chart glyph carried the
same visual weight as each other and as the text around them. They are SVG
symbols now, on a 24px grid, inheriting currentColor.

These checks are static and run in the normal suite.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SPRITE = ROOT / "templates" / "_icons.html"

#: Pictorial emoji. Arrows, box drawing and the tick/cross pair are
#: typography rather than iconography and stay.
PICTORIAL = re.compile("[\U0001F300-\U0001FAFF\U00002600-\U000027BF]")
TYPOGRAPHIC = set("→←↑↓↗↘▲▼●■✓✕✖⇅⌀★")

#: The same glyphs written as HTML entities, which a character-level pass
#: cannot see -- the footer hid a warning triangle this way on every page.
ENTITY = re.compile(r"&#(?:x[0-9a-fA-F]+|\d+);")


def _templates() -> list[Path]:
    dirs = [ROOT / "templates", ROOT / "notifications", ROOT / "position_guard",
            ROOT / "covered_calls", ROOT / "cas_tracker"]
    out: list[Path] = []
    for d in dirs:
        if d.exists():
            out.extend(p for p in d.rglob("*.html") if "node_modules" not in p.parts)
    return sorted(out)


@pytest.mark.parametrize("template", _templates(), ids=lambda p: p.name)
def test_no_pictorial_emoji_as_iconography(template: Path):
    body = template.read_text()
    found = {g for g in PICTORIAL.findall(body) if g not in TYPOGRAPHIC}
    assert not found, (
        f"{template.name} uses emoji where the icon set belongs: {sorted(found)}. "
        f'Use <svg class="ico"><use href="#i-..."/></svg>.'
    )


@pytest.mark.parametrize("template", _templates(), ids=lambda p: p.name)
def test_no_pictorial_emoji_hidden_in_an_entity(template: Path):
    offenders = []
    for m in ENTITY.finditer(template.read_text()):
        raw = m.group(0)[2:-1]
        code = int(raw[1:], 16) if raw[0] in "xX" else int(raw)
        char = chr(code)
        if PICTORIAL.match(char) and char not in TYPOGRAPHIC:
            offenders.append(f"{m.group(0)} ({char})")
    assert not offenders, (
        f"{template.name} writes emoji as entities, which no character-level "
        f"pass can see: {offenders}"
    )


def test_every_icon_referenced_is_defined():
    """A <use> pointing at a missing symbol renders nothing at all."""
    defined = set(re.findall(r'<symbol id="(i-[a-z-]+)"', SPRITE.read_text()))
    assert defined, "the sprite defines no symbols"

    missing: dict[str, set[str]] = {}
    for template in _templates():
        used = set(re.findall(r'<use href="#(i-[a-z-]+)"', template.read_text()))
        gap = used - defined
        if gap:
            missing[template.name] = gap
    assert not missing, f"icons referenced but not defined: {missing}"
