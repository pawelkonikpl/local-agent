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
- Przy blokadzie anty-botowej (CAPTCHA, 403) narzędzie zwraca `blocked` i kończy. Nie obchodzimy zabezpieczeń (tak jak w `narzedzie-web-search.md`: łamie to regulamin serwisu i jest kruche).

Kod, na którym to stoi:
- `SearchEngine` (`web_agent/engines/base.py`): `name`, `extract_js`, `url_for`, `parse`, `detect_block`, `is_no_results`. Serwis to po prostu kolejna implementacja tego Protocolu, więc `SearchService.search` (Sense-Act-Verify, rate-limit, timeout, audyt) działa bez zmian.
- `build_results` (`engines/common.py`) odrzuca reklamy (`is_ad`). W sklepie oferty sponsorowane są prawdziwymi ofertami, więc tu je oznaczamy, a nie wyrzucamy.
- `WebSearchTool` (`api/tools/web_search.py`) jest wzorcem dla narzędzia po stronie `api`.

## Zakres

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
     - `parse`: `build_results(raw, max_results, allegro_url, ads="flag")`, gdzie `allegro_url` przyjmuje tylko `https://allegro.pl/...` (link wskazujący poza serwis jest odrzucany), obcina query string poza identyfikatorem oferty i zostawia ścieżkę `/oferta/...`;
     - `detect_block`: status 403/429/5xx albo strona ochrony anty-botowej (znaczniki tekstu i/lub obecność elementu wyzwania; ustalić na zapisanej stronie blokady) → powód, np. `"allegro.pl bot protection (captcha)"`;
     - `is_no_results`: znacznik tekstowy strony „brak wyników” z fixture'a.
   - `sites/__init__.py`: `SITES: dict[str, Callable[[], SearchEngine]] = {"allegro.pl": AllegroSite}`, `SITE_NAMES`, `get_site(name) -> SearchEngine` (nieznana nazwa → `ValueError`).
   - Dodanie kolejnego serwisu (np. `ceneo.pl`, `olx.pl`) = nowy plik w `sites/`, wpis w `SITES`, domeny w `allowlist.txt` proxy. Żadnych zmian w `api` poza listą w konfiguracji (pkt 7).
4. **HTTP (`main.py`)**:
   - `POST /v1/site-search` (body `SiteSearchQuery`, odpowiedź `SearchResponse`, ten sam bearer co `/v1/search`). Nieznany `site` → 422. Wywołuje `search_service.search(SearchQuery(query=…, max_results=…), get_site(site))`.
   - `GET /v1/sites` → `{"sites": SITE_NAMES}`, z tym samym bearerem.
5. **CLI (`cli.py`)**: nowa podkomenda `web-agent site <site> "<query>" [--max-results N] [--json] [--headful] [--cdp-url URL] [--screenshot out.png]`. Te same kody wyjścia co `search`. `format_text` dopisuje cenę i `[sponsored]`, gdy są.
6. **Deploy**: dopisać domeny Allegro (te z `extra_origins` i `allegro.pl`) do `deploy/egress-proxy/allowlist.txt`, z komentarzem `# site_search: allegro.pl`.

### api

7. **`api/tools/site_search.py` → `SiteSearchTool`**:
   - `name = "site_search"`, `capabilities = {"reads_untrusted", "external_effect"}`.
   - `description` (po angielsku): szuka bezpośrednio w wyszukiwarce wskazanego serwisu i zwraca oferty (tytuł, cena, URL); używać zamiast `web_search` z `site:`, gdy serwis jest na liście; ceny są odczytane ze strony i mogą się zmienić.
   - `input_schema`: `site` jako `enum` z `settings.web_agent_sites` (domyślnie `["allegro.pl"]`), `query` (wymagane), `max_results` 1–10.
   - Walidacja jak w `WebSearchTool._validate` (wydzielić wspólne sprawdzanie `query` i `max_results` do małej funkcji w `api/tools/web_common.py`, żeby nie kopiować) + `site` spoza listy → `ToolInputError`.
   - Wynik formatowany tym samym spotlightingiem co `web_search` (wspólna funkcja z planu bezpieczeństwa, pkt 9), z ceną w linii tytułu (`1. <title> — <price>`) i dopiskiem `[sponsored]`. `source="site_search:<site>"`.
   - Rejestrowany w `build_default_registry` obok `WebSearchTool`, gdy ustawione jest `web_agent_url` i lista `web_agent_sites` nie jest pusta.
