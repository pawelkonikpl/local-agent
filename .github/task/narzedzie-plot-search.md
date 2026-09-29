# Narzędzia `plot_search` i `plot_details`: ogłoszenia działek z portali nieruchomości (etap 1 monitora działek)

## Kontekst

Skill `monitor-dzialek-gorskich` (kopia w `.agents/sillks/monitor-dzialek-gorskich/`) to powtarzalny przegląd działek budowlanych w górach PL/SK: kryteria i lokalizacje z plików referencyjnych → osobne zapytanie na każdą miejscowość i portal → filtr budżetu → sprawdzenie ogłoszenia na jego stronie → raport z porównaniem do poprzedniego przebiegu.

Dziś w czacie nie da się tego zrobić sensownie:
- `web_search` zwraca tylko tytuł i snippet z SearXNG, często sprzed miesięcy (skill wprost ostrzega przed cenami ze snippetów);
- `site_search` zna tylko Allegro i zwraca tytuł + cenę, bez powierzchni, zł/m², typu działki i daty;
- w przeglądzie 20.09.2026 (sesja bez przeglądarki) otodom i OLX dawały 403, adresowo listę ogólnopolską. `site-agent` (prawdziwy Chrome z oknem, Xvfb) przeszedł ochronę Allegro, więc jest szansa, że przejdzie i te portale.

**Decyzje użytkownika (28.09.2026):**
- Dwa etapy. **Etap 1 (ten plan):** portale nieruchomości w web-agent + bezstanowe narzędzia w `api`. **Etap 2 (`agent-monitor-dzialek.md`):** agent-orkiestrator, który zna workflow skilla, wie, które narzędzia wołać i jakie informacje im przekazać.
- **Narzędzia są bezstanowe.** Nie znają budżetu, regionów ani poprzednich przeglądów. Kryteria, kolejność regionów, filtrowanie, porównanie i raport należą do agenta (etap 2). Narzędzie dostaje w argumentach wszystko, czego potrzebuje do jednego wyszukiwania.
- Portale w etapie 1: **nieruchomosci-online.pl, otodom.pl, olx.pl, nehnutelnosti.sk, adresowo.pl**.

Kod, na którym to stoi:
- `SearchService.search` (`web_agent/search.py`): Sense-Act-Verify, filtr originów, rate-limit per `engine.name`, `block_settle_s`, karta zostawiana człowiekowi przy blokadzie, audyt.
- `web_agent/sites/` + `allegro.py`: wzorzec serwisu (stałe selektory na górze, allowlista pól, `EXCLUDED_CONTAINERS`, `MarkerEngine`).
- `build_results` (`engines/common.py`): `sanitize`, sygnały injection, `withheld`.
- `WebAgentClient`, `spotlight`, `WebAgentTool` (`api/tools/web_agent.py`) i `SiteSearchTool` jako wzorzec narzędzia.
- Instancja `site-agent` (`sites_only`, `reuse_default_context`, egress przez `egress-proxy` z allowlistą).

## Zakres

### 0. Rozpoznanie portali (przed kodem, jak prototyp Allegro)

Dla każdego portalu, w kontenerze `site-agent`, zapisać fixture'y przez CLI (pkt 5) i ustalić, a potem wpisać do docstringu modułu:
- **czy listing przechodzi** w `site-agent` (status, strona ochrony, czy `block_settle_s` pomaga). Portal, który mimo to zwraca blokadę przy każdym zapytaniu, **nie trafia do `PORTALS`**; decyzję i datę zapisać w sekcji „Decyzje po rozpoznaniu” tego pliku (krok 1 dev-flow), zamiast obchodzić ochronę;
- **schemat URL listingu działek na sprzedaż w miejscowości** i parametry: cena do, powierzchnia od, sortowanie;
- **jak nazwa miejscowości zamienia się w lokalizację portalu** (pkt 2);
- **czy dane ofert są w osadzonym JSON** (`__NEXT_DATA__` na otodom i nehnutelnosti.sk, `window.__PRERENDERED_STATE__` na OLX). Jeśli tak i jest stabilniejszy od DOM, czytać z niego, ale **tylko pola z allowlisty** (pkt 1), przez model Pydantic, nigdy „cały obiekt”;
- znaczniki „brak wyników”, strony blokady, ogłoszenia nieaktualnego/zakończonego;
- originy potrzebne do wyrenderowania (DevTools → Network) i hosty do `egress-proxy/allowlist.txt`.

