# Narzędzie `web_search` — pierwszy Solver CDP (web-agent)

## Kontekst

Źródło: `.agents/sillks/web-agent/instrukcja-agent-cdp.md` — architektura hybrydowa **Solver** (deterministyczny kod sterujący Chrome przez CDP, zero tokenów) + **Operator** (LLM Vision, wołany tylko gdy potrzebna interpretacja). Pętla Sense → Act → Verify (weryfikacja innym kanałem niż ten, który wykonał akcję) i drabina technik interakcji (szczebel 1: syntetyczny JS/DOM → 2: zaufany `Input.*` → 3: emulacja behawioralna).

To zadanie buduje **pierwsze narzędzie** na tej architekturze: wyszukiwanie w internecie. Świadomie wąsko — tylko szczebel 1 (nawigacja + odczyt DOM), bez Operatora — ale szkielet Solvera (`BrowserSession` z "zmysłami" CDP, Protocol dla wyszukiwarek) ma być bazą pod kolejne narzędzia (`web_fetch`, interakcje).

Stan zweryfikowany w kodzie:
- Nie ma jeszcze rejestru narzędzi ani pętli ich wykonywania — to Etap 5 (`plan-implmentacji.md`, sekcja "Rejestr narzędzi, pętla, zatwierdzanie") i `dodac-toeknizer.md` pkt 4. Warstwa providerów potrafi już przenosić `tool_use`/`tool_result` (`tools-w-providerach.md`).
- Kontener sesji (Etap 4) ma z założenia **jedyne wyjście sieciowe do llm-proxy** — przeglądarka z dostępem do internetu nie może tam żyć. Stąd osobny serwis `web-agent` z własną siecią wychodzącą, odizolowany od `db`/`llm-proxy`, wołany po HTTP przez stronę, która wykonuje narzędzia (w przyszłości `api`/session-agent przez relay).
- Wzorzec serwisu do skopiowania: `services/local-model` (FastAPI, `/health`, bearer `INTERNAL_PROXY_TOKEN`, hardening w `podman-compose.yml`: `read_only`, `cap_drop: ALL`, `user: 10001:10001`, limity).
- Preferencja projektu: abstrakcje przez `Protocol` + konkretna implementacja (jak `LLMBackend` w `llm-proxy/backends/base.py`) — dotyczy tu wyszukiwarek.

## Zakres

1. **Nowy serwis `services/web-agent/`** (pakiet `web_agent`), członek uv workspace (dopisać do `[tool.uv.workspace].members` i `testpaths` w root `pyproject.toml`). Zależności: `playwright`, `fastapi`, `uvicorn[standard]`, `pydantic-settings`. Skrypt `[project.scripts] web-agent = "web_agent.cli:main"`.

2. **Konfiguracja (`config.py`, pydantic-settings, prefiks env `WEB_AGENT_`)** poza `internal_proxy_token` (bez prefiksu, jak w local-model):
   - `cdp_url: str | None = None` — jeśli ustawione, `connect_over_cdp(cdp_url)` do istniejącego Chrome (np. lokalny `--remote-debugging-port=9222` przy debugowaniu); inaczej `chromium.launch(headless=...)`.
   - `headless: bool = True`, `navigation_timeout_s: float = 15`, `search_timeout_s: float = 25`, `max_concurrent_searches: int = 2`, `min_interval_s: float = 1.0` (odstęp między zapytaniami do tej samej wyszukiwarki), `default_region: str = "pl-pl"`, `user_agent: str | None = None` (None = domyślny Playwrighta).

3. **Modele (`models.py`, pydantic)**:
   - `SearchQuery`: `query: str` (1–400 znaków po `strip()`), `max_results: int = 5` (1–10), `region: str | None`.
   - `SearchResult`: `rank: int`, `title: str`, `url: str` (tylko `http`/`https`), `snippet: str`, `domain: str`.
   - `SearchResponse`: `status: Literal["ok", "no_results", "blocked", "error"]`, `engine: str`, `query: str`, `results: list[SearchResult]`, `error: str | None`, `elapsed_ms: int`.

