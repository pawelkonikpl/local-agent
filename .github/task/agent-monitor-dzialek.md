# Agent „Monitor działek górskich”: orkiestrator workflow skilla nad bezstanowymi narzędziami (etap 2)

> **Szkic.** Zależy od `narzedzie-plot-search.md` (etap 1). Przed implementacją rozstrzygnąć „Pytania otwarte” na końcu i wpisać decyzje tutaj (krok 1 dev-flow).

## Kontekst

Decyzja użytkownika (28.09.2026): narzędzia są bezstanowe, a konkretna implementacja monitora to **agent**. Agent jest orkiestratorem: zna workflow skilla `monitor-dzialek-gorskich`, wie, które narzędzia wołać, w jakiej kolejności i jakie informacje im przekazać (portal, miejscowość, budżet w walucie portalu, filtry), a potem filtruje, porównuje i pisze raport.

Dziś w `api` nie ma pojęcia agenta: `ChatGeneration.run` wysyła wszystkie narzędzia z `build_default_registry()`, prompt systemowy to tylko `WEB_RULES` (gdy jakieś narzędzie czyta sieć), limit to `chat_max_tool_iterations = 10` i `chat_max_tokens = 4096`. Przegląd ze skilla to kilkanaście–kilkadziesiąt wywołań narzędzi i raport dłuższy niż 4096 tokenów, więc nie zmieści się w obecnych limitach.

Skill w repo leży w `.agents/sillks/monitor-dzialek-gorskich/` (literówka w nazwie katalogu, nieśledzony w git): `SKILL.md` + `references/{kryteria,lokalizacje,serwisy,weryfikacja}.md`. Pliki referencyjne należą do użytkownika i on je edytuje.

## Zakres (propozycja)

1. **Definicja agenta jako pliki w repo:** `agents/monitor-dzialek-gorskich/`:
   - `AGENT.md`: frontmatter (`name`, `title`, `description`, `tools: [plot_search, plot_details, web_search, get_current_time]`, `max_tool_iterations`, `max_tokens`) + treść = instrukcje dla modelu, przepisane z `SKILL.md` na narzędzia aplikacji: „wejdź na stronę ogłoszenia” → `plot_details`; „przeszukaj portal” → `plot_search` (jedno wywołanie na portal i miejscowość); „zapisz plik `dzialki-RRRR-MM-DD.md`” → raport jako ostatnia wiadomość (pkt 5).
   - `references/*.md` przeniesione z `.agents/sillks/…` bez zmian treści; katalog `.agents/sillks/` usunąć.
   - Agent **nie edytuje** plików referencyjnych (brak narzędzi zapisu i brak flow zatwierdzania). Krok 6 skilla zostaje: agent proponuje zmianę, użytkownik ją wprowadza.
2. **Ładowanie (`api/agents/`):** model Pydantic `AgentProfile` (pola frontmatter + `prompt` + `references: dict[str, str]`), `load_agents(dir)`. Katalog montowany read-only do kontenera `api`; profile czytane przy starcie generacji (pliki są małe), żeby edycja kryteriów działała bez restartu. Nieznane narzędzie we frontmatter → błąd przy starcie `api`, nie w trakcie rozmowy.
3. **Dostęp do plików referencyjnych:** narzędzie `read_agent_reference(name)` z `name` jako `enum` nazw plików agenta, tworzone per agent (instancja związana z jego katalogiem, bez stanu per użytkownik). Odpowiada Krokowi 0 skilla („wczytaj konfigurację”) i nie dokleja ~23 KB do promptu przy każdym z kilkudziesięciu wywołań modelu. Alternatywa: wszystko w prompcie systemowym + cache promptu w llm-proxy (patrz pytania otwarte).
4. **Sesja z agentem:**
   - `chat_sessions.agent: str | None` + migracja Alembic; `POST /sessions` przyjmuje `agent` (nazwa z `GET /agents`); `None` = dzisiejszy czat bez zmian.
   - `ChatGeneration`: prompt systemowy = prompt agenta + `WEB_RULES` (gdy agent ma narzędzia czytające sieć); narzędzia = podzbiór rejestru z listy agenta + `read_agent_reference`; limity iteracji i tokenów z profilu (z górnym limitem w `Settings`, żeby plik w repo nie mógł ustawić dowolnego kosztu).
   - `GenerationGuard` bez zmian.
   - GUI: wybór agenta przy tworzeniu sesji, nazwa agenta w liście sesji.
