
## Kontekst
Projekt jest provider agonostin nie powinie zalezec od jakiegokoleiej dostawcy, Do providerow trzeba uzyc kocentput Protocaol/Abstrac i poziej impemenetacja klasy.
Repo `local-agent` jest obecnie puste (brak commitów, tylko placeholder `main.py`, pusty `README.md`, `pyproject.toml` z jedną zależnością `fastapi`). Budujemy od zera samohostowaną, wieloużytkownikową platformę webową do czatu z Claude, gdzie każda sesja czatu dostaje własny, izolowany sandbox (kontener Podman rootless) z narzędziami agenta (Bash/Read/Write). Cel: bezpieczne środowisko dla wieluuserów współdzielących jeden klucz Anthropic, z zatwierdzaniem akcji narzędzi, monitoringiem kosztów i historią sesji.

Zdecydowane wcześniej z userem (nie do renegocjacji bez wyraźnego powodu):
- **Pętla agenta: B2** — własna pętla na surowym Anthropic Messages API (bez Agent SDK), żeby mieć pełną kontrolę nad narzędziami, zatwierdzaniem i przyszłą wielodostawczością.
- **Postgres od dnia pierwszego** (nie SQLite).
- **podman-compose** (nie pojedynczy pod).
- **Control-plane kontenera sesji: Opcja B** — Unix domain socket na wolumenie współdzielonym wyłącznie z sandbox-managerem, żaden port sieciowy wchodzący do kontenera sesji. Sieć kontenera sesji ogranicza się faktycznie do jednego wychodzącego połączenia: do llm-proxy.
- **GUI ↔ api: SSE** (POST obok strumienia dla akcji: zatwierdzanie/odrzucanie narzędzia, stop).
- **Zakładanie kont: tylko admin** (brak publicznej rejestracji).
- **Fork sesji (Etap 7): klonuje też workspace** (pliki), nie tylko transkrypt.

Drobne decyzje przyjęte z rekomendowanym defaultem (niskie ryzyko pomyłki, można zrewidować przy danym etapie bez przepisywania wcześniejszej pracy):
- sandbox-manager jest bezstanowy (bez własnego Postgresa), rekoncyliacja osieroconych kontenerów przez label + zapytanie do api o listę ważnych session_id.
- Task-queue/pub-sub: in-process `asyncio` + Postgres `LISTEN/NOTIFY` zamiast Redis, dopóki `api` nie musi skalować się poziomo.
- Duży output narzędzi: inline `jsonb` z ucięciem, bez zewnętrznego blob storage w v1.
- Zatwierdzanie: jeden tryb per sesja (ask/auto), bez polityki per-typ-narzędzia w v1.
- Kompaktowanie kontekstu: próg 80% okna kontekstu, okno przesuwne 10–20 ostatnich wiadomości, podsumowanie widoczne w GUI jako zwinięta notatka systemowa.
- GUI: pnpm (albo npm, jeśli user wolałby nie dokładać narzędzia).

## Układ repo (docelowy, budowany przyrostowo etap po etapie)

```
local-agent/
  podman-compose.yml
  deploy/Containerfile.{api,gui,llm-proxy,sandbox-manager,session-agent}
  docs/architecture.md, docs/security-testing.md (Etap 8)
  services/
    api/src/api/            # FastAPI: main.py, config.py, deps.py, db/models/, db/migrations/ (Alembic),
                             # auth/, admin/, chat/ (Etap 2+), sandbox_client.py (Etap 4+), monitoring/ (Etap 6)
    llm-proxy/src/llm_proxy/  # main.py, auth.py, proxy.py, metering.py, db.py — własny dostęp do Postgresa
    sandbox-manager/src/sandbox_manager/  # podman_client.py, containers.py, control_relay.py, cleanup.py
    session-agent/src/session_agent/      # entrypoint.py, control/, agent/{loop,approval,compaction}.py, tools/
  libs/shared/src/shared/   # wspólne definicje tabel SQLAlchemy, protokół control-plane, cennik modeli
  gui/src/                  # React + Vite: features/{auth,chat,sessions,approval,admin,workspace-browser}
  scripts/seed_admin.py, dev_up.sh, dev_down.sh
```

