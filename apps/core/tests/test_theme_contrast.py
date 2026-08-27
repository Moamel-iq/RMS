"""
Contrast, asserted from the tokens rather than from a screenshot.

The dark theme shipped a top bar that stayed near-white while the text on it
stayed near-white too — about 1.19:1, which is text nobody can read. It was a
literal colour in a rule that every other surface had already tokenised, and no
test could have caught it because nothing here compared two colours.

So this module reads `static/css/app.css`, resolves each theme's token block,
and measures the pairs the reader actually looks at, in every theme: WCAG AA is
4.5:1 for body text, 3:1 for large text, icons and the boundaries of controls.
A token that fails is named with its ratio, because "contrast failed" is not a
finding anybody can act on.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STYLES = Path(__file__).resolve().parents[3] / "static" / "css" / "app.css"

#: The three ways a reader can end up in a theme: the default, the operating
#: system's preference, and an explicit choice.
THEME_SELECTORS = {
    "light": r"^:root \{",
    "dark (system)": r':root:not\(\[data-theme="light"\]\) \{\n    color-scheme: dark;',
    "dark (chosen)": r'^:root\[data-theme="dark"\] \{',
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
    ("--ink", "--topbar-surface", TEXT_AA, "the module name in the top bar"),
    ("--ink-subtle", "--topbar-surface", LARGE_AA, "the top bar's icons"),
    ("--ink-muted", "--topbar-surface", TEXT_AA, "the search box's placeholder"),
    ("--control-border", "--topbar-surface", LARGE_AA, "the boundary of a control in the top bar"),
    ("--ink", "--surface", TEXT_AA, "body text on a card"),
    ("--ink-muted", "--surface", TEXT_AA, "secondary text on a card"),
    ("--ink", "--surface-subtle", TEXT_AA, "body text on a sunken panel"),
    ("--ink-muted", "--surface-muted", TEXT_AA, "text on a muted panel"),
    ("--control-border", "--surface", LARGE_AA, "the boundary of a field"),
    ("--info-700", "--surface", LARGE_AA, "the focus ring"),
    ("--danger-700", "--danger-100", TEXT_AA, "an error message on its tint"),
    ("--warning-700", "--warning-100", TEXT_AA, "a warning on its tint"),
    ("--success-100", "--success-700", TEXT_AA, "a success chip"),
    ("--brand-700", "--brand-50", TEXT_AA, "a brand-tinted chip"),
)


@pytest.mark.parametrize("theme", list(THEME_SELECTORS))
def test_every_theme_meets_wcag_aa_on_the_pairs_a_reader_looks_at(theme: str) -> None:
    tokens = _tokens(THEME_SELECTORS[theme])
    # A theme block states only what differs; the light block is the base.
    base = _tokens(THEME_SELECTORS["light"])
    resolved = {**base, **tokens}
    # `--topbar-surface` is translucent, so it is flattened onto the page ground.
    backdrop = resolved.get("--canvas", resolved["--surface"])

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


def test_the_top_bar_takes_its_colour_from_a_token_in_every_theme() -> None:
    """
    The defect this module exists for: a literal near-white on `.topbar` that
    the dark theme could not reach.
    """
    css = STYLES.read_text(encoding="utf-8")
    topbar = css[css.index(".topbar {") :]
    topbar = topbar[: topbar.index("\n}")]
    assert "background: var(--topbar-surface)" in css
    assert "rgba(255, 255, 255" not in topbar
    for selector in THEME_SELECTORS.values():
        assert "--topbar-surface" in _tokens(selector), selector
