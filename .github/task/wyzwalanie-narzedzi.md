# Wyzwalanie narzędzi w czacie — pętla tool_use w `api`

## Kontekst

Warstwa providerów umie już przenosić narzędzia (`tools-w-providerach.md`): `llm-proxy` przyjmuje `tools` i dla każdego backendu emituje bloki `tool_use` (`content_block_start` + `input_json_delta` + `content_block_stop`) oraz `stop_reason: "tool_use"`. Brakuje strony, która narzędzia **podaje modelowi, wykonuje i odsyła wynik** — to jest to zadanie.

Stan zweryfikowany w kodzie:
- `services/api/src/api/chat/streaming.py:run_generation` wysyła do `llm-proxy` payload bez `tools`, z SSE czyta tylko `content_block_delta`/`text_delta` i `error`, zapisuje jedną wiadomość `assistant` z jednym blokiem `text` pod z góry wyliczonym `assistant_sequence_number` (`routes.py:post_message` → `last_seq + 2`).
- `SessionTaskManager` trzyma `accumulated_text` i przy późnym dołączeniu (`subscribe`) odtwarza tylko tekst jako jedną syntetyczną `delta`.
- Tabela `tool_call_events` istnieje od migracji `0001` (model `api/db/models/tool_call_event.py`, statusy `proposed|approved|rejected|running|succeeded|failed`), nikt jej jeszcze nie zapisuje.
- `sessions.approval_mode` ma `server_default="ask"`.
- GUI (`gui/src/api/client.ts:consumeEventStream`) rozumie tylko eventy `delta` i `error`; `ChatView.tsx:toDisplayMessage` renderuje tylko bloki `text`, więc wiadomość `user` z samymi `tool_result` pokazałaby się jako pusty dymek.
- Nie ma jeszcze `sandbox-manager`/`session-agent` (Etap 4), ani `web-agent` (`narzedzie-web-search.md` — niezaimplementowane).

Decyzje podjęte z userem dla tego zadania:
- **Pętla żyje w `api`** (w `run_generation`), nie czeka na session-agent. To jest właściwe miejsce dla narzędzi wykonywanych **poza sandboxem** (np. przyszły `web_search`, który i tak idzie `api` → `web-agent`). Narzędzia sandboxowe (Bash/Read/Write) dojdą później przez session-agent pod tym samym kształtem `Tool`.
- **Tylko tryb auto** — każde wywołanie wykonywane od razu. Tryb `ask` + komponent tak/nie w GUI to osobne, kolejne zadanie (`dodac-toeknizer.md` pkt 5).
- **Pierwsze narzędzie: proste, wbudowane `get_current_time`** — udowadnia tor danych end-to-end bez zależności zewnętrznych (i ma realną wartość: model nie zna bieżącej daty). `web_search` dojdzie jako kolejny `Tool`, gdy będzie gotowy `web-agent`.

## Zakres

1. **Abstrakcja narzędzia — `services/api/src/api/tools/base.py`**:
   - `ToolResult` (dataclass, frozen): `content: str`, `is_error: bool = False`.
   - `Tool(Protocol)`: `name: str`, `description: str`, `input_schema: dict` (JSON Schema, 1:1 z formatem Anthropic `tools=[...]`), `async def run(self, input: dict) -> ToolResult`.
   - `ToolInputError(Exception)` — narzędzie rzuca ją przy niepoprawnym wejściu; rejestr zamienia ją na `ToolResult(is_error=True)`.
   - Kształt celowo taki sam jak rejestr z Etapu 5 (`plan-implmentacji.md`, "Rejestr narzędzi…"), żeby session-agent mógł go potem przejąć (ewentualnie przeniesienie do `libs/shared` — nie w tym zadaniu).

2. **Rejestr — `services/api/src/api/tools/registry.py`**:
   - `ToolRegistry(tools: Iterable[Tool], *, timeout_s: float, max_output_chars: int)`; zduplikowana nazwa → `ValueError` w konstruktorze.
   - `definitions() -> list[dict]` — `[{"name", "description", "input_schema"}, ...]` w kolejności rejestracji.
   - `async execute(name: str, input: dict) -> ToolResult` — **nigdy nie rzuca** dla błędów narzędzia (rzuca tylko `asyncio.CancelledError`, żeby cancel generacji działał):
     - nieznana nazwa → `is_error`, `"Unknown tool: <name>"`,
     - `ToolInputError` → `is_error`, `"Invalid input: <msg>"`,
     - `asyncio.timeout(timeout_s)` przekroczony → `is_error`, `"Tool timed out after <n>s"`,
     - każdy inny `Exception` → `is_error`, `"Tool failed: <ExceptionType>: <msg>"` + `logger.exception`,
     - `content` dłuższy niż `max_output_chars` → ucięty z dopiskiem `"\n[output truncated: <N> chars total]"`.
   - `build_default_registry() -> ToolRegistry` — rejestruje `CurrentTimeTool()`, parametry z `settings`.

