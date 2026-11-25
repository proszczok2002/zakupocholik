from ctypes import sizeof
import os
from pathlib import Path
import threading
from time import monotonic
from urllib.parse import urldefrag, urlsplit
import webbrowser

import addonHandler
import api
from comtypes import COMError
import controlTypes
import core
import globalPluginHandler
import gui
from exceptions import CallCancelled
from keyboardHandler import KeyboardInputGesture, ignoreInjection
from logHandler import log
from scriptHandler import script
import ui
from winBindings import user32
import winUser
import wx

from .browser import BROWSERS, browser_page
from .browser_filters import chrome_filter_source, firefox_filter_source, MAX_SOURCE_LENGTH
from .catalog_filters import load_olx_conditions
from .dialog import FiltersDialog
from .favorites import Favorites
from .feedback import Feedback
from .filter_navigation import catalog_filter_targets, open_catalog_filters, read_vinted_conditions
from .headings import ShopDocument
from .listings import ListingNavigation, listing_site
from .navigation import focus_search, read_offer
from .sites import FilterLoadError, allegro_listing, load_filters, parse_filter_form
from .updater import UpdateManager


FILTER_CACHE_SIZE = 8
FILTER_CACHE_SECONDS = 180
SAVED_FILTERS_HINT = " Możesz wczytać filtry z zapisanego pliku HTML w menu Zakupocholik."


def _send_address(address):
    data = address.encode("utf-16-le")
    units = [int.from_bytes(data[index:index + 2], "little") for index in range(0, len(data), 2)]
    events = (user32.INPUT * (2 * len(units)))()
    for index, code in enumerate(units):
        for event, flags in (
            (events[2 * index], user32.KEYEVENTF.UNICODE),
            (events[2 * index + 1], user32.KEYEVENTF.UNICODE | user32.KEYEVENTF.KEYUP),
        ):
            event.type = user32.INPUT_TYPE.KEYBOARD
            event.ii.ki = user32.KEYBDINPUT(0, code, flags, 0, 0)
    with ignoreInjection():
        return user32.SendInput(len(events), events, sizeof(user32.INPUT)) == len(events)


