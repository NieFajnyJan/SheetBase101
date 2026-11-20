
# Specyfikacja projektu – SheetBase

System do ewidencji i rozliczania wspólnych wydatków biurowych (aplikacja Django).

**Persona – Operator:** użytkownik, który kupuje rzeczy dla biura i wykłada za nie pieniądze; pozostali członkowie oddają mu swoją część.

---

## 1. Cele

- Wiedzieć, kto komu ile jest winny i czy wszyscy oddali swoją część.
- Sprawdzać, czy suma całkowita się zgadza.
- Śledzić wydatki i wszystkie zmiany w celu wykrywania nieprawidłowości.
- Prezentować wydatki w czytelnej formie.
- Zestawiać koszty roczne i miesięczne do porównania.
- Logowanie przez Google.

---

## 2. Wymagania funkcjonalne

### 2.1. Transakcje

- Zapisywanie **wszystkich** transakcji – użytkownicy dodają nawet najmniejsze wydatki dotyczące biura.
- Dwa rodzaje transakcji:
  - **wykonane** – prawdziwa płatność, która już się odbyła: kto zapłacił, komu, ile, za co i kiedy;
  - **zaplanowane** – otwarty dług: kto jest winny, komu, ile i za co. Pieniądze jeszcze nie zostały przekazane.
- **Sklepy** to osobni „użytkownicy”, którzy nie mogą się logować; zakup to płatność do sklepu, zwrot to płatność od sklepu. Nowy sklep można dodać na stronie transakcji.
- Data transakcji nie może być z przyszłości ani starsza niż 2 lata.
- Historia wykonanych transakcji wraz z ich sumą; filtry: kto zapłacił, komu, rok, miesiąc, ukrycie anulowanych.

### 2.2. Korekty i anulowanie

- Wykonanej transakcji nie da się edytować ani usunąć. Błąd poprawia się **korektą** – nową wersją transakcji z podanym powodem; obowiązuje najnowsza korekta, a historia zmian jest widoczna.
- Transakcję można **anulować** – wtedy liczy się jako 0 zł. Anulowanej nie można już poprawiać.
- Korygować i anulować może tylko osoba, która dodała transakcję.
- Jeśli płatność spłaciła długi albo zakup był podzielony, nie można zmienić jej kwoty ani osób – trzeba ją anulować i dodać od nowa.

### 2.3. Kwoty

- Dokładne ceny z groszami – liczby z dokładnością do dwóch miejsc po przecinku (nigdy liczby zmiennoprzecinkowe).

### 2.4. Długi i spłaty

- Strona **Planned** pokazuje wszystkie otwarte długi (kto jest winny, komu, za co, ile) i ich sumę.
- Kierunek długu: każdy wiersz mówi, kto jest winny komu.
- Dług może dodać, edytować i usunąć każdy członek, dopóki nie jest spłacony. Spłaconego długu nie da się zmienić ani usunąć.
- Płatność między członkami spłaca długi płacącego wobec odbiorcy – domyślnie od najstarszego, ale płacący może wybrać, które. Przed zapisaniem pokazywany jest ekran potwierdzenia.
- **Mark paid** przy długu (dostępne dla dłużnika i wierzyciela) otwiera od razu potwierdzenie płatności.
- Reszta z niedokładnej spłaty przechodzi do zaplanowanych transakcji.
  *Przykład:* ktoś jest winny Operatorowi 90 zł i oddaje banknot 100 zł – nadwyżka 10 zł staje się zaplanowaną transakcją.
- Płatność między członkami, która nie spłaca żadnego długu, jest pożyczką – powstaje dług w drugą stronę.
- Anulowanie płatności przywraca długi, które spłaciła.
- Przy długu z podzielonego zakupu widać, z którego zakupu pochodzi.

### 2.5. Dzielenie wydatków