3. **Narzędzie `get_current_time` — `services/api/src/api/tools/current_time.py`**:
   - `CurrentTimeTool(now: Callable[[], datetime] = lambda: datetime.now(UTC))` (zegar wstrzykiwany dla testów).
   - `input_schema`: `{"type": "object", "properties": {"timezone": {"type": "string", "description": "IANA timezone name, e.g. Europe/Warsaw. Defaults to UTC."}}, "additionalProperties": false}`.
   - `description` po angielsku, 1–2 zdania: zwraca bieżącą datę i godzinę; używać, gdy odpowiedź zależy od "teraz".
   - Wynik: `"2026-09-26T14:03:12+02:00 (Saturday, Europe/Warsaw)"` — ISO 8601 z offsetem, dzień tygodnia po angielsku, strefa. Brak `timezone` → UTC. Nieznana strefa (`ZoneInfoNotFoundError`) albo `timezone` nie-string → `ToolInputError`.
   - Dodać `tzdata` do zależności `services/api/pyproject.toml` (obraz `python:*-slim` nie gwarantuje `/usr/share/zoneinfo`).

4. **Konfiguracja (`api/config.py`)**: `chat_tools_enabled: bool = True`, `chat_max_tool_iterations: int = 10`, `tool_timeout_s: float = 30`, `tool_output_max_chars: int = 16000`. (Limit iteracji niższy niż 50 z Etapu 5 — tu narzędzia to tanie odczyty, a każda iteracja to pełne wywołanie modelu; to wentyl kosztowy.)

5. **Rejestr w aplikacji**: `create_app()` ustawia `app.state.tool_registry = build_default_registry()` (jak `session_task_manager` — bez async setupu, dostępne w testach bez lifespan). `routes.py`: dependency `get_tool_registry(request)`, przekazywane do `run_generation`.

6. **Parser strumienia — nowy moduł `services/api/src/api/chat/anthropic_stream.py`** (czysta klasa, bez HTTP i DB):
   - `TurnAccumulator.feed(event_type: str, data: dict) -> list[TurnEvent]`, gdzie `TurnEvent` to `TextDelta(text)` albo `ToolUseComplete(id, name, input)`.
   - Obsługuje: `content_block_start` (`text` / `tool_use` z `id`, `name`), `content_block_delta` (`text_delta` → dopisuje do bloku i zwraca `TextDelta`; `input_json_delta` → dokleja `partial_json` do bufora bloku), `content_block_stop` (dla `tool_use`: `json.loads` bufora, pusty bufor → `{}`; zwraca `ToolUseComplete`), `message_delta` (`stop_reason`), `error` (`error_message`).
   - Właściwości po strumieniu: `content_blocks: list[dict]` (bloki w kolejności indeksów, w formacie Anthropic do zapisu: `{"type":"text","text"}` / `{"type":"tool_use","id","name","input"}`), `stop_reason: str | None`, `error_message: str | None`.
   - `tool_use` bez `content_block_stop` (ucięty strumień / `max_tokens`) **nie trafia** do `content_blocks`. `tool_use` z niepoprawnym JSON-em → trafia z `input={}` i flagą `input_error` (patrz pkt 7 — dostaje `tool_result` z błędem, bez wykonywania).
   - Puste bloki `text` pomijane w `content_blocks`.