Punkt wyjścia z `serwisy.md` skilla (do potwierdzenia):

| Portal | Listing | Uwagi |
|---|---|---|
| nieruchomosci-online.pl | `{miejscowosc}.nieruchomosci-online.pl/dzialki,sprzedaz/` | pokazuje też ogłoszenia archiwalne — liczyć tylko aktywne |
| otodom.pl | `otodom.pl/pl/wyniki/sprzedaz/dzialka/{woj}/{powiat}/{gmina}/{miejscowosc}` | ścieżka hierarchiczna, wymaga rozwiązania lokalizacji |
| olx.pl | `olx.pl/nieruchomosci/dzialki/sprzedaz/{miejscowosc}/` | oferty „wyróżnione” = sponsorowane |
| nehnutelnosti.sk | `nehnutelnosti.sk/{obec}/pozemky/predaj/` | pokazuje medianę cen w lokalizacji |
| adresowo.pl | `adresowo.pl/dzialki/{miejscowosc}{-przyrostek}/` | przyrostki (`korbielow-4`) nie do zgadnięcia — potrzebny lookup |

### web-agent

1. **Kontrakt (`libs/contracts/src/contracts/listings.py`, nowy plik, re-eksport w `web_agent/models.py`):**
   - `LISTING_SEARCH_PATH = "/v1/listing-search"`, `LISTING_DETAILS_PATH = "/v1/listing-details"`, `PORTALS_PATH = "/v1/portals"`.
   - `ListingSort = Literal["relevance", "newest", "price_asc"]` (tylko to, co mają wszystkie portale; zł/m² liczy agent).
   - `ListingStatus = Literal["ok", "no_results", "unknown_location", "blocked", "error"]`.
   - `ListingSearchRequest`: `portal: str`, `location: str` (1–100 znaków po `strip()`, np. „Białka Tatrzańska”, „Oravská Lesná”), `max_price: int | None` (≥ 1, w walucie portalu), `min_area_m2: int | None` (≥ 1), `max_results: int = 10` (1–20), `sort: ListingSort = "newest"`.
   - `Listing`: `rank`, `title`, `url`, `location: str | None` (tak, jak pokazuje portal), `price: Decimal | None`, `currency: Literal["PLN", "EUR"] | None`, `area_m2: Decimal | None`, `price_per_m2: Decimal | None` (z portalu albo policzone, gdy są cena i powierzchnia), `plot_type: str | None` (dosłownie z portalu: „budowlana”, „rolna”, „rolno-budowlana”, „stavebný pozemok”, „rekreačný pozemok”…; bez tłumaczenia i bez zgadywania), `listed_at: date | None`, `updated_at: date | None`, `private_seller: bool | None`, `flags: list[str]`, `withheld: bool`.
   - `ListingSearchResponse`: `status`, `portal`, `location` (z zapytania), `resolved_location: str | None` (co portal dopasował — nagłówek/okruszki strony wyników; pozwala agentowi zauważyć, że „Groń” trafił w inną wieś), `total_count: int | None` (licznik ofert portalu), `search_url: str | None` (strona wyników do otwarcia przez człowieka), `results`, `error`, `elapsed_ms`.
   - `ListingDetailsRequest`: `portal: str`, `url: str`.
   - `ListingDetailsResponse`: `status: Literal["ok", "inactive", "blocked", "error"]`, `portal`, `listing: Listing | None`, `params: list[ListingParam]` (`name`, `value`; np. „Media: prąd, woda”, „Dojazd: asfaltowy”, „Kształt”, „Wymiary”, „Ogrodzenie”, „Typ ogłoszeniodawcy”, na SK „Inžinierske siete”, „Prístupová cesta”), `error`, `elapsed_ms`.
   - Walidacja kwot i powierzchni w modelu, jak w `SearchRequest`; na granicy tylko modele, nie `dict` (`CLAUDE.md`).
