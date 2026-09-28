# Lista serwisów do przeszukania

> **Ten plik jest do edycji przez Ciebie.** Dopisuj, usuwaj i zmieniaj kolejność bez pytania.
> Konwencja: `✅` = sprawdzone, realnie zwraca oferty · `❓` = do zweryfikowania · `⛔️` = wyłączone z przeszukiwania (zostawiam wpis, żeby pamiętać dlaczego).
> Kolejność ma znaczenie — Claude idzie od góry, więc na górze trzymaj to, co daje najlepsze wyniki.

---

## POLSKA — portale ogólne

| | Serwis | Uwagi / wzorzec adresu |
|---|---|---|
| ✅ | **otodom.pl** | Największa baza, biura + prywatne. Wyniki: `otodom.pl/pl/wyniki/sprzedaz/dzialka/{województwo}/{powiat}/{gmina}`. ⚠️ W sesji bez przeglądarki nie działa — błąd 403 (20.09.2026); widać tylko tytuły z wyszukiwarki. Wtedy przejrzyj ręcznie albo uruchom przegląd z przeglądarką |
| ✅ | **adresowo.pl** | Oferty bez pośredników, często najniższe ceny. `adresowo.pl/dzialki/{miejscowosc}/`. Adresy miejscowości mają często przyrostki, np. `/dzialki/korbielow-4/`, `/dzialki/zawoja-X/`, `/dzialki/ustrzyki-dolne-z/` — bierz je z wyszukiwarki. ⚠️ W sesji bez przeglądarki strony miejscowości zwracają ogólnopolską listę zamiast lokalnej (20.09.2026) |
| ✅ | **nieruchomosci-online.pl** | Podstrony per miejscowość — najwygodniejsze do wąskich lokalizacji: `{miejscowosc}.nieruchomosci-online.pl/dzialki,sprzedaz/` |
| ✅ | **olx.pl** | Dużo ofert prywatnych. `olx.pl/nieruchomosci/dzialki/sprzedaz/` + fraza. ⚠️ W sesji bez przeglądarki nie działa — błąd 403 (20.09.2026) |
| ✅ | **mieszkania.trovit.pl** | Agregator (ciągnie m.in. adresowo i Gratkę). Dobry na szybki przegląd wielu źródeł naraz. ⚠️ W sesji bez przeglądarki nie działa — błąd 401 (20.09.2026) |
| ✅ | **szybko.pl** | Ma wyszukiwanie po słowie kluczowym w opisie — działa fraza „stok narciarski" |
| ✅ | **domiporta.pl** | |
| ✅ | **gethome.pl** | Ma filtr „w górach": `gethome.pl/dzialki/{wojewodztwo}/t/gory/` |
| ✅ | **kazo.pl** | Agregator, podstrony per miejscowość |
| ✅ | **okolica.pl** | Dodane 20.09.2026. `okolica.pl/dzialka/sprzedam/{miejscowosc}/`. Miała ofertę z Korbielowa, której nie było na nieruchomosci-online. Pokazuje datę dodania |
| ❓ | **lento.pl** | Podstrony per miejscowość, np. `{miejscowosc}.lento.pl/nieruchomosci/dzialki-i-grunty.html`. ⚠️ W sesji bez przeglądarki nie działa — błąd 403 (20.09.2026) |
| ❓ | **gratka.pl** | Wchodzi pośrednio przez Trovit — sprawdzić, czy warto odpytywać osobno |
| ❓ | **morizon.pl** | |
| ❓ | **sprzedajemy.pl** | |
| ❓ | **nieruchomosci.abyhom.pl** | Agregator, wychodził w wynikach |
| ❓ | **nestoria.pl**, **mieszkanie.mitula.com.pl** | Agregatory, dużo duplikatów |
| ❓ | **allegro.pl** (kategoria działki) | |

## POLSKA — źródła nieoczywiste (tu bywają okazje)

| | Źródło | Uwagi |
|---|---|---|
| ❓ | **licytacje.komornik.pl** / **elicytacje.komornik.pl** | Licytacje komornicze — ceny wywoławcze poniżej rynku, ale trzeba wiedzieć, co się kupuje |
| ❓ | **BIP-y gmin** (przetargi na sprzedaż nieruchomości) | Gminy sprzedają uzbrojone działki w przetargach, często taniej niż rynek. Warto dopisać konkretne gminy z `lokalizacje.md` |
| ❓ | **KOWR** (ziemia rolna) | Tylko jeśli dopuszczasz grunt do odrolnienia |
| ❓ | **Lokalne biura nieruchomości** | Np. w Sudetach i Beskidach część ofert nie trafia na duże portale |
| ✅ | **bieszczady-nieruchomosci.pl**, **sanpark.nieruchomosci.pl** | Dodane 20.09.2026. Biura z Bieszczad (Ustrzyki, Lesko). Miały działki z Ustjanowej Górnej i Równi z bezterminowymi WZ. Nie podają daty ogłoszenia |

