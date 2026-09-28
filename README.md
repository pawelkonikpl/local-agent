# local-agent

Samohostowana, wieloużytkownikowa platforma webowa do czatu z Claude, gdzie każda sesja czatu dostaje
własny, izolowany sandbox (kontener Podman rootless). Pełny plan wieloetapowy: patrz zapisany plan
architektoniczny; szczegóły Etapu 1 w `.github/task/etap-1-szkielet.md`, Etapu 2 w
`.github/task/etap-2-czat.md`.

## Wymagania

- [uv](https://docs.astral.sh/uv/) (Python 3.12+, workspace)
- Node 22+ i npm (GUI)
- Podman + `podman-compose`

## Dev workflow

```bash
cp .env.example .env   # i uzupełnij SEED_ADMIN_PASSWORD, ANTHROPIC_API_KEY, INTERNAL_PROXY_TOKEN

# Postgres
podman-compose up -d db

# Backend: instalacja (api, llm-proxy, libs/shared przez workspace uv), migracje, seed admina
uv sync
cd services/api && uv run alembic upgrade head && cd ../..
uv run python scripts/seed_admin.py

# Backend: serwer dev — api
uv run uvicorn api.main:app --reload --app-dir services/api/src

# Backend: serwer dev — llm-proxy (osobny terminal; port 8081 zgodny z domyślnym LLM_PROXY_URL w .env.example)
uv run uvicorn llm_proxy.main:app --reload --app-dir services/llm-proxy/src --port 8081

# Testy (wymaga DATABASE_URL wskazującego na tymczasową/testową bazę — patrz uwaga niżej)
uv run pytest

# GUI: serwer dev (proxy do API na localhost:8000, konfigurowalne przez VITE_API_PROXY_TARGET)
cd gui && npm install && npm run dev
```

`api` mówi do `llm-proxy` przez `LLM_PROXY_URL` + `INTERNAL_PROXY_TOKEN` (prosty współdzielony sekret w env,
nie w Postgresie); `llm-proxy` ma własny, bezpośredni dostęp do Postgresa (`DATABASE_URL`) i jest jedynym
serwisem, który zna prawdziwy `ANTHROPIC_API_KEY`. `llm-proxy` sam jest provider-agnostic: routing po polu
`model` w requeście — `claude-*` idzie do Anthropica, `local-model` do serwisu `local-model` (patrz niżej),
cokolwiek innego do prawdziwego OpenAI (`OPENAI_API_KEY`, opcjonalne). `api` zawsze woła `llm-proxy`,
niezależnie od tego, który provider faktycznie obsłuży dany request.

Migracje Alembic muszą być odpalane z `services/api/` (tam leży `alembic.ini`) — `uv run` z workspace
resolvuje środowisko, ale nie zmienia katalogu roboczego.

### Testy

`uv run pytest` łączy się z bazą wskazaną w `DATABASE_URL` i **tworzy/usuwa w niej cały schemat** przy
starcie/końcu sesji testowej — to musi być dedykowana, tymczasowa baza (np. `local_agent_test`), nigdy
baza deweloperska z realnymi danymi.

## Cały stack przez podman-compose

```bash
podman-compose up -d db
cd services/api && uv run alembic upgrade head && cd ../..
uv run python scripts/seed_admin.py
podman-compose build api llm-proxy gui
podman-compose up -d api llm-proxy gui
```

API: http://localhost:8000, GUI: http://localhost:8080 (nginx serwuje statyczny build i przekazuje
`/auth/*`, `/admin/*` do `api`).

## Struktura repo

```
services/api/         FastAPI: auth, RBAC, admin, chat (sesje/wiadomości/SSE), DB models, migracje Alembic
services/llm-proxy/   FastAPI: metered streaming forwarder, provider-agnostic (Anthropic/OpenAI/local-model),
                      własny dostęp do Postgresa
libs/shared/          Wspólne definicje tabel SQLAlchemy (sessions, messages, token_usage, user_usage_*)
gui/                  React + Vite: login -> dashboard -> czat
deploy/               Containerfile.{api,llm-proxy,gui,local-model}, konfiguracja nginx; local-model to
                      llama-server z llama.cpp (bez własnego kodu): mały CPU-only model GGUF
                      (Qwen3.5-4B Q4_K_M) do dev/testów, jeden z providerów llm-proxy
scripts/             seed_admin.py
```