2. **Rozwiązywanie lokalizacji** (per portal, ustalone w pkt 0):
   - `slug`: deterministyczna transliteracja (`ą→a`, `ł→l`, `č→c`, `ý→y`…, spacje → `-`, tylko `[a-z0-9-]`). Wynik nigdy nie zawiera innych znaków, więc model nie wstawi nic do ścieżki URL-a.
   - `lookup`: autocomplete lokalizacji portalu wołany w tej samej karcie (ta sama sesja przeglądarki i te same originy) przed nawigacją na listing; odpowiedź parsowana do modelu Pydantic, bierzemy pierwszy kandydat typu „miejscowość” (a nie ulica/region), w odpowiedzi `resolved_location`. Brak kandydata → `unknown_location` z listą do 3 nazw zaproponowanych przez portal w `error` (agent może przeformułować zapytanie: gmina zamiast wsi — Krok 2 skilla).
   - Strona wyników, która pokazuje inną lokalizację niż zapytanie albo przekierowuje na ogólnopolską listę (adresowo w sesji 20.09) → `unknown_location`, nie `ok`. Wykrywanie: `resolved_location` z nagłówka strony porównany z zapytaniem po transliteracji.
3. **Portale (`web_agent/portals/`, nie importują Playwrighta, jak `sites/`):**
   - Protocol `ListingPortal` (`portals/base.py`): `name`, `hosts`, `extra_origins`, `block_settle_s`, `currency`, `extract_js`, `details_js`, `resolve_location(...)`, `url_for(request, location) -> str`, `parse(raw, max_results) -> list[Listing]`, `parse_details(raw) -> (Listing, list[ListingParam])`, `detect_block`, `is_no_results`, `is_inactive`, `offer_url(url) -> str | None` (walidacja URL-a z `plot_details`, pkt 4).
   - Po jednym module na portal: `nieruchomosci_online.py`, `otodom.py`, `olx.py`, `nehnutelnosti_sk.py`, `adresowo.py`; `portals/__init__.py`: `PORTALS`, `PORTAL_NAMES`, `get_portal(name)` (nieznana nazwa → `ValueError`), jak `sites/__init__.py`.
   - Wspólne parsowanie w `portals/common.py`: cena z tekstu („290 000 zł”, „68 800 €”, „cena do negocjacji” → `None`), powierzchnia („870 m²”, „0,87 ha” → 8700, „8,7 a” (ar) → 870), daty („dodano 12.09.2026”, „dzisiaj”, „wczoraj” → data względem zegara wstrzykniętego w parserze). Czyste funkcje z testami.
   - **Allowlista pól** (zasada z `narzedzie-site-search.md`): z kafelka tylko tytuł, cena, powierzchnia, zł/m², lokalizacja, typ działki, daty, typ ogłoszeniodawcy, znacznik „wyróżnione/promowane”. Na stronie ogłoszenia (`details_js`) tylko te pola plus **tabela parametrów strukturalnych** (nazwa → wartość, każda wartość maks. 150 znaków, maks. 20 parametrów). **Opis ogłoszenia nie jest czytany** (tekst sprzedawcy; skill i tak każe streszczać, a nie kopiować) — narzędzie mówi modelowi, że opis trzeba przeczytać na stronie. Nie czytamy też komentarzy, „podobnych ofert”, reklam, danych kontaktowych (telefon, e-mail, imię sprzedawcy).
   - `EXCLUDED_CONTAINERS` jak w `allegro.py` jako druga warstwa.
   - Wszystkie teksty przechodzą przez `sanitize` i `injection_signals` jak w `build_results`; wydzielić z `build_results` funkcję, która robi to dla jednego wpisu, i użyć jej w obu miejscach (nie kopiować).
   - Oferty wyróżnione/promowane zostają, z flagą `sponsored` (to prawdziwe oferty).
   - Portal zwraca to, co dał portal: jeśli URL-em nie da się zadać `max_price`/`min_area_m2`, `parse` odrzuca oferty poza nimi po odczycie (oferta bez ceny/powierzchni zostaje — agent zdecyduje). W odpowiedzi nie ma ofert ponad `max_price`.
