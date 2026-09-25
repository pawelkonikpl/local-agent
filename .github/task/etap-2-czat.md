# Etap 2 — Czat (llm-proxy, streaming SSE, sesje czatu)

## Kontekst

Repo `local-agent`: samohostowana, wieloużytkownikowa platforma webowa do czatu z Claude, gdzie każda sesja czatu dostaje własny izolowany sandbox (kontener Podman rootless). Pełny plan wieloetapowy: `~/.claude/plans/pure-rolling-raven.md`. Ten plik dotyczy tylko Etapu 2.

Etap 1 (`.github/task/etap-1-szkielet.md`) jest zaimplementowany: `services/api` (FastAPI, auth, RBAC, Alembic z pełnym schematem — w tym `sessions`, `messages`, `token_usage` z kolumnami `provider`/`token_source`, `user_usage_limits`/`user_usage_counters`), `gui` (Vite+React, login→dashboard).

Zdecydowane wcześniej (nie do renegocjacji bez wyraźnego powodu): GUI↔api przez SSE (od tego etapu), Postgres od dnia pierwszego, brak tokenów proxy/sesyjnych trzymanych w Postgresie (nawet jako hash) — patrz `no-secrets-in-postgres`. Na tym etapie **nie ma jeszcze sandboxa** (Etap 4) ani rejestru narzędzi/zatwierdzania (Etap 5) — to czysty streaming czatu tekstowego: `api` rozmawia z `llm-proxy`, `llm-proxy` rozmawia z Anthropic API.

## Zakres Etapu 2

1. `libs/shared/src/shared/`: wspólne definicje tabel SQLAlchemy (`token_usage`, `user_usage_limits`, `user_usage_counters`, `sessions`, `messages`) wydzielone z `services/api/src/api/db/models/`, tak żeby `llm-proxy` mógł ich używać bez duplikowania modeli i bez importowania kodu `api`. `api` pozostaje jedynym właścicielem migracji Alembic — `libs/shared` dostarcza tylko definicje ORM, nie migruje schematu.
2. Nowy serwis `services/llm-proxy/src/llm_proxy/`: `main.py`, `config.py`, `proxy.py`, `metering.py`, `db.py`.
   - Endpoint zgodny z Anthropic Messages API (`POST /v1/messages`, streaming), forwardujący do prawdziwego `api.anthropic.com` prawdziwym kluczem (`ANTHROPIC_API_KEY`, znanym tylko `llm-proxy`).
   - `metering.py`: przed forwardowaniem sprawdza `user_usage_counters` vs `user_usage_limits` dla usera z requestu — odrzuca czytelnym błędem, jeśli budżet przekroczony.
   - Po zakończeniu streamu: zapis wiersza `token_usage` (`provider='anthropic'`, `token_source='api_reported'` z bloku `usage` zwróconego przez Anthropic) i inkrementacja `user_usage_counters`.
   - Własny, bezpośredni dostęp do Postgresa (nie przez `api`) — sprawdzenie budżetu musi być synchroniczne ze streamem.
   - Autoryzacja wywołań `api → llm-proxy` na tym etapie: prosty współdzielony sekret w env (np. nagłówek z tokenem z `INTERNAL_PROXY_TOKEN`), **nie** per-sesyjny podpisany token — ten wzorzec (HMAC/JWT + dostarczanie przez `podman secret`) jest zarezerwowany dla Etapu 4/5, gdy o dostęp do `llm-proxy` będą prosić niezaufane kontenery sesji. Bez zapisu żadnego tokenu w Postgresie.
3. `services/api/src/api/chat/`: `routes.py`, `schemas.py`, `streaming.py`.
   - `POST /sessions` — tworzy sesję czatu (właściciel = zalogowany user).
   - `GET /sessions` — lista sesji zalogowanego użytkownika (tylko własne).
   - `GET /sessions/{id}/messages` — pełna historia wiadomości w kolejności `sequence_number`.
   - `POST /sessions/{id}/messages` — zapisuje wiadomość użytkownika, woła `llm-proxy` przez `httpx.AsyncClient` w trybie stream, przekazuje delty do przeglądarki jako `text/event-stream`, po zakończeniu zapisuje wiadomość asystenta z kolejnym `sequence_number`.
   - RBAC/ownership: user widzi i modyfikuje tylko sesje, których jest właścicielem (dep sprawdzający `sessions.user_id`).
