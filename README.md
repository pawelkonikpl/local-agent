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

# Backend: instalacja (serwisy i libs/* przez workspace uv), migracje, seed admina
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
`model` w requeście — `claude-*` idzie do Anthropica, identyfikatory pasujące do `OPENAI_MODELS_PATTERN` do
prawdziwego OpenAI (`OPENAI_API_KEY`, opcjonalne); każdy backend sam mówi (`serves(model)`), które modele obsługuje. `api` zawsze woła `llm-proxy`,
niezależnie od tego, który provider faktycznie obsłuży dany request. Listę modeli do wyboru w czacie
(i dostępne poziomy reasoningu) `llm-proxy` bierze od samych providerów — każdy backend ma metodę
`list_models()` (Anthropic: `GET /v1/models` z `capabilities.effort`, OpenAI: `models.list()` filtrowane
przez `OPENAI_MODELS_PATTERN`) — i wystawia ją jako `GET /v1/models` (cache `MODELS_CACHE_TTL_S`, domyślnie 10 min).

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

### `site_search`: wyszukiwanie w sklepie

Narzędzie `site_search` szuka bezpośrednio w wyszukiwarce sklepu (dziś `allegro.pl`) i zwraca oferty
z cenami, także posortowane po cenie. Allegro odrzuca przeglądarkę headless, więc obsługuje je kontener
`site-agent`: prawdziwy Google Chrome z oknem na wirtualnym ekranie (Xvfb), sterowany przez web-agent
w trybie „tylko sklepy”. Tylko linux/amd64 (Google Chrome nie ma wersji arm64).

```bash
podman-compose up -d site-agent site-vnc
```

- Pierwsze zapytanie po starcie trwa kilka sekund dłużej: Allegro sprawdza przeglądarkę niewidocznym
  testem, a web-agent czeka, aż strona sama się przeładuje. Profil przeglądarki leży na tmpfs, więc po
  restarcie kontenera test powtarza się.
- Gdyby Allegro pokazało prawdziwą captchę, model poprosi Cię o jej rozwiązanie na ekranie przeglądarki:
  http://localhost:6080/vnc.html (tylko z tego komputera). Agent nigdy nie klika w weryfikację sam.
- `site-agent` jest tylko w sieci `sites` (z `api`, `egress-proxy`, `site-vnc`): headless `web-agent`
  go nie widzi. Ruch Chrome'a idzie przez listę dozwolonych domen `egress-proxy`.
- Po zmianie sieci albo `allowlist.txt` `podman-compose up -d` nie odtwarza istniejących kontenerów:
  usuń je (`podman rm -f ...`) i uruchom ponownie.
- Ręczny test bez czatu:
  `podman exec -w /app/services/web-agent local-agent_site-agent_1 web-agent site allegro.pl "raspberry pi 4" --sort price_asc --cdp-url http://127.0.0.1:9222`
  (`--save-html /tmp/plik.html` zapisuje stronę, np. jako fixture do testów).

### `plot_search` i `plot_details`: działki z portali nieruchomości

Narzędzie `plot_search` szuka działek na sprzedaż w jednej miejscowości na jednym portalu
(nieruchomosci-online.pl, otodom.pl, olx.pl, adresowo.pl, nehnutelnosti.sk) i zwraca ceny, powierzchnie,
zł/m², typ działki i daty; `plot_details` otwiera jedno ogłoszenie z wyniku i czyta jego tabelę parametrów
(opisu nie czyta). Obsługuje je ten sam kontener `site-agent` co `site_search`; lista portali:
`WEB_AGENT_PORTALS`. Ręczny test bez czatu:
`podman exec -w /app/services/web-agent local-agent_site-agent_1 web-agent listings nehnutelnosti.sk "Oravská Lesná" --max-price 68800 --cdp-url http://127.0.0.1:9222`
(`web-agent listing <portal> <url>` dla jednego ogłoszenia, `--save-html` zapisuje stronę).

## Struktura repo

```
services/api/         FastAPI: auth, RBAC, admin, chat (sesje/wiadomości/SSE), DB models, migracje Alembic
services/llm-proxy/   FastAPI: metered streaming forwarder, provider-agnostic (Anthropic/OpenAI),
                      własny dostęp do Postgresa
services/web-agent/   FastAPI + Playwright/CDP: wyszukiwarka dla narzędzi web_search, site_search i plot_search
libs/shared/          Wspólne definicje tabel SQLAlchemy (sessions, messages, token_usage, user_usage_*)
libs/contracts/       Kontrakty między serwisami (tylko pydantic): ramki SSE, auth bearer, API llm-proxy
                      i web-agent — obie strony importują te same modele zamiast trzymać kopie
gui/                  React + Vite: login -> dashboard -> czat
deploy/               Containerfile.{api,llm-proxy,gui,...}, konfiguracja nginx
scripts/             seed_admin.py
```
