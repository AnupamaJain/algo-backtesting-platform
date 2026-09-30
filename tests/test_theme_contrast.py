"""No text in the terminal may be the same colour as what it sits on.

These pages were written light -- white cards, #f4f4f9 bodies -- and later
had a dark palette put under them. Light text then landed on light
surfaces: a table of live positions measured 1.04:1 against its own row,
which is to say it was not there. The checks below are static (no browser),
so they run in the normal suite and catch the reintroduction at the source.

A full rendered audit lives in tools/contrast_audit.py, which drives a real
browser and computes WCAG ratios against the actual composited background.
It reported 44 failures when this file was written and reports 0 now.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIRS = [ROOT / "templates", ROOT / "notifications", ROOT / "position_guard",
                 ROOT / "covered_calls", ROOT / "cas_tracker"]


def _templates() -> list[Path]:
    out: list[Path] = []
    for d in TEMPLATE_DIRS:
        if d.exists():
            out.extend(p for p in d.rglob("*.html") if "node_modules" not in p.parts)
    return sorted(out)


def _luminance(hex_colour: str) -> float:
    h = hex_colour.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    f = [v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4 for v in (r, g, b)]
    return 0.2126 * f[0] + 0.7152 * f[1] + 0.0722 * f[2]


#: Anything this bright is a light-mode surface and cannot carry our text.
LIGHT = 0.72
#: --surface, the panel most runtime-coloured text is drawn on.
SURFACE_LUM = 0.00834
#: WCAG's floor for large/bold UI text. Below this, text is not there.
MIN_RATIO = 3.0


def _ratio(hex_colour: str, against: float = SURFACE_LUM) -> float:
    a, b = _luminance(hex_colour), against
    lo, hi = min(a, b), max(a, b)
    return (hi + 0.05) / (lo + 0.05)

# Semantic chips keep a light fill on purpose: they pair it with their own
# dark ink in the same rule, so they are self-consistent at any page theme.
CHIP = re.compile(r"status-(ok|warn|err)|pill-(ok|err|429)|badge-(success|danger|warning)|"
                  r"warning-box|method-chip|slider|swatch|legend")


@pytest.mark.parametrize("template", _templates(), ids=lambda p: p.name)
def test_no_light_surface_carries_the_dark_theme(template: Path):
    """A background bright enough to be a light-mode card."""
    offenders = []
    for css in re.findall(r"<style[^>]*>(.*?)</style>", template.read_text(), re.S):
        for rule in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
            selector = re.sub(r"\s+", " ", rule.group(1)).strip()
            if CHIP.search(selector):
                continue
            for decl in rule.group(2).split(";"):
                prop, _, value = decl.partition(":")
                if prop.strip().lower() not in ("background", "background-color"):
                    continue
                for tok in re.findall(r"#[0-9a-fA-F]{3}\b|#[0-9a-fA-F]{6}\b", value):
                    if _luminance(tok) > LIGHT:
                        offenders.append(f"{selector} {{ {prop.strip()}: {tok} }}")
    assert not offenders, (
        f"{template.name} paints light surfaces that the theme's light text "
        f"disappears into: {offenders[:6]}"
    )


@pytest.mark.parametrize("template", _templates(), ids=lambda p: p.name)
def test_no_light_mode_ink_survives_in_javascript(template: Path):
    """Row tints and text colours assigned from JS bypass every stylesheet.

    The wave extractor set group rows to #e3f2fd and prices to #333 at
    runtime; nothing in CSS could reach them and they audited at 1.04:1.
    """
    body = template.read_text()
    offenders = []

    # A rule that sets a row's background and its text together is
    # self-consistent at any page theme -- the ATM strike is dark olive on
    # gold and reads fine -- so those two are judged against each other.
    paired: dict[int, str] = {}
    for bg in re.finditer(r"style\.backgroundColor\s*=\s*['\"](#[0-9a-fA-F]{3,6})['\"]", body):
        nearby = body[bg.end():bg.end() + 200]
        fg = re.search(r"style\.color\s*=\s*['\"](#[0-9a-fA-F]{3,6})['\"]", nearby)
        if fg:
            paired[bg.end() + fg.start()] = bg.group(1)

    for m in re.finditer(r"style\.(background)?[Cc]olor\s*=\s*['\"](#[0-9a-fA-F]{3,6})['\"]", body):
        tok = m.group(2)
        if m.group(1):                      # a background
            partner_ink = None
            nearby = body[m.end():m.end() + 200]
            fg = re.search(r"style\.color\s*=\s*['\"](#[0-9a-fA-F]{3,6})['\"]", nearby)
            if fg:
                partner_ink = fg.group(1)
            if partner_ink:                 # judged as a pair below
                if _ratio(partner_ink, _luminance(tok)) < MIN_RATIO:
                    offenders.append(f"{m.group(0)} with {partner_ink} "
                                     f"({_ratio(partner_ink, _luminance(tok)):.2f}:1)")
                continue
            if _luminance(tok) > LIGHT:
                offenders.append(f"{m.group(0)} is a light-mode row tint")
        else:                               # ink
            if m.start() in paired:
                continue                    # already judged against its own row
            if _ratio(tok) < MIN_RATIO:
                offenders.append(f"{m.group(0)}  ({_ratio(tok):.2f}:1 on --surface)")

    assert not offenders, (
        f"{template.name} assigns colours from JavaScript that no stylesheet "
        f"can retheme, and they do not read: {offenders[:6]}"
    )