4. `gui/src/features/chat/` (lub `pages/chat`): lista sesji, widok czatu z konsumpcją SSE (odczyt strumienia odpowiedzi z `POST`, nie `EventSource` z GET — bo trzeba wysłać treść wiadomości), renderowanie wiadomości user/assistant, żywe dopisywanie delt tekstu.
5. `podman-compose.yml`: dodanie usługi `llm-proxy` (własny `DATABASE_URL`, `ANTHROPIC_API_KEY`, `INTERNAL_PROXY_TOKEN`); `api` dostaje `LLM_PROXY_URL` + `INTERNAL_PROXY_TOKEN`.
6. Dev workflow: `llm-proxy` i `libs/shared` dochodzą do `[tool.uv.workspace] members` w root `pyproject.toml`; instrukcja uruchomienia `uv run uvicorn llm_proxy.main:app --reload --app-dir services/llm-proxy/src` obok istniejącej dla `api`.

Poza zakresem (kolejne etapy): wiele kart/reconnect do żywego strumienia i `SessionTaskManager` (Etap 3 — na tym etapie odświeżenie w trakcie streamu może dać niekompletną wiadomość, to akceptowalne), sandbox/Podman i realne narzędzia (Etap 4/5), zatwierdzanie tool-call (Etap 5), monitoring/dashboard (Etap 6).

## Kryteria akceptacji

- `POST /sessions` bez cookie → 401; z cookie → 201 i zwraca `id` nowej sesji.
- `GET /sessions` zwraca wyłącznie sesje należące do zalogowanego usera (sesja innego usera nie pojawia się na liście).
- `POST /sessions/{id}/messages` z treścią wiadomości: odpowiedź `text/event-stream` z deltami tekstu kończąca się zdarzeniem zamknięcia; po zakończeniu w `messages` są dwa nowe wiersze (user + assistant) z rosnącym `sequence_number`; w `token_usage` pojawia się nowy wiersz, `user_usage_counters` jest zinkrementowany.
- Przekroczenie `user_usage_limits`: `llm-proxy` odrzuca request czytelnym błędem, żadne zapytanie do Anthropic nie wychodzi, żadna wiadomość asystenta nie jest zapisywana.
- `GET /sessions/{id}/messages` dla cudzej sesji → 403/404 (nie 200 z danymi).
- GUI: zalogowany user otwiera sesję, wysyła wiadomość, widzi odpowiedź renderującą się na żywo token po tokenie; odświeżenie strony poza trwającym streamem pokazuje pełną, spójną historię.
- `uv run pytest`: testy `metering.py` (sprawdzanie limitu, inkrementacja licznika), testy integracyjne `api/chat` na tymczasowej bazie Postgres z zamockowanym/stubowanym `llm-proxy` (brak realnych wywołań do Anthropic w testach).

## Notatki implementacyjne

- Dokładny format zdarzeń SSE między `api` a przeglądarką (nazwy `event:`, kształt `data:` — delta tekstu vs zdarzenie końcowe z `usage`/błędem) nie był wcześniej ustalony — do zaprojektowania przy implementacji, najlepiej w formie, którą da się później bez przepisywania rozszerzyć o `tool_call_proposed`/`tool_call_result` z control-plane sesji (Etap 5).
- Testy `llm-proxy` wymagają fałszywego/zamockowanego upstreamu Anthropic (np. podmieniony transport `httpx`) — brak realnych kluczy/wywołań sieciowych w CI.
- `INTERNAL_PROXY_TOKEN` to zwykły sekret w env/compose, nie trafia do Postgresa — spójne z decyzją z Etapu 1 o nietrzymaniu tokenów proxy w bazie.