def _address_bar_focused(focus, browser):
    if (focus is None or focus.appModule.appName != browser or focus.treeInterceptor is not None
            or controlTypes.State.FOCUSED not in focus.states):
        return False
    if browser == "firefox":
        # Firefox exposes its address field as an editable combo box in IA2.
        return (focus.role in {controlTypes.Role.COMBOBOX, controlTypes.Role.EDITABLETEXT}
                and getattr(focus, "IA2Attributes", {}).get("id") == "urlbar-input"
                and controlTypes.State.EDITABLE in focus.states)
    return focus.role == controlTypes.Role.EDITABLETEXT


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
    scriptCategory = "Zakupocholik"

    def __init__(self):
        super().__init__()
        self._loading = False
        self._catalog_filters_open = False
        self._active = True
        self._filter_cache = {}
        self._navigation = ListingNavigation()
        self._favorites = Favorites()
        self._feedback = Feedback()
        self._updates = UpdateManager()
        self._tray = gui.mainFrame.sysTrayIcon
        self._tools_menu = self._tray.toolsMenu
        self._menu = wx.Menu()
        self._menu_ids = []
        for label, command in (
            ("Ulubione listy…", self._favorites.open),
            ("Lista wyników wyszukiwania…", self._navigation.show_offers),
            ("Filtry bieżącej listy…", self._request_filters),
            ("Wczytaj filtry Allegro Lokalnie z pliku HTML…", self._request_saved_filters),
            ("Przejdź do pola wyszukiwania", focus_search),
            ("Odczytaj bieżącą ofertę", read_offer),
            ("Wróć do ostatnio otwartej oferty", self._navigation.restore),
            ("Odczytaj liczbę wyników", self._navigation.speak_count),
        ):
            self._bind_menu(self._menu, label, lambda command=command: self._menu_command(command))
        self._menu_item = self._tools_menu.AppendSubMenu(self._menu, "Zakupocholik")
        self._menu.AppendSeparator()
        for label, command in (
            ("Instrukcja i skróty", self._open_help),
            ("Zgłoś błąd lub propozycję…", self._feedback.open),
            ("Sprawdź aktualizacje…", lambda: self._updates.check(manual=True)),
            ("Ustawienia aktualizacji…", self._updates.settings),
        ):
            self._bind_menu(self._menu, label, lambda command=command: wx.CallAfter(command))
        self._menu.AppendSeparator()
        support = wx.Menu()
        self._menu.AppendSubMenu(support, "Wesprzyj autora")
        for label, url in (
            ("PayPal", "https://paypal.me/proszczok"),
            ("Revolut", "https://revolut.me/r_rosiejka"),
        ):
            self._bind_menu(support, label, lambda url=url: wx.CallAfter(webbrowser.open, url))
        self._bind_menu(self._menu, "Strona autora", lambda: wx.CallAfter(webbrowser.open, "https://proszczok.pl/"))

    def _bind_menu(self, menu, label, command):
        item = menu.Append(wx.ID_ANY, label)
        self._menu_ids.append(item.GetId())
        self._tray.Bind(wx.EVT_MENU, lambda event: command(), id=item.GetId())

    def terminate(self):
        self._active = False
        self._navigation.close()
        self._feedback.close()
        self._updates.close()
        for item_id in self._menu_ids:
            self._tray.Unbind(wx.EVT_MENU, id=item_id)
        self._tools_menu.DestroyItem(self._menu_item)
        super().terminate()

    def chooseNVDAObjectOverlayClasses(self, obj, clsList):
        if obj.role == controlTypes.Role.DOCUMENT and obj.appModule.appName in BROWSERS:
            obj._shopping_navigation = self._navigation
            clsList.insert(0, ShopDocument)

    def _menu_command(self, command):
        page = browser_page(gui.mainFrame.prevFocus)
        wx.CallAfter(command, page)

    def _open_help(self):
        try:
            addon = addonHandler.getCodeAddon()
            path = addon.getDocFilePath() or Path(addon.path) / "doc" / "pl" / addon.manifest["docFileName"]
            os.startfile(path)
        except (addonHandler.AddonError, OSError):
            ui.message("Nie udało się otworzyć instrukcji Zakupocholika.")

    @script(description="Przejdź do pola wyszukiwania ofert", gesture="kb:NVDA+alt+s")
    def script_focusSearch(self, gesture):
        focus_search(browser_page())

    @script(description="Odczytaj bieżącą ofertę OLX, Vinted lub Allegro Lokalnie", gesture="kb:NVDA+alt+o")
    def script_readOffer(self, gesture):
        read_offer(browser_page())

    @script(description="Wróć do ostatnio otwartej oferty na liście", gesture="kb:NVDA+alt+b")
    def script_restoreOffer(self, gesture):
        self._navigation.restore(browser_page())

    @script(description="Odczytaj liczbę wyników wyszukiwania ofert", gesture="kb:NVDA+alt+w")
    def script_speakResultCount(self, gesture):
        self._navigation.speak_count(browser_page())

    @script(description="Otwórz filtry bieżącej listy ofert", gesture="kb:NVDA+alt+f")
    def script_allegroFilters(self, gesture):
        self._request_filters(browser_page())

    @script(description="Otwórz ulubione listy ofert", gesture="kb:NVDA+shift+alt+l")
    def script_openFavorites(self, gesture):
        wx.CallAfter(self._favorites.open, browser_page())

    @script(description="Otwórz listę wczytanych wyników wyszukiwania", gesture="kb:NVDA+shift+alt+w")
    def script_showSearchResults(self, gesture):
        wx.CallAfter(self._navigation.show_offers, browser_page())

    def _request_filters(self, page):
        site = listing_site(page.url) if page else None
        if site in {"OLX", "Vinted"}:
            # ShowModal must run outside NVDA's event queue so focus and speech
            # events can be processed while the dialog is open.
            if not self._catalog_filters_open:
                self._catalog_filters_open = True
                wx.CallAfter(self._prepare_catalog_filters, page, site)
            return
        if site != "Allegro Lokalnie":
            ui.message("Zakupocholik. Otwórz listę wyników OLX, Vinted lub Allegro Lokalnie.")
            return
        if self._loading:
            ui.message("Zakupocholik pobiera już filtry.")
            return
        self._loading = True
        ui.message("Pobieram filtry Allegro Lokalnie.")

        def fetch():
            try:
                form = load_filters(page.url)
                error = None
            except FilterLoadError as failure:
                form = None
                error = str(failure)
                log.debugWarning(f"Zakupocholik: {error}")
            except (OSError, LookupError, TypeError, ValueError):
                log.debugWarning("Zakupocholik: nie można pobrać formularza filtrów", exc_info=True)
                form = None
                error = "Nie udało się odczytać filtrów tej listy Allegro Lokalnie."
            wx.CallAfter(self._finish_loading, form, page, error)

        threading.Thread(target=fetch, name="Zakupocholik filters", daemon=True).start()

    def _prepare_catalog_filters(self, page, site):
        targets = catalog_filter_targets(page, site) if self._active else ()
        if not targets:
            self._catalog_filters_open = False
            return
        condition = next((target for target in targets if target.condition), None)
        if site == "OLX" and condition:
            ui.message("Odczytuję dostępne stany przedmiotu na OLX.")

            def fetch():
                try:
                    conditions = load_olx_conditions(page.url)
                except (OSError, ValueError):
                    log.debugWarning("Zakupocholik: nie udało się odczytać stanu przedmiotu na OLX", exc_info=True)
                    conditions = ()
                wx.CallAfter(self._show_catalog_filters, page, site, targets, conditions)

            threading.Thread(target=fetch, name="Zakupocholik OLX condition", daemon=True).start()
            return
        if site == "Vinted" and condition and self._start_vinted_conditions(page, targets, condition):
            return
        self._show_catalog_filters(page, site, targets, ())

    def _start_vinted_conditions(self, page, targets, target):
        if not (winUser.getForegroundWindow() == page.window and page.matches(browser_page())):
            return False
        try:
            button = page.document.getNVDAObjectFromIdentifier(*target.identifier)
            if button is None or controlTypes.State.COLLAPSED not in button.states:
                return False
            button.doAction()
        except (CallCancelled, COMError, LookupError, NotImplementedError, OSError):
            return False
        core.callLater(150, self._poll_vinted_conditions, page, targets, button, monotonic() + 1.8)
        return True

    def _poll_vinted_conditions(self, page, targets, button, deadline):
        current = browser_page() if self._active and winUser.getForegroundWindow() == page.window else None
        if not page.matches(current) or current.document is not page.document:
            self._catalog_filters_open = False
            return
        try:
            conditions = read_vinted_conditions(page.document)
        except (CallCancelled, COMError, LookupError, OSError, ValueError):
            conditions = ()
        if not conditions and monotonic() < deadline:
            core.callLater(150, self._poll_vinted_conditions, page, targets, button, deadline)
            return
        try:
            expanded = bool(conditions) or controlTypes.State.EXPANDED in button.states
        except (CallCancelled, COMError, LookupError, OSError):
            expanded = False
        if expanded:
            KeyboardInputGesture.fromName("escape").send()
        core.callLater(150, self._show_catalog_filters, page, "Vinted", targets, conditions)

    def _show_catalog_filters(self, page, site, targets, conditions):
        try:
            if not self._active:
                return
            current = browser_page()
            if not page.matches(current) or current.document is not page.document:
                ui.message("Karta wyników się zmieniła. Otwórz filtry ponownie.")
                return
            target = open_catalog_filters(page, site, targets, conditions)
            if target and target != page.url and listing_site(target) == site:
                core.callLater(120, self._focus_address, target, page, 5)
        finally:
            self._catalog_filters_open = False

    def _finish_loading(self, form, page, error):
        previous = self._filter_cache.get(page.url)
        fresh_cache = previous and monotonic() - previous[0] <= FILTER_CACHE_SECONDS
        if (form is None and not fresh_cache and self._active and page.browser in BROWSERS
                and winUser.getForegroundWindow() == page.window and page.matches(browser_page())):
            ui.message("Odczytuję filtry ze źródła bieżącej karty."
                       + (" W Firefoksie może to chwilę potrwać." if page.browser == "firefox" else ""))
            KeyboardInputGesture.fromName("control+u").send()
            source_timeout = 25 if page.browser == "firefox" else 12
            core.callLater(350, self._wait_for_source, page, error, monotonic() + source_timeout)
            return
        self._show_filters(form, page, error + SAVED_FILTERS_HINT if error else None)

    def _wait_for_source(self, page, error, deadline):
        if not self._active:
            return
        if winUser.getForegroundWindow() != page.window:
            self._loading = False
            return
        current = browser_page()
        wanted = urldefrag(page.url)[0]
        if current and current.document is not page.document and current.url != "about:blank":
            source_url = urldefrag(current.url.removeprefix("view-source:"))[0]
            if source_url != wanted:
                self._loading = False
                return
            if current.document is not None and current.document.isReady:
                if page.browser == "firefox":
                    reader = firefox_filter_source(current.document, page.url)
                    self._read_firefox_source(reader, page, current.document, monotonic() + 50)
                    return
                try:
                    form = chrome_filter_source(current.document, page.url)
                except (CallCancelled, COMError, LookupError, OSError, ValueError):
                    log.debugWarning("Zakupocholik: nie udało się odczytać źródła karty Chrome", exc_info=True)
                    self._source_read_failed()
                    return
                if form is not None:
                    self._finish_source_read(form, page, current.document)
                    return
        if monotonic() < deadline:
            core.callLater(200, self._wait_for_source, page, error, deadline)
        else:
            self._loading = False
            ui.message(error + SAVED_FILTERS_HINT)

    def _read_firefox_source(self, reader, page, source, deadline):
        if not self._active:
            return
        active = browser_page()
        if (winUser.getForegroundWindow() != page.window or not active
                or active.document is not source or active.window != page.window):
            self._loading = False
            return
        try:
            form = next(reader)
        except (CallCancelled, COMError, LookupError, OSError, TypeError, ValueError, StopIteration):
            log.debugWarning("Zakupocholik: nie udało się odczytać źródła karty Firefoksa", exc_info=True)
            self._source_read_failed()
            return
        if form is not None:
            self._finish_source_read(form, page, source)
        elif monotonic() < deadline:
            core.callLater(10, self._read_firefox_source, reader, page, source, deadline)
        else:
            self._source_read_failed()

    def _source_read_failed(self):
        self._loading = False
        ui.message("Nie udało się odczytać źródła. Wróć do wyników." + SAVED_FILTERS_HINT)

    def _finish_source_read(self, form, page, source):
        active = browser_page()
        if (active and active.document is source and active.window == page.window
                and winUser.getForegroundWindow() == page.window):
            KeyboardInputGesture.fromName("control+w").send()
            core.callLater(200, self._return_from_source, form, page, monotonic() + 3)
        else:
            self._loading = False

    def _return_from_source(self, form, page, deadline):
        if not self._active:
            return
        if page.matches(browser_page()):
            self._show_filters(form, page, None)
        elif winUser.getForegroundWindow() == page.window and monotonic() < deadline:
            core.callLater(150, self._return_from_source, form, page, deadline)
        else:
            self._loading = False
            ui.message("Wróć do karty z wynikami i otwórz filtry ponownie.")

    def _request_saved_filters(self, page):
        if page is None or not allegro_listing(page.url):
            ui.message("Aby wczytać filtry z pliku HTML, przejdź do wyników Allegro Lokalnie.")
            return
        if self._loading:
            ui.message("Zakupocholik pobiera już filtry.")
            return
        gui.mainFrame.prePopup()
        try:
            with wx.FileDialog(
                gui.mainFrame, "Wybierz zapisaną stronę bieżących wyników Allegro Lokalnie",
                wildcard="Strony HTML (*.html;*.htm)|*.html;*.htm",
                style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
            ) as dialog:
                if dialog.ShowModal() != wx.ID_OK:
                    return
                path = Path(dialog.GetPath())
        finally:
            gui.mainFrame.postPopup()

        def read_file():
            try:
                with path.open("rb") as saved_page:
                    data = saved_page.read(MAX_SOURCE_LENGTH + 1)
                if len(data) > MAX_SOURCE_LENGTH:
                    raise FilterLoadError("Zapisana strona jest za duża.")
                form = parse_filter_form(data.decode("utf-8-sig"), page.url)
                listing_path = urlsplit(page.url).path.rstrip("/")
                clear_path = urlsplit(form.clear_url).path.rstrip("/")
                if listing_path != clear_path and not listing_path.startswith(clear_path + "/"):
                    raise FilterLoadError("Plik pochodzi z innej listy Allegro Lokalnie.")
            except (OSError, TypeError, ValueError) as failure:
                log.debugWarning("Zakupocholik: nie udało się wczytać zapisanej strony", exc_info=True)
                message = str(failure) if isinstance(failure, FilterLoadError) else "Nie udało się odczytać zapisanego pliku HTML."
                wx.CallAfter(self._show_filters, None, page, message)
            else:
                wx.CallAfter(self._show_filters, form, page, None)

        self._loading = True
        threading.Thread(target=read_file, name="Zakupocholik saved filters", daemon=True).start()

    def _show_filters(self, form, page, error):
        self._loading = False
        if not self._active:
            return
        if not page.matches(browser_page()):
            ui.message("Karta wyników się zmieniła. Otwórz filtry ponownie na właściwej liście.")
            return
        cached = form is None
        now = monotonic()
        if cached:
            previous = self._filter_cache.get(page.url)
            if previous is None or now - previous[0] > FILTER_CACHE_SECONDS:
                ui.message(error)
                return
            form = previous[1]
        else:
            self._filter_cache.pop(page.url, None)
            if len(self._filter_cache) >= FILTER_CACHE_SIZE:
                del self._filter_cache[next(iter(self._filter_cache))]
            self._filter_cache[page.url] = now, form
        dialog = FiltersDialog(gui.mainFrame, form, cached=cached)
        target = None
        gui.mainFrame.prePopup()
        try:
            if dialog.ShowModal() == wx.ID_OK:
                target = dialog.result_url
        finally:
            dialog.closed = True
            gui.mainFrame.postPopup()
            dialog.Destroy()
        if allegro_listing(target):
            core.callLater(120, self._focus_address, target, page, 5)

    def _focus_address(self, target, page, attempts):
        if not self._active:
            return
        if not (winUser.getForegroundWindow() == page.window and page.matches(browser_page())):
            if attempts:
                core.callLater(100, self._focus_address, target, page, attempts - 1)
            else:
                ui.message("Nie udało się wrócić do karty z wynikami.")
            return
        KeyboardInputGesture.fromName("control+l").send()
        core.callLater(100, self._load_address, target, page, 8)

    def _load_address(self, target, page, attempts):
        if not self._active or winUser.getForegroundWindow() != page.window:
            return
        try:
            address_focused = _address_bar_focused(api.getFocusObject(), page.browser)
        except (CallCancelled, COMError, LookupError, OSError):
            address_focused = False
        if address_focused:
            if _send_address(target):
                KeyboardInputGesture.fromName("enter").send()
                self._navigation.wait_for_filtered_count(page, target)
            else:
                ui.message("Nie udało się wpisać adresu wyników.")
        elif attempts:
            core.callLater(100, self._load_address, target, page, attempts - 1)
        else:
            ui.message("Nie udało się ustawić kursora w pasku adresu.")