4. **Strona ogłoszenia (`plot_details`)**: `offer_url(url)` przyjmuje tylko `https://` + host z `hosts` portalu + ścieżkę pasującą do wzorca oferty portalu (np. otodom `/pl/oferta/…`, OLX `/d/oferta/…`, nehnutelnosti.sk `/detail/…`), zwraca URL bez query stringu i fragmentu; inaczej `None` → HTTP 422. To jedyne miejsce, w którym URL pochodzi od modelu, i jest ograniczone do stron ofert jednego portalu. Ogłoszenie zakończone/usunięte (znacznik z pkt 0 albo 404/410) → `status="inactive"`.
5. **Serwis i HTTP:**
   - Wydzielić z `SearchService.search` wspólną wizytę na stronie (limit, sesja z originami, `navigate`, `detect_block` + `block_settle_s`, `keep_open_for_human`, `evaluate`, audyt, timeout, `BrowserError`) do jednej metody używanej przez `search`, `listing_search` i `listing_details`. Zachowanie `search` bez zmian — istniejące testy przechodzą bez modyfikacji.
   - `ListingService` (albo metody `SearchService`) z `listing_search(request, portal)` i `listing_details(request, portal)`; `lookup` lokalizacji to dodatkowe `evaluate` (fetch z originu portalu) w tej samej sesji, przed nawigacją — liczy się do tego samego timeoutu.
   - `main.py`: `POST /v1/listing-search`, `POST /v1/listing-details`, `GET /v1/portals` — ta sama zależność `_require_sites` i bearer co `/v1/site-search`. Nieznany `portal` albo zły URL oferty → 422.
   - CLI: `web-agent listings <portal> "<miejscowość>" [--max-price N] [--min-area N] [--sort …] [--json] [--save-html out.html] [--cdp-url URL]` oraz `web-agent listing <portal> <url> …`. Do zapisu fixture'ów w pkt 0.
6. **Deploy:** hosty portali (+ originy z pkt 0) w `deploy/egress-proxy/allowlist.txt` pod komentarzem `# plot_search: <portal>`. Bez nowych kontenerów — portale obsługuje `site-agent`.

### api

7. **`api/tools/plot_search.py` → `PlotSearchTool`** (podklasa `WebAgentTool` albo jej odpowiednik dla nowego kontraktu — jeśli `WebAgentTool` jest za mocno związany z `SearchResponse`, uogólnić go typem odpowiedzi zamiast kopiować HTTP i obsługę błędów):
   - `name = "plot_search"`, `capabilities = {"reads_untrusted", "external_effect"}`.
   - `description` (po angielsku), mniej więcej: *„Searches one real-estate portal for building plots for sale in one town or village (Poland: …; Slovakia: …) and returns listings with price, area, price per m², plot type, dates and URL. One call = one portal + one locality; to survey a region, call it for each locality and portal. Use Slovak place names for Slovak portals. Plot type is what the portal states — 'rolna' / 'rekreačný' is not a building plot.”*
   - `input_schema`: `portal` (`enum` z `settings.web_agent_portals`), `location` (wymagane, przykład), `max_price` (integer, „w walucie portalu: PLN dla .pl, EUR dla .sk”), `min_area_m2`, `max_results` (1–20), `sort` (`enum`).
   - Wynik (spotlighting, `source="plot_search:<portal>"`), jedna oferta = dwie linie:
     `1. Czarna Góra — 290 000 PLN · 870 m² · 333 PLN/m² · budowlana · dodano 2026-09-12 · prywatne`
     `   https://…`
     Nagłówek: portal, `resolved_location`, `total_count`, zastosowane filtry, `search_url`. Brakujące pole → `?`, nie pomijane (agent ma widzieć, czego brakuje).
   - `unknown_location` → zwykły (nie błędny) wynik z podpowiedziami portalu i zdaniem: spróbuj gminy albo sąsiedniej miejscowości.
   - `blocked` → jak w `SiteSearchTool`: poproś użytkownika o rozwiązanie weryfikacji pod `site_browser_view_url`, nie ponawiaj.
