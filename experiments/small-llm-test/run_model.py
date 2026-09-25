import resource
import time

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

MODEL_ID = "Qwen/Qwen2.5-0.5B-Instruct"


def main() -> None:
    start = time.monotonic()
    tokenizer = AutoTokenizer.from_pretrained(MODEL_ID)
    # float32 on this CPU (no native bf16 support) runs on well-optimized AVX2/FMA
    # kernels instead of PyTorch's slow emulated-bf16 path; 0.5B params keeps the
    # fp32 footprint (~2GB) well under the VM's 3.7GB.
    model = AutoModelForCausalLM.from_pretrained(MODEL_ID, dtype=torch.float32)
    model.eval()
    load_s = time.monotonic() - start

    messages = [{"role": "user", "content": "In one sentence, what is a sandbox in computer security?"}]
    inputs = tokenizer.apply_chat_template(
        messages, add_generation_prompt=True, return_tensors="pt", return_dict=True
    )
    prompt_len = inputs["input_ids"].shape[-1]

    gen_start = time.monotonic()
    with torch.no_grad():
        output = model.generate(**inputs, max_new_tokens=80, do_sample=False)
    gen_s = time.monotonic() - gen_start

    text = tokenizer.decode(output[0][prompt_len:], skip_special_tokens=True)
    peak_rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024

    print(f"--- load: {load_s:.1f}s, generate: {gen_s:.1f}s ({output.shape[-1] - prompt_len} tokens) ---")
    print(f"--- peak RSS: {peak_rss_mb:.0f} MB ---")
    print(f"--- response ---\n{text}")


if __name__ == "__main__":
    main()
