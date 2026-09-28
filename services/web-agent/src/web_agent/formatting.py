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
    lines = f"{result.rank}. {result.title}\n   {result.url}\n   {result.snippet}".rstrip()
    if result.flags:
        lines += f"\n   [flags: {', '.join(result.flags)}]"
    return lines