8. **`api/tools/plot_details.py` → `PlotDetailsTool`**: `name = "plot_details"`, input `portal` + `url` (URL z wyniku `plot_search`). Wynik: pola oferty, parametry strukturalne, daty i zdanie „Description not read — ask the user to read it on the page before relying on it”. `inactive` → „listing is no longer active” (zwykły wynik — dla agenta to informacja ❌ „zniknęła z rynku”). URL spoza portalu → `ToolInputError` jeszcze w `api`, przed żądaniem HTTP (ta sama walidacja co w web-agent: host + wzorzec ścieżki, wspólna lista wzorców w `contracts`).
9. **Konfiguracja:** `web_agent_portals: list[str]` w `api/config.py` (domyślnie portale, które przeszły pkt 0). Rejestracja w `build_default_registry`, gdy `web_agent_sites_url` ustawione i lista nie jest pusta. `.env.example` i README: jedno zdanie.
10. **Prompt systemowy:** zdanie 7 w `WEB_RULES` rozszerzyć o ogłoszenia nieruchomości (ceny i dane są z portalu z chwili wyszukiwania; opis i stan prawny trzeba sprawdzić u źródła).

## Poza zakresem

- Workflow skilla: kolejność regionów, budżet, filtr twardych kryteriów, Top 3, raport, porównanie z poprzednim przeglądem, propozycje zmian w plikach referencyjnych — to etap 2 (`agent-monitor-dzialek.md`).
- Kolejne portale z `serwisy.md` (domiporta, okolica.pl, kazo.pl, reality.sk, bazos.sk, lokalne biura). Konstrukcja ma je umożliwiać bez zmian w kodzie wspólnym.
- Paginacja (tylko pierwsza strona wyników). Przy `total_count` większym niż liczba wyników agent zawęża filtry.
- Narzędzia weryfikacyjne (geoportal, SOPO/PIG, ZBGIS, MPZP gminy, nocowanie.pl, kurs NBP). Osobny plan, jeśli agent ma z nich korzystać.
- Czytanie opisów ogłoszeń. Jeśli okaże się niezbędne, to osobny plan z oceną ryzyka injection.
- Przeliczanie walut — robi agent (kurs podaje użytkownik w kryteriach).
- Testy automatyczne to krok 4 dev-flow; kryteria niżej są dla nich źródłem.

## Ryzyka

- **Ochrona anty-botowa otodom/OLX** może nie przepuścić nawet `site-agent`. Wtedy portal wypada z etapu 1 (pkt 0), bez obchodzenia zabezpieczeń.
- **Lokalizacja to najbardziej kruchy element** (homonimy wsi, przyrostki adresowo, hierarchia otodom). Stąd `resolved_location` i `unknown_location` zamiast cichego zwracania ofert z innego miejsca.
- **Regulaminy portali.** Pojedyncze wyszukiwania na prośbę użytkownika, z `min_interval_s` per portal, bez masowego pobierania; to samo podejście co przy Allegro. Etap 2 (kilkanaście–kilkadziesiąt zapytań na przegląd) — nie zwiększać równoległości ponad `max_concurrent_searches`.
- **Czas:** jedno wywołanie = lookup + jedna nawigacja; musi zmieścić się w `web_search_timeout_s` (28 s) i `tool_timeout_s` (30 s). `block_settle_s` per portal wlicza się w ten czas.
- **Selektory/JSON portali się zmieniają.** Rozróżnienie `no_results` vs `error: unexpected page layout` jak w `SearchService`.