`api` jest jedynym właścicielem migracji Alembic. `llm-proxy` i `sandbox-manager` importują definicje tabel z `libs/shared`, nigdy nie migrują schematu same.

## Schemat Postgres (pełny od Etapu 1, rozszerzalny bez przepisywania)

Kluczowe tabele i po co istnieją od razu, nawet zanim coś z nich czyta:
- `users`, `auth_sessions` (sesje logowania, osobne od `sessions` czatu — token_hash, nie surowy token), `sessions` (jednostka czat+sandbox: status, approval_mode, sandbox_container_id, workspace_volume_name, **`forked_from_session_id`/`forked_from_message_id` nullable od Etapu 1** żeby fork w Etapie 7 nie wymagał migracji).
- `messages` (content jako jsonb — bloki Anthropic 1:1, sequence_number monotoniczny per sesja).
- `tool_call_events` (proposed→approved/rejected→running→succeeded/failed, kto zatwierdził).
- `token_usage` (append-only ledger od Etapu 1, nawet zanim Etap 6 to wizualizuje — żeby nie stracić danych historycznych), `user_usage_limits`/`user_usage_counters` (budżety per user, sprawdzane synchronicznie przez llm-proxy).
- `llm_session_tokens` (token proxy per sesja, osobna domena zaufania od `auth_sessions`).
- `context_compactions` (Etap 5), `audit_log` i `provider_api_keys` (Etap 8, czysto addytywne — nietworzyć wcześniej, nic innego nie ma do nich FK).

Pełne DDL (kolumny, typy, CHECK-e, indeksy) — patrz raport agenta planującego w sekcji 2 transkryptu; do wpisania jako pierwsza migracja Alembic w Etapie 1.

## Etap 1 — Szkielet (do zaimplementowania teraz, w pełnym detalu)

1. **FastAPI**: `create_app()` w `api/main.py`, montuje `auth.routes`, `admin.routes`. Start-up sprawdza tylko połączenie z DB — migracje uruchamiane jawnie (`uv run alembic upgrade head`), nie in-process, żeby dwa `--reload` workery się nie ścigały.
2. **Auth**: argon2-cffi (`PasswordHasher`, rehash przy zmianie parametrów). **Sesje po stronie serwera** (tabela `auth_sessions`, cookie `la_session` = `secrets.token_urlsafe(32)`, w bazie tylko `sha256(token)`) — wybrane nad podpisanym cookie, bo daje natychmiastową rewokację (potrzebną i tak w Etapie 8) bez historii rotacji klucza podpisującego. Cookie: `HttpOnly`, `Secure` (prod), `SameSite=Lax`. Endpointy: `POST /auth/login`, `POST /auth/logout`, `GET /auth/me`.
3. **RBAC**: `deps.require_role("admin")` nad `get_current_user`, dwie role (`admin`/`user`) w kolumnie `role`, bez osobnej tabeli uprawnień na razie.
4. **Provisioning**: `scripts/seed_admin.py` (idempotentny, z env `SEED_ADMIN_EMAIL`/`SEED_ADMIN_PASSWORD`), potem `POST /admin/users` (admin-only) do tworzenia kolejnych kont. Brak publicznej rejestracji.
5. **Alembic**: `services/api/src/api/db/migrations/`, `alembic.ini` czyta `DATABASE_URL`, pierwszamigracja tworzy cały schemat z sekcji wyżej.
6. **podman-compose.yml** (usługi na tym etapie: `db` postgres:16-alpine z healthcheckiem, `api`, `gui`; sieci `core`/`edge`; `llm-proxy` i `sandbox-manager` dochodzą w Etapach 2 i 4).
7. **Dev workflow**: `uv` workspace w root `pyproject.toml` (`[tool.uv.workspace] members = [...]`), `uv sync`, `uv run alembic upgrade head`, `uv run python scripts/seed_admin.py`, `uv run uvicorn api.main:app --reload --app-dir services/api/src`, `uv run pytest`.

