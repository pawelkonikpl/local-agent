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


def count_prompt_tokens(messages: list[dict]) -> int:
    inputs = _tokenizer.apply_chat_template(messages, add_generation_prompt=True, return_tensors="pt")
    return inputs.shape[-1]


def count_generated_tokens(text: str) -> int:
    return len(_tokenizer(text, add_special_tokens=False).input_ids)


def generate_stream(messages: list[dict], max_new_tokens: int) -> Iterator[str]:
    """Yields text chunks as they're generated.

    Runs `model.generate` on a background thread feeding a `TextIteratorStreamer`, so the
    caller can start forwarding tokens before generation finishes. Single-flight only —
    fine for a dev/test service, not for concurrent request serving.
    """
    inputs = _tokenizer.apply_chat_template(
        messages, add_generation_prompt=True, return_tensors="pt", return_dict=True
    )
    streamer = TextIteratorStreamer(_tokenizer, skip_prompt=True, skip_special_tokens=True)
    generation_kwargs = dict(**inputs, max_new_tokens=max_new_tokens, do_sample=False, streamer=streamer)
    thread = Thread(target=_model.generate, kwargs=generation_kwargs)
    thread.start()
    try:
        yield from streamer
    finally:
        thread.join()
