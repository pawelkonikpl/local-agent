from collections.abc import Iterator
from threading import Thread

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer, TextIteratorStreamer

from local_model.config import settings

_tokenizer = None
_model = None


def load() -> None:
    """Loads tokenizer + weights once at process startup (~2.7GB resident for the 0.5B
    checkpoint in float32 — see experiments/small-llm-test for why float32, not bf16, on
    this CPU)."""
    global _tokenizer, _model
    _tokenizer = AutoTokenizer.from_pretrained(settings.model_id)
    _model = AutoModelForCausalLM.from_pretrained(settings.model_id, dtype=torch.float32)
    _model.eval()


def count_prompt_tokens(messages: list[dict], tools: list[dict] | None = None) -> int:
    inputs = _tokenizer.apply_chat_template(
        messages, tools=tools, add_generation_prompt=True, return_tensors="pt", return_dict=True
    )
    return inputs["input_ids"].shape[-1]


def count_generated_tokens(text: str) -> int:
    return len(_tokenizer(text, add_special_tokens=False).input_ids)


def generate_stream(
    messages: list[dict], max_new_tokens: int, tools: list[dict] | None = None
) -> Iterator[str]:
    """Yields text chunks as they're generated.

    Runs `model.generate` on a background thread feeding a `TextIteratorStreamer`, so the
    caller can start forwarding tokens before generation finishes. Single-flight only —
    fine for a dev/test service, not for concurrent request serving.

    `tools` go through Qwen2.5's own chat template, which lists them in the system prompt.
    `skip_special_tokens=True` is safe for tool calling: `<tool_call>` / `</tool_call>` are
    added tokens with `special: false` in Qwen2.5's tokenizer, so they survive decoding.
    """
    inputs = _tokenizer.apply_chat_template(
        messages, tools=tools, add_generation_prompt=True, return_tensors="pt", return_dict=True
    )
    streamer = TextIteratorStreamer(_tokenizer, skip_prompt=True, skip_special_tokens=True)
    generation_kwargs = dict(**inputs, max_new_tokens=max_new_tokens, do_sample=False, streamer=streamer)
    thread = Thread(target=_model.generate, kwargs=generation_kwargs)
    thread.start()
    try:
        yield from streamer
    finally:
        thread.join()
