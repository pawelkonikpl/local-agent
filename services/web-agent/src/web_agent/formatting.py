from web_agent.models import SearchResponse


def format_text(response: SearchResponse) -> str:
    """Compact, readable text for a search outcome: a numbered list, or one sentence on why not."""
    if response.status == "ok":
        return "\n".join(
            f"{result.rank}. {result.title}\n   {result.url}\n   {result.snippet}".rstrip()
            for result in response.results
        )
    if response.status == "no_results":
        return f'No results for "{response.query}".'
    if response.status == "blocked":
        return f"The search engine blocked the request: {response.error}."
    return f"The search failed: {response.error}."