8. **Prompt systemowy**: jedno zdanie w `web_rules` (plan bezpieczeństwa, pkt 12): ceny i dane ofert pochodzą ze strony serwisu i przed decyzją o zakupie użytkownik powinien je sprawdzić na stronie oferty. Model nie kupuje i nie wypełnia formularzy (nie ma takich narzędzi).

## Poza zakresem

- Wchodzenie na stronę pojedynczej oferty (szczegóły, dostępność, sprzedawca). To zadanie dla `web_fetch` albo `site_offer(site, offer_url)` z URL-em walidowanym względem serwisu. Osobny plan.
- Filtry i sortowanie serwisu (cena od/do, stan, kategoria) w parametrach narzędzia. Łatwe do dodania później jako pola `SiteSearchQuery` mapowane w `url_for`, ale każdy serwis ma inne.
- Kolejne serwisy poza Allegro. Konstrukcja ma je umożliwiać bez zmian w kodzie wspólnym.
- Oficjalne API Allegro (developer.allegro.pl). Warto sprawdzić, czy wyszukiwanie ofert jest dostępne dla naszego typu aplikacji. Jeśli tak, to byłaby osobna implementacja `SearchEngine` bez przeglądarki (stabilniejsza i zgodna z regulaminem), wymagająca rejestracji aplikacji i sekretu w env web-agent.
- Obchodzenie ochrony anty-botowej, logowanie, koszyk, zakupy.
- Testy automatyczne to krok 4 dev-flow. Kryteria niżej są dla nich źródłem.

## Ryzyko do sprawdzenia na początku implementacji

Duże serwisy, prawdopodobnie także Allegro, stosują ochronę przed botami, która może odrzucać headless Chromium z kontenera. **Pierwszy krok implementacji**: ręcznie `uv run web-agent site allegro.pl "rower gravel" --screenshot /tmp/a.png` najpierw lokalnie (`--headful`), potem w kontenerze.
- Jeśli w kontenerze strona konsekwentnie pokazuje wyzwanie, zatrzymać się i wrócić do planu (krok 1). Opcje: oficjalne API, `WEB_AGENT_CDP_URL` do przeglądarki użytkownika na hoście (poza kontenerem, z osobnym profilem bez zalogowanych sesji), albo inny serwis.
- Nie dodawać fałszywych nagłówków, opóźnień „dla ludzkości” ani innych technik maskowania.

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
- Test używa podmienionego originu (lokalny serwer udaje `allegro.pl` przez `url_for` wstrzyknięte w teście); żądania do originów spoza zbioru są blokowane (`blocked_requests` niepuste dla fixture'a z zewnętrznym skryptem).

**HTTP i api**
- `POST /v1/site-search` bez tokenu → 401; `{"site": "amazon.pl", "query": "x"}` → 422; poprawne body ze zmockowanym serwisem → 200 i body zgodne z `SearchResponse`.
- `SiteSearchTool.input_schema` ma `site.enum == ["allegro.pl"]`; input `{"site": "allegro.pl"}` bez `query` → `ToolInputError`; `{"site": "evil.example", "query": "x"}` → `ToolInputError`, bez żądania HTTP.
- Wynik `ok` w `_format`: znaczniki `untrusted_web_content` z `source="site_search:allegro.pl"`, cena w linii tytułu, `[sponsored]` przy ofertach sponsorowanych.
- `GenerationGuard`: po `site_search` generacja jest oznaczona jako tainted (tak jak po `web_search`).

**Ręcznie**
- `uv run web-agent site allegro.pl "rower gravel" --max-results 5` wypisuje 5 ofert z cenami i URL-ami `allegro.pl/oferta/…` w < 15 s (albo czytelne `blocked`, patrz „Ryzyko”).
- W czacie: „znajdź na allegro rower gravel do 3000 zł” → model woła `site_search` z `site="allegro.pl"`, a nie `web_search` z `site:`.
- W logach `egress-proxy` są tylko żądania do domen z listy Allegro (i nic do Google).
- Istniejące testy (`uv run pytest`) przechodzą.

## Notatki implementacyjne

- `sites/` nie importuje Playwrighta, tak jak `engines/`.
- Selektory Allegro to najbardziej kruchy element. Stąd rozróżnienie `no_results` vs `error: unexpected page layout` (już jest w `SearchService`), które powie, kiedy selektory się zestarzały.
- `min_interval_s` z `RateLimiter` działa per `engine.name`, więc Allegro ma własny odstęp niezależny od SearXNG. Nie zwiększać go „dla ludzkości”; to uprzejmość wobec serwisu, nie maskowanie.
- Kod i komentarze po angielsku (konwencja repo), plan po polsku.
