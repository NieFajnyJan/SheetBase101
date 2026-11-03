
# Specyfikacja projektu – SheetBase

System do ewidencji i rozliczania wspólnych wydatków biurowych (aplikacja Django).

---

## 1. Cele

- Wiedzieć, czy wszyscy oddali po równo, a jeśli nie – kto.
- Sprawdzać, czy suma całkowita się zgadza.
- Kwota wydana przez Jaśka musi się zgadzać z wydatkami pozostałych członków.
- Śledzić wydatki w celu wykrywania nieprawidłowości.
- Prezentować wydatki w czytelnej formie.
- Zestawiać miesięczne koszty do porównania *(niski priorytet)*.
- logowanie przez google, discord

---

## 2. Wymagania funkcjonalne

### 2.1. Transakcje

- Zapisywanie **wszystkich** transakcji – użytkownicy będą dodawać nawet najmniejsze wydatki dotyczące biura.
- Historia transakcji wraz z ich sumą.
- Statusy transakcji: **wykonane**, **niewykonane**, **zaplanowane**.
- Księgowanie podwójne (double-entry bookkeeping) – do doczytania.

### 2.2. Kwoty

- Dokładne ceny z groszami – liczby z dokładnością do dwóch miejsc po przecinku.

### 2.3. Salda i zadłużenie

- Każdy użytkownik ma własne saldo *all-time* (wszystkie wydatki od momentu założenia arkusza).
- Saldo kont aktualizuje się na stronie dynamicznie – po dodaniu przelewu między użytkownikami saldo zmienia się automatycznie.
- Bieżący podgląd, kto ile jest komu winny – najlepiej w formie tabeli.
- Kierunek długu: czy dana osoba jest winna Jaśkowi, czy Jasiek jej.

### 2.4. Dzielenie wydatków

- Domyślnie wydatek dzielony jest na wszystkich użytkowników.
- **Wyjątki:** możliwość zmiany liczby osób (dzielnika), np. gdy ktoś nie chce się składać na dany wydatek.

### 2.5. Zaplanowane transakcje

- Drobne wydatki sumują się w większe kwoty, które można sensownie oddać (priorytetyzacja opłat – mniejsze wydatki czekają, aż uzbiera się większa suma).
- Reszta z zaokrąglonych spłat przechodzi do zaplanowanych transakcji.
  *Przykład:* ktoś jest winny Jaśkowi 90 zł i oddaje banknot 100 zł – nadwyżka 10 zł staje się zaplanowaną transakcją.

### 2.6. Rozliczenia i faktury

- Wystawianie faktur za miesiąc lub inny wybrany okres. Umowa zakłada zwrot pieniędzy kwartalnie, ale niektórzy mogą chcieć otrzymać fakturę wcześniej.
- Rozliczenie roczne – tabela z historią wydatków z całego roku; w nowym roku widoczny jest rok poprzedni i bieżący.
- Kalkulator refakturowania z podatkiem – do przeanalizowania (na początek wersja podstawowa).

### 2.7. Integracje

- Integracja z Discordem (bot).

---

## 3. Wymagania techniczne

| Obszar | Rozwiązanie |
|---|---|
| Framework | Django |
| Baza danych | SQLite |
| Logowanie zdarzeń | logi aplikacji |
| Observability | OpenTelemetry |
| Audit log | rejestr zmian i operacji użytkowników |
| Identyfikacja użytkowników | IdP (Identity Provider) |
| Uwierzytelnianie | do ustalenia (powiązane z IdP) |
| Hosting | do ustalenia |

---

## 4. Otwarte pytania

- **Rozliczenie** – co dokładnie ma obejmować?
- **Wliczanie procentów** – do czego mają być wliczane?
- **Kalkulator refakturowania** – jakie stawki podatku i jaki zakres?
- **Logowanie** – chodzi o logi aplikacji czy o logowanie użytkowników?
- **Hosting** – gdzie aplikacja będzie hostowana?
