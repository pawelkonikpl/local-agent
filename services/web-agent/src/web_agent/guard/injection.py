"""Heuristics for page text that talks to the model instead of the reader.

A helper layer, not a defence: an adaptive attacker will phrase around any regex. What these catch
is the unsophisticated and copy-pasted injections, and a false positive costs one withheld snippet.
The actual protection is architectural (spotlighting, the api's tool guard, origin and egress
allowlists). Keep the patterns here and simple; don't grow them into a "full" filter.
"""

import re

_FLAGS = re.IGNORECASE | re.MULTILINE

PATTERNS: dict[str, list[re.Pattern[str]]] = {
    "instruction_override": [
        re.compile(r"\b(ignore|disregard|forget)\s+(all\s+|any\s+)?(the\s+)?(previous|prior|above|earlier)\s+"
                   r"(instructions|prompts?|rules|directions)", _FLAGS),
        re.compile(r"\bzignoruj\s+(wszystkie\s+)?(poprzednie|wcześniejsze|powyższe)\s+(polecenia|instrukcje)", _FLAGS),
        re.compile(r"\byou\s+are\s+now\b", _FLAGS),
        re.compile(r"\bjesteś\s+teraz\b", _FLAGS),
    ],
    "fake_system": [
        re.compile(r"^\s*(system|administrator|admin)\s*:", _FLAGS),
        re.compile(r"<\|(system|im_start|im_end)\|>", _FLAGS),
        re.compile(r"\[/?INST\]", _FLAGS),
        re.compile(r"</?\s*(tool_result|untrusted_web_content|system)\b[^>]*>", _FLAGS),
    ],
    "ai_addressed": [
        re.compile(r"\b(AI|LLM)\s+(assistant|agent|model)s?\s+(must|should|need\s+to)\b", _FLAGS),
        re.compile(r"\bif\s+you\s+are\s+an?\s+(AI|LLM|language\s+model|assistant)\b", _FLAGS),
        re.compile(r"\bjeśli\s+jesteś\s+(modelem|AI|asystentem|sztuczną\s+inteligencją)", _FLAGS),
    ],
    "fake_approval": [
        re.compile(r"\buser\s+(has\s+)?(already\s+)?(pre-?)?approved\b", _FLAGS),
        re.compile(r"\bużytkownik\s+(już\s+)?(wyraził\s+zgodę|zatwierdził)", _FLAGS),
    ],
    "payment_demand": [
        re.compile(r"\b(licen[sc]e|licencj\w*|verification\s+fee|opłat\w*\s+weryfikacyjn\w*)\b.{0,80}?"
                   r"\b(pay|zapłać|send|wyślij|crypto|BTC|ETH|USDT|wallet|portfel\w*)\b", _FLAGS | re.DOTALL),
        re.compile(r"\b(pay|zapłać|send|wyślij)\b.{0,80}?\b(BTC|ETH|USDT|crypto|kryptowalut\w*)\b.{0,80}?"
                   r"\b(licen[sc]e|licencj\w*|verification\s+fee|opłat\w*\s+weryfikacyjn\w*)\b", _FLAGS | re.DOTALL),
    ],
    "credential_request": [
        re.compile(r"\b(enter|provide|type|podaj|wpisz)\s+(your\s+|swój\s+|swoje\s+)?(password|hasło|OTP|"
                   r"one-time\s+(code|password)|kod\s+jednorazowy|seed\s+phrase|frazę\s+seed)", _FLAGS),
    ],
}


def injection_signals(text: str) -> list[str]:
    """Names of the pattern categories `text` matches, in `PATTERNS` order."""
    return [name for name, patterns in PATTERNS.items() if any(p.search(text) for p in patterns)]