7. **Pętla — `run_generation` w `streaming.py`** (rozbić na małe funkcje, np. `_stream_turn`, `_run_tools`; logika HTTP→SSE z obecnej wersji przechodzi do `TurnAccumulator`):
   - Sygnatura: `assistant_sequence_number` → `first_sequence_number: int` (pierwszy wolny numer po wiadomości usera); kolejne wiadomości dostają kolejne numery. Nowy argument `tools: ToolRegistry`.
   - Payload: jak dziś + `"tools": registry.definitions()` tylko gdy `settings.chat_tools_enabled` i rejestr niepusty (inaczej klucza `tools` nie ma — zero regresji dla czystego czatu).
   - Iteracja (max `chat_max_tool_iterations` wywołań modelu z wykonaniem narzędzi):
     1. strumieniuj turę; `TextDelta` → `manager.publish_delta`; `ToolUseComplete` → `manager.publish_event("tool_call", {"id", "name", "input"})`,
     2. zapisz wiadomość `assistant` z `accumulator.content_blocks` (jeśli niepusta),
     3. jeśli `stop_reason != "tool_use"` albo brak bloków `tool_use` → koniec (`done`),
     4. dla każdego `tool_use` **sekwencyjnie**: wstaw `ToolCallEvent(session_id, message_id=<id wiadomości z kroku 2>, tool_name, input, status="running")`, `registry.execute(...)` (dla `input_error` — bez wykonania, `ToolResult("Invalid tool input: malformed JSON", is_error=True)`), zaktualizuj event: `status="succeeded"|"failed"` (wg `is_error`), `output={"content", "is_error"}`, `completed_at=now()`; `manager.publish_event("tool_result", {"id", "content", "is_error"})`,
     5. zapisz wiadomość `user` z blokami `{"type":"tool_result","tool_use_id","content","is_error"}` (wszystkie wyniki tej tury w jednej wiadomości), dopisz obie wiadomości do lokalnej historii i wróć do 1.
   - Po wyczerpaniu limitu (wyniki ostatniej tury już zapisane, kolejne wywołanie modelu się nie odbywa) → `error`: `"Tool iteration limit reached (<n>)"`.
   - **Niezmiennik historii: każdy zapisany `tool_use` ma odpowiadający mu zapisany `tool_result` w następnej wiadomości.** Stąd:
     - cancel / błąd sieci / `error` z upstreamu **w trakcie strumienia** → zapisz tylko bloki `text` z tej tury (ukończone `tool_use` odrzucone), zwróć komunikat błędu jak dziś,
     - cancel **w trakcie wykonywania narzędzi** → bieżące i jeszcze niewykonane `tool_use` dostają syntetyczny `tool_result` `"Tool execution cancelled"` z `is_error: true` (eventy: `failed`), wiadomość `user` z wynikami zapisana, potem `"Generation cancelled"`. Zapis w `finally`/obsłudze `CancelledError` bez ponownego `await` na anulowanym zadaniu narzędzia.
   - Wiadomość w bazie zapisywana **przed** publikacją kolejnego etapu (trwałość = baza, zgodnie z Etapem 5).

8. **`SessionTaskManager`** — replay dla późnych subskrybentów musi obejmować narzędzia:
   - `accumulated_text` → `replay: list[bytes]` (ramki SSE w kolejności); kolejne `delta` sklejane w ostatnią ramkę `delta`, jeśli poprzednia też była `delta` (replay nie rośnie liniowo z liczbą tokenów).
   - Nowa metoda `publish_event(session_id, event: str, data: dict)` — dopisuje do `replay` i rozsyła; `publish_delta` zostaje (sklejanie).
   - `subscribe` wrzuca do nowej kolejki cały `replay`.

9. **Historia wysyłana do modelu — nowy moduł `services/api/src/api/chat/history.py`**: `build_llm_messages(messages: Sequence[Message]) -> list[dict]`, używana w `post_message` zamiast obecnej list comprehension:
   - filtruje do ról `user`/`assistant` (jak dziś),
   - **scala kolejne wiadomości o tej samej roli** w jedną (konkatenacja `content`) — dziś da się to wywołać, gdy generacja padła bez tekstu (user, user), a po limicie iteracji historia kończy się `user(tool_result)` przed nowym `user(text)`,
   - **naprawa osieroconych `tool_use`**: jeśli wiadomość `assistant` ma `tool_use`, którego `id` nie występuje jako `tool_use_id` w następnej wiadomości `user` (np. proces `api` padł między zapisami), dokleja na początek tej wiadomości `user` (albo tworzy ją) syntetyczny `tool_result` `"Tool execution was interrupted"`, `is_error: true`. Bloki `tool_result` muszą stać przed tekstem w wiadomości `user`.

10. **Kontrakt SSE do przeglądarki** (docstring w `streaming.py`): `delta {text}` (bez zmian), nowe `tool_call {id, name, input}` i `tool_result {id, content, is_error}`, `done {}` / `error {message}` (bez zmian).

