# Etap 1 — Szkielet (auth, RBAC, schemat DB, compose)

## Kontekst

Repo `local-agent`: samohostowana, wieloużytkownikowa platforma webowa do czatu z Claude, gdzie każda sesja czatu dostaje własny izolowany sandbox (kontener Podman rootless). Pełny plan wieloetapowy: `~/.claude/plans/pure-rolling-raven.md`. Ten plik dotyczy tylko Etapu 1.

Zdecydowane wcześniej (nie do renegocjacji bez wyraźnego powodu): własna pętla agenta na surowym Anthropic Messages API (przyjdzie w Etapie 5), Postgres od dnia pierwszego, podman-compose, control-plane sesji przez Unix socket (Etap 4+), GUI↔api przez SSE (Etap 2+), konta tylko przez admina (brak publicznej rejestracji), fork sesji klonuje workspace (Etap 7).

## Zakres Etapu 1

1. FastAPI `create_app()` w `services/api/src/api/main.py` — montuje `auth` i `admin` routery. Startup sprawdza tylko połączenie z DB (bez migracji in-process).
2. Auth: argon2-cffi, sesje po stronie serwera (tabela `auth_sessions`, cookie `la_session` = `secrets.token_urlsafe(32)`, w DB tylko `sha256(token)`), rehash przy zmianie parametrów hasha. Cookie `HttpOnly`, `Secure` w prod, `SameSite=Lax`. Endpointy: `POST /auth/login`, `POST /auth/logout`, `GET /auth/me`.
3. RBAC: `deps.require_role("admin")` nad `get_current_user`, role `admin`/`user` w kolumnie `role` na `users` (bez osobnej tabeli uprawnień).
4. Provisioning: `scripts/seed_admin.py` (idempotentny, z env `SEED_ADMIN_EMAIL`/`SEED_ADMIN_PASSWORD`), `POST /admin/users` (admin-only) do tworzenia kolejnych kont.
5. Alembic (`services/api/src/api/db/migrations/`, `alembic.ini` czyta `DATABASE_URL`): pierwsza migracja tworzy **cały** schemat: `users`, `auth_sessions`, `sessions` (z `forked_from_session_id`/`forked_from_message_id` nullable), `messages`, `tool_call_events`, `token_usage` (z `provider`/`token_source` — patrz notatki), `user_usage_limits`, `user_usage_counters`. **Nie** tworzyć teraz: `context_compactions` (Etap 5), `audit_log`, `provider_api_keys` (Etap 8) — czysto addytywne później. **Bez tabeli tokenów proxy** — patrz notatki.
6. `podman-compose.yml`: usługi `db` (postgres:16-alpine, healthcheck), `api`, `gui`; sieci `core`/`edge`. `llm-proxy` i `sandbox-manager` dochodzą w Etapach 2 i 4 — nie tworzyć teraz.
7. Dev workflow: `uv` workspace w root `pyproject.toml`, `uv sync`, `uv run alembic upgrade head`, `uv run python scripts/seed_admin.py`, `uv run uvicorn api.main:app --reload --app-dir services/api/src`, `uv run pytest`.
8. `gui/`: szkielet Vite + React + TS, ekran login → dashboard (pobiera `GET /auth/me`), serwowany w prod przez nginx z reverse-proxy do `api`.

Poza zakresem (kolejne etapy): czat/SSE (2), wiele sesji/task manager (3), sandbox/Podman (4), narzędzia/zatwierdzanie/kompaktowanie (5), monitoring (6), historia/fork/export (7), hardening/audit/rate-limity (8).

## Kryteria akceptacji

- `podman-compose up -d db` → kontener `db` healthy.
- `uv run alembic upgrade head` nakłada pierwszą migrację bez błędów; `alembic downgrade base` czyści schemat bez błędów.
- `uv run python scripts/seed_admin.py` tworzy admina; drugie uruchomienie jest no-op (idempotentny).
- `POST /auth/login` z poprawnymi danymi zwraca `Set-Cookie` i dane użytkownika; ze złymi danymi → 401.
- `GET /auth/me` z cookie zwraca zalogowanego użytkownika; bez cookie / z unieważnionym cookie → 401.
- `POST /auth/logout` unieważnia sesję (kolejne `GET /auth/me` z tym samym cookie → 401).
- Endpoint admin-only (`POST /admin/users`) zwraca 403 dla roli `user`, 201 dla `admin`.
- `uv run pytest` zielone: hash/rehash argon2, tworzenie/wygasanie/rewokacja `auth_sessions`, testy integracyjne endpointów na tymczasowej bazie Postgres.
- `podman-compose up -d api gui` startuje; GUI: ekran login, po zalogowaniu przekierowanie do dashboardu pokazującego e-mail/rolę z `/auth/me`.

## Notatki implementacyjne

- Pełne DDL kolumn/typów/CHECK/indeksów nie było wcześniej spisane w transkrypcie — zaprojektowane od zera przy implementacji, zgodnie z opisem tabel w sekcji "Schemat Postgres" planu głównego.
- `sessions.forked_from_message_id` odwołuje się do `messages`, które powstaje w tej samej migracji po `sessions` — FK dodany przez `ALTER TABLE` po utworzeniu `messages` (`use_alter`), żeby uniknąć cyklicznej zależności przy tworzeniu tabel.
- GUI używa npm (nie pnpm — pnpm nie jest zainstalowany na hoście, plan dopuszczał ten fallback).
- **Korekta (ustalona z userem przy implementacji Etapu 1):** `token_usage` ma od razu kolumny `provider` (`'anthropic'`/`'local'`) i `token_source` (`'api_reported'`/`'computed'`) — architektura ma w przyszłości wspierać też lokalny (self-hosted) model LLM obok Anthropic, a dla takiego backendu nie ma odpowiedzi z blokiem `usage`, tokeny trzeba liczyć samemu po stronie klienta. Dodane teraz, żeby Etap 2 (llm-proxy) nie wymagał migracji przy dołączeniu drugiego providera.
- **Korekta (ustalona z userem przy implementacji Etapu 1):** usunięto tabelę `llm_session_tokens` z planu głównego — user nie chce trzymać tokenu proxy w Postgresie, nawet jako hash. Token proxy (Etap 4/5) będzie: podpisany (HMAC/JWT z `session_id`+`user_id`+wygasaniem, kluczem znanym tylko `api` i `llm-proxy`), dostarczany do kontenera sesji przez `podman secret` (albo env jako fallback), weryfikowany bezstanowo przez `llm-proxy` — bez zapisu w DB; rewokacja przez sprawdzenie `sessions.status` w bezpośrednim dostępie `llm-proxy` do Postgresa, nie przez dedykowaną tabelę tokenów.
