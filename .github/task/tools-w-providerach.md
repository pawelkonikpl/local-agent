# Tool calling w każdym providerze llm-proxy

## Kontekst

`llm-proxy` wystawia jeden kontrakt: `POST /v1/messages` w formacie Anthropic Messages API (request) i strumień SSE w kształcie Anthropic (response) — niezależnie od providera (`LLMBackend` Protocol w `services/llm-proxy/src/llm_proxy/backends/base.py`). Routing po `model` w `main.py:_resolve_backend`:

- `claude-*` → `AnthropicBackend` — czysty passthrough do `api.anthropic.com`. `tools`, bloki `tool_use`/`tool_result` i zdarzenia `input_json_delta` już dziś przechodzą bez zmian (payload jest forwardowany 1:1).
- `local-model` → `OpenAIBackend` wskazany na serwis `services/local-model` (transformers, Qwen2.5-0.5B-Instruct).
- cokolwiek innego → `OpenAIBackend` wskazany na prawdziwe OpenAI.

Stan zweryfikowany w kodzie:
- `OpenAIBackend` (`openai_backend.py`) jest tekstowy: `_to_openai_messages` wyrzuca wszystko poza blokami `text` (gubi `tool_use`/`tool_result`), `tools`/`tool_choice` z payloadu nie są przekazywane, strumień emituje tylko jeden blok `text` na indeksie 0, `finish_reason == "tool_calls"` nie jest mapowany.
- `local-model` (`local_model/main.py`) filtruje wiadomości do ról `user/assistant/system`, ignoruje `tools`, nie rozpoznaje wywołań narzędzi w wygenerowanym tekście, zawsze kończy `finish_reason: "stop"`.

Cel: każdy provider potrafi przyjąć definicje narzędzi, zwrócić wywołanie narzędzia jako blok `tool_use` w strumieniu Anthropic i przyjąć wynik (`tool_result`) w kolejnej turze. **Samo wykonywanie narzędzi** (rejestr, pętla agenta, zatwierdzanie, komponent GUI tak/nie) to session-agent / Etap 5 i osobne zadania (`dodac-toeknizer.md` pkt 4–5) — tu tylko warstwa providerów, bez której ta pętla nie zadziała dla nie-Claude'owych modeli.

## Zakres

1. **Kontrakt (`backends/base.py`)** — doprecyzować docstring `LLMBackend`: backend musi obsłużyć `tools`, `tool_choice`, bloki `tool_use`/`tool_result` w `messages` i emitować bloki `tool_use` (`content_block_start` z `{"type":"tool_use","id","name","input":{}}` + `content_block_delta` z `input_json_delta`) oraz `stop_reason: "tool_use"`.

2. **Anthropic** — bez zmian w kodzie (passthrough). Tylko test potwierdzający, że `tools` trafia do upstreamu i zdarzenia `tool_use` wracają nienaruszone.

3. **OpenAIBackend — tłumaczenie requestu** (nowy moduł `backends/openai_translate.py`, żeby `openai_backend.py` nie puchł):
   - `tools` Anthropic `{name, description, input_schema}` → OpenAI `{"type":"function","function":{name, description, parameters: input_schema}}`.
   - `tool_choice`: `{"type":"auto"}` → `"auto"`, `{"type":"any"}` → `"required"`, `{"type":"tool","name":X}` → `{"type":"function","function":{"name":X}}`, `{"type":"none"}` → `"none"`. Brak `tools` → nie wysyłać ani `tools`, ani `tool_choice`.
   - Wiadomość `assistant` z blokami `tool_use` → jedna wiadomość OpenAI `{"role":"assistant","content": <tekst albo None>, "tool_calls":[{"id","type":"function","function":{"name","arguments": json.dumps(input)}}]}`.
   - Wiadomość `user` z blokami `tool_result` → dla każdego bloku osobna wiadomość `{"role":"tool","tool_call_id": tool_use_id, "content": <tekst>}` (content bloku może być stringiem albo listą bloków `text`; `is_error: true` → prefiks `"Error: "` w treści), **przed** ewentualnym tekstem użytkownika z tej samej wiadomości (kolejność wymagana przez OpenAI: `tool` musi następować zaraz po `assistant.tool_calls`).
   - `system` i zwykły tekst — jak dziś.

