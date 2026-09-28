from contracts.web_agent import SPONSORED_FLAG

from web_agent.models import SearchResponse, SearchResult


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