### Weryfikacja Etapu 1
`podman-compose up -d db` → healthy → `alembic upgrade head` → `seed_admin.py` → `podman-compose up-d api gui` → `curl -X POST /auth/login` zwraca `Set-Cookie` → `curl -b cookies /auth/me` zwraca usera → endpoint admin-only zwraca 403 dla zwykłego usera / 200 dla admina → GUI: login przekierowuje do dashboardu → `uv run pytest` (argon2 hash/rehash, tworzenie/wygasanie/rewokacja sesji, integracyjne na tymczasowej bazie Postgres).

## Etapy 2–8 (plan wysokopoziomowy, z detalem tam gdzie ryzykowne)

**Etap 2 — Czat**: `api/chat/{routes,schemas,streaming}.py`. `POST /sessions`, `GET /sessions`, `GET /sessions/{id}/messages`, `POST /sessions/{id}/messages`. `api` woła `llm-proxy` przez `httpx` ze streamingiem, przekazuje delty do przeglądarki przez SSE, zapisuje każdą ukończoną wiadomość z kolejnym `sequence_number`. Na tym etapie `sandbox-manager` jeszcze nie istnieje — bez narzędzi, czysty streaming czatu.

**Etap 3 — Wiele sesji**: `SessionTaskManager` w `api/chat/streaming.py` — rejestr `asyncio.Task` +`asyncio.Queue` subskrybentów per `session_id`, wspiera wiele kart przeglądarki i reconnect po odświeżeniu (backfill przez `GET /sessions/{id}/messages?after=<seq>` + doczepienie do żywego strumienia; trwający w locie fragment odpowiedzi trzymany w pamięci i replayowany nowemu subskrybentowi). `POST /sessions/{id}/cancel` → `task.cancel()`.

**Etap 4 — Sandbox**: `sandbox-manager` z API (`POST/DELETE/GET /sessions`, `GET /sessions/{id}/stream`), jedyny komponent z dostępem do socketu Podmana, bezstanowy (rekoncyliacja osieroconych kontenerów przez label `local-agent.managed-by=sandbox-manager` + zapytanie do api o ważne session_id, niewłasna tabela lease). Sieci: `agent-egress` (`--internal`, tworzona przez sandbox-manager) — tylko llm-proxy (dual-homed `core`+`agent-egress`) i kontenery sesji. Konkretne flagi `podman run`: `--read-only`, `--tmpfs /tmp`, `--cap-drop=all`, `--security-opt no-new-privileges`, `--pids-limit`, `--memory`/`--cpus`, `--user 10001:10001`, wolumeny `workspace` i `control` per sesja. Da się budować i testować niezależnie od czatu (wywołania bezpośrednio do API sandbox-managera).

⚠️ **Checkpoint do ręcznej walidacji na Twoim hoście** (nie da się zweryfikować z tej sesji): potwierdź, że sieć `agent-egress` z rootless Podman/netavark faktycznie izoluje jak zakładamy — z kontenera na tej sieci `curl` do `llm-proxy` powinien działać, do internetu i do `db` — nie.

**Etap 5 — Narzędzia i zatwierdzanie** (najbardziej ryzykowna, nowatorska część — pełny design poniżej).

**Etap 6 — Monitoring**: Rozwazyc LangSmith bo na poczatek to leszpsza droga, w większości addytywne endpointy nad tabelami z wcześniejszych etapów. `prometheus_client`/`prometheus-fastapi-instrumentator` w `api`, `llm-proxy`, `sandbox-manager`; `deploy/prometheus.yml` + serwis w compose; dashboard admina jako nowe ekrany GUI + `GET /admin/usage`, `GET /admin/sessions/active`.

