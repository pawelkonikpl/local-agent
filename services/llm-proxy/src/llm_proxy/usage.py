class UsageAccumulator:
    """Tracks model/input/output tokens for one request, populated either by a backend
    observing Anthropic-shaped SSE events as they pass through, or set directly by a backend
    that already knows the numbers from its own upstream's usage data."""

    def __init__(self, fallback_model: str) -> None:
        self.model = fallback_model
        self.input_tokens = 0
        self.output_tokens = 0

    def observe(self, event_type: str, data: dict) -> None:
        if event_type == "message_start":
            message = data.get("message", {})
            self.model = message.get("model", self.model)
            usage = message.get("usage", {})
            self.input_tokens = usage.get("input_tokens", self.input_tokens)
            self.output_tokens = usage.get("output_tokens", self.output_tokens)
        elif event_type == "message_delta":
            usage = data.get("usage", {})
            if "output_tokens" in usage:
                self.output_tokens = usage["output_tokens"]
