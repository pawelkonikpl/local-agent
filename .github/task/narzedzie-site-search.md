# Narzędzie `site_search`: wyszukiwanie bezpośrednio na stronie serwisu

## Kontekst

Dziś jedyną drogą do internetu jest `web_search`: przeglądarka w web-agent otwiera stronę wyników naszego SearXNG, który odpytuje Google, Brave itd. Żeby znaleźć coś na Allegro, model pisze `site:allegro.pl …`. Skutki:
- dostaje tylko to, co zaindeksowała zewnętrzna wyszukiwarka (często nieaktualne oferty, stare ceny, strony kategorii zamiast ofert);
- nie ma ceny ani dostępności, tylko tytuł i snippet;
- wynik zależy od rankingu Google, a nie od wyszukiwarki serwisu.

Cel: model wywołuje `site_search(site="allegro.pl", query="rower gravel")`, a przeglądarka **wchodzi bezpośrednio** na stronę wyszukiwania serwisu i czyta z niej oferty.

Założenia, które wynikają z `warstwa-bezpieczenstwa-web-agent.md` (to zadanie jest **po** nim i z niego korzysta):
- **Model nie buduje URL-i** (§2.4, §6.2 dokumentu źródłowego). Podaje tylko nazwę serwisu z zamkniętej listy i tekst zapytania, a adres składa kod z szablonu zapisanego dla serwisu. Dzięki temu model nie może skierować przeglądarki na dowolną domenę ani przemycić danych w ścieżce URL-a.
- **Zbiór dozwolonych źródeł serwisu jest stałą w kodzie** (`extra_origins`, pkt 7 planu bezpieczeństwa), a jego domeny są na liście proxy (pkt 8).
- Wyniki przechodzą przez to samo `sanitize`, sygnały injection i domenowe oraz spotlighting co `web_search`.
- Przy blokadzie anty-botowej (CAPTCHA, 403) narzędzie zwraca `blocked` i kończy. Nie obchodzimy zabezpieczeń (tak jak w `narzedzie-web-search.md`: łamie to regulamin serwisu i jest kruche). Weryfikację może rozwiązać **człowiek** w widocznym oknie przeglądarki (§5 dokumentu źródłowego: CAPTCHA przekazuje się człowiekowi).

**Decyzja po sprawdzeniu (28.09.2026):** Allegro chroni listing przez DataDome. Z kontenera w chmurze zarówno `curl`, jak i headless Chromium dostają 403 ze stroną wyzwania z `captcha-delivery.com` i zero ofert. Użytkownik wybrał wariant **z własną przeglądarką na hoście**: prawdziwy Chrome, widoczne okno, domowe łącze, osobny profil bez zalogowanych sesji. Serwis `site_search` obsługuje osobna instancja web-agent uruchomiona na hoście i podłączona do tego Chrome przez CDP. `web_search` bez zmian zostaje w kontenerze (headless, SearXNG).

Kod, na którym to stoi:
- `SearchEngine` (`web_agent/engines/base.py`): `name`, `extract_js`, `url_for`, `parse`, `detect_block`, `is_no_results`. Serwis to po prostu kolejna implementacja tego Protocolu, więc `SearchService.search` (Sense-Act-Verify, rate-limit, timeout, audyt) działa bez zmian.
- `build_results` (`engines/common.py`) odrzuca reklamy (`is_ad`). W sklepie oferty sponsorowane są prawdziwymi ofertami, więc tu je oznaczamy, a nie wyrzucamy.
- `WebSearchTool` (`api/tools/web_search.py`) jest wzorcem dla narzędzia po stronie `api`.

## Zakres

### Przeglądarka na hoście (wariant wybrany przez użytkownika)

Układ:
```
api (kontener) --HTTP+bearer--> web-agent "sites" (host, uv run) --CDP 127.0.0.1:9222--> Chrome (host, osobny profil)
                                                                                           |
                                                                    --proxy-server--> egress-proxy (kontener, port 127.0.0.1:3128)
```

