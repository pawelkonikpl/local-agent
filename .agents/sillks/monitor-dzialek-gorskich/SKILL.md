---
name: monitor-dzialek-gorskich
description: Cykliczny przegląd ofert działek budowlanych w górach (Polska i Słowacja) położonych blisko stoków narciarskich, w zadanym budżecie. Uruchamiaj ten skill zawsze, gdy użytkownik mówi "sprawdź działki", "co nowego w działkach", "przeskanuj oferty", "szukam działki w górach", "działka blisko stoku", "działka budowlana w Beskidach/Sudetach/Bieszczadach", "pozemok na Slovensku", "stavebný pozemok pri lyžiarskom stredisku", albo prosi o kolejny, ponowny czy comiesięczny przegląd rynku działek górskich — także wtedy, gdy nie użyje słowa "skill" i nie poda regionu ani budżetu, bo te są zapisane w plikach konfiguracyjnych skilla. Skill czyta kryteria, listę serwisów i listę lokalizacji z edytowalnych plików referencyjnych, przeszukuje portale w PL i SK, filtruje po budżecie i odległości od wyciągu, oznacza oferty nowe względem poprzedniego uruchomienia i zwraca tabelę porównawczą oraz plik wyników do zachowania na kolejny raz.
---

# Monitor działek górskich

Ten skill służy do **powtarzalnego** skanowania rynku działek budowlanych w górach blisko stoków narciarskich. Wartość nie leży w pojedynczym wyszukiwaniu — leży w tym, że kolejne uruchomienia da się porównać z poprzednimi. Rynek gruntów rusza się wolno, ale oferty mieszczące się w budżecie znikają szybko, więc sens ma raczej regularne zaglądanie niż jedno wielkie badanie raz na rok.

Trzy pliki w `references/` należą do użytkownika i to on je aktualizuje. Traktuj je jako źródło prawdy o tym, czego szukać i gdzie:

- `references/kryteria.md` — budżet, wielkość, wymagania (co jest twarde, a co miękkie)
- `references/serwisy.md` — lista portali PL i SK do przeszukania
- `references/lokalizacje.md` — regiony, ośrodki narciarskie, poziomy cen z poprzednich przeglądów
- `references/weryfikacja.md` — checklista due diligence (to raczej stała wiedza, rzadziej wymaga zmian)

## Krok 0: wczytaj konfigurację

Przeczytaj `kryteria.md`, `serwisy.md` i `lokalizacje.md` **zanim** zaczniesz cokolwiek wyszukiwać. Bez tego zgadujesz budżet i regiony, a użytkownik już raz je zapisał właśnie po to, żeby nie musieć ich powtarzać.

Jeśli użytkownik poda w czacie inne parametry ("tym razem tylko Słowacja", "podnieś budżet do 300 tys."), mają one pierwszeństwo przed plikami **dla tego jednego uruchomienia**. Nie edytuj plików samowolnie — zaproponuj to na końcu (Krok 6).

## Krok 1: ustal punkt odniesienia

Zapytaj krótko albo sprawdź, czy użytkownik ma wyniki z poprzedniego uruchomienia (plik `dzialki-RRRR-MM-DD.md`, który ten skill generuje). Jeśli tak — wczytaj go i porównuj. Jeśli nie, to pierwszy przebieg i po prostu to zaznacz; nie udawaj, że masz historię, której nie masz.

Porównanie z poprzednim razem jest tym, co odróżnia ten skill od zwykłego wyszukiwania. Bez niego użytkownik dostanie w kółko te same oferty i przestanie czytać.

## Krok 2: przeszukaj serwisy

Idź przez listę z `serwisy.md`, region po regionie z `lokalizacje.md`.

- **Osobne zapytanie na każdą lokalizację.** Jedno zbiorcze zapytanie typu "działka w górach blisko stoku" zwraca płytkie, ogólnopolskie śmieci. "działka budowlana Sokolec", "działka Korbielów sprzedaż", "stavebný pozemok Oravská Lesná" zwracają konkret.
- **Zapytania po słowacku dla Słowacji.** `stavebný pozemok`, `predaj`, `lyžiarske stredisko`, `pozemok pre rodinné domy`. Polskie frazy nie znajdą słowackich ogłoszeń.
- **Skaluj wysiłek.** Sensowny przegląd to kilkanaście–dwadzieścia zapytań, nie trzy. Jeden region = zwykle 2–4 zapytania (portal ogólny + portal z podstronami miejscowości + agregator).
- **Portale z podstronami miejscowości** (np. `{miejscowosc}.nieruchomosci-online.pl`) bywają najskuteczniejsze, bo od razu filtrują geograficznie.
- Jeśli zapytanie nie trafia, przeformułuj je — inne słowo, sąsiednia miejscowość, nazwa gminy zamiast wsi. Powtarzanie tej samej frazy nie zmieni wyników.