5. **Raport i porównanie z poprzednim przeglądem (bezstanowo):** ostatnia wiadomość agenta to raport w formacie z Kroku 5 skilla. Porównanie z poprzednim przebiegiem: na początku agent prosi o poprzedni raport (Krok 1 skilla), użytkownik wkleja go do czatu. Opcjonalnie w GUI przycisk „pobierz jako .md” przy wiadomości.

## Poza zakresem

- Pod-agenci / delegowanie (orkiestrator zleca np. przegląd jednego regionu osobnemu agentowi z własnym kontekstem) — patrz pytania otwarte.
- Automatyczne cykliczne uruchamianie (cron) i powiadomienia.
- Zapis raportów i historii ofert w bazie.
- Narzędzia weryfikacyjne (geoportal, SOPO/PIG, ZBGIS, nocowanie.pl, kurs NBP).
- Flow zatwierdzania narzędzi (`approval_mode`).

## Ryzyka

- **Koszt:** każde wywołanie modelu wysyła całą historię; 30 wywołań narzędzi × rosnący kontekst. Mitigacje: zwięzłe wyniki `plot_search` (etap 1), `read_agent_reference` zamiast pełnych plików w prompcie, limity z profilu.
- **Małe modele** mogą nie utrzymać workflow na kilkadziesiąt kroków (pominąć regiony, zgubić budżet). Agent rekomenduje model w `AGENT.md`; weryfikacja ręczna na Claude i `gpt-*`.
- **Prompt injection przez ogłoszenia** przy długim workflow: agent ma tylko narzędzia do odczytu, więc szkoda ogranicza się do błędnego raportu; spotlighting i `WEB_RULES` jak dziś.

## Kryteria akceptacji (wstępne)

- `GET /agents` zwraca `monitor-dzialek-gorskich` z tytułem i opisem; `POST /sessions {"agent": "nieznany"}` → 422.
- Sesja bez agenta: payload do llm-proxy identyczny jak dziś (ten sam zestaw narzędzi, ten sam prompt).
- Sesja z agentem: `tools` w payloadzie = dokładnie lista z `AGENT.md` + `read_agent_reference`; `system` zaczyna się od promptu agenta i zawiera `WEB_RULES`.
- `read_agent_reference("kryteria.md")` zwraca treść pliku; nazwa spoza listy → `ToolInputError`; `"../../etc/passwd"` → `ToolInputError`.
- Zmiana budżetu w `references/kryteria.md` na dysku jest widoczna w następnej generacji bez restartu `api`.
- `AGENT.md` z narzędziem, którego nie ma w rejestrze → `api` nie startuje, z komunikatem wskazującym plik.
- Ręcznie: „sprawdź działki” w sesji z agentem → agent czyta kryteria i lokalizacje, woła `plot_search` osobno dla miejscowości z Priorytetu 1 na kilku portalach, sprawdza czołówkę przez `plot_details` i kończy raportem w formacie z Kroku 5, z budżetem z pliku.

## Pytania otwarte

1. **„Z kim gadać”:** czy orkiestrator ma delegować zadania innym agentom (np. osobny agent na region albo na weryfikację ogłoszenia, z własnym kontekstem i zwracający streszczenie)? Jeśli tak — to osobny etap (narzędzie `delegate(agent, task)` uruchamiające zagnieżdżoną generację), bo zmienia pętlę w `streaming.py`.
2. **Poprzedni przegląd:** wystarczy wklejanie raportu przez użytkownika, czy agent ma sam czytać raporty z wcześniejszych sesji tego agenta? To drugie wymaga przekazania kontekstu użytkownika do narzędzi (zmiana w `Tool`/`ToolRegistry.execute`) albo zapisu raportów w bazie.
3. **Pliki referencyjne:** `read_agent_reference` (na żądanie) czy całość w prompcie systemowym z cache promptu w llm-proxy?
4. **Gdzie mieszkają definicje agentów:** w repo (edycja = commit/edycja pliku na dysku) czy w bazie per użytkownik z edycją w GUI?
