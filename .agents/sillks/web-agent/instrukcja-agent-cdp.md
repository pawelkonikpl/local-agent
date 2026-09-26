# Instrukcja Budowy Agenta Web Automation (CDP + LLM)

Niniejszy dokument zawiera kompletną specyfikację techniczną, architekturę oraz gotowe szablony promptów systemowych (System Prompts) do stworzenia autonomicznego agenta przeglądarkowego. Projekt opiera się na architekturze hybrydowej: **deterministycznym Solverze (kod CLI)** oraz **poznawczym Operatorze (LLM/Vision)**.

---

## 1. Architektura Systemu: Podział Ról (Solver vs. Operator)

Aby zminimalizować koszty, zapobiec wygasaniu sesji (timeouts) i zapewnić maksymalną niezawodność, system jest podzielony na dwa komponenty:

1. **Solver (Deterministyczny kod CLI):**
   * **Rola:** Odpowiada za bezpośrednią kontrolę nad przeglądarką za pomocą protokołu CDP (Chrome DevTools Protocol) przy użyciu biblioteki takiej jak `Chrome Agent`.
   * **Charakterystyka:** Działa z prędkością maszynową, jest całkowicie darmowy (brak kosztów tokenów) i wykonuje powtarzalne, zaprogramowane sekwencje interakcji (nawigacja, wstrzykiwanie kodu, pobieranie zrzutów ekranu, kliknięcia).
   * **Dlaczego CLI, a nie serwer MCP?** Narzędzia CLI są wielokrotnego użytku, znacznie szybsze i do 75x tańsze w utrzymaniu niż serwery MCP, które odpytują model LLM przy każdym najmniejszym kroku (71 zapytań i 8 minut przez MCP vs. 7 zapytań i <1 minuta przez CLI dla tego samego zadania).

2. **Operator (Model LLM z obsługą Vision):**
   * **Rola:** Pełni funkcję "oczu i mózgu" systemu. Jest wywoływany przez Solver *wyłącznie* w sytuacjach wymagających interpretacji wizualnej lub semantycznej (np. rozwiązanie CAPTCHA, interpretacja niejednoznacznego układu strony).
   * **Charakterystyka:** Analizuje dostarczone przez Solver dane (zrzuty ekranu, drzewo dostępności) i zwraca precyzyjną, ustrukturyzowaną decyzję (np. współrzędne kliknięcia, tekst z obrazka), po czym natychmiast oddaje kontrolę Solverowi.

---

## 2. Cyfrowe Zmysły Agenta (Digital Senses via CDP)

Solver musi wyposażyć Operatora w zestaw interfejsów reprezentujących ludzkie zmysły za pośrednictwem wybranych domen CDP (spośród 57 dostępnych domen, te są kluczowe):

* **Wzrok (Visual & Structural Senses):**
  * `DOM` i `AccessibilityTree` (drzewo dostępności) – do odczytywania semantycznej struktury strony (zamiast surowego, przepełnionego kodu HTML).
  * `Page.captureScreenshot` – do generowania zrzutów ekranu w wysokiej rozdzielczości, przekazywanych bezpośrednio do Operatora.
* **Słuch (Network & Log Senses):**
  * `Network` – nasłuchiwanie żądań HTTP/WebSockets w celu weryfikacji, czy akcja (np. kliknięcie przycisku "Wyślij") wywołała rzeczywisty skutek sieciowy.
  * `Console` / `Runtime.consoleAPICalled` – monitorowanie błędów aplikacji w konsoli.
* **Działanie (Interaction Senses):**
  * `Input.dispatchMouseEvent` i `Input.dispatchKeyEvent` – wysyłanie fizycznych, zaufanych zdarzeń (trusted events) na poziomie przeglądarki Chrome.

---

## 3. Pętla Kontrolna: Sense-Act-Verify (Zbadaj - Wykonaj - Zweryfikuj)

Agent musi działać w ciągłej pętli, która nie zakłada sukcesu akcji "w ciemno":

