# Test: mały LLM lokalnie w sandboxie

Jednorazowy, odizolowany test feasibility: czy ten host (Intel i9-9880H, 16GB RAM, bez
użytecznego GPU dla CPU-inference) udźwignie lokalny model przez `transformers`, bez
kwantyzacji, w kontenerze Podman z tymi samymi zasadami hardeningu co docelowy sandbox
sesji (`.github/task/plan-implmentacji.md`, Etap 4).

Nie jest częścią stosu z `podman-compose.yml` — to osobny, wyrzucalny eksperyment.

Model: `Qwen/Qwen2.5-0.5B-Instruct` (Apache-2.0, ~0.5B parametrów, tekstowy) — ungated,
nie wymaga tokena HF ani akceptacji licencji (w odróżnieniu od rodziny Gemma, która jest
gated).

Ładowany w `float32`, nie w natywnym `bf16` publikacji: ten CPU (i9-9880H, Coffee Lake,
2019) nie ma sprzętowego wsparcia bf16, więc PyTorch emuluje je programowo — w teście na
`Qwen2.5-1.5B-Instruct` dało to ~0.4 tok/s. `float32` idzie na natywnych kernelach
AVX2/FMA i jest o rząd wielkości szybsze; przy 0.5B parametrów mieści się (~2GB) bez OOM,
którego wcześniej dawało `float32` na 1.5B (~6GB, ponad limit maszyny Podman).

## Hardening (`run.sh`)

- `--read-only` + `--tmpfs /tmp` — rootfs niemodyfikowalny, jedyny zapis to wolumen cache HF.
- `--cap-drop=all`, `--security-opt no-new-privileges` — zero capabilities, brak eskalacji.
- `--pids-limit=256`, `--memory=3g`, `--cpus=6` — limity zasobów.
- `--user 10001:10001` — bez roota.
- `--network=slirp4netns` — dostęp do internetu tylko do pobrania wag z Hugging Face; nic
  z hosta nie jest osiągalne z kontenera.

## Użycie

```bash
./run.sh
```

Skrypt wypisze czas ładowania modelu, czas generacji, szczytowe RSS procesu (realny ślad
pamięci) i odpowiedź modelu na testowy prompt — to odpowiedź na "czy ten komputer to
obsłuży", zamiast zgadywania.

## Sprzątanie

```bash
podman volume rm small-llm-test-cache   # usuwa pobrane wagi
podman rmi small-llm-test
```
