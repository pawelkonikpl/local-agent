from contracts.web_agent import SPONSORED_FLAG

from web_agent.models import Listing, ListingDetailsResponse, ListingSearchResponse, SearchResponse, SearchResult


def format_text(response: SearchResponse) -> str:
    """Compact, readable text for a search outcome: a numbered list, or one sentence on why not."""
    if response.status == "ok":
        return "\n".join(_format_result(result) for result in response.results)
    if response.status == "no_results":
        return f'No results for "{response.query}".'
    if response.status == "blocked":
        return f"The search engine blocked the request: {response.error}."
    return f"The search failed: {response.error}."


def _format_result(result: SearchResult) -> str:
    if result.withheld:
        return f"{result.rank}. [withheld: {', '.join(result.flags)}]\n   {result.url}"
    heading = f"{result.rank}. {result.title}"
    if result.price:
        heading += f" — {result.price}"
    if SPONSORED_FLAG in result.flags:
        heading += " [sponsored]"
    lines = f"{heading}\n   {result.url}\n   {result.snippet}".rstrip()
    other_flags = [flag for flag in result.flags if flag != SPONSORED_FLAG]
    if other_flags:
        lines += f"\n   [flags: {', '.join(other_flags)}]"
    return lines


def format_listings(response: ListingSearchResponse) -> str:
    """A portal search for a person at the terminal: where it looked, then one offer per line."""
    where = f"{response.portal}, {response.resolved_location or response.location}"
    if response.status == "ok":
        heading = f"{where}: {response.total_count if response.total_count is not None else '?'} offers\n{response.search_url}"
        return "\n".join([heading, *(_format_listing(listing) for listing in response.results)])
    if response.status == "no_results":
        return f"No offers on {where}.\n{response.search_url}"
    if response.status == "unknown_location":
        return f"Unknown location: {response.error}."
    if response.status == "blocked":
        return f"The portal blocked the request: {response.error}."
    return f"The search failed: {response.error}."


def format_details(response: ListingDetailsResponse) -> str:
    if response.status == "ok" and response.listing is not None:
        lines = [_format_listing(response.listing)]
        lines.extend(f"   {param.name}: {param.value}" for param in response.params)
        return "\n".join(lines)
    if response.status == "inactive":
        return "The offer is no longer active."
    if response.status == "blocked":
        return f"The portal blocked the request: {response.error}."
    return f"Reading the offer failed: {response.error}."


def _format_listing(listing: Listing) -> str:
    if listing.withheld:
        return f"{listing.rank}. [withheld: {', '.join(listing.flags)}]\n   {listing.url}"
    fields = [
        f"{listing.price} {listing.currency or ''}".strip() if listing.price is not None else "?",
        f"{listing.area_m2} m²" if listing.area_m2 is not None else "?",
        f"{listing.price_per_m2}/m²" if listing.price_per_m2 is not None else "?",
        listing.plot_type or "?",
        str(listing.listed_at or "?"),
    ]
    heading = f"{listing.rank}. {listing.title} ({listing.location or '?'}) — {' · '.join(fields)}"
    if SPONSORED_FLAG in listing.flags:
        heading += " [sponsored]"
    return f"{heading}\n   {listing.url}"