**Etap 7 — Historia**: wyszukiwanie przez `pg_trgm`/`ILIKE` (prościej niż `tsvector` na start). Fork: `POST /sessions/{id}/fork {at_message_id}` kopiuje wiadomości do `forked_from_message_id` **oraz** klonuje wolumen workspace (export/import wolumenu Podmana albo krótkotrwały kontener pomocniczy robiący `cp -a` między zamontowanymi wolumenami). Export: `GET /sessions/{id}/export?format=json|markdown`.

**Etap 8 — Hardening**: limity per user egzekwowane w `llm-proxy/metering.py` (tabele `user_usage_limits`/`user_usage_counters` istnieją od Etapu 1/5) + UI admina do ich ustawiania. Audit log przez jawne wywołania `audit.record(...)` w kluczowych miejscach (login, zmiana roli, create/delete sesji, zatwierdzenie narzędzia, akcje admina) — nie generyczny listener ORM. Rotacja klucza API: tabela `provider_api_keys`, llm-proxy używa aktywnego klucza, stary ważny krótko dla requestów w locie. Testy ucieczki z sandboxa: checklist w `docs/security-testing.md` (zapis poza `/workspace`, dostęp sieciowy do `db`/`sandbox-manager`, wyczerpanie pamięci/pids/cpu, eskalacja uprawnień) — gVisor/microVM świadomie poza zakresem v1 (zaufani userzy), odnotowane jako kolejny krok jeśli to założenie się zmieni.

## Etap 5 w pełnym detalu — control-plane, rejestr narzędzi, zatwierdzanie, kompaktowanie

### Control-plane (Opcja B, zatwierdzona)
`session-agent`, PID 1 w kontenerze sesji, nasłuchuje na `/control/agent.sock` — wolumen `control` współdzielony **wyłącznie** z kontenerem `sandbox-manager` (montowany przy tworzeniu kontenera). `sandbox-manager` dołącza do tego socketu i wystawia go jako WebSocket relay dla `api` (`GET /sessions/{id}/stream` na wewnętrznym API sandbox-managera). Kontener sesji: **zero portów wchodzących**, jedyne wyjście sieciowe to `llm-proxy`. `api` nigdy nie rozmawia z kontenerem bezpośrednio — zawsze przez sandbox-manager, co rozszerza zasadę "tylko sandbox-manager dotyka kontenerów" z socketu Podmana na całą komunikację z kontenerem.

Protokół — ramki JSON linia-po-linii:
```
# api -> session-agent
{"type": "user_message", "message_id": "...", "content": [...]}
{"type": "approval_decision", "tool_call_event_id": "...", "decision": "approved"|"rejected", "decided_by": "<user_id>"}
{"type": "cancel"}

# session-agent -> api
{"type": "assistant_delta", ...} / {"type": "assistant_message_complete", ..., "usage": {...}}
{"type": "tool_call_proposed", "tool_call_event_id": "...", "tool_name": "...", "input": {...}}
{"type": "tool_call_result", "tool_call_event_id": "...", "status": "...", "output": "...", "exit_code": 0}
{"type": "fs.tree" | "fs.read", ...}   # workspace browser
{"type": "session_error" | "heartbeat", ...}
```
`api` zapisuje każdą ramkę do Postgresa (`messages`/`tool_call_events`) **przed** przekazaniem do SSE — trwałość to zapis do bazy, nie socket. Kontener po restarcie po idle-teardown (Etap 4) odtwarzakontekst zawsze tak samo: najnowszy `context_compactions.summary_text` + wszystkie `messages` po `summarized_up_to_message_id` — to samo dla "odśwież przeglądarkę" (Etap 3) i "cold start po teardown".

