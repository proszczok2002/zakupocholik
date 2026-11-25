from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from comtypes import COMError
from exceptions import CallCancelled
import gui
from gui import guiHelper
from logHandler import log
from NVDAState import WritePaths
import ui
import winUser
import wx

from .browser import has_olx_listing_filters
from .sites import listing_results_key, results_site


TITLE = "Ulubione listy — Zakupocholik"


@dataclass(frozen=True)
class Favorite:
    name: str
    url: str

    @property
    def label(self):
        return f"{self.name} — {results_site(self.url)}"


def _current_listing(page):
    if page is None or not (site := results_site(page.url)):
        return None
    try:
        document = page.document
        if site == "OLX" and not has_olx_listing_filters(document):
            return None
        name = document.rootNVDAObject.name if document else None
    except (AttributeError, CallCancelled, COMError, LookupError, OSError):
        return None
    return Favorite(" ".join((name or "").split()) or site, page.url)


class FavoriteStore:
    def __init__(self, path):
        self.path = path

    def read(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        if not isinstance(data, list):
            raise ValueError("Nieprawidłowy format ulubionych list.")
        favorites = []
        for entry in data:
            if not isinstance(entry, dict):
                raise ValueError("Nieprawidłowa ulubiona lista.")
            name, url = entry.get("name"), entry.get("url")
            if not isinstance(name, str) or not name.strip() or not results_site(url):
                raise ValueError("Nieprawidłowa nazwa lub adres ulubionej listy.")
            favorites.append(Favorite(name, url))
        return favorites

    def write(self, favorites):
        pending = None
        try:
            with NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                    prefix=self.path.stem + "-", suffix=".tmp", delete=False) as stream:
                pending = Path(stream.name)
                json.dump([{"name": item.name, "url": item.url} for item in favorites],
                          stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            pending.replace(self.path)
        finally:
            if pending is not None:
                pending.unlink(missing_ok=True)


class FavoritesDialog(wx.Dialog):
    def __init__(self, parent, store, favorites, current):
        super().__init__(parent, title=TITLE)
        self.store = store
        self.favorites = favorites
        self.current = current
        self.result_url = None
        helper = guiHelper.BoxSizerHelper(self, orientation=wx.VERTICAL)
        self.items = helper.addLabeledControl(
            "Zapisane listy", wx.ListBox, size=self.FromDIP((490, 170)),
        )
        self.items.Bind(wx.EVT_LISTBOX, self._selection_changed)
        self.items.Bind(wx.EVT_LISTBOX_DCLICK, self._open)
        self.items.Bind(wx.EVT_KEY_DOWN, self._list_key)
        self.Bind(wx.EVT_CHAR_HOOK, self._edit_key)
        self.hint = wx.StaticText(self, label="Aby zapisać listę, otwórz wyniki na OLX, Vinted lub Allegro Lokalnie.")
        self.hint.Wrap(self.FromDIP(490))
        helper.addItem(self.hint)

        self.open_button = wx.Button(self, wx.ID_OK, label="&Otwórz listę")
        self.open_button.Bind(wx.EVT_BUTTON, self._open)
        helper.addItem(self.open_button)
        self.save_button = wx.Button(self, label="&Zapisz bieżącą listę do ulubionych…")
        self.save_button.Bind(wx.EVT_BUTTON, self._add)
        helper.addItem(self.save_button)
        self.rename_button = wx.Button(self, label="Zmień &nazwę…")
        self.rename_button.Bind(wx.EVT_BUTTON, self._rename)
        helper.addItem(self.rename_button)
        self.remove_button = wx.Button(self, label="&Usuń…")
        self.remove_button.Bind(wx.EVT_BUTTON, self._remove)
        helper.addItem(self.remove_button)
        self.close_button = wx.Button(self, wx.ID_CANCEL, label="Zamknij")
        helper.addDialogDismissButtons(self.close_button)
        self.SetSizer(helper.sizer)
        self._refresh()
        self.CentreOnParent()

    def _current_index(self):
        if self.current is None:
            return None
        key = listing_results_key(self.current.url)
        return next((index for index, item in enumerate(self.favorites) if listing_results_key(item.url) == key), None)

    def _selection_changed(self, event=None):
        selected = self.items.GetSelection() != wx.NOT_FOUND
        for button in (self.open_button, self.rename_button, self.remove_button):
            button.Enable(selected)

    def _refresh(self, index=None):
        current_index = self._current_index()
        can_save = self.current is not None and current_index is None
        self.hint.Show(self.current is None)
        self.save_button.Show(can_save)
        self.save_button.Enable(can_save)
        self.items.Set([item.label for item in self.favorites])
        self.items.Enable(bool(self.favorites))
        if self.favorites:
            if index is None:
                index = current_index if current_index is not None else 0
            self.items.SetSelection(min(index, len(self.favorites) - 1))
        self._selection_changed()
        self.Fit()
        self.Layout()
        if self.favorites:
            self.open_button.SetDefault()
            self.items.SetFocus()
        elif can_save:
            self.save_button.SetDefault()
            self.save_button.SetFocus()
        else:
            self.close_button.SetDefault()
            self.close_button.SetFocus()

    def reactivate(self, page):
        # Return to the prompt if a rename or deletion is still in progress.
        window = self
        while child := next((child for child in window.GetChildren()
                             if isinstance(child, wx.Dialog) and child.IsShown() and child.IsModal()), None):
            window = child
        window.Raise()
        if winUser.getForegroundWindow() != window.GetHandle():
            winUser.setForegroundWindow(window.GetHandle())
        if window is self and (current := _current_listing(page)) is not None and current != self.current:
            self.current = current
            self._refresh()

    def _edit_key(self, event):
        if wx.GetTopLevelParent(event.GetEventObject()) is self and not event.HasModifiers():
            if event.GetKeyCode() == wx.WXK_F2:
                self._rename(event)
                return
            if event.GetKeyCode() == wx.WXK_DELETE:
                self._remove(event)
                return
        event.Skip()

    def _list_key(self, event):
        if event.GetKeyCode() in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            self._open()
        else:
            event.Skip()

    def _open(self, event=None):
        index = self.items.GetSelection()
        if index != wx.NOT_FOUND:
            self.result_url = self.favorites[index].url
            self.EndModal(wx.ID_OK)

    def _name(self, title, initial):
        with wx.TextEntryDialog(self, "Nazwa ulubionej listy", title, initial) as dialog:
            while dialog.ShowModal() == wx.ID_OK:
                if name := dialog.GetValue().strip():
                    return name
                gui.messageBox("Wpisz nazwę listy.", title, wx.OK | wx.ICON_INFORMATION, self)
        return None

    def _save(self, favorites, index, message):
        try:
            self.store.write(favorites)
        except (OSError, UnicodeError):
            log.debugWarning("Zakupocholik: zapis ulubionych list nie powiódł się", exc_info=True)
            gui.messageBox("Nie udało się zapisać zmian w ulubionych listach.", TITLE, wx.OK | wx.ICON_ERROR, self)
            return
        self.favorites = favorites
        self._refresh(index)
        ui.message(message)

    def _add(self, event):
        if self.current is None or self._current_index() is not None:
            return
        if name := self._name("Zapisz bieżącą listę do ulubionych", self.current.name):
            self._save([*self.favorites, replace(self.current, name=name)], len(self.favorites), "Zapisano listę.")

    def _rename(self, event):
        index = self.items.GetSelection()
        if index == wx.NOT_FOUND:
            return
        item = self.favorites[index]
        if (name := self._name("Zmień nazwę listy", item.name)) and name != item.name:
            updated = self.favorites.copy()
            updated[index] = replace(item, name=name)
            self._save(updated, index, "Zmieniono nazwę listy.")

    def _remove(self, event):
        index = self.items.GetSelection()
        if index == wx.NOT_FOUND:
            return
        item = self.favorites[index]
        if gui.messageBox(f"Usunąć listę „{item.name}” z ulubionych?", TITLE,
                          wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION, self) == wx.YES:
            self._save(self.favorites[:index] + self.favorites[index + 1:], index, "Usunięto listę.")


class Favorites:
    def __init__(self):
        self.store = FavoriteStore(Path(WritePaths.configDir) / "zakupocholik-ulubione.json")
        self._busy = False
        self._dialog = None

    def open(self, page):
        if self._dialog is not None:
            self._dialog.reactivate(page)
            return
        if self._busy:
            return
        self._busy = True
        url = None
        try:
            current = _current_listing(page)
            gui.mainFrame.prePopup()
            try:
                try:
                    favorites = self.store.read()
                except (OSError, ValueError):
                    log.debugWarning("Zakupocholik: odczyt ulubionych list nie powiódł się", exc_info=True)
                    gui.messageBox("Nie udało się odczytać zapisanych list.", TITLE, wx.OK | wx.ICON_ERROR)
                    return
                with FavoritesDialog(gui.mainFrame, self.store, favorites, current) as dialog:
                    self._dialog = dialog
                    try:
                        if dialog.ShowModal() == wx.ID_OK:
                            url = dialog.result_url
                    finally:
                        self._dialog = None
            finally:
                gui.mainFrame.postPopup()
        finally:
            self._busy = False
        if url:
            try:
                os.startfile(url)
            except OSError:
                gui.messageBox("Nie udało się otworzyć listy w domyślnej przeglądarce.", TITLE, wx.OK | wx.ICON_ERROR)
