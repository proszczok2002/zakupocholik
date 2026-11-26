# Zakupocholik

Zakupocholik to polski dodatek do NVDA dla Google Chrome i Firefoksa. Ułatwia przeglądanie ofert, czytanie opisów i ustawianie filtrów na OLX, Vinted i Allegro Lokalnie.

## Pobieranie i instalacja

1. [Pobierz najnowszą wersję Zakupocholika](https://github.com/proszczok2002/zakupocholik/releases/latest).
2. Otwórz plik `.nvda-addon`, potwierdź instalację i uruchom NVDA ponownie.

Do działania potrzebujesz NVDA 2026.1 lub nowszej. Pełna pomoc jest też dostępna bez internetu w menu **NVDA → Narzędzia → Zakupocholik → Instrukcja i skróty**.

## Przeglądanie ofert

Na OLX i Vinted użyj `H` do przechodzenia po nagłówkach lub `4`, by przechodzić po nagłówkach poziomu 4. Na listach ofert dodatek tworzy nagłówki z tytułów, także w wynikach wyszukiwania na ministronach OLX. Polecane profile na listach są linkami. Nazwy profili są dostępne jako nagłówki w widokach osób, np. w Obserwowanych OLX i wyszukiwarce użytkowników Vinted. NVDA zgłasza poziom nagłówka także przy czytaniu strzałkami i w czytaniu ciągłym. Na Allegro Lokalnie użyj `3`. Dodaj `Shift`, żeby wrócić do poprzedniego nagłówka. Na otwartej ofercie OLX nawigacja nagłówkami prowadzi raz do tego samego sprzedawcy, kiedy portal powtarza identyczny nagłówek z linkiem do jego profilu. Na otwartej ofercie Vinted `1` prowadzi do tytułu, a `2` do kolejnych sekcji, takich jak cena, opis i dane sprzedającego.

| Skrót | Działanie |
| --- | --- |
| `NVDA+Alt+F` | Otwiera filtry bieżącej listy. |
| `NVDA+Alt+S` | Przenosi do wyszukiwarki ofert. |
| `NVDA+Alt+O` | Na liście odczytuje informacje o ofercie. Na otwartej ofercie pokazuje tytuł i opis sprzedającego. Przycisk **Skopiuj** kopiuje oba do schowka. |
| `NVDA+Alt+B` | Wraca do tytułu ostatnio otwartej oferty na tej samej liście. |
| `NVDA+Alt+W` | Powtarza liczbę wyników, gdy portal ją podaje. Liczba jest odczytywana także po zastosowaniu filtrów. |
| `NVDA+Shift+Alt+L` | Otwiera ulubione listy wyników z zapisanymi filtrami. |
| `NVDA+Shift+Alt+W` | Otwiera listę wczytanych ofert. Strzałki przechodzą po ofertach, a Enter otwiera wybraną. |

## Ulubione listy

Na liście OLX, Vinted lub Allegro Lokalnie ustaw filtry i naciśnij `NVDA+Shift+Alt+L`. Wybierz **Zapisz bieżącą listę do ulubionych** i nadaj jej nazwę, np. „ThinkPad do 1000 zł”.

Ten sam skrót działa również przy zamkniętej przeglądarce. Wybierz listę z zapisanych wyników i naciśnij `Enter`, żeby otworzyć ją w domyślnej przeglądarce Windows. Ponowne wywołanie skrótu przywraca fokus do otwartego okna. Klawisz `F2` zmienia nazwę zaznaczonej listy, a `Delete` pyta o potwierdzenie jej usunięcia. Ulubione są zapisane lokalnie w konfiguracji NVDA.

Gdy bieżąca lista jest już w ulubionych, okno od razu ją zaznacza. Przycisk zapisu jest dostępny dla nowych list i wraca po usunięciu pozycji z ulubionych.

## Ustawianie filtrów

1. Na liście ofert naciśnij `NVDA+Alt+F`.
2. W oknie Zakupocholika użyj `Tab`, żeby przejść między polami. W grupie **Stan przedmiotu** przejdź do listy opcji, wybierz pozycje strzałkami i zaznacz spacją jeden lub kilka stanów.
3. Wybierz **Zastosuj filtry**, żeby wczytać wyniki. `Esc` anuluje zmiany.

- **OLX** – w oknie ustawisz cenę, stan przedmiotu, „Tylko z przesyłką” i „Tylko ze zdjęciem”, zależnie od bieżącej listy.
- **Vinted** – w oknie ustawisz cenę, stan i sortowanie. Dodatkowe stany, takie jak „Nie w pełni sprawny”, zależą od kategorii.
- **Allegro Lokalnie** – okno pokazuje filtry danej listy, takie jak stan, marka, cena i miejscowość. Gdy jest dostępna, grupa **Stan przedmiotu** jest od razu wybrana. W polu **Szukaj w opcjach grupy** możesz wpisać fragment nazwy.

Na OLX i Vinted przycisk **Przejdź do filtrów na stronie** prowadzi do pozostałych opcji.

## Aktualizacje i zgłoszenia

Aktualizacje są pobierane ze [strony Zakupocholika](https://proszczok.pl/zakupocholik/). Aby sprawdzić je ręcznie, wybierz w NVDA **Narzędzia → Zakupocholik → Sprawdź aktualizacje**. Przed pobraniem dodatek pokazuje opis zmian. W **Ustawieniach aktualizacji** możesz zmienić interwał i odczytać czas oraz wynik ostatniego automatycznego sprawdzenia.

Problemy i propozycje możesz przekazać autorowi z menu **Narzędzia → Zakupocholik → Zgłoś błąd lub propozycję**.

## Kod źródłowy i budowanie

Kod dodatku jest w katalogu [Zakupocholik](Zakupocholik/), a źródła strony w [site](site/). Wersję odpowiadającą pobranej paczce znajdziesz w tagu wydania od `v1.19`.

W katalogu repozytorium uruchom polecenie `py -3.13 -B tools/release.py`. Paczka instalacyjna i pliki bieżącego wydania pojawią się w `dist`.
