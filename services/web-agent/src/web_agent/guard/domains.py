"""Signals about where a link points: lookalikes of known brands, punycode, sensitive categories.

Informational: a flagged result is still shown, with a warning the model passes on. Domain age and
reputation (Safe Browsing) need external APIs; when added, they belong in `domain_signals` too.
"""

import re
from urllib.parse import urlparse

import tldextract

# Uses the Public Suffix List snapshot bundled with tldextract: no download, no disk cache (the
# container's filesystem is read-only).
_extract = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)

# brand -> its official registrable domains. Deliberately short; extend as needed.
PROTECTED_BRANDS: dict[str, set[str]] = {
    "paypal": {"paypal.com"},
    "allegro": {"allegro.pl", "allegrolokalnie.pl"},
    "pkobp": {"pkobp.pl"},
    "mbank": {"mbank.pl"},
    "debank": {"debank.com"},
    "binance": {"binance.com"},
    "coinbase": {"coinbase.com"},
    "github": {"github.com"},
    "google": {"google.com", "google.pl"},
    "microsoft": {"microsoft.com"},
    "apple": {"apple.com"},
    "amazon": {"amazon.com", "amazon.pl", "amazon.de"},
    "walmart": {"walmart.com"},
    "revolut": {"revolut.com"},
}

# category -> known registrable domains of that category.
SENSITIVE_DOMAINS: dict[str, set[str]] = {
    "banking": {"pkobp.pl", "mbank.pl", "ing.pl", "santander.pl", "pekao.com.pl", "revolut.com"},
    "payments": {"paypal.com", "przelewy24.pl", "payu.pl", "blik.com"},
    "crypto": {"binance.com", "coinbase.com", "debank.com", "kraken.com", "metamask.io"},
}

# category -> words that, inside a domain label, put the site in that category.
SENSITIVE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "banking": ("bank",),
    "crypto": ("crypto", "wallet", "coin"),
    "adult": ("porn", "xxx"),
    "piracy": ("torrent", "warez"),
    "gambling": ("casino",),
}

MAX_BRAND_DISTANCE = 2
# Below this length an edit distance of 1-2 matches too many unrelated names.
MIN_BRAND_LENGTH_FOR_TYPOS = 6


def registrable_domain(url: str) -> str:
    """`example.co.uk` for `https://www.example.co.uk/x`; the bare host if it has no public suffix."""
    host = urlparse(url).hostname or ""
    parts = _extract(host)
    return parts.top_domain_under_public_suffix or host


def domain_signals(url: str) -> list[str]:
    host = urlparse(url).hostname or ""
    if not host:
        return []
    signals: list[str] = []
    if any(label.startswith("xn--") for label in host.split(".")):
        signals.append("punycode")
    registrable = registrable_domain(url)
    label = _extract(host).domain
    if lookalike := _lookalike_of(label, registrable):
        signals.append(f"lookalike_domain:{lookalike}")
    signals.extend(f"sensitive_category:{category}" for category in _categories(label, registrable))
    return signals


def _lookalike_of(label: str, registrable: str) -> str | None:
    """The brand `registrable` imitates, or None if it is that brand's own domain or unrelated."""
    for brand, official in PROTECTED_BRANDS.items():
        if registrable in official:
            return None
    for brand, official in PROTECTED_BRANDS.items():
        # Combosquatting: the brand as a separate token of the label (paypal-login.com,
        # mbank24.net) -- not any substring, or pineapple.com would imitate apple.
        if brand in _tokens(label):
            return sorted(official)[0]
        if len(brand) >= MIN_BRAND_LENGTH_FOR_TYPOS and 0 < _distance(label, brand) <= MAX_BRAND_DISTANCE:
            return sorted(official)[0]
    return None


def _tokens(label: str) -> list[str]:
    return [token for token in re.split(r"[-_0-9]+", label) if token]


def _categories(label: str, registrable: str) -> list[str]:
    categories = [category for category, domains in SENSITIVE_DOMAINS.items() if registrable in domains]
    for category, keywords in SENSITIVE_KEYWORDS.items():
        if category not in categories and any(keyword in label for keyword in keywords):
            categories.append(category)
    return categories


def _distance(a: str, b: str) -> int:
    """Levenshtein distance."""
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, 1):
        current = [i]
        for j, char_b in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (char_a != char_b)))
        previous = current
    return previous[-1]