## SŁOWACJA

| | Serwis | Uwagi |
|---|---|---|
| ✅ | **nehnutelnosti.sk** | Największy portal. Podstrony per obec: `nehnutelnosti.sk/{obec}/pozemky/predaj/`. Pokazuje też medianę cen w lokalizacji |
| ✅ | **reality.sk** | Podstrony per obec: `reality.sk/{obec}/` |
| ✅ | **reality.bazos.sk** | Odpowiednik OLX — dużo ofert od właścicieli. `reality.bazos.sk/inzeraty/{fraza}/`. ⚠️ W sesji bez przeglądarki strony wyszukiwania (`/inzeraty/...`) zwracają błąd 404, a pojedyncze ogłoszenia (`/inzerat/...`) działają — linki do nich bierz z wyszukiwarki (20.09.2026) |
| ✅ | **reality.bazar.sk** | Filtruje po typie: `pozemky/rekreacne/{region}/predaj/`. ⚠️ Wyszukiwarka na www.bazar.sk blokuje automat (robots.txt, 20.09.2026); samego reality.bazar.sk wtedy nie testowano |
| ✅ | **bezmaklerov.sk** | Wyłącznie bez pośredników |
| ❓ | **topreality.sk** | Do zweryfikowania przy najbliższym przeglądzie |
| ❓ | Biura regionalne: **bosen.sk**, **schneiderreal.sk**, **limoreal**, **OREA** | Wychodziły w wynikach dla Liptowa i Orawy |

## Narzędzia weryfikacyjne (nie ogłoszeniowe, ale używaj ich w Kroku 4)

| Narzędzie | Do czego |
|---|---|
| **geoportal.gov.pl** | Numery i granice działek, podgląd ewidencji (PL) |
| **Geoportal SOPO / PIG** | Mapa osuwisk — krytyczne w Karpatach |
| **System Informacji Przestrzennej gminy** | Plan miejscowy (MPZP) — jedyne wiarygodne źródło przeznaczenia terenu w PL |
| **kataster.skgeodesy.sk** (ZBGIS) | Kataster nieruchomości SK — właściciele, udziały, rodzaj gruntu |
| **Územný plán obce** (strona gminy SK) | Odpowiednik MPZP na Słowacji |
| **nocowanie.pl** | `nocowanie.pl/wyciagi_narciarskie/{wies}/` — wyciągi we wsi i najbliższe, z odległością w km od wsi; oznacza też wyciągi nieczynne na stałe. Dodane 20.09.2026 |
| **mapa.targeo.pl** | Współrzędne adresów i ulic (PL) — do policzenia odległości od działki do dolnej stacji wyciągu w linii prostej. Dodane 20.09.2026 |
| **Kurs NBP (tabela A)** | Aktualny kurs € do przeliczania budżetu na Słowację |

---

## Notatki z przeglądów

*(Dopisuj tu, co zadziałało, a co nie — następnym razem Claude to przeczyta.)*

- **2026-09-06** — pierwszy przegląd. Najlepsze trafienia dały: nieruchomości-online (podstrony miejscowości), otodom, adresowo. Na Słowacji: nehnutelnosti.sk i bazos.sk.
- **2026-09-20** — przegląd w sesji w chmurze, bez przeglądarki. Działały: nieruchomosci-online (podstrony miejscowości, pokazują też ogłoszenia archiwalne — licz tylko aktywne), domiporta (ma sekcję „w promieniu 15 km”), okolica.pl, kazo.pl, lokalne biura z Bieszczad, nehnutelnosti.sk i reality.sk (te same ogłoszenia, duplikaty), pojedyncze ogłoszenia z bazos. Nie działały: otodom i OLX (403), Trovit (401), lento (403), adresowo (lista ogólnopolska), wyszukiwarka bazos (404). Adresy stron do pobrania brać z wyników wyszukiwarki — samodzielnie zbudowane adresy wymagały zgody użytkownika i przepadały, gdy nie było go przy komputerze.
