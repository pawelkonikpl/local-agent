import re
from dataclasses import dataclass

# Characters a reader can't see but a model reads: zero-width characters, bidi controls, and C0/C1
# controls other than newline and tab.
_INVISIBLE = re.compile(
    "[​-‏⁠-⁤﻿‪-‮⁦-⁩"
    "\x00-\x08\x0b-\x1f\x7f-\x9f]"
)
# The Unicode Tags block mirrors ASCII invisibly; it has no legitimate use in page text, so it is
# a smuggling signal on its own, not just noise.
_TAGS = re.compile("[\U000e0000-\U000e007f]")


@dataclass(frozen=True)
class SanitizedText:
    text: str
    removed_invisible: int
    removed_tags: int


def sanitize(text: str) -> SanitizedText:
    """`text` without invisible characters, plus how many of each kind were removed."""
    without_tags, removed_tags = _TAGS.subn("", text)
    clean, removed_invisible = _INVISIBLE.subn("", without_tags)
    return SanitizedText(clean, removed_invisible, removed_tags)