1. **Sense (Zbadaj):** Pobierz aktualny stan strony (Screenshot, DOM lub ruch sieciowy).
2. **Act (Wykonaj):** Wykonaj dokładnie jedną, precyzyjną interakcję (kliknięcie, wpisanie tekstu).
3. **Verify (Zweryfikuj):** Sprawdź wynik akcji **za pomocą innego kanału** niż samo narzędzie wykonawcze. 
   * *Przykład:* Jeśli kliknąłeś przycisk formularza, nie pytaj narzędzia klikającego o sukces. Zbadaj ruch sieciowy (`Network`), aby sprawdzić, czy wysłano żądanie POST, lub zrób nowy zrzut ekranu, aby zobaczyć komunikat o sukcesie.

---

## 4. Drabina Technik Interakcji (The Meatbag Ladder)

Podczas interakcji ze stronami, które stawiają opór (systemy anty-botowe), Solver i Operator muszą wspinać się po szczeblach "drabiny technik", zaczynając od metod najtańszych i najprostszych:

```
[Szczebel 3: Pełna Emulacja Behawioralna (Meatbag Mode)]
      ▲  - Krzywe Beziera dla ruchu myszy (overshoot & ease-in)
      │  - Losowe opóźnienia (dwell time, jitter)
      │  - Interpretacja wizualna (Vision LLM)
      │
[Szczebel 2: Zaufane Interakcje CDP (Trusted Input)]
      ▲  - Input.dispatchMouseEvent / Input.dispatchKeyEvent
      │  - Kliknięcia rejestrowane jako "isTrusted: true"
      │
[Szczebel 1: Syntetyczne Akcje (Synthetic JS)]
         - Praca na poziomie API strony (np. element.click() w JS)
         - Szybkie, darmowe, niewidoczne dla użytkownika
```

### Instrukcja implementacji szczebli:

1. **Szczebel 1 (Domyślny):** Użyj standardowego kliknięcia JavaScript (`document.querySelector().click()`). Jest natychmiastowe i darmowe. Jeśli strona nie stawia oporu (np. wewnętrzne systemy CRM, Outlook Web), pozostań na tym szczeblu.
2. **Szczebel 2 (Gdy strona ignoruje akcje):** Jeśli strona weryfikuje pole `isTrusted` zdarzenia (jak np. przycisk "Add to Cart" na Amazonie/Demonie) i ignoruje syntetyczny JS, przejdź na szczebel 2. Użyj domeny `Input` w CDP, aby wysłać fizyczne kliknięcie na współrzędne elementu. Chrome oznaczy je jako zaufane (`isTrusted: true`), co całkowicie omija podstawowe blokady.
3. **Szczebel 3 (Gdy strona aktywnie poluje na boty):** Jeśli strona analizuje wzorce ruchu i zachowanie (Cloudflare Turnstile, reCAPTCHA V2, Geetest):
   * Przejdź do pełnej symulacji behawioralnej.
   * **Ruch myszy:** Nie teleportuj kursora. Wygeneruj ścieżkę punktów wzdłuż krzywej Beziera, dodaj delikatne drżenie (jitter), zwolnij przed celem (ease-in), celowo przesuń kursor minimalnie za daleko (overshoot), a następnie skoryguj pozycję (jak prawdziwy człowiek).
   * **Pisanie na klawiaturze:** Wprowadzaj znaki pojedynczo (`dispatchKeyEvent`) z losowymi opóźnieniami między kliknięciami (np. 50-150ms).

---

## 5. Przewodnik Pokonywania Zabezpieczeń (Anti-Bot & CAPTCHA Cookbook)