4. **OpenAIBackend — tłumaczenie strumienia**:
   - Nie otwierać z góry bloku tekstowego na indeksie 0. Otwierać blok `text` leniwie przy pierwszej delcie `content`; blok `tool_use` przy pierwszym fragmencie `delta.tool_calls[i]` z nowym `index` (ma `id` i `function.name`). Indeksy bloków Anthropic nadawane sekwencyjnie (0, 1, 2…), osobno od `index` OpenAI; mapa `openai_tool_index → anthropic_block_index`.
   - Przełączenie z tekstu na tool call (albo między tool callami) zamyka poprzedni blok `content_block_stop`.
   - `function.arguments` fragmenty → `content_block_delta` z `{"type":"input_json_delta","partial_json": fragment}`.
   - `finish_reason`: `"tool_calls"` → `stop_reason: "tool_use"`, `"length"` → `"max_tokens"`, reszta → `"end_turn"`.
   - Pusta odpowiedź (brak jakiejkolwiek delty) → nadal poprawny strumień (`message_start` … `message_stop`), z jednym pustym blokiem `text`, żeby klient `anthropic` SDK nie dostał wiadomości bez contentu.
   - Usunąć z docstringa `OpenAIBackend` zdanie "Text-only for now…".

5. **local-model — tools przez chat template Qwen2.5** (`services/local-model`):
   - Przyjąć `tools` i `tool_choice` z payloadu OpenAI. `tools` przekazać do `apply_chat_template(..., tools=tools)` w pełnym kształcie `{"type":"function","function":{...}}` — tak jak w dokumentacji function callingu Qwen2.5 i w `get_json_schema` z transformers; template robi `tool | tojson`, więc model widzi dokładnie format, na którym był trenowany (Qwen2.5 wstawia je do promptu systemowego) — zarówno w `generate_stream`, jak i `count_prompt_tokens`, żeby liczenie tokenów wejścia uwzględniało definicje narzędzi.
   - Przestać odrzucać role `tool` i `tool_calls` z `assistant`: przekazać do template'u `{"role":"tool","content":...}` oraz `{"role":"assistant","content":..., "tool_calls":[{"type":"function","function":{"name", "arguments": <dict>}}]}` — **uwaga**: template Qwen2.5 robi `arguments | tojson`, więc `arguments` przychodzące z OpenAIBackend jako string JSON trzeba zdeserializować do dict przed template'em (inaczej podwójne kodowanie).
   - Parser wyjścia: model emituje `<tool_call>\n{"name": ..., "arguments": {...}}\n</tool_call>`. Strumieniowanie tekstu jak dziś, ale gdy w strumieniu pojawi się `<tool_call>`, przestać emitować delty `content` i buforować do `</tool_call>` (albo końca generacji); każdy kompletny blok sparsować i wyemitować jako chunk `delta.tool_calls=[{"index": i, "id": "call_<hex>", "type":"function","function":{"name", "arguments": json.dumps(arguments)}}]`. Bufor musi obsłużyć tag rozcięty między chunkami (trzymać ogon tekstu, który może być prefiksem `<tool_call>`, zanim go wyemitujesz).
   - Co najmniej jeden poprawnie sparsowany tool call → `finish_reason: "tool_calls"`. Niepoprawny JSON w `<tool_call>` → potraktować cały surowy fragment jako zwykły tekst (emitować jako `content`), nie 500.
   - `tool_choice == "none"` → nie przekazywać `tools` do template'u. `"required"`/wymuszone narzędzie — mały model tego nie gwarantuje; best-effort (bez twardego wymuszania, bez błędu).
   - Logika parsera w osobnym, czystym module (`local_model/tool_calls.py`) — testowalna bez ładowania wag.
   - Zweryfikować, że `TextIteratorStreamer(skip_special_tokens=True)` nie wycina `<tool_call>`/`</tool_call>` (w Qwen2.5 to added tokens; jeśli są oznaczone jako special — przełączyć na `skip_special_tokens=False` i ręcznie odfiltrować `<|im_end|>`/`<|endoftext|>`).

6. **Metering** — bez zmian w logice: `usage` z OpenAI/local-model jak dziś; upewnić się tylko, że ścieżka tool call nadal ustawia `usage.input_tokens/output_tokens` (chunk z `usage` przychodzi po `finish_reason: "tool_calls"`).

Poza zakresem: wykonywanie narzędzi, rejestr narzędzi, pętla agenta w session-agent, zatwierdzanie i komponent GUI tak/nie (Etap 5 / osobne zadania), równoległe tool calle w local-model ponad to, co model sam wygeneruje, strumieniowanie `input_json_delta` kawałkami w local-model (tam `arguments` idą jednym fragmentem — to w porządku), obsługa bloków `image`/`thinking` w OpenAIBackend.