### llm-proxy
**Zdecydowane: provider-agnostic przez `LLMBackend` Protocol wewnątrz llm-proxy**, nie przez wymuszanie jednego SDK na wszystkich providerach. Routing po polu `model` requestu: `claude-*` → `AnthropicBackend` (natywny `httpx` do `api.anthropic.com`, czysty passthrough — pełna wierność cache'owania/tool_use), cokolwiek innego → `OpenAIBackend` (biblioteka `openai`, dowolny provider mówiący natywnie formatem OpenAI Chat Completions: prawdziwy OpenAI albo `local-model`). Świadomie odrzucone: kierowanie realnego ruchu do Claude przez warstwę kompatybilności OpenAI SDK (`openai` SDK wskazane na `api.anthropic.com`) — Anthropic sam opisuje ją jako nieprzeznaczoną do produkcji, bez wsparcia dla prompt cachingu, bez pełnych bloków `thinking`, z ignorowanym `strict` (brak gwarancji poprawnego JSON-a z narzędzi); realny Claude zawsze idzie natywnym REST.

Niezależnie od providera, `LLMBackend.stream()` zawsze emituje zdarzenia SSE w kształcie Anthropic Messages API — to jedyny kontrakt, jaki rozumie SDK `anthropic` używane przez session-agent (patrz niżej). `OpenAIBackend` tłumaczy strumień chat-completion na te zdarzenia; na razie tylko tekst, bez `tool_calls` → `tool_use` (dochodzi razem z pętlą narzędzi w Etapie 5).

`local-model` (`services/local-model/`) to osobny serwis (transformers, mały model CPU-only, poza uv workspace) wystawiający `/v1/chat/completions` w formacie OpenAI — jeden z providerów `llm-proxy` (`model: "local-model"`), nie osobna ścieżka bypassująca metering: wszystkie requesty od `api`/session-agent zawsze idą przez `llm-proxy`, który centralnie pilnuje budżetu i zapisuje `token_usage` niezależnie od tego, który backend faktycznie odpowiedział.

Kontener sesji używa **standardowego SDK `anthropic`** wskazanego przez `ANTHROPIC_BASE_URL=http://llm-proxy:8080` i `ANTHROPIC_API_KEY=<opaque proxy token>` — zero customowego klienta HTTP w sandboksowanym kodzie. Token mintowany przez `api` przy tworzeniu kontenera (`llm_session_tokens`, hash w bazie, surowy token tylko wstrzyknięty jako env). `llm-proxy/metering.py` sprawdza `user_usage_counters` vs `user_usage_limits` **przed** przekazaniem requestu dalej (odrzuca z czytelnym błędem, jeśli budżet przekroczony), forwarduje do `api.anthropic.com` prawdziwym kluczem (znanym tylko llm-proxy),streamuje odpowiedź, na końcu zapisuje `token_usage` i inkrementuje `user_usage_counters` z bloku `usage` zwróconego przez Anthropic. llm-proxy ma **własny, bezpośredni dostęp do Postgresa** (nie przez api) — sprawdzenie budżetu musi być synchroniczne ze streamem, round-trip przez HTTP api dodałby latencję na hot path.

### Rejestr narzędzi, pętla, zatwierdzanie (`session-agent/agent/`)
- **Rejestr** (`tools/registry.py`): każde `Tool` ma `name`, `description`, `input_schema` (1:1 z formatem `tools=[...]` Anthropic), async `run(input)`. v1: `BashTool` (`asyncio.create_subprocess_shell`, timeout, cwd=`/workspace`, output ucięty), `ReadTool`/`WriteTool` (walidacja ścieżki pod `/workspace` — read-only rootfs to prawdziwe wymuszenie, to tylko lepszy komunikat błędu).
- **Pętla** (`agent/loop.py`): (1) załaduj kontekst po kompaktowaniu, strumieniuj `messages.create(..., tools=[...], stream=True)` przez llm-proxy; (2) delty tekstu od razu na control-plane; (3) dla każdego `tool_use`: jeśli `approval_mode == "ask"`, wyślij `tool_call_proposed` i czekaj na `approval_decision` (future keyowany po id, timeout per-decyzja domyślnie 15 min → syntetyczne odrzucenie, żeby pętla nigdy nie wisiała w nieskończoność); jeśli `auto`, wykonaj od razu; (4) zatwierdzone wywołania wykonywane **sekwencyjnie** (przewidywalna kolejność Bash + UX zatwierdzania), odrzucone dostają syntetyczny `tool_result` ("user rejected this action"); (5) zapisz przez control-plane, wróć do (1), chyba że tura nie miała `tool_use`, przyszedł `cancel`, albo osiągnięto limit iteracji per tura (domyślnie 50 — wentyl bezpieczeństwa).
- **Kompaktowanie** (`agent/compaction.py`): przed każdą turą sprawdź szacowane tokeny wejścia względem progu 80% okna modelu (tabela `model -> window size` w `libs/shared/pricing.py`). Jeśli przekroczone: zachowaj ostatnie 10–20 wiadomości verbatim, starsze wyślij do osobnego wywołania podsumowującego (bez narzędzi) z promptem zachowującym: pliki utworzone/zmienione ze ścieżkami, kluczowe decyzje, niedokończone wątki, napotkane błędy. **Oryginalne wiadomości w Postgresie nigdy nie są modyfikowane ani usuwane** — kompaktowanie zmienia tylko to, co idzie do modelu w kolejnych turach; pełna historia zostaje dla wyszukiwania/exportu/audytu (Etapy 7–8). Zapisz każde kompaktowanie w `context_compactions`, żeby cold start mógł deterministycznie odtworzyć efektywny kontekst. Podsumowanie pokazywane w GUI jako zwinięta notatka systemowa.

## Weryfikacja per etap

| Etap | Jak sprawdzić |
|---|---|
| 1 | `podman-compose up -d db` → healthy → `alembic upgrade head` → seed → curl login/me → RBAC 403/200 → `pytest` |
| 2 | llm-proxy za stubem/mockiem Anthropic, POST wiadomości, obserwuj SSE — delty w kolejności, poprawny `sequence_number` |
| 3 | Dwie karty na tej samej sesji, refresh w trakcie streamu, `/cancel` — obie karty widzą te same delty, refresh robi backfill, cancel realnie zatrzymuje task |
| 4 | Wywołania bezpośrednio do API sandbox-managera (bez czatu): `podman inspect` pokazuje limity/read-only/cap-drop, `podman exec touch /etc/x` fails, idle timeout uruchamia reaper. **Ręczny checkpoint sieci `agent-egress` na Twoim hoście.** |
| 5 | Wymuszone wywołanie narzędzia wymagające zatwierdzenia: `tool_call_events` = proposed → approve przez POST realnie odpala narzędzie w kontenerze, reject daje syntetyczny tool_result; długa sesja → pojawia się wpis w `context_compactions`, stare wiadomości w `messages` nienaruszone |
| 6 | `/metrics` na każdym serwisie scrape'uje się poprawnie, liczby w dashboardzie zgadzają się z ręcznym `SUM(token_usage)` |
| 7 | Fork w środku rozmowy → sklonowany workspace + wiadomości do punktu forka; export round-tripuje czytelnie |
| 8 | Przekroczenie `user_usage_limits` → llm-proxy odrzuca z czytelnym błędem; checklist ucieczki z sandboxa w `docs/security-testing.md` — każdy punkt pass/fail |

## Kluczowe pliki do stworzenia (Etap 1 jako pierwszy krok)

- `services/api/src/api/main.py`, `config.py`, `deps.py`
- `services/api/src/api/auth/security.py`, `auth/routes.py`
- `services/api/src/api/db/models/{user,auth_session,session,message,tool_call_event,token_usage}.py`
- `services/api/src/api/db/migrations/` (Alembic, pierwsza migracja z pełnym schematem)
- `podman-compose.yml`, `deploy/Containerfile.api`, `deploy/Containerfile.gui`
- `scripts/seed_admin.py`
- root `pyproject.toml` (uv workspace) + `services/api/pyproject.toml`
- `gui/` szkielet Vite + React (login → dashboard)

Kolejne pliki (`llm-proxy`, `sandbox-manager`, `session-agent`, `libs/shared`) dochodzą w Etapach 2, 4, 5 zgodnie z planem wyżej — nie tworzyć ich pustych na zapas w Etapie 1.