## Krok 3: filtruj

Odrzuć wszystko, co nie przechodzi twardych kryteriów z `kryteria.md`. W praktyce najczęściej wypada:

- cena ponad budżet (ale patrz sekcja "poza budżetem" w formacie wyników — warto ją pokazać osobno, żeby użytkownik widział pułap rynku),
- działka rolna albo leśna podana jako "budowlana" przez sprzedającego bez pokrycia w planie,
- na Słowacji: `rekreačný pozemok` zamiast `stavebný pozemok`, jeśli użytkownik chce dom całoroczny,
- brak realnego dojazdu (droga po cudzej działce bez służebności).

## Krok 4: zweryfikuj przed rekomendacją

Snippety z wyszukiwarki bywają nieaktualne o wiele miesięcy — cena z ogłoszenia sprzed roku nic nie mówi o tym, czy działka jest jeszcze na sprzedaż. Zanim wstawisz ofertę do czołówki, wejdź na stronę ogłoszenia i sprawdź cenę, powierzchnię i datę aktualizacji.

Przed sekcją z rekomendacjami przeczytaj `references/weryfikacja.md` i przełóż ją na konkret dla znalezionych działek — zwłaszcza plan miejscowy, dojazd i (w Karpatach) osuwiska. Ładna cena za m² przy terenie osuwiskowym to nie okazja, tylko problem.

Nie kopiuj opisów ogłoszeń słowo w słowo — streszczaj własnymi słowami.

## Krok 5: format wyników

Zawsze ta sama struktura, żeby dało się porównywać przeglądy między sobą:

```
# Przegląd działek — [data]
Zakres: [regiony] · Budżet: [kwota] · Porównanie z: [data poprzedniego / "pierwszy przegląd"]

## Podsumowanie
[2–4 zdania: co się zmieniło od ostatniego razu, gdzie jest teraz najlepszy stosunek ceny do odległości od wyciągu]

## Tabela ofert
| Status | Lokalizacja | Cena | Pow. | zł/m² | Wyciąg | Media / dojazd | Link |

## Top 3 — na co zadzwonić w pierwszej kolejności
[dla każdej: dlaczego, i co konkretnie sprawdzić przed rozmową]

## Poza budżetem, ale warto wiedzieć
[2–3 oferty pokazujące pułap rynku w danym regionie]

## Do sprawdzenia / uwagi
[świeżość danych, martwe portale, sygnały ostrzegawcze]
```

Kolumna **Status**: 🆕 nowa · ➖ była poprzednio · 💰 zmiana ceny (podaj starą → nową) · ❌ zniknęła z rynku.

Przy pierwszym przebiegu wszystko jest 🆕 i wystarczy to zaznaczyć jednym zdaniem, zamiast wypełniać kolumnę emotką w każdym wierszu.

Ceny słowackie podawaj w euro, a w nawiasie orientacyjnie w złotych — i zaznacz, że przelicznik jest orientacyjny, bo kurs się rusza.

Na koniec zapisz cały raport jako plik `dzialki-RRRR-MM-DD.md` i przekaż go użytkownikowi. To ten plik będzie punktem odniesienia następnym razem, więc powiedz mu wprost, żeby go zachował.

## Krok 6: zaproponuj aktualizację konfiguracji

Skill ma się z czasem uczyć. Jeśli w trakcie przeglądu zauważysz, że:

- któryś portal z `serwisy.md` konsekwentnie nic nie zwraca albo przestał działać,
- pojawił się serwis, którego nie ma na liście, a miał dobre oferty,
- poziomy cen w `lokalizacje.md` wyraźnie odjechały od rzeczywistości,
- region okazał się beznadziejny przy tym budżecie (albo odwrotnie — nagle się otworzył),

powiedz o tym krótko na końcu i zaproponuj konkretną zmianę w pliku. Nie wprowadzaj jej bez zgody użytkownika — te pliki są jego.

## Czego unikać

- Nie prezentuj ofert, których nie sprawdziłeś na stronie ogłoszenia, jako pewnych — szczególnie w Top 3.
- Nie ograniczaj się do jednego portalu, nawet jeśli zwrócił dużo wyników. Oferty prywatne (adresowo, OLX, bazoš) często nie trafiają na duże portale i to tam bywają najniższe ceny.
- Nie mieszaj działek rolnych z budowlanymi w jednej tabeli bez wyraźnego oznaczenia — to zupełnie inny produkt i inne ryzyko.
- Nie pomijaj kosztów po zakupie (przyłącza, droga, projekt, w PL renta planistyczna przy odrolnieniu). Jeśli działka jest tania, bo nieuzbrojona, powiedz to.
- Nie zgaduj odległości od wyciągu na oko — sprawdź, albo napisz, że to szacunek z opisu ogłoszenia.