- Zakup (albo dług) można podzielić po równo między wybrane osoby – po zaznaczeniu „Split between people” wszyscy członkowie są domyślnie zaznaczeni i można odznaczyć tych, którzy się nie składają.
- Każda osoba poza tą, której należą się pieniądze, dostaje zaplanowaną transakcję na swoją część. Części zawsze sumują się dokładnie do kwoty; nadmiarowe grosze trafiają do osób wylosowanych.
- Domyślnie dzielona jest cała kwota zakupu; opcjonalnie można podać mniejszą kwotę do podziału (np. gdy tylko część zakupu jest wspólna).
- Przed zapisaniem pokazywany jest podział do potwierdzenia.
- Części jednego podziału tworzą grupę – usunięcie jednej usuwa całą grupę (nie da się, jeśli ktoś z grupy już zapłacił).

### 2.6. Podsumowania

- Podsumowanie od początku (all-time) oraz za bieżący i poprzedni rok.
- Dla każdego członka: zakupy, zwroty, zapłacone innym członkom, otrzymane od innych członków i łącznie wydane (zakupy − zwroty + zapłacone − otrzymane).
- W podsumowaniu all-time: data pierwszej transakcji, liczba pełnych miesięcy i średni koszt na miesiąc.
- Dla każdego roku: ile kupiono w każdym sklepie i ile sklep zwrócił.

### 2.7. Audit log

- Każda zmiana danych (dodanie, edycja, usunięcie użytkownika, transakcji, korekty i zaplanowanej transakcji) oraz każde logowanie, wylogowanie i nieudana próba logowania jest zapisywana: **kto**, **kiedy**, **z jakiego IP**, **co** (wartości przed → po).
- Log jest w osobnej bazie (`audit.sqlite3`). Baza odrzuca zmianę i usunięcie wpisów, a każdy wpis przechowuje hash poprzedniego, więc zmiana lub usunięcie wpisu jest wykrywalne.
- Aplikacja nie pokazuje logu i nie pozwala go zmienić (także adminowi). Wpisy są też wypisywane w terminalu serwera.
- Wpisy są przechowywane **bezterminowo** – to niewielka ilość danych.
- Znane ograniczenie (zaakceptowane): SQLite nie ma osobnych użytkowników bazy, więc ktoś z dostępem do pliku może usunąć zabezpieczenia i zmienić log. `audit_check` to wykryje.

**Komendy** (uruchamiane w folderze z `manage.py`):

| Komenda | Co robi |
|---|---|
| `python manage.py migrate --database audit` | Zakłada bazę logu – bez tego nic nie jest zapisywane. |
| `python manage.py audit_snapshot` | Zapisuje w logu obecny stan rekordów, których log jeszcze nie zna. Uruchomić raz, zaraz po założeniu logu na istniejących danych. |
| `python manage.py audit_log` | Pokazuje ostatnie wpisy (najnowsze na końcu). |
| `python manage.py audit_check` | Sprawdza, czy żaden wpis nie został zmieniony ani usunięty i czy dane w aplikacji zgadzają się z logiem. Wypisuje niezgodności albo „The log is intact and agrees with the data.” |

Filtry `audit_log` (można łączyć):

- `--user jan` – tylko wpisy tego użytkownika
- `--model "planned transaction"` – tylko ten rodzaj rekordu (`user`, `done transaction`, `correction`, `planned transaction`)
- `--object 12` – tylko rekord o tym id (razem z `--model`)
- `--action delete` – tylko ta akcja: `create`, `update`, `delete`, `login`, `logout`, `login_failed`, `snapshot`
- `--since 2026-11-01` – od tej daty
- `--limit 100` – ostatnie N wpisów (domyślnie 50, `0` = wszystkie)

---

## 3. Wymagania techniczne

| Obszar | Rozwiązanie |
|---|---|
| Framework | Django |
| Baza danych | SQLite (dane) + osobna baza SQLite na audit log |
| Audit log | rejestr zmian i operacji użytkowników (sekcja 2.7) |
| Logowanie użytkowników | django-allauth: Google; brak kont z hasłem (hasło tylko dla admina w `/admin/`) |
| Testy | testy Django uruchamiane w GitHub Actions |

---

## 4. Otwarte pytania

- **Hosting** – gdzie aplikacja będzie hostowana?