4. **Solver — `browser.py`** (jedyne miejsce, które zna Playwright/CDP):
   - `BrowserManager`: jedna przeglądarka na proces, start leniwy przy pierwszym użyciu (CLI) albo w `lifespan` (serwis), automatyczny relaunch gdy `browser.is_connected()` == False. Chromium z `--disable-dev-shm-usage`; w kontenerze `--no-sandbox` (izolację zapewnia kontener — patrz pkt 9), lokalnie bez tej flagi.
   - `async with manager.session() as s:` → **nowy, czysty `BrowserContext` na każde wyszukiwanie** (brak ciasteczek/stanu między zapytaniami i użytkownikami), zamykany w `finally` także przy timeout/wyjątku.
   - `BrowserSession` = strona + `CDPSession` (`context.new_cdp_session(page)`), z włączonymi domenami `Network` i `Runtime`. "Zmysły":
     - `navigate(url) -> NavigationOutcome` (Act) — `Page.navigate`, czekanie na `load`; **Verify innym kanałem**: status głównego dokumentu z eventu CDP `Network.responseReceived` (`type == "Document"`), nie z wartości zwróconej przez nawigację.
     - `evaluate(js) -> Any` — `Runtime.evaluate` z `returnByValue`, szczebel 1 drabiny.
     - `text_content() -> str` — `document.body.innerText` (do detekcji blokady).
     - `screenshot() -> bytes` — `Page.captureScreenshot`, używane tylko przez CLI `--screenshot` (debug; przyszły kanał dla Operatora).
     - `console_errors: list[str]` — z `Runtime.consoleAPICalled`/`Runtime.exceptionThrown`, dołączane do logów przy `status="error"`.

5. **Wyszukiwarki — `engines/`**:
   - `engines/base.py`: `SearchEngine(Protocol)` z `name: str`, `url_for(query: SearchQuery, region: str) -> str`, `extract_js: str` (JS zwracający surowe `[{title, href, snippet, is_ad}]`), `parse(raw: list[dict], max_results: int) -> list[SearchResult]` (czysta funkcja), `detect_block(status: int | None, text: str) -> str | None` (powód blokady albo None), `is_no_results(text: str) -> bool`.
   - `engines/duckduckgo.py`: `DuckDuckGoEngine` na wersji HTML `https://html.duckduckgo.com/html/?q=<q>&kl=<region>` (bez JS, stabilne selektory: `.result`, `.result__a`, `.result__snippet`, reklamy `.result--ad`). `parse`: dekodowanie linków przekierowujących `//duckduckgo.com/l/?uddg=<url-encoded>` do docelowego URL, odrzucenie reklam i schematów innych niż http(s), deduplikacja po URL (bez fragmentu `#…`), ucięcie do `max_results`, `rank` od 1, `domain` z `urlparse(url).hostname` bez `www.`, whitespace w title/snippet znormalizowany do pojedynczych spacji, snippet ucięty do 300 znaków. `detect_block`: status 403/429/5xx albo znana strona anty-botowa DDG (formularz/komunikat "anomaly"/CAPTCHA) → powód.
   - `engines/__init__.py`: `get_engine(name: str = "duckduckgo") -> SearchEngine`, nieznana nazwa → `ValueError`.

6. **Orkiestracja — `search.py`**: `async def run_search(query: SearchQuery, engine: SearchEngine, manager: BrowserManager) -> SearchResponse` — pętla Sense-Act-Verify:
   1. rate-limit (`asyncio.Semaphore(max_concurrent_searches)` + `min_interval_s` per silnik),
   2. Act: `navigate(url_for(...))`,
   3. Verify: status dokumentu z `Network` → `detect_block` → jeśli blokada: `status="blocked"`, `error=<powód>`, **koniec, bez ponawiania i bez wspinania się po drabinie**,
   4. Sense: `evaluate(extract_js)` → `parse`; pusta lista + `is_no_results` → `no_results`; pusta lista bez znacznika "brak wyników" → `error` ("unexpected page layout" — sygnał, że selektory się zdezaktualizowały),
   5. całość w `asyncio.timeout(search_timeout_s)` → `status="error"`, `error="timeout"`.
   Funkcja **nigdy nie rzuca** dla błędów strony/sieci — zawsze zwraca `SearchResponse` (LLM ma dostać czytelny `tool_result`, nie 500). Rzuca tylko przy błędzie programistycznym.

