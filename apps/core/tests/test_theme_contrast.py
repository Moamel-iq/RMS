"""
Contrast, asserted from the rebuilt design-system tokens rather than a screenshot.

This module reads `static/css/erp-design-system.css`, resolves each theme's
token block, and measures the pairs the reader actually looks at. WCAG AA is
4.5:1 for body text, 3:1 for large text, icons and the boundaries of controls.
A token that fails is named with its ratio, because "contrast failed" is not a
finding anybody can act on.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STYLES = Path(__file__).resolve().parents[3] / "static" / "css" / "erp-design-system.css"

#: The three ways a reader can end up in a theme: the default, the operating
#: system's preference, and an explicit choice.
THEME_SELECTORS = {
    "light": r"^\s*:root\s*\{",
    "dark (system)": r'^\s*:root:not\(\[data-theme="light"\]\)\s*\{',
    "dark (chosen)": r'^\s*:root\[data-theme="dark"\]\s*\{',
}

TEXT_AA = 4.5
LARGE_AA = 3.0


def _tokens(selector_pattern: str) -> dict[str, str]:
    css = STYLES.read_text(encoding="utf-8")
    match = re.search(selector_pattern, css, re.M)
    assert match, f"no token block matches {selector_pattern}"
    start = css.index("{", match.start()) + 1
    depth, index = 1, start
    while depth:
        if css[index] == "{":
            depth += 1
        elif css[index] == "}":
            depth -= 1
        index += 1
    return {
        name: value.strip()
        for name, value in re.findall(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", css[start : index - 1])
    }


def _channels(value: str) -> tuple[float, float, float, float]:
    value = value.strip()
    if value.startswith("#"):
        digits = value[1:]
        if len(digits) == 3:
            digits = "".join(c * 2 for c in digits)
        return (
            int(digits[0:2], 16),
            int(digits[2:4], 16),
            int(digits[4:6], 16),
            1.0,
        )
    numbers = [float(n) for n in re.findall(r"[\d.]+", value)]
    if len(numbers) == 3:
        return numbers[0], numbers[1], numbers[2], 1.0
    return numbers[0], numbers[1], numbers[2], numbers[3]


def _over(colour: str, backdrop: str) -> tuple[float, float, float]:
    """Flatten a translucent colour onto what sits behind it."""
    r, g, b, alpha = _channels(colour)
    br, bg, bb, _ = _channels(backdrop)
    return (
        r * alpha + br * (1 - alpha),
        g * alpha + bg * (1 - alpha),
        b * alpha + bb * (1 - alpha),
    )


def _luminance(rgb: tuple[float, float, float]) -> float:
    parts = []
    for channel in rgb:
        c = channel / 255
        parts.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * parts[0] + 0.7152 * parts[1] + 0.0722 * parts[2]


def contrast(foreground: str, background: str, *, backdrop: str) -> float:
    front = _luminance(_over(foreground, backdrop))
    back = _luminance(_over(background, backdrop))
    lighter, darker = max(front, back), min(front, back)
    return round((lighter + 0.05) / (darker + 0.05), 2)


#: (foreground token, background token, minimum, what the reader is looking at)
#: Table rules and other decorative separators are deliberately absent: WCAG
#: asks 3:1 of boundaries that make a control identifiable, not of every line
#: on the page, and holding a hairline to that ratio would coarsen the whole
#: register look for no reader's benefit.
PAIRS: tuple[tuple[str, str, float, str], ...] = (
    ("--ui-text", "--ui-surface", TEXT_AA, "body text on a card"),
    ("--ui-text-soft", "--ui-surface", TEXT_AA, "supporting text on a card"),
    ("--ui-text-muted", "--ui-surface", LARGE_AA, "large muted labels and icons"),
    ("--ui-text", "--ui-bg", TEXT_AA, "body text on the application canvas"),
    ("--ui-text-soft", "--ui-bg", TEXT_AA, "supporting text on the canvas"),
    ("--ui-text", "--ui-surface-soft", TEXT_AA, "body text on a soft panel"),
)


@pytest.mark.parametrize("theme", list(THEME_SELECTORS))
def test_every_theme_meets_wcag_aa_on_the_pairs_a_reader_looks_at(theme: str) -> None:
    tokens = _tokens(THEME_SELECTORS[theme])
    # A theme block states only what differs; the light block is the base.
    base = _tokens(THEME_SELECTORS["light"])
    resolved = {**base, **tokens}
    backdrop = resolved["--ui-bg"]

    failures = []
    for foreground, background, minimum, description in PAIRS:
        assert foreground in resolved, f"{theme}: {foreground} is not defined"
        assert background in resolved, f"{theme}: {background} is not defined"
        ratio = contrast(resolved[foreground], resolved[background], backdrop=backdrop)
        if ratio < minimum:
            failures.append(
                f"{description}: {foreground} on {background} is {ratio}:1, needs {minimum}:1"
            )
    assert not failures, f"{theme} theme —\n  " + "\n  ".join(failures)


def test_the_app_header_takes_its_colour_from_a_token_in_every_theme() -> None:
    """The rebuilt header must follow the selected theme, not pin a light literal."""
    css = STYLES.read_text(encoding="utf-8")
    header = css[css.index(".ui-app-header {") :]
    header = header[: header.index("\n  }")]
    assert "background: color-mix(in srgb, var(--ui-surface) 94%, transparent)" in header
    assert "rgba(255, 255, 255" not in header
    for selector in THEME_SELECTORS.values():
        resolved = {**_tokens(THEME_SELECTORS["light"]), **_tokens(selector)}
        assert "--ui-surface" in resolved, selector