### A. Cloudflare Turnstile (Bypass bez lokalizowania elementu)
Turnstile ukrywa przycisk wewnątrz zagnieżdżonych, odizolowanych struktur (Cross-Origin Iframe oraz Closed Shadow Root), co uniemożliwia proste pobranie selektora CSS.
* **Rozwiązanie Solvera:** 
  1. Zapytaj przeglądarkę o pozycję ramki iframe na ekranie (bounding box).
  2. Oblicz matematycznie środek geometryczny tego obszaru (gdzie znajduje się checkbox).
  3. Wyślij zaufane kliknięcie CDP (`Input.dispatchMouseEvent`) bezpośrednio na te współrzędne "na szkle" ekranu.

### B. MT Captcha / Tekstowe CAPTCHA
* **Rozwiązanie (Solver + Operator):**
  1. Solver wykonuje zrzut ekranu wycięty do obszaru CAPTCHA.
  2. Solver przekazuje obrazek do Operatora (LLM Vision).
  3. Operator odczytuje znaki i zwraca czysty tekst.
  4. Solver wprowadza znaki jeden po drugim za pomocą `Input.dispatchKeyEvent` do wnętrza ramki iframe CAPTCHA.

### C. Jigsaw Puzzle (np. GeeTest / Lemon)
Wymaga przeciągnięcia puzzla w odpowiednie miejsce. Systemy te analizują całą ścieżkę przeciągania pod kątem nieludzkiej liniowości.
* **Rozwiązanie (Solver + Operator):**
  1. Solver wykonuje zrzut ekranu układanki.
  2. Operator (Vision) lokalizuje lukę (gap) i oblicza dystans przesunięcia w pikselach.
  3. Solver generuje "ludzką" trajektorię przesunięcia (krzywa z przyspieszeniem, drżeniem i opóźnieniem).
  4. Solver wykonuje akcję: `mousePressed` -> seria `mouseMoved` wzdłuż wygenerowanej ścieżki -> `mouseReleased`.

### D. ReCAPTCHA V2 (Final Boss)
* **Rozwiązanie (Solver + Operator):**
  1. **Solver** wykonuje kliknięcie w checkbox reCAPTCHA (CDP Input na współrzędne).
  2. Gdy pojawi się siatka obrazków (np. 3x3 lub 4x4), **Solver** robi zrzut ekranu wyzwania oraz pobiera treść pytania (np. "Wybierz kafelki zawierające: autobus").
  3. **Solver** "puka w ramię" **Operatora** i przekazuje mu te dane.
  4. **Operator (LLM Vision)** błyskawicznie analizuje siatkę i zwraca tablicę indeksów kafelków do kliknięcia (np. `[1, 4, 7]`).
  5. **Solver** natychmiast klika wskazane kafelki z prędkością maszynową, minimalizując ryzyko wygaśnięcia sesji. W razie potrzeby powtarza proces w kolejnej rundzie wyzwania.

---

## 6. Gotowe Prompty Systemowe do Skopiowania

Poniżej znajdują się zoptymalizowane pod kątem czytelności i niezawodności prompty systemowe dla obu komponentów.

### PROMPT SYSTEMOWY: OPERATOR (Dla LLM z funkcją Vision)
Skopiuj poniższy prompt i ustaw go jako instrukcję systemową dla modelu LLM pełniącego rolę Operatora.