7. **Definicja narzędzia — `tool.py`**:
   - `WEB_SEARCH_TOOL: dict` w formacie Anthropic `tools=[...]`: `name: "web_search"`, `description` (po angielsku, 2–3 zdania: kiedy używać, że zwraca tytuły/URL/snippety a nie treść stron), `input_schema` = JSON Schema odpowiadające `SearchQuery` (`query` required, `max_results` 1–10, `region` opcjonalny). Schema generowana z `SearchQuery.model_json_schema()` albo pisana ręcznie — ale test pilnuje zgodności.
   - `format_for_llm(response: SearchResponse) -> str` — zwięzła lista numerowana `1. <title>\n   <url>\n   <snippet>`; dla `blocked`/`error`/`no_results` jedno zdanie z powodem. To będzie `content` bloku `tool_result`.
   - Bez podpinania do pętli czatu (patrz "Poza zakresem").

8. **Wejścia**:
   - **CLI (`cli.py`)** — główny sposób ręcznego testowania i "Solver CLI" z instrukcji: `uv run web-agent search "<query>" [--max-results N] [--region pl-pl] [--json] [--headful] [--cdp-url URL] [--screenshot out.png]`. Domyślnie wypisuje `format_for_llm`, z `--json` — `SearchResponse.model_dump_json(indent=2)`. Kod wyjścia 0 dla `ok`/`no_results`, 2 dla `blocked`, 1 dla `error`.
   - **HTTP (`main.py`)** — `create_app()` + `app`, `GET /health`, `POST /v1/search` (body = `SearchQuery`, odpowiedź = `SearchResponse`, bearer `INTERNAL_PROXY_TOKEN` jak `local_model.main._require_bearer_auth`; błąd walidacji → 422, brak/zły token → 401). Status wyszukiwania (`blocked`/`error`) zwracany w body z HTTP 200.

9. **Deploy**:
   - `deploy/Containerfile.web-agent`: `python:3.12-slim`, `pip install` zależności + `playwright install --with-deps chromium` jako root do `PLAYWRIGHT_BROWSERS_PATH=/ms-playwright`, potem `USER 10001:10001`, `COPY services/web-agent/src/web_agent`, `CMD uvicorn web_agent.main:app --port 8080`.
   - `podman-compose.yml`: serwis `web-agent` z hardeningiem jak `local-model` (`read_only`, `tmpfs: /tmp` — profil Chromium ląduje w `/tmp`, `cap_drop: ALL`, `no-new-privileges`, `user: 10001:10001`, `pids_limit: 512`, `mem_limit: 1536m`, `cpus: 2`, healthcheck na `/health`), port dev `8091:8080`. **Nowa sieć `web`** — tylko `web-agent` i `api`; `web-agent` NIE jest w `core`, więc nie widzi `db` ani `llm-proxy`, a ma normalne wyjście do internetu.
   - `.env.example`: dopisać zakomentowane `WEB_AGENT_CDP_URL=` z jednolinijkowym opisem.

## Poza zakresem

- Podpięcie `web_search` do pętli czatu / rejestru narzędzi / zatwierdzania — Etap 5. Konsekwencja projektowa do zapisania tam: `web_search` jest narzędziem wykonywanym **poza sandboxem sesji** (przez `api` → `web-agent`), bo sandbox nie ma internetu.
- Operator (LLM Vision), szczebel 2 i 3 drabiny, sekcja "Anti-Bot & CAPTCHA Cookbook" z instrukcji. Przy blokadzie narzędzie zgłasza `blocked` i kończy. Obchodzenie zabezpieczeń anty-botowych cudzej wyszukiwarki łamie jej regulamin i jest kruche; właściwa odpowiedź na blokady to kolejna implementacja `SearchEngine` (np. własny SearXNG albo oficjalne API wyszukiwarki), nie wspinanie się po drabinie.
- Pobieranie i czytanie treści stron z wyników (`web_fetch`) — osobne, następne narzędzie na tym samym `BrowserSession`.
- Cache wyników, wiele wyszukiwarek naraz, paginacja wyników.
- Testy automatyczne — krok 4 dev-flow (kryteria niżej są dla nich źródłem).

