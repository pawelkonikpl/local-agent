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

**Zmiana decyzji po prototypie (28.09.2026, zastępuje wariant z hostem):** prawdziwy Google Chrome **z oknem na wirtualnym ekranie (Xvfb) w kontenerze podmana** na komputerze użytkownika, uruchomiony przez nas z `--remote-debugging-port` (nie przez Playwright, więc bez flag automatyzacji), dostaje listing Allegro. Prototyp: 10/10 ofert z ceną w 4–5 s. Pierwsze 1–3 zapytania na świeżym profilu dostają 403 ze stroną DataDome, ale to **niewidoczny test urządzenia** (`ct.captcha-delivery.com/i.js`), nie captcha: wykonuje się sam w przeglądarce i ustawia ciasteczko. Z hosta `curl` dostaje 403, więc samo domowe IP nie wystarcza; potrzebna jest prawdziwa przeglądarka. Wariant z hostem odpada, bo: wymaga `0.0.0.0` + firewalla (kontener nie dosięga `127.0.0.1` hosta w podman machine na macOS — sprawdzone), dwóch ręcznie uruchamianych skryptów i przeglądarki poza izolacją kontenera. Sekcja „Przeglądarka w kontenerze `site-agent`” niżej zastępuje „Przeglądarkę na hoście”.

Kod, na którym to stoi:
- `SearchEngine` (`web_agent/engines/base.py`): `name`, `extract_js`, `url_for`, `parse`, `detect_block`, `is_no_results`. Serwis to po prostu kolejna implementacja tego Protocolu, więc `SearchService.search` (Sense-Act-Verify, rate-limit, timeout, audyt) działa bez zmian.
- `build_results` (`engines/common.py`) odrzuca reklamy (`is_ad`). W sklepie oferty sponsorowane są prawdziwymi ofertami, więc tu je oznaczamy, a nie wyrzucamy.
- `WebSearchTool` (`api/tools/web_search.py`) jest wzorcem dla narzędzia po stronie `api`.

## Zakres

### Przeglądarka w kontenerze `site-agent`

Układ:
```
api --HTTP+bearer (sieć sites)--> site-agent: web-agent (sites_only) --CDP 127.0.0.1:9222--> Google Chrome (Xvfb, ten sam kontener)
                                                                                                  |
                                                                          --proxy-server--> egress-proxy (sieć sites)
człowiek --http://localhost:6080--> site-vnc (noVNC) --VNC (sieć sites)--> x11vnc w site-agent
```