## Kryteria akceptacji

- `claude-*` z `tools` w payloadzie: request do upstreamu zawiera `tools` bez zmian; zdarzenia `content_block_start` typu `tool_use` + `input_json_delta` + `stop_reason: "tool_use"` docierają do klienta bajt-w-bajt (poza re-framingiem linii).
- OpenAI (mock `httpx2.MockTransport`) z payloadem Anthropic zawierającym `tools` i `tool_choice: {"type":"any"}`: body wysłane do `/chat/completions` ma `tools` w formacie `{"type":"function","function":{..., "parameters": ...}}` i `tool_choice: "required"`.
- Payload bez `tools` → body do OpenAI nie zawiera kluczy `tools` ani `tool_choice` (brak regresji dla czystego czatu).
- Historia `[user text, assistant(text + tool_use id=A), user(tool_result A)]` tłumaczy się na `[user, assistant{content, tool_calls[id=A, arguments=JSON string]}, tool{tool_call_id=A}]`; `tool_result` z `is_error: true` ma treść z prefiksem `Error: `.
- Strumień OpenAI: tekst "Sprawdzam", potem dwa tool calle z argumentami pociętymi na kilka fragmentów, `finish_reason: "tool_calls"` → klient dostaje bloki: 0 `text`, 1 `tool_use`, 2 `tool_use`, każdy zamknięty `content_block_stop`, połączone `partial_json` każdego bloku to poprawny JSON argumentów, `message_delta.stop_reason == "tool_use"`.
- Strumień OpenAI tylko z tool callem (bez tekstu) → pierwszy blok to od razu `tool_use` na indeksie 0, bez pustego bloku `text`.
- Oficjalny klient `anthropic` SDK (`client.messages.stream(...).get_final_message()`) nad strumieniem z `OpenAIBackend` zwraca `content` z blokiem `ToolUseBlock` o poprawnym `name`, `id` i `input` jako dict — test end-to-end przez ASGI transport.
- Ścieżka tool call zapisuje `token_usage` z `prompt_tokens`/`completion_tokens` z ostatniego chunka OpenAI.
- local-model, parser (bez wag): tekst `"ok <tool_call>\n{\"name\":\"get_weather\",\"arguments\":{\"city\":\"Kraków\"}}\n</tool_call>"` podany w dowolnym pocięciu na chunki (w tym `<tool_` | `call>`) → delta tekstu `"ok "` + jeden tool call `get_weather` z `{"city":"Kraków"}`, `finish_reason: "tool_calls"`; niepoprawny JSON wewnątrz tagu → cały fragment jako tekst, `finish_reason: "stop"`.
- local-model: wiadomości `role: tool` i `assistant.tool_calls` trafiają do `apply_chat_template` (nie są odfiltrowywane), `arguments` jako dict; `tools` przekazane do template'u w generacji i przy liczeniu `prompt_tokens`.
- Ręcznie (docker/podman compose, prawdziwy Qwen2.5-0.5B): request przez llm-proxy z `model: "local-model"` i jednym prostym narzędziem (np. `get_weather(city)`) z pytaniem "Jaka jest pogoda w Krakowie?" zwraca blok `tool_use`; kolejna tura z `tool_result` daje odpowiedź tekstową. (0.5B może być zawodny — kryterium to poprawny tor danych, nie jakość modelu.)
- Istniejące testy `services/llm-proxy/tests` przechodzą bez zmian w asercjach dla czystego tekstu.

## Notatki implementacyjne

- Nie przepuszczamy ruchu Claude'a przez warstwę kompatybilności OpenAI — Anthropic zostaje natywnym passthrough (decyzja z `plan-implmentacji.md`, sekcja llm-proxy).
- Tłumaczenia request/stream w OpenAIBackend jako czyste funkcje / mała klasa stanu strumienia (`_StreamTranslator` z metodami `on_chunk(chunk) -> list[bytes]`, `finish() -> list[bytes]`) — łatwo testować jednostkowo bez HTTP.
- `id` bloku `tool_use` = `id` tool calla z OpenAI (ta sama wartość musi wrócić jako `tool_use_id` → `tool_call_id` w kolejnej turze, więc nie generować nowego).
- local-model jest poza uv workspace (osobne zależności) — testy parsera w jego własnym katalogu, bez importu `torch`/`transformers` w module parsera.