## Kryteria akceptacji

**Czyste funkcje (bez przeglądarki)**
- Parsowanie: `"290 000 zł"` → `290000 PLN`; `"68 800 €"` → `68800 EUR`; `"Zapytaj o cenę"` → `None`; `"0,87 ha"` → `8700`; `"870 m²"` → `870`; `"8,7 a"` → `870`; `"dzisiaj"` przy zegarze 2026-09-28 → `2026-09-28`.
- `price_per_m2` liczone, gdy portal go nie podaje, zaokrąglone do 0,01; brak ceny lub powierzchni → `None`.
- Slug: `"Białka Tatrzańska"` → `bialka-tatrzanska`; `"Oravská Lesná"` → `oravska-lesna`; `"Łapsze Niżne"` → `lapsze-nizne`; `"../../x?y"` → tylko `[a-z0-9-]`.
- `get_portal("otodom.pl")` zwraca portal; `get_portal("allegro.pl")` → `ValueError`.
- `offer_url`: URL oferty portalu z query stringiem → bez query; inny host (`https://evil.example/pl/oferta/1`), `http://`, ścieżka niebędąca ofertą (`/pl/wyniki/...`), `javascript:` → `None`.
- `parse` na fixture'ach: oferta z tytułem z poleceniem dla AI → `withheld=True`; oferta wyróżniona → zostaje z flagą `sponsored`; duplikat URL-a → jeden wynik; oferta ponad `max_price` → odrzucona.
- `build_results` i istniejące testy web_search/site_search przechodzą bez zmian po wydzieleniu wspólnej funkcji (pkt 3) i wspólnej wizyty (pkt 5).