11. **GUI**:
    - `client.ts`: `ContentBlock` rozszerzony o pola `tool_use`/`tool_result` (`id`, `name`, `input`, `tool_use_id`, `content`, `is_error`). `consumeEventStream`, `sendMessage`, `attachToStream` przyjmują obiekt handlerów `{ onDelta, onToolCall, onToolResult }` zamiast pojedynczego `onDelta`.
    - `ChatView.tsx`: `DisplayMessage` dostaje `toolCalls: { id, name, input, result?: string, isError?: boolean }[]`. Mapowanie historii: `tool_use` z wiadomości `assistant` → `toolCalls`; wiadomość `user` złożona wyłącznie z `tool_result` **nie jest osobnym dymkiem** — jej wyniki dopinane do `toolCalls` poprzedzającego dymka asystenta po `tool_use_id`; kolejne wiadomości `assistant` rozdzielone tylko takimi wiadomościami scalane w jeden dymek (jeden dymek na turę usera, tak jak w trakcie streamu). Podczas streamu `onToolCall`/`onToolResult` aktualizują `toolCalls` bieżącego dymka.
    - Render: każde wywołanie jako zwinięty `<details>` nad/pod tekstem w kolejności wystąpienia (`summary`: nazwa narzędzia + stan: running / ok / error; w środku `input` jako sformatowany JSON i wynik). Stan błędu wizualnie odróżniony. Minimalne style w `index.css`, w konwencji istniejących klas.
    - `registerFirstMessage` dalej bierze pierwszą wiadomość usera **z tekstem** (bez zmian w logice, ale sprawdzić, że wiadomości z `tool_result` jej nie łapią).

12. **Dokumentacja planu**: w `plan-implmentacji.md`, sekcja Etap 5 "Rejestr narzędzi…", dopisać jedno zdanie: narzędzia poza sandboxem (np. `get_current_time`, `web_search`) wykonuje pętla w `api` (`api/tools/`), session-agent przejmie narzędzia sandboxowe pod tym samym kształtem `Tool`.

## Poza zakresem

- Tryb `ask`, statusy `proposed/approved/rejected`, endpoint decyzji, komponent tak/nie w GUI — osobne zadanie. **Do tego czasu `approval_mode` sesji (domyślnie `"ask"`) jest ignorowany — wszystko działa jak `auto`.** Dopuszczalne tylko dlatego, że jedyne narzędzie jest read-only i bez skutków ubocznych; zadanie z zatwierdzaniem musi to zmienić, zanim dojdzie jakiekolwiek narzędzie z efektami.
- `web_search` jako `Tool` (klient HTTP `api` → `web-agent`) — po `narzedzie-web-search.md`.
- Narzędzia sandboxowe (Bash/Read/Write), session-agent, control-plane.
- Równoległe wykonywanie narzędzi, `tool_choice`, strumieniowanie częściowego `input_json` do GUI, prompt systemowy, kompaktowanie kontekstu.
- Zmiana etykiety "Claude" → "Model" w GUI (`dodac-toeknizer.md` pkt 3 — osobno).
- Testy automatyczne — krok 4 dev-flow (kryteria niżej są dla nich źródłem).

## Kryteria akceptacji

- Payload do `llm-proxy` (mock `httpx.MockTransport`) zawiera `tools` z definicją `get_current_time`; przy `chat_tools_enabled=False` klucza `tools` nie ma.
- Pełny cykl (mock zwraca kolejno: turę z `tool_use` `get_current_time` `{"timezone":"Europe/Warsaw"}` z `partial_json` pociętym na ≥2 fragmenty i `stop_reason: "tool_use"`, potem turę tekstową `end_turn`):
  - `llm-proxy` wywołane dokładnie 2 razy; drugie `messages` kończy się `assistant[tool_use id=X]` + `user[tool_result tool_use_id=X]`, a treść wyniku zawiera `Europe/Warsaw`,
  - w `messages` sesji: `user(text)` seq 1, `assistant(tool_use)` seq 2, `user(tool_result)` seq 3, `assistant(text)` seq 4,
  - jeden wiersz `tool_call_events`: `status="succeeded"`, `input == {"timezone": "Europe/Warsaw"}`, `message_id` = id wiadomości seq 2, `output.is_error == false`, `completed_at` ustawione,
  - kolejność eventów SSE u klienta: `tool_call`, `tool_result`, `delta`…, `done`.