## Kryteria akceptacji

- `DuckDuckGoEngine.parse` (bez przeglądarki): surowe wpisy z linkiem `//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa%3Fb%3D1&rut=...` → `url == "https://example.com/a?b=1"`, `domain == "example.com"`; wpis z `is_ad: true` odrzucony; wpis z `href` `javascript:`/`mailto:` odrzucony; dwa wpisy z tym samym URL różniące się tylko `#fragment` → jeden wynik; 8 poprawnych wpisów i `max_results=5` → 5 wyników z `rank` 1..5.
- `detect_block`: status 429 → powód niepusty; status 200 + tekst strony anty-botowej DDG (fixture) → powód niepusty; status 200 + normalna strona wyników → `None`.
- Fixture'y HTML (`tests/fixtures/ddg_results.html`, `ddg_no_results.html`, `ddg_blocked.html`) — zapisane z prawdziwych odpowiedzi DDG. Z przeglądarką (test oznaczony markerem `browser`, pomijany gdy Chromium nie jest zainstalowany; strona serwowana lokalnie, bez internetu): `extract_js` + `parse` na `ddg_results.html` zwraca ≥5 wyników z niepustym `title` i URL `http(s)`, żaden nie wskazuje na `duckduckgo.com`; `ddg_no_results.html` → `status="no_results"`; `ddg_blocked.html` → `status="blocked"`, `results == []`.
- `run_search` z przekroczonym `search_timeout_s` (np. lokalny serwer, który nie odpowiada) → `status="error"`, `error="timeout"`, bez wyjątku; po 20 kolejnych wyszukiwaniach (w tym timeoutach) liczba otwartych `BrowserContext` == 0.
- `WEB_SEARCH_TOOL["input_schema"]` akceptuje `{"query": "x"}` i odrzuca `{}`, `{"query": ""}`, `{"query": "x", "max_results": 11}` — spójnie z walidacją `SearchQuery`.
- `format_for_llm`: dla `ok` zawiera każdy URL z wyników dokładnie raz; dla `blocked` jedno zdanie z powodem, bez listy.
- HTTP: `POST /v1/search` bez tokenu → 401; z `{"query": ""}` → 422; z poprawnym tokenem i zmockowanym `run_search` → 200 i body zgodne z `SearchResponse`.
- Ręcznie (lokalnie): `uv run playwright install chromium`, potem `uv run web-agent search "python 3.13 release notes" --max-results 5` wypisuje 5 wyników z prawdziwymi URL-ami w < 10 s; `--json` daje poprawny JSON; `--headful` pokazuje okno przeglądarki; `--screenshot /tmp/s.png` zapisuje zrzut.
- Ręcznie (podman-compose): `podman-compose up -d web-agent` → healthy; `curl -H "Authorization: Bearer $INTERNAL_PROXY_TOKEN" -H 'content-type: application/json' -d '{"query":"podman rootless"}' localhost:8091/v1/search` → `status: "ok"`; `podman exec <web-agent> python -c "import socket; socket.create_connection(('db', 5432), 2)"` kończy się błędem (brak dostępu do `core`).
- Istniejące testy (`uv run pytest`) przechodzą bez zmian.

## Notatki implementacyjne

- Playwright jest tu transportem do CDP i menedżerem procesu przeglądarki; "zmysły" idą przez `CDPSession.send(...)`/eventy CDP, żeby kolejne narzędzia (szczebel 2 = `Input.dispatchMouseEvent`, Operator = `Page.captureScreenshot`) dochodziły bez zmiany warstwy.
- Import Playwrighta tylko w `browser.py` (i `cli.py`/`main.py` przez niego). `engines/*`, `models.py`, `tool.py` mają być importowalne i testowalne bez Playwrighta i bez przeglądarki.
- Selektory DDG trzymać w jednym miejscu (stałe na górze `duckduckgo.py`) — to najbardziej kruchy element; stąd rozróżnienie `no_results` vs `error: unexpected page layout`.
- Nie ustawiać sztucznych opóźnień "dla ludzkości" ani fałszywych nagłówków — `min_interval_s` to uprzejmość wobec wyszukiwarki, nie maskowanie.
- Kod i komentarze po angielsku (konwencja repo), plan po polsku.