A. **Chrome użytkownika — osobny, dedykowany profil.** Skrypt `scripts/site-browser.sh` (Linux/macOS; ścieżkę do Chrome'a da się nadpisać zmienną `CHROME`) uruchamia:
   ```
   "$CHROME" --user-data-dir="$HOME/.local-agent/site-browser" \
     --remote-debugging-address=127.0.0.1 --remote-debugging-port=9222 \
     --proxy-server=http://127.0.0.1:3128 \
     --no-first-run --no-default-browser-check about:blank
   ```
   - **Nigdy główny profil** użytkownika. CDP daje pełną kontrolę nad przeglądarką, a zalogowane sesje (poczta, bank) w tym samym profilu byłyby w zasięgu agenta. Nowsze Chrome i tak nie włączają zdalnego debugowania na domyślnym profilu.
   - W tym profilu **nie logujemy się** do Allegro ani nigdzie indziej (zasada 5 dokumentu: bez zalogowanych sesji, gdy zadanie ich nie wymaga). Opisane w nagłówku skryptu i w README.
   - Port CDP tylko na `127.0.0.1`.
   - Ruch Chrome'a idzie przez `egress-proxy`, więc lista dozwolonych domen działa także tutaj. `podman-compose.yml`: `egress-proxy` publikuje `127.0.0.1:3128:3128`. To działa, bo `egress-proxy` jest też w sieci `egress` (z NAT). Na hoście port jest dostępny tylko lokalnie.

B. **Druga instancja web-agent na hoście** (`uv run uvicorn web_agent.main:app --port 8093` z `WEB_AGENT_CDP_URL=http://127.0.0.1:9222` i `WEB_AGENT_SITES_ONLY=true`):
   - `sites_only=true` → `POST /v1/search` odpowiada 404. Ta instancja obsługuje tylko `/v1/site-search` i `/v1/sites`, a kontenerowa odwrotnie: `sites_enabled` domyślnie `false`, więc `/v1/site-search` → 404. Wyszukiwanie w SearXNG nie trafia do przeglądarki użytkownika, a Allegro nie trafia do headless.
   - `api` w compose łączy się pod `WEB_AGENT_SITES_URL` (domyślnie `http://host.containers.internal:8093`) z bearerem `INTERNAL_PROXY_TOKEN`. Instancja na hoście musi słuchać na interfejsie osiągalnym z kontenera. **Zweryfikować na starcie implementacji**, czy przy rootless podman (pasta) wystarcza `127.0.0.1`. Jeśli trzeba `0.0.0.0`, opisać w README, że port 8093 ma być zablokowany na firewallu dla sieci LAN (token i tak jest wymagany).
   - Skrypt `scripts/site-agent.sh` uruchamia tę instancję z właściwymi zmiennymi.

C. **Kontekst przeglądarki przy CDP do Chrome'a użytkownika.** `BrowserManager` dostaje tryb `reuse_default_context` (włączany razem z `cdp_url` w instancji sites):
   - każde wyszukiwanie to **nowa karta** w domyślnym kontekście profilu (`browser.contexts[0].new_page()`), zamykana w `finally`, a nie nowy `BrowserContext`;
   - powód: DataDome i podobne systemy wydają ciasteczko po przejściu weryfikacji. Czysty kontekst przy każdym zapytaniu oznaczałby wyzwanie przy każdym zapytaniu. Ciasteczka zostają tylko w dedykowanym profilu bez logowań, więc trwały stan nie zawiera danych użytkownika;
   - filtr originów przez CDP `Fetch` (plan bezpieczeństwa, pkt 6) działa bez zmian, bo sesja CDP jest per karta.

D. **Blokada = przekazanie człowiekowi.** Gdy `detect_block` wykryje wyzwanie, karta **nie jest zamykana** (w trybie `reuse_default_context`), a odpowiedź to `status="blocked"` z `error` np. `"allegro.pl asks for human verification; solve it in the site browser window and retry"`. `SiteSearchTool` w `api` formatuje to tak, żeby model poprosił użytkownika o rozwiązanie weryfikacji w oknie przeglądarki i ponowienie prośby. Agent nigdy nie klika w wyzwanie sam.
   - Wymaga poszerzenia zbioru originów o domenę wyzwania (`geo.captcha-delivery.com` lub inną, ustaloną z prawdziwej strony) **tylko** dla ramek wyzwania, żeby człowiek mógł je rozwiązać. Najprościej: dopisać ją do `extra_origins` Allegro i do listy proxy. Treść tej ramki nigdy nie trafia do modelu (`extract_js` czyta tylko listing).
   - Otwarte karty z wyzwaniem nie mogą się mnożyć: przed otwarciem nowej karty web-agent zamyka poprzednią kartę z wyzwaniem dla tego serwisu, jeśli użytkownik jej nie rozwiązał.

E. **Fixture'y z prawdziwej strony.** Z chmury nie da się pobrać listingu, więc selektory ustala się na stronach zapisanych przez użytkownika:
   - CLI dostaje flagę `--save-html PATH` (dla `search` i `site`): po nawigacji zapisuje `document.documentElement.outerHTML` przez `Runtime.evaluate`;
   - użytkownik uruchamia `scripts/site-browser.sh`, potem `uv run web-agent site allegro.pl "rower gravel" --cdp-url http://127.0.0.1:9222 --save-html allegro_results.html`, i analogicznie dla zapytania bez wyników i dla strony wyzwania (jeśli się pojawi). Pliki trafiają do `services/web-agent/tests/fixtures/`;
   - **implementacja ma dwie fazy**: (1) wszystko poza selektorami Allegro (tryb sites, CLI z `--save-html`, skrypty, `api`, szkielet `AllegroSite` z selektorami do uzupełnienia); (2) selektory, `detect_block`, `is_no_results` i testy na fixture'ach, gdy użytkownik je dostarczy. Faza 1 może być zmergowana bez fazy 2 tylko z `web_agent_sites` pustym w `api`, żeby narzędzie nie było oferowane.

### web-agent

1. **Model wyniku (`models.py`)**:
   - `SearchResult` dostaje `price: str | None = None` (tekst ceny tak, jak pokazuje strona, po normalizacji whitespace, np. `"1 299,00 zł"`; bez parsowania na liczbę, bo formaty są różne).
   - Nowy `SiteSearchQuery`: `site: str`, `query` (jak w `SearchQuery`: 1–400 znaków po `strip()`), `max_results: int = 5` (1–10). Bez `region`.
2. **Wspólne (`engines/common.py`)**:
   - `extract_js` dostaje opcjonalny selektor `price` (zwraca wtedy też `price` w wierszu).
   - `build_results(..., ads: Literal["drop", "flag"] = "drop")`. `"flag"` zostawia wpis z flagą `sponsored` w `flags`. Istniejące silniki wołają z domyślnym `"drop"`, więc ich zachowanie się nie zmienia.
3. **Serwisy (`web_agent/sites/`)**:
   - `sites/allegro.py` → `AllegroSite` (implementuje `SearchEngine`):
     - `name = "allegro.pl"`;
     - `url_for` → `https://allegro.pl/listing?string=<urlencoded query>`;
     - `extra_origins` = originy potrzebne do wyrenderowania listingu (skrypty i style serwisu). Ustalić z prawdziwej strony w DevTools (zakładka Network, typy `Script`, `Stylesheet`, `XHR/Fetch`) i wpisać jako stałą. Obrazki i fonty i tak blokujemy;
     - `extract_js`: wszystkie selektory jako stałe na górze pliku, jak w `duckduckgo.py`. Kotwice semantyczne zamiast wygenerowanych nazw klas: link oferty = `a[href*="/oferta/"]`, kontener = najbliższy `article` nad linkiem, cena = element z `aria-label` lub tekstem ceny w kontenerze, sponsorowane = kontener zawierający etykietę „Sponsorowane” / „Oferta sponsorowana”. **Najpierw sprawdzić**, czy strona ma dane listingu w osadzonym JSON (`<script type="application/json">`). Jeśli tak i jest stabilniejszy od DOM, czytać z niego. Decyzję i powód zapisać w docstringu;
     - **Czytamy tylko wybrane pola, nigdy „całą treść”.** `extract_js` zwraca wyłącznie: tytuł oferty (tekst linku `/oferta/`), cenę, URL, znacznik „sponsorowane” oraz opcjonalnie krótkie parametry strukturalne z kafelka (np. „stan: nowy”, „dostawa od …”) jako `snippet`, maks. 150 znaków. **Nie czytamy** sekcji z tekstem pisanym przez innych użytkowników ani sprzedawców: opinii i ocen, komentarzy, pytań i odpowiedzi, opisów ofert, „historii sprzedawcy”, bannerów i bloków reklamowych. To lista dozwolonych pól (allowlist), a nie lista zakazanych sekcji: wszystko, czego selektor pola jawnie nie wskazuje, nie trafia do modelu, także gdy serwis doda nową sekcję;
     - dodatkowo, jako druga warstwa, `extract_js` przed odczytem pól pomija elementy leżące w kontenerach komentarzy/opinii (stała `EXCLUDED_CONTAINERS` na górze pliku, np. selektory sekcji `opinie`, `komentarze`, `pytania`, `[itemprop="review"]`, `[data-role*="review"]`; ustalić z fixture'a). Kafelek oferty, którego pole leży w takim kontenerze, jest pomijany;
     - tytuły ofert też pisze sprzedawca, więc nadal przechodzą przez `sanitize` i sygnały injection z planu bezpieczeństwa (wstrzymanie wyniku z flagą);
     - `parse`: `build_results(raw, max_results, allegro_url, ads="flag")`, gdzie `allegro_url` przyjmuje tylko `https://allegro.pl/...` (link wskazujący poza serwis jest odrzucany), obcina query string poza identyfikatorem oferty i zostawia ścieżkę `/oferta/...`;
     - `detect_block`: status 403/429/5xx albo strona ochrony anty-botowej (znaczniki tekstu i/lub obecność elementu wyzwania; ustalić na zapisanej stronie blokady) → powód, np. `"allegro.pl bot protection (captcha)"`;
     - `is_no_results`: znacznik tekstowy strony „brak wyników” z fixture'a.
   - `sites/__init__.py`: `SITES: dict[str, Callable[[], SearchEngine]] = {"allegro.pl": AllegroSite}`, `SITE_NAMES`, `get_site(name) -> SearchEngine` (nieznana nazwa → `ValueError`).
   - Dodanie kolejnego serwisu (np. `ceneo.pl`, `olx.pl`) = nowy plik w `sites/`, wpis w `SITES`, domeny w `allowlist.txt` proxy. Żadnych zmian w `api` poza listą w konfiguracji (pkt 7).
4. **HTTP (`main.py`)**:
   - `POST /v1/site-search` (body `SiteSearchQuery`, odpowiedź `SearchResponse`, ten sam bearer co `/v1/search`). Nieznany `site` → 422. Wywołuje `search_service.search(SearchQuery(query=…, max_results=…), get_site(site))`.
   - `GET /v1/sites` → `{"sites": SITE_NAMES}`, z tym samym bearerem.
5. **CLI (`cli.py`)**: nowa podkomenda `web-agent site <site> "<query>" [--max-results N] [--json] [--headful] [--cdp-url URL] [--screenshot out.png]`. Te same kody wyjścia co `search`. `format_text` dopisuje cenę i `[sponsored]`, gdy są.
6. **Deploy**:
   - dopisać domeny Allegro (`allegro.pl`, te z `extra_origins`, domenę wyzwania z pkt D) do `deploy/egress-proxy/allowlist.txt`, z komentarzem `# site_search: allegro.pl`;
   - `podman-compose.yml`: `egress-proxy` publikuje `127.0.0.1:3128:3128`, `api` dostaje `WEB_AGENT_SITES_URL: ${WEB_AGENT_SITES_URL:-http://host.containers.internal:8093}`;
   - konfiguracja web-agent (`config.py`): `sites_only: bool = False`, `sites_enabled: bool = False` (`sites_only` włącza też `sites_enabled`), `reuse_default_context: bool = False`;
   - `.env.example` i README: krótka instrukcja „site_search przez własną przeglądarkę” (kolejność: `podman-compose up`, `scripts/site-browser.sh`, `scripts/site-agent.sh`) z ostrzeżeniem o dedykowanym profilu i braku logowania.

### api

7. **`api/tools/site_search.py` → `SiteSearchTool`**:
   - `name = "site_search"`, `capabilities = {"reads_untrusted", "external_effect"}`.
   - `description` (po angielsku): szuka bezpośrednio w wyszukiwarce wskazanego serwisu i zwraca oferty (tytuł, cena, URL); używać zamiast `web_search` z `site:`, gdy serwis jest na liście; ceny są odczytane ze strony i mogą się zmienić.
   - `input_schema`: `site` jako `enum` z `settings.web_agent_sites` (domyślnie `["allegro.pl"]`), `query` (wymagane), `max_results` 1–10.
   - Walidacja jak w `WebSearchTool._validate` (wydzielić wspólne sprawdzanie `query` i `max_results` do małej funkcji w `api/tools/web_common.py`, żeby nie kopiować) + `site` spoza listy → `ToolInputError`.
   - Wynik formatowany tym samym spotlightingiem co `web_search` (wspólna funkcja z planu bezpieczeństwa, pkt 9), z ceną w linii tytułu (`1. <title> — <price>`) i dopiskiem `[sponsored]`. `source="site_search:<site>"`.
   - Łączy się z `settings.web_agent_sites_url` (instancja na hoście, nie kontenerowy `web_agent_url`). Rejestrowany w `build_default_registry`, gdy `web_agent_sites_url` jest ustawione i lista `web_agent_sites` nie jest pusta.
   - Brak połączenia (instancja na hoście nie działa) → `ToolResult` z błędem: „The site browser isn't running on the user's computer; ask the user to start it (scripts/site-browser.sh and scripts/site-agent.sh).”
   - `status="blocked"` → komunikat dla modelu: poproś użytkownika o rozwiązanie weryfikacji w oknie przeglądarki na jego komputerze i o ponowienie prośby; nie ponawiaj sam.
8. **Prompt systemowy**: jedno zdanie w `web_rules` (plan bezpieczeństwa, pkt 12): ceny i dane ofert pochodzą ze strony serwisu i przed decyzją o zakupie użytkownik powinien je sprawdzić na stronie oferty. Model nie kupuje i nie wypełnia formularzy (nie ma takich narzędzi).

## Poza zakresem

- Wchodzenie na stronę pojedynczej oferty (szczegóły, dostępność, sprzedawca). To zadanie dla `web_fetch` albo `site_offer(site, offer_url)` z URL-em walidowanym względem serwisu. Osobny plan.
- Filtry i sortowanie serwisu (cena od/do, stan, kategoria) w parametrach narzędzia. Łatwe do dodania później jako pola `SiteSearchQuery` mapowane w `url_for`, ale każdy serwis ma inne.
- Kolejne serwisy poza Allegro. Konstrukcja ma je umożliwiać bez zmian w kodzie wspólnym.
- Oficjalne API Allegro (developer.allegro.pl). Warto sprawdzić, czy wyszukiwanie ofert jest dostępne dla naszego typu aplikacji. Jeśli tak, to byłaby osobna implementacja `SearchEngine` bez przeglądarki (stabilniejsza i zgodna z regulaminem), wymagająca rejestracji aplikacji i sekretu w env web-agent.
- Obchodzenie ochrony anty-botowej, logowanie, koszyk, zakupy.
- Testy automatyczne to krok 4 dev-flow. Kryteria niżej są dla nich źródłem.

## Ryzyka

- **Headless z kontenera: sprawdzone, zablokowane** (patrz „Decyzja” w kontekście). Stąd przeglądarka na hoście.
- **Czy prawdziwy Chrome z domowego łącza przechodzi?** Nie wiadomo, dopóki użytkownik nie sprawdzi. Pierwszy krok fazy 2: `scripts/site-browser.sh`, ręcznie otworzyć listing Allegro w tym oknie, potem CLI z `--save-html`. Jeśli wyzwanie pojawia się przy każdym zapytaniu mimo rozwiązania, wrócić do planu (krok 1): oficjalne API albo inny serwis.
- **Instancja na hoście musi działać**, żeby narzędzie działało. Narzędzie mówi to modelowi wprost (pkt 7), zamiast udawać awarię wyszukiwarki.
- **Dostęp kontenera do hosta** (`host.containers.internal`) zależy od konfiguracji sieci podmana. Zweryfikować na starcie fazy 1 (pkt B).
- Nie dodawać fałszywych nagłówków, opóźnień „dla ludzkości” ani innych technik maskowania. Weryfikację rozwiązuje człowiek.

## Kryteria akceptacji

**Czyste funkcje (bez przeglądarki)**
- `AllegroSite().url_for(SearchQuery(query="rower gravel 28\""))` → `https://allegro.pl/listing?string=rower+gravel+28%22` (poprawne kodowanie, żadnych innych parametrów).
- `get_site("allegro.pl")` zwraca `AllegroSite`, `get_site("amazon.pl")` → `ValueError`.
- `AllegroSite.parse`:
  - wpis z `href` `https://allegro.pl/oferta/rower-123?bi_s=ads&x=1` → URL bez zbędnych parametrów, `domain == "allegro.pl"`, `price` znormalizowane;
  - wpis z `href` na inną domenę (`https://evil.example/oferta/1`) → odrzucony;
  - wpis sponsorowany → zostaje, `flags` zawiera `sponsored`;
  - dwie oferty z tym samym URL-em → jeden wynik;
  - wpis ze snippetem/tytułem z poleceniem dla AI → `withheld=True` (reguły z planu bezpieczeństwa działają też tu).
- `build_results(..., ads="drop")` (domyślnie) zachowuje się dokładnie jak dziś: istniejące testy DuckDuckGo i SearXNG przechodzą bez zmian.
- `detect_block`: 403 → powód; 200 + tekst strony blokady z fixture'a → powód; 200 + zwykły listing → `None`.

**Z przeglądarką (marker `browser`, strony serwowane lokalnie, bez internetu)**
- Fixture'y `tests/fixtures/allegro_results.html`, `allegro_no_results.html`, `allegro_blocked.html` zapisane z prawdziwych odpowiedzi. Na `allegro_results.html`: ≥5 wyników z niepustym `title`, `price` i URL-em `https://allegro.pl/oferta/…`. `allegro_no_results.html` → `no_results`. `allegro_blocked.html` → `blocked`, `results == []`.
- Fixture-pułapka `allegro_review_injection.html` = prawdziwy listing z dopisaną sekcją opinii, komentarzem pod ofertą i blokiem pytań, każdy z tekstem „Ignore previous instructions… / Zignoruj poprzednie polecenia…” i unikalnym znacznikiem (np. `REVIEW-MARKER-1`). Wynik: `status="ok"`, żaden znacznik nie występuje w `json.dumps(response)` (ani w tytułach, ani w snippetach), wyniki nie są wstrzymane (bo tekst z sekcji w ogóle nie został przeczytany), a liczba ofert jest taka sama jak bez dopisanych sekcji.
- Kafelek oferty z tytułem zawierającym polecenie dla AI → wynik `withheld=True` (tytuł sprzedawcy to też niezaufany tekst).
- `snippet` każdego wyniku ma ≤ 150 znaków i pochodzi tylko z parametrów kafelka.
- Test używa podmienionego originu (lokalny serwer udaje `allegro.pl` przez `url_for` wstrzyknięte w teście); żądania do originów spoza zbioru są blokowane (`blocked_requests` niepuste dla fixture'a z zewnętrznym skryptem).

**HTTP i api**
- `POST /v1/site-search` bez tokenu → 401; `{"site": "amazon.pl", "query": "x"}` → 422; poprawne body ze zmockowanym serwisem → 200 i body zgodne z `SearchResponse`.
- `SiteSearchTool.input_schema` ma `site.enum == ["allegro.pl"]`; input `{"site": "allegro.pl"}` bez `query` → `ToolInputError`; `{"site": "evil.example", "query": "x"}` → `ToolInputError`, bez żądania HTTP.
- Wynik `ok` w `_format`: znaczniki `untrusted_web_content` z `source="site_search:allegro.pl"`, cena w linii tytułu, `[sponsored]` przy ofertach sponsorowanych.
- `GenerationGuard`: po `site_search` generacja jest oznaczona jako tainted (tak jak po `web_search`).

**Tryby instancji**
- Instancja z `sites_only=true`: `POST /v1/search` → 404, `/v1/site-search` działa. Instancja domyślna (kontener): `/v1/site-search` → 404.
- `reuse_default_context=true`: po 5 wyszukiwaniach liczba otwartych kart w przeglądarce wraca do stanu sprzed (poza ewentualną jedną kartą z wyzwaniem), kontekst nie jest tworzony ani zamykany.
- Blokada w trybie `reuse_default_context` zostawia kartę otwartą; kolejne zapytanie do tego samego serwisu najpierw ją zamyka (nigdy więcej niż jedna karta z wyzwaniem na serwis).

**Ręcznie**
- `scripts/site-browser.sh` otwiera okno Chrome'a z nowym profilem w `~/.local-agent/site-browser`; `curl http://127.0.0.1:9222/json/version` odpowiada, a z innego komputera w sieci port 9222 jest niedostępny.
- `uv run web-agent site allegro.pl "rower gravel" --cdp-url http://127.0.0.1:9222 --max-results 5` wypisuje 5 ofert z cenami i URL-ami `allegro.pl/oferta/…` w < 15 s, a w oknie widać otwierającą się i zamykaną kartę. Przy wyzwaniu: `blocked`, karta zostaje; po ręcznym rozwiązaniu ponowienie daje wyniki.
- Z zatrzymanym `scripts/site-agent.sh` narzędzie w czacie zwraca komunikat o niedziałającej przeglądarce na komputerze użytkownika.
- W czacie: „znajdź na allegro rower gravel do 3000 zł” → model woła `site_search` z `site="allegro.pl"`, a nie `web_search` z `site:`.
- W logach `egress-proxy` są tylko żądania do domen z listy Allegro (i nic do Google). Wejście w tym oknie na stronę spoza listy (np. ręcznie `example.com`) kończy się odmową proxy — to oczekiwane: to okno służy tylko agentowi.
- Istniejące testy (`uv run pytest`) przechodzą.

## Notatki implementacyjne

- `sites/` nie importuje Playwrighta, tak jak `engines/`.
- Modele Pydantic zamiast `dict` (reguła z `CLAUDE.md`): surowy wynik `extract_js` parsować na granicy do modelu (np. `RawOffer` z `title`, `href`, `price`, `snippet`, `is_ad`) i dalej przekazywać model. W `api` odpowiedź web-agent walidować do modelu `SearchResponse` po stronie `api` zamiast czytać `body.get(...)`. Przy okazji przepisać tak samo istniejące `build_results(raw: list[dict])` i `web_common.format_result(result: dict)`.
- Zasada dla każdego kolejnego serwisu w `sites/`: model dostaje tylko pola wskazane selektorami (tytuł, cena, URL, parametry). Treści pisane przez innych ludzi (opinie, komentarze, Q&A, opisy) są wyłączone domyślnie. Jeśli kiedyś będą potrzebne (np. „podsumuj opinie”), to osobne narzędzie z własnym planem, a nie rozszerzenie `site_search`.
- Selektory Allegro to najbardziej kruchy element. Stąd rozróżnienie `no_results` vs `error: unexpected page layout` (już jest w `SearchService`), które powie, kiedy selektory się zestarzały.
- `min_interval_s` z `RateLimiter` działa per `engine.name`, więc Allegro ma własny odstęp niezależny od SearXNG. Nie zwiększać go „dla ludzkości”; to uprzejmość wobec serwisu, nie maskowanie.
- Kod i komentarze po angielsku (konwencja repo), plan po polsku.