A. **`deploy/Containerfile.site-agent`**: `python:3.12-slim` + `google-chrome-stable` (tylko amd64; podman machine użytkownika jest amd64) + `xvfb` + `x11vnc`, web-agent jak w `Containerfile.web-agent`, ale bez przeglądarek Playwrighta (`connect_over_cdp` ich nie potrzebuje). `deploy/site-agent/entrypoint.sh` uruchamia Xvfb, x11vnc, Chrome i uvicorn; gdy którykolwiek proces padnie, kontener kończy się (compose: `restart: unless-stopped`).
   - Chrome: `--user-data-dir=/tmp/chrome-profile` (tmpfs: profil ginie przy restarcie, więc brak trwałego stanu; ciasteczko DataDome zdobywa się od nowa przez pkt F), `--remote-debugging-port=9222` (Chrome słucha wtedy tylko na `127.0.0.1` kontenera), `--proxy-server=http://egress-proxy:3128`, `--no-sandbox` (jak Chromium Playwrighta w `web-agent`: sandbox Chrome'a nie działa w kontenerze bez dodatkowych uprawnień; izolację daje kontener), `--no-first-run --no-default-browser-check`.
   - Kontener jak `web-agent`: `read_only`, `tmpfs /tmp`, `cap_drop: ALL`, `no-new-privileges`, użytkownik 10001, limity pamięci/CPU.
   - Żadnych logowań w tym profilu; nikt poza agentem i człowiekiem przez noVNC z niego nie korzysta.
B. **Sieci**: nowa sieć `sites` (`internal: true`) dla `site-agent`, `api`, `egress-proxy` i `site-vnc`. `site-agent` **nie** jest w sieci `web`, więc headless `web-agent` (czytający dowolne strony) nie dosięga ani CDP, ani VNC tej przeglądarki. `api` łączy się pod `WEB_AGENT_SITES_URL` (domyślnie `http://site-agent:8080`).
C. **Kontekst przeglądarki**: tryb `reuse_default_context` jak w fazie 1 (nowa karta w domyślnym kontekście profilu, zamykana w `finally`). Filtr originów przez CDP `Fetch` działa bez zmian.
D. **Blokada = przekazanie człowiekowi** (dla prawdziwej captchy, gdy pkt F nie wystarczy): karta zostaje otwarta (najwyżej jedna na serwis), `SiteSearchTool` prosi model, żeby poprosił użytkownika o rozwiązanie weryfikacji na ekranie przeglądarki pod `http://localhost:6080` i o ponowienie prośby. Agent nigdy nie klika w wyzwanie sam.
E. **`site-vnc`** (`deploy/Containerfile.site-vnc`: `debian:trixie-slim` + `novnc` + `websockify`): `websockify --web /usr/share/novnc 6080 site-agent:5900`, port `127.0.0.1:6080:6080`. Opublikowany port wymaga sieci z wyjściem, więc `site-vnc` jest też w osobnej sieci `site-vnc-out` (nie-internal); nic poza nim w niej nie ma. x11vnc bez hasła, ale osiągalny tylko z sieci `sites` (api, egress-proxy, site-vnc).
F. **Niewidoczny test anty-botowy**: serwis deklaruje `block_settle_s` (Allegro: 10 s; DuckDuckGo, SearXNG: 0). Gdy `detect_block` zgłosi blokadę, `SearchService` czeka do `block_settle_s` na ponowne załadowanie strony przez nią samą (strona testu przeładowuje się po zaliczeniu, jak u zwykłego użytkownika) i sprawdza blokadę jeszcze raz na nowym dokumencie. Bez ponownego załadowania albo z blokadą po nim: `blocked` jak w pkt D. To nie jest obchodzenie zabezpieczeń: nic nie klikamy, niczego nie podrabiamy, strona sama ocenia przeglądarkę. Jedna nawigacja od nas, zero ponowień.
G. **Fixture'y**: `web-agent site ... --save-html` (CLI) — tak zapisano `raspberry_pi_5.html` w prototypie; selektory w `sites/allegro.py` ustalone na nim.

### web-agent

1. **Model wyniku (`models.py`)**:
   - `SearchResult` dostaje `price: str | None = None` (tekst ceny tak, jak pokazuje strona, po normalizacji whitespace, np. `"1 299,00 zł"`; bez parsowania na liczbę, bo formaty są różne).
   - Nowy `SiteSearchQuery`: `site: str`, `query` (jak w `SearchQuery`: 1–400 znaków po `strip()`), `max_results: int = 5` (1–10), `sort` (niżej). Bez `region`.
   - `SearchQuery` i `SiteSearchQuery` dostają `sort: Literal["relevance", "price_asc", "price_desc"] = "relevance"`. Silniki web (DuckDuckGo, SearXNG) go ignorują; serwis mapuje go w `url_for` (Allegro: `order=p` / `order=pd`, dla `relevance` bez parametru).
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
   - dopisać domeny Allegro (`allegro.pl`, te z `extra_origins`, domeny DataDome) do `deploy/egress-proxy/allowlist.txt`, z komentarzem `# site_search: allegro.pl`;
   - `podman-compose.yml`: usługi `site-agent` i `site-vnc`, sieci `sites` i `site-vnc-out` (pkt A–E), `api` w sieci `sites` z `WEB_AGENT_SITES_URL: ${WEB_AGENT_SITES_URL-http://site-agent:8080}`; `egress-proxy` w sieci `sites`, **bez** portu na hoście;
   - konfiguracja web-agent (`config.py`): `sites_only: bool = False`, `sites_enabled: bool = False` (`sites_only` włącza też `sites_enabled`), `reuse_default_context: bool = False`;
   - `.env.example` i README: krótko — `site_search` działa w kontenerze `site-agent`, ekran przeglądarki pod `http://localhost:6080`, tylko amd64. Skrypty hosta (`scripts/site-browser.sh`, `scripts/site-agent.sh`) usunąć.

### api

7. **`api/tools/site_search.py` → `SiteSearchTool`**:
   - `name = "site_search"`, `capabilities = {"reads_untrusted", "external_effect"}`.
   - `description` (po angielsku): szuka bezpośrednio w wyszukiwarce wskazanego serwisu i zwraca oferty (tytuł, cena, URL); używać zamiast `web_search` z `site:`, gdy serwis jest na liście; ceny są odczytane ze strony i mogą się zmienić.
   - `input_schema`: `site` jako `enum` z `settings.web_agent_sites` (domyślnie `["allegro.pl"]`), `query` (wymagane), `max_results` 1–10, `sort` jako `enum` `relevance` / `price_asc` / `price_desc` (domyślnie `relevance`). Opis: dla „najtańszy”, „najtaniej” użyj `price_asc` i konkretnego zapytania (sortowanie po cenie wyciąga też akcesoria).
   - Walidacja jak w `WebSearchTool._validate` (wydzielić wspólne sprawdzanie `query` i `max_results` do małej funkcji w `api/tools/web_common.py`, żeby nie kopiować) + `site` spoza listy → `ToolInputError`.
   - Wynik formatowany tym samym spotlightingiem co `web_search` (wspólna funkcja z planu bezpieczeństwa, pkt 9), z ceną w linii tytułu (`1. <title> — <price>`) i dopiskiem `[sponsored]`. `source="site_search:<site>"`.
   - Łączy się z `settings.web_agent_sites_url` (kontener `site-agent`, nie `web_agent_url`). Rejestrowany w `build_default_registry`, gdy `web_agent_sites_url` jest ustawione i lista `web_agent_sites` nie jest pusta.
   - Brak połączenia (`site-agent` nie działa) → `ToolResult` z błędem: usługa wyszukiwania w sklepach nie działa, poproś użytkownika o `podman-compose up -d site-agent`.
   - `status="blocked"` → komunikat dla modelu: poproś użytkownika o rozwiązanie weryfikacji na ekranie przeglądarki pod `settings.site_browser_view_url` (domyślnie `http://localhost:6080/vnc.html?autoconnect=1&resize=scale`) i o ponowienie prośby; nie ponawiaj sam.
8. **Prompt systemowy**: jedno zdanie w `web_rules` (plan bezpieczeństwa, pkt 12): ceny i dane ofert pochodzą ze strony serwisu i przed decyzją o zakupie użytkownik powinien je sprawdzić na stronie oferty. Model nie kupuje i nie wypełnia formularzy (nie ma takich narzędzi).

## Poza zakresem

- Wchodzenie na stronę pojedynczej oferty (szczegóły, dostępność, sprzedawca). To zadanie dla `web_fetch` albo `site_offer(site, offer_url)` z URL-em walidowanym względem serwisu. Osobny plan.
- Filtry serwisu (cena od/do, stan, kategoria) w parametrach narzędzia. Sortowanie po cenie jest w zakresie (pkt 1); filtry łatwo dodać później tak samo, jako pola `SiteSearchQuery` mapowane w `url_for`.
- Kolejne serwisy poza Allegro. Konstrukcja ma je umożliwiać bez zmian w kodzie wspólnym.
- Oficjalne API Allegro (developer.allegro.pl). Warto sprawdzić, czy wyszukiwanie ofert jest dostępne dla naszego typu aplikacji. Jeśli tak, to byłaby osobna implementacja `SearchEngine` bez przeglądarki (stabilniejsza i zgodna z regulaminem), wymagająca rejestracji aplikacji i sekretu w env web-agent.
- Obchodzenie ochrony anty-botowej, logowanie, koszyk, zakupy.
- Testy automatyczne to krok 4 dev-flow. Kryteria niżej są dla nich źródłem.

## Ryzyka

- **Headless z kontenera w chmurze: zablokowane; Chrome z oknem w kontenerze lokalnie: przechodzi** (prototyp, patrz „Zmiana decyzji”). Jeśli DataDome zacznie pokazywać captchę przy każdym zapytaniu, wrócić do planu (krok 1): oficjalne API albo inny serwis.
- **`site-agent` musi działać**, żeby narzędzie działało. Narzędzie mówi to modelowi wprost (pkt 7), zamiast udawać awarię wyszukiwarki.
- **Tylko amd64**: Google Chrome nie ma linuksowej wersji arm64. Na arm64 trzeba by Chromium z Debiana (nie sprawdzone z DataDome).
- **Czy strona testu DataDome przeładowuje się sama** (pkt F): do sprawdzenia na świeżym profilu. Jeśli nie, pierwsze zapytanie po starcie kontenera kończy się `blocked`, a kolejne przechodzą.
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
- Instancja z `sites_only=true`: `POST /v1/search` → 404, `/v1/site-search` działa. Instancja domyślna (`web-agent`): `/v1/site-search` → 404.
- `reuse_default_context=true`: po 5 wyszukiwaniach liczba otwartych kart w przeglądarce wraca do stanu sprzed (poza ewentualną jedną kartą z wyzwaniem), kontekst nie jest tworzony ani zamykany.
- Blokada w trybie `reuse_default_context` zostawia kartę otwartą; kolejne zapytanie do tego samego serwisu najpierw ją zamyka (nigdy więcej niż jedna karta z wyzwaniem na serwis).
- `block_settle_s`: strona blokady, która w tym czasie sama przeładuje się na zwykłą stronę → wynik z nowej strony; bez przeładowania → `blocked` po `block_settle_s`; silnik z `block_settle_s = 0` → `blocked` od razu (DuckDuckGo jak dziś).
- `AllegroSite().url_for(SearchQuery(query="x", sort="price_asc"))` → `…/listing?string=x&order=p`; `price_desc` → `order=pd`; `relevance` → bez `order`.

**Ręcznie**
- `podman-compose up -d --build site-agent site-vnc api`: `site-agent` zdrowy; `http://localhost:6080/vnc.html` pokazuje ekran przeglądarki; z innego komputera w sieci port 6080 jest niedostępny.
- Świeży kontener, pierwsze zapytanie w czacie „znajdź na allegro najtańsze raspberry pi 4” → `site_search(site="allegro.pl", sort="price_asc")`, oferty z cenami rosnąco i URL-ami `allegro.pl/oferta/…` w < 20 s (w tym czekanie z pkt F).
- W czacie: „znajdź na allegro rower gravel do 3000 zł” → model woła `site_search` z `site="allegro.pl"`, a nie `web_search` z `site:`.
- `podman-compose stop site-agent` → narzędzie w czacie zwraca komunikat o niedziałającej usłudze.
- `podman exec local-agent_web-agent_1 python -c "import urllib.request as u; u.urlopen('http://site-agent:8080/health', timeout=3)"` → błąd (brak trasy): headless web-agent nie widzi `site-agent`.
- W logach `egress-proxy` przy wyszukiwaniu są tylko hosty z listy Allegro/DataDome.
- Istniejące testy (`uv run pytest`) przechodzą.

## Notatki implementacyjne

- `sites/` nie importuje Playwrighta, tak jak `engines/`.
- Modele Pydantic zamiast `dict` (reguła z `CLAUDE.md`): surowy wynik `extract_js` parsować na granicy do modelu (np. `RawOffer` z `title`, `href`, `price`, `snippet`, `is_ad`) i dalej przekazywać model. W `api` odpowiedź web-agent walidować do modelu `SearchResponse` po stronie `api` zamiast czytać `body.get(...)`. Przy okazji przepisać tak samo istniejące `build_results(raw: list[dict])` i `web_common.format_result(result: dict)`.
- Zasada dla każdego kolejnego serwisu w `sites/`: model dostaje tylko pola wskazane selektorami (tytuł, cena, URL, parametry). Treści pisane przez innych ludzi (opinie, komentarze, Q&A, opisy) są wyłączone domyślnie. Jeśli kiedyś będą potrzebne (np. „podsumuj opinie”), to osobne narzędzie z własnym planem, a nie rozszerzenie `site_search`.
- Selektory Allegro to najbardziej kruchy element. Stąd rozróżnienie `no_results` vs `error: unexpected page layout` (już jest w `SearchService`), które powie, kiedy selektory się zestarzały.
- `min_interval_s` z `RateLimiter` działa per `engine.name`, więc Allegro ma własny odstęp niezależny od SearXNG. Nie zwiększać go „dla ludzkości”; to uprzejmość wobec serwisu, nie maskowanie.
- Kod i komentarze po angielsku (konwencja repo), plan po polsku.