- Nieznane narzędzie w `tool_use` → `tool_result` z `is_error: true` i `"Unknown tool"`, pętla idzie dalej, event `failed`, generacja kończy się `done`.
- `get_current_time` z `timezone: "Mars/Olympus"` → `is_error: true`; narzędzie rzucające wyjątek → `is_error: true` z typem wyjątku, bez 500; narzędzie przekraczające `tool_timeout_s` → `is_error: true`, `"timed out"`.
- `tool_use` z niepoprawnym JSON-em wejścia → zapisany z `input={}`, `tool_result` z błędem, narzędzie **nie** zostało wykonane.
- Wynik dłuższy niż `tool_output_max_chars` → ucięty, z dopiskiem o długości.
- Mock zawsze zwraca `tool_use` → dokładnie `chat_max_tool_iterations` wywołań `llm-proxy`, terminalny event `error` z `"Tool iteration limit reached"`, ostatnia zapisana wiadomość to `user(tool_result)`; kolejny `POST /messages` działa i wysłana historia nie ma dwóch sąsiednich wiadomości o tej samej roli.
- Cancel w trakcie wolnego narzędzia → zapisany `assistant(tool_use)` ma w następnej wiadomości `tool_result` `"Tool execution cancelled"` (`is_error: true`), event `failed`, terminalny `error` = `"Generation cancelled"`.
- Cancel / zerwanie strumienia w trakcie tury z ukończonym już `tool_use` → w bazie tylko tekst z tej tury, żadnego `tool_use` bez wyniku.
- `build_llm_messages`: `[user, user]` → jedna wiadomość `user` z połączonym `content`; `assistant` z `tool_use id=A` bez wyniku w następnej wiadomości → dodany syntetyczny `tool_result` A przed tekstem usera.
- Późny subskrybent (`GET /sessions/{id}/stream`) dołączający po wykonaniu narzędzia dostaje w replayu `tool_call`, `tool_result` i dotychczasowy tekst w tej kolejności; kilkaset delt tekstu przed narzędziem daje w replayu jedną ramkę `delta`.
- `CurrentTimeTool` z zamrożonym zegarem `2026-09-26T12:03:12Z`: `{"timezone":"Europe/Warsaw"}` → `"2026-09-26T14:03:12+02:00 (Saturday, Europe/Warsaw)"`; `{}` → wynik w UTC (`+00:00`).
- `ToolRegistry` z dwoma narzędziami o tej samej nazwie → `ValueError`.
- Czysty tekst (mock bez `tool_use`) → jedno wywołanie `llm-proxy`, jedna wiadomość `assistant`, te same eventy SSE co dziś; istniejące testy `services/api/tests` przechodzą bez zmian w asercjach (poza dostosowaniem do nowej sygnatury `run_generation`, jeśli jakiś test woła ją bezpośrednio).
- GUI: `npm run build` i lint przechodzą.
- Ręcznie (compose, `chat_model` = `claude-sonnet-5`): "Która jest teraz godzina w Tokio?" → w dymku pojawia się zwinięte wywołanie `get_current_time` (input z `Asia/Tokyo`, wynik), potem odpowiedź tekstowa z tą godziną; po odświeżeniu strony historia wygląda tak samo, bez pustego dymka usera; druga karta otwarta w trakcie widzi wywołanie narzędzia. W `tool_call_events` wiersz `succeeded`.
- Ręcznie (`chat_model` = `local-model`): to samo pytanie — tor danych działa (blok `tool_use` → wynik → kolejna tura); 0.5B może nie wywołać narzędzia albo odpowiedzieć słabo — kryterium to brak błędów i poprawna historia, nie jakość.

## Notatki implementacyjne

- `api` rozmawia z `llm-proxy` formatem Anthropic Messages — `tool_use`/`tool_result` zapisujemy w `messages.content` 1:1 jako bloki Anthropic (jak zakłada schemat), bez własnego formatu pośredniego. Provider-agnostyczność zapewnia `llm-proxy` (`OpenAIBackend` tłumaczy te bloki), `api` nie wie, jaki backend odpowiada.
- `id` bloku `tool_use` przychodzi od modelu/backendu — nie generować własnego; ta sama wartość wraca jako `tool_use_id`.
- `llm-proxy` liczy `token_usage` per wywołanie, więc tura z narzędziem daje kilka wierszy — `GET /sessions/{id}/usage` sumuje je bez zmian.
- `ToolCallEvent` jest modelem `api` (`api/db/models`), nie `shared` — zostaje tam; `api` jest jedynym zapisującym.
- Publikacja `tool_call` po `content_block_stop`, nie po `content_block_start` — GUI dostaje od razu kompletne `input`.
- Nowe narzędzie w przyszłości = nowa klasa spełniająca `Tool` + wpis w `build_default_registry`; żadnych zmian w pętli.
- Kod, komentarze, `description` narzędzi i komunikaty błędów po angielsku (konwencja repo), plan po polsku.