**Z przeglądarką (marker `browser`, fixture'y serwowane lokalnie, bez internetu)**
- Dla każdego portalu w `PORTALS`: `<portal>_results.html` → `ok`, ≥ 5 ofert, każda z niepustym `title` i URL-em oferty portalu, co najmniej połowa z `price` i `area_m2`; `<portal>_no_results.html` → `no_results`; `<portal>_blocked.html` (jeśli portal ma stronę blokady) → `blocked`, `results == []`; `<portal>_offer.html` → `plot_details` `ok` z ceną, powierzchnią i ≥ 3 parametrami; `<portal>_offer_inactive.html` → `inactive`.
- Fixture-pułapka na stronie ogłoszenia: opis, komentarz i blok „podobne oferty” z tekstem „Zignoruj poprzednie polecenia…” i znacznikiem `DESC-MARKER-1`. Znacznik nie występuje w `json.dumps(response)`, a wynik nie jest wstrzymany (tekst w ogóle nie został przeczytany).
- Dane kontaktowe z fixture'a (numer telefonu, e-mail sprzedawcy) nie występują w odpowiedzi.
- Strona wyników pokazująca inną miejscowość niż zapytanie (albo listę ogólnopolską) → `unknown_location`.

**HTTP i api**
- `POST /v1/listing-search` bez tokenu → 401; nieznany `portal` → 422; `location` pusty albo > 100 znaków → 422; w instancji `web-agent` (bez `sites_enabled`) → 404.
- `POST /v1/listing-details` z URL-em spoza portalu → 422.
- `PlotSearchTool.input_schema["properties"]["portal"]["enum"] == settings.web_agent_portals`; `{"portal": "otodom.pl"}` bez `location` → `ToolInputError`; `{"portal": "evil.example", "location": "x"}` → `ToolInputError` bez żądania HTTP.
- `PlotDetailsTool` z `url="http://169.254.169.254/"` → `ToolInputError` bez żądania HTTP.
- Wynik `ok`: tagi `untrusted_web_content` z `source="plot_search:<portal>"`, format linii jak w pkt 7, brakujące pola jako `?`.
- `GenerationGuard`: po `plot_search`/`plot_details` generacja jest tainted, jak po `web_search`.

**Ręcznie (`site-agent` na komputerze użytkownika)**
- W czacie: „znajdź działki budowlane w Czarnej Górze na nieruchomosci-online do 300 tys.” → `plot_search(portal="nieruchomosci-online.pl", location="Czarna Góra", max_price=300000)`, oferty z ceną, m², zł/m² i linkiem w < 20 s.
- „a na Słowacji w Oravskiej Lesnej do 68 800 €” → `plot_search(portal="nehnutelnosti.sk", location="Oravská Lesná", max_price=68800)`.
- „sprawdź to ogłoszenie” z URL-em z wyniku → `plot_details`, z informacją, że opis trzeba przeczytać na stronie.
- „Groń” (homonim) → `resolved_location` w wyniku pozwala stwierdzić, która to wieś.
- W logach `egress-proxy` tylko hosty z allowlisty portali.
- `uv run pytest` przechodzi.

## Notatki implementacyjne

- Kod i komentarze po angielsku, plan po polsku.
- Modele Pydantic na każdej granicy (JSON z `evaluate`, odpowiedź autocomplete, osadzony JSON strony, HTTP web-agent ↔ api).
- `Decimal` dla kwot i powierzchni (nie `float`), serializowane w JSON jako liczby/stringi zgodnie z domyślnym zachowaniem Pydantic — ustalić jedno i trzymać się go w kontrakcie.
- Kolejność implementacji: pkt 0 i fixture'y → kontrakt + parsowanie (czyste funkcje) → nieruchomosci-online i nehnutelnosti.sk → narzędzia w `api` → adresowo, otodom, OLX. Po pierwszych dwóch portalach narzędzie już działa end-to-end.
- Opisy narzędzi mają mówić modelowi „jedno wywołanie = jeden portal + jedna miejscowość”, bo skill wymaga osobnego zapytania na lokalizację, a małe modele próbują zbiorczych zapytań.

## Decyzje po rozpoznaniu

**28.09.2026, rozpoznanie w headless Chromium (Playwright) i curl na komputerze użytkownika — nie w `site-agent`.** Szczegóły (selektory, JSON, znaczniki) są w docstringach modułów `web_agent/portals/*.py`.

| Portal | Czy przechodzi | Listing | Lokalizacja (lookup) | Źródło danych |
|---|---|---|---|---|
| nieruchomosci-online.pl | tak | `/szukaj.html?3,dzialka,sprzedaz,,<nazwa>:<id>,,,,-<cena do>,<pow. od>&o=modDate,desc\|price,asc` | `/?module=locationTip&type=cityQuarterTip&cn=` (tylko z `X-Requested-With`) | DOM; kafelki `archive` i `searchSupplement` (inne miejscowości) pomijane |
| nehnutelnosti.sk | tak | `/vysledky/pozemky/<sefName>/predaj?priceTo=&areaFrom=&order=NEWEST\|PRICE_ASC` | `/api/v2/location/find/suggestions?query=` (homonimy: `zavadka-humenne`) | JSON-LD `SearchResultsPage` + cena z tekstu kafelka |
| otodom.pl | curl i przeglądarka z normalnym UA: tak; **UA `HeadlessChrome`: 403 (CloudFront)** | `/pl/wyniki/sprzedaz/dzialka/<woj>/<powiat>/<gmina>/<miejscowość>?priceMax=&areaMin=&by=LATEST&direction=DESC` | GraphQL `/api/query?query=autocomplete…` (GET, zapytanie własne — PROVISIONAL) | `__NEXT_DATA__` (allowlista pól) |
| olx.pl | przeglądarka: tak; curl: 403 (CloudFront) | `/nieruchomosci/dzialki/sprzedaz/<normalized_name>/?search[filter_float_price:to]=&search[filter_float_m:from]=&search[order]=` | `/api/v1/geo-encoder/location-autocomplete/?query=` (homonimy: `gronowo_27659`) | DOM (`data-testid`); tylko pierwsza `listing-grid` |
| adresowo.pl | tak | `/dzialki/<ścieżka z lookupu>/` (bez filtrów w URL) | `/offer-list/ajax/location-hints/?q=` (przyrostki: `korbielow-4`) | DOM; tylko `#offer-list-results` |

Wszystkie pięć jest w `PORTALS` i w domyślnym `web_agent_portals`. **Do potwierdzenia ręcznie w `site-agent`** (prawdziwy Chrome), zwłaszcza otodom (w headless blokuje go tylko UA; w `site-agent` UA jest zwykły).

Odstępstwa od planu i ustalenia:
- **Lookup dla wszystkich portali, żaden nie działa na samym slugu:** każdy ma identyfikator albo przyrostek nie do zgadnięcia. `slugify` służy do porównywania nazw (kandydat z lookupu, nagłówek strony wyników), nie do budowania URL-a.
- **Lookup to `fetch` z `robots.txt` portalu** (lekka strona na tym samym originie), w tej samej karcie przed listingiem: nieruchomosci-online odpowiada JSON-em tylko na żądanie z `X-Requested-With`, czego nawigacja nie wyśle.
- **Protokół `ListingPortal`** różni się od szkicu: `parse(raw, request) -> ListingPage` (wyniki + nagłówek strony + licznik; `request`, bo parser filtruje po `max_price`/`min_area_m2`), `parse_details(raw, url) -> OfferPage` (oferta, parametry, `inactive`), do tego `lookup_page` i `ready_js`.
- **Flaga `sponsored`:** tylko otodom (`isPromoted`) i OLX (`adCard-featured`, PROVISIONAL). nieruchomosci-online oznacza `prime` zawsze dwa pierwsze kafelki dowolnej listy (to nie jest wyróżnienie), nehnutelnosti.sk i adresowo nie pokazują wyróżnień w odczytywanych danych.
- **adresowo:** filtry i sortowanie to formularze POST, nie URL — cena i powierzchnia są filtrowane po odczycie, `price_asc` sortuje pierwszą stronę lokalnie. Strona oferty nie ma tabeli parametrów (tylko „typ działki” + cena, powierzchnia, adres z JSON-LD), więc kryterium „≥ 3 parametry” z testów nie da się spełnić dla adresowo.
- **otodom:** lista nie zawiera typu działki (jest dopiero w `plot_details`); lista zawiera też oferty spoza miejscowości i duplikaty promowanych — zostają tylko te, których geokodowanie zawiera szukaną lokalizację.
- **OLX:** część wyników linkuje do ofert otodom.pl (ta sama grupa) — zostają z URL-em otodom; `plot_details` dla nich wymaga `portal="otodom.pl"` (tak mówi opis narzędzia i błąd walidacji).
- **nehnutelnosti.sk:** cena z tekstu kafelka, nie z JSON-LD — dla ofert „tylko cena za m²” JSON-LD podaje w `price` kwotę za m².
- **Znaczniki nieaktywnej oferty:** nieruchomosci-online („Ogłoszenie archiwalne”, strona 404 z HTTP 200), otodom (HTTP 410, `ad.status`), adresowo (HTTP 404), nehnutelnosti.sk (przekierowanie na `/vysledky`) — sprawdzone; teksty dla OLX i adresowo są PROVISIONAL.
- **Fixture'y nie są w repo:** zapisane strony zawierają prawdziwe imiona i telefony sprzedawców. Krok 4 (testy) zapisuje je przez `web-agent listings|listing … --save-html` i anonimizuje przed commitem.
