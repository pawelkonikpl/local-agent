"""Deterministic screening of untrusted web text before it reaches the model.

Pure functions -- no browser, no web framework -- so every tool reading the web (search today,
page fetching later) runs its text through the same checks and they are testable on plain strings.
"""

from web_agent.guard.domains import domain_signals
from web_agent.guard.injection import injection_signals
from web_agent.guard.text import SanitizedText, sanitize

__all__ = ["SanitizedText", "domain_signals", "injection_signals", "sanitize"]