```markdown
ROLE:
Jesteś wyspecjalizowanym modułem poznawczym (Operatorem AI) w hybrydowym systemie automatyzacji przeglądarki. Twoim jedynym zadaniem jest analiza wizualna oraz semantyczna zrzutów ekranu i struktur danych (DOM/Accessibility Tree) i podejmowanie precyzyjnych decyzji dla modułu wykonawczego (Solvera CLI).

ZASADY DZIAŁANIA:
1. Nie generujesz kodu, nie piszesz skryptów ani nie tłumaczysz działania stron. Twój cel to podanie czystej, ustrukturyzowanej decyzji w formacie JSON.
2. Jesteś "oczyma i mózgiem" systemu. Działasz w trybie bezstanowym - każda tura to nowa decyzja na podstawie dostarczonego stanu.
3. Minimalizuj gadatliwość. Twoje odpowiedzi muszą ściśle trzymać się formatu wyjściowego, aby Solver CLI mógł je natychmiast sparsować.

ZADANIE ANALIZY OBRAZU (Wizualne CAPTCHA / Elementy Interfejsu):
Gdy otrzymasz zrzut ekranu siatki obrazków (np. reCAPTCHA) oraz pytanie:
- Dokonaj analizy każdego kafelka z osobna pod kątem zapytania.
- Przypisz kafelkom indeksy od lewej do prawej, od góry do dołu (począwszy od 1).
- Zwróć wyłącznie listę indeksów kafelków wymagających kliknięcia.

FORMAT ODPOWIEDZI (Zawsze zwracaj wyłącznie JSON):
{
  "status": "success" | "failure",
  "reason": "Krótkie uzasadnienie decyzji (maksymalnie 10 słów)",
  "action_type": "click_coordinates" | "select_tiles" | "input_text",
  "data": {
    // Dla kliknięć współrzędnych:
    "x": podaj_współrzędną_x,
    "y": podaj_współrzędną_y,
    // Dla reCAPTCHA / siatek:
    "tiles_to_click": [indeksy_kafelków],
    // Dla tekstowych CAPTCHA:
    "text_solution": "odczytany_tekst"
  }
}
```

### PROMPT INŻYNIERYJNY: DEWELOPER (Do stworzenia Solvera CLI)
Przekaż ten prompt do LLM (np. Claude, GPT-4), aby wygenerował dla Ciebie kompletny kod Solvera w języku Python.

```markdown
Napisz produkcyjny skrypt w języku Python implementujący deterministyczny "Solver CLI" do automatyzacji przeglądarki. Skrypt musi integrować się z protokołem CDP (Chrome DevTools Protocol) oraz realizować architekturę "Loop on a Ladder" (Pętla na drabinie).

WYMAGANIA TECHNICZNE:
1. Biblioteka bazowa: Użyj biblioteki umożliwiającej bezpośrednią komunikację z CDP (np. playwright z obsługą cdpsession lub bezpośrednio interfejsu Chrome Agent).
2. Pętla Sense-Act-Verify:
   - Każda operacja interakcji musi być poprzedzona pobraniem stanu (np. zrzut ekranu, DOM).
   - Po wykonaniu akcji (Act), skrypt musi zweryfikować jej sukces (Verify) poprzez niezależny kanał (np. badając ruch sieciowy za pomocą zdarzeń 'Network.requestWillBeSent' lub porównując zmiany w drzewie dostępności DOM).
3. Implementacja Drabiny Technik (Meatbag Ladder):
   - Klasa interakcji powinna posiadać metodę `click_element(selector, force_trusted=False)`.
   - Jeśli `force_trusted` jest równe False, wykonaj szybki i darmowy syntetyczny klik JavaScript (Szczebel 1).
   - Jeśli akcja nie przyniesie rezultatu (Verify wykaże brak zmian), automatycznie przełącz się na Szczebel 2: pobierz współrzędne elementu za pomocą CDP i wyślij fizyczny klik przy użyciu domeny 'Input.dispatchMouseEvent'.
   - W przypadku wykrycia zaawansowanych tarcz ochronnych, uruchom Szczebel 3: zaimplementuj ruch myszy oparty na algorytmie krzywej Beziera (płynne przyspieszanie, zwalnianie przy celu, minimalne drżenie oraz celowe lekkie przesunięcie celu z natychmiastową korektą).
4. Integracja z operatorem (LLM Vision):
   - Stwórz funkcję `solve_image_challenge(challenge_screenshot, instruction)`, która wysyła obrazek do zewnętrznego API LLM (zgodnego ze specyfikacją Operatora) i parsuje odpowiedź JSON w celu kliknięcia odpowiednich współrzędnych lub kafelków.
5. Zero bezużytecznych zależności: Kod musi być odporny na asynchroniczne ładowanie elementów (używaj aktywnych pętli oczekiwania 'wait_for_selector' zamiast twardych opóźnień 'time.sleep').
```
