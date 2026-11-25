from decimal import InvalidOperation
import threading
from unicodedata import combining, normalize

import ui
import wx
from logHandler import log

from .sites import ALL_SOURCES, DISTANCE_SORT, START_SORT, search_cities, validated_numbers


def _search_text(text):
    folded = normalize("NFKD", text.casefold().replace("ł", "l"))
    return "".join(character for character in folded if not combining(character))


class FiltersDialog(wx.Dialog):
    def __init__(self, parent, form, *, cached=False, site="Allegro Lokalnie", more_filters=False):
        title = f"Zakupocholik — filtry {site}"
        if cached:
            title += " — ostatnio pobrane dla tej listy"
        super().__init__(parent, title=title)
        self.form = form
        self.result_url = None
        self.closed = False
        self.cleared = False
        self.has_location = site == "Allegro Lokalnie"
        self.flag_group = None if self.has_location else form.flag_group
        self.selections = {
            group.name: {choice.value for choice in group.choices if choice.selected}
            for group in form.groups
        }
        self.active_group = -1
        self.visible_choices = ()

        body = wx.BoxSizer(wx.VERTICAL)
        self.scroll = wx.ScrolledWindow(self, style=wx.TAB_TRAVERSAL | wx.VSCROLL)
        self.scroll.SetScrollRate(0, self.FromDIP(12))
        body.Add(self.scroll, proportion=1, flag=wx.EXPAND | wx.ALL, border=12)
        layout = wx.BoxSizer(wx.VERTICAL)

        category = None
        if self.has_location and form.categories:
            category = self._add_listing_links(layout, "Kategoria", "Przejdź do kategorii", form.categories)
        if self.has_location and form.active_filters:
            self._add_listing_links(layout, "Aktywny filtr", "Usuń wybrany filtr", form.active_filters)

        self.flag_controls = {}
        if self.flag_group:
            for choice in self.flag_group.choices:
                control = wx.CheckBox(self.scroll, label=choice.label)
                control.SetValue(choice.selected)
                layout.Add(control, flag=wx.EXPAND | wx.BOTTOM, border=7)
                self.flag_controls[choice.value] = control
        self.number_controls = {}
        for field in form.numbers:
            self.number_controls[field.name] = self._add_labeled(layout, field.label, wx.TextCtrl, value=field.value)

        self.sort = None
        if form.sort or self.has_location:
            self.sort = self._add_labeled(layout, "Sortowanie", wx.Choice,
                                          choices=[choice.label for choice in form.sort])
            if form.sort:
                self.sort.SetSelection(next((i for i, item in enumerate(form.sort) if item.selected), 0))
            self.sort.Enable(bool(form.sort))
        if form.groups or self.has_location:
            self._add_filter_groups(layout)
        if self.has_location:
            self._add_location_controls(layout)
        self.scroll.SetSizer(layout)

        footer = wx.BoxSizer(wx.VERTICAL)
        body.Add(footer, flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=12)
        reset_label = "&Wyczyść filtry" if self.has_location else "&Wyczyść pola w tym oknie"
        reset = wx.Button(self, label=reset_label)
        footer.Add(reset)
        reset.Bind(wx.EVT_BUTTON, self._reset)
        if more_filters:
            more = wx.Button(self, label="Przejdź do &filtrów na stronie…")
            more.Bind(wx.EVT_BUTTON, lambda event: self.EndModal(wx.ID_HELP))
            footer.Add(more, flag=wx.TOP, border=6)
        apply = wx.Button(self, wx.ID_OK, label="&Zastosuj filtry")
        cancel = wx.Button(self, wx.ID_CANCEL, label="Anuluj")
        buttons = wx.StdDialogButtonSizer()
        buttons.AddButton(apply)
        buttons.AddButton(cancel)
        buttons.Realize()
        footer.Add(buttons, flag=wx.EXPAND | wx.TOP, border=10)
        self.Bind(wx.EVT_BUTTON, self._apply, id=wx.ID_OK)
        apply.SetDefault()
        self.SetSizer(body)
        screen = wx.GetClientDisplayRect()
        width, height = self.FromDIP((520, 680 if self.has_location else 510))
        self.SetSize((min(width, screen.width - 32), min(height, screen.height - 48)))
        self.CentreOnParent()
        first = category or next(iter(self.flag_controls.values()), None)
        if first is None:
            first = next(iter(self.number_controls.values()), None)
        if first is None:
            first = self.city_text if self.has_location else self.groups if form.groups else self.sort
        (first or apply).SetFocus()

    def _add_filter_groups(self, layout):
        form = self.form
        self.groups = self._add_labeled(
            layout, "Grupa filtrów", wx.Choice, choices=[group.label for group in form.groups],
        )
        self.option_search = self._add_labeled(
            layout, "Szukaj w opcjach grupy", wx.TextCtrl, style=wx.TE_PROCESS_ENTER,
        )
        self.option_count = wx.StaticText(self.scroll)
        layout.Add(self.option_count, flag=wx.BOTTOM, border=4)
        self.choices = self._add_labeled(
            layout, "Opcje wybranej grupy", wx.ListCtrl, size=(450, 125),
            style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.LC_NO_HEADER,
        )
        self.choices.InsertColumn(0, "Opcja")
        self.choices.EnableCheckBoxes()
        self.choices.Bind(wx.EVT_KEY_DOWN, self._toggle_choice)
        self.groups.Bind(wx.EVT_CHOICE, self._change_group)
        self.option_search.Bind(wx.EVT_TEXT, self._filter_choices)
        self.option_search.Bind(wx.EVT_TEXT_ENTER, self._focus_choices)
        self.groups.Enable(bool(form.groups))
        self.option_search.Enable(bool(form.groups))
        self.choices.Enable(bool(form.groups))
        if form.groups:
            self.groups.SetSelection(next((i for i, group in enumerate(form.groups) if group.name == "stan"), 0))
            self._change_group()

    def _add_location_controls(self, layout):
        form = self.form
        self.cities = [None] + ([form.city] if form.city else [])
        self.distances = [0] + [distance for distance in form.distances if distance]
        self.city_text = self._add_labeled(layout, "Nazwa miejscowości", wx.TextCtrl, style=wx.TE_PROCESS_ENTER)
        self.search_button = wx.Button(self.scroll, label="S&zukaj miejscowości")
        layout.Add(self.search_button, flag=wx.BOTTOM, border=8)
        self.search_button.Bind(wx.EVT_BUTTON, self._find_cities)
        self.city_text.Bind(wx.EVT_TEXT_ENTER, self._find_cities)
        self.city_choice = self._add_labeled(layout, "Miejscowość", wx.Choice)
        self._show_cities(form.city)
        self.city_choice.Bind(wx.EVT_CHOICE, lambda event: self._update_distance())
        self.distance = self._add_labeled(
            layout, "Promień wyszukiwania", wx.Choice,
            choices=["Tylko miejscowość"] + [f"{n} km" for n in self.distances[1:]],
        )
        self.distance.SetSelection(self.distances.index(form.distance) if form.distance in self.distances else 0)
        self._update_distance()

    def _add_listing_links(self, layout, label, action, links):
        choice = self._add_labeled(layout, label, wx.Choice, choices=[link.label for link in links])
        choice.SetSelection(0)
        button = wx.Button(self.scroll, label=action)
        layout.Add(button, flag=wx.BOTTOM, border=8)

        def follow(event):
            self.result_url = links[choice.GetSelection()].url
            self.EndModal(wx.ID_OK)

        button.Bind(wx.EVT_BUTTON, follow)
        return choice

    def _add_labeled(self, layout, title, control_type, **kwargs):
        label = wx.StaticText(self.scroll, label=title)
        control = control_type(self.scroll, name=title, **kwargs)
        layout.Add(label, flag=wx.BOTTOM, border=2)
        layout.Add(control, flag=wx.EXPAND | wx.BOTTOM, border=7)
        return control

    def _save_group(self):
        if self.active_group >= 0:
            group = self.form.groups[self.active_group]
            selected = self.selections[group.name]
            selected.difference_update(choice.value for choice in self.visible_choices)
            selected.update(choice.value for index, choice in enumerate(self.visible_choices)
                            if self.choices.IsItemChecked(index))

    def _change_group(self, event=None):
        self._save_group()
        self.active_group = self.groups.GetSelection()
        self.option_search.ChangeValue("")
        self._show_choices()

    def _filter_choices(self, event):
        self._save_group()
        self._show_choices()

    def _show_choices(self):
        if self.active_group >= 0:
            group = self.form.groups[self.active_group]
            query = _search_text(self.option_search.GetValue()).strip()
            self.visible_choices = tuple(
                choice for choice in group.choices if query in _search_text(choice.label)
            )
            self.choices.DeleteAllItems()
            for index, choice in enumerate(self.visible_choices):
                self.choices.InsertItem(index, choice.label)
                self.choices.CheckItem(index, choice.value in self.selections[group.name])
            self.option_count.SetLabel(f"Pasujące opcje: {len(self.visible_choices)} z {len(group.choices)}.")
            self.choices.Enable(bool(self.visible_choices))
            if self.visible_choices:
                self.choices.SetColumnWidth(0, wx.LIST_AUTOSIZE)
                self.choices.Select(0)
                self.choices.Focus(0)
                self.choices.EnsureVisible(0)

    def _focus_choices(self, event):
        if not self.visible_choices:
            ui.message("Brak pasujących opcji.")
            return
        self.choices.SetFocus()
        ui.message(self.option_count.GetLabel())

    def _toggle_choice(self, event):
        item = self.choices.GetFocusedItem()
        if event.GetKeyCode() == wx.WXK_SPACE and item >= 0:
            self.choices.CheckItem(item, not self.choices.IsItemChecked(item))
        else:
            event.Skip()

    def _show_cities(self, selected):
        self.city_choice.SetItems([city.label if city else "Cała Polska" for city in self.cities])
        self.city_choice.SetSelection(self.cities.index(selected))

    def _update_distance(self):
        self.distance.Enable(self.city_choice.GetSelection() > 0 and len(self.distances) > 1)

    def _find_cities(self, event):
        if not self.search_button.IsEnabled():
            return
        name = self.city_text.GetValue().strip()
        if not 2 <= len(name) <= 120:
            ui.message("Wpisz od 2 do 120 znaków nazwy miejscowości.")
            self.city_text.SetFocus()
            return
        self.search_button.Disable()
        ui.message("Szukam miejscowości.")

        def fetch():
            try:
                cities = search_cities(name)
            except (OSError, ValueError, LookupError, TypeError):
                log.debugWarning("Zakupocholik: wyszukiwanie miejscowości nie powiodło się", exc_info=True)
                cities = None
            wx.CallAfter(self._found_cities, cities)

        threading.Thread(target=fetch, name="Zakupocholik cities", daemon=True).start()

    def _found_cities(self, cities):
        if self.closed:
            return
        self.search_button.Enable()
        if not cities:
            ui.message("Nie znaleziono miejscowości." if cities == [] else "Nie udało się pobrać listy miejscowości.")
            return
        selected = self.cities[self.city_choice.GetSelection()]
        options = {city.slug: city for city in cities}
        if selected:
            options.setdefault(selected.slug, selected)
        self.cities = [None, *options.values()]
        self._show_cities(cities[0] if len(cities) == 1 else options.get(selected.slug) if selected else None)
        self._update_distance()
        self.city_choice.SetFocus()
        ui.message(f"Liczba znalezionych miejscowości: {len(cities)}.")

    def _reset(self, event):
        self.cleared = True
        for control in self.flag_controls.values():
            control.SetValue(False)
        for field in self.number_controls.values():
            field.Clear()
        for group in self.form.groups:
            self.selections[group.name] = set(ALL_SOURCES) if group.name == "zrodlo" else set()
        if self.form.groups:
            self.active_group = -1
            self._change_group()
        if self.form.sort:
            self.sort.SetSelection(next((i for i, item in enumerate(self.form.sort) if item.value == START_SORT), 0))
        if self.has_location:
            self.city_text.Clear()
            self.city_choice.SetSelection(0)
            self.distance.SetSelection(0)
            self._update_distance()
        ui.message("Wyczyszczono filtry w oknie. Wybierz Zastosuj filtry, żeby odświeżyć wyniki.")

    def _apply(self, event):
        self._save_group()
        if self.flag_group:
            self.selections[self.flag_group.name] = {name for name, control in self.flag_controls.items()
                                                     if control.IsChecked()}
        city = self.cities[self.city_choice.GetSelection()] if self.has_location else None
        distance = self.distances[self.distance.GetSelection()] if self.has_location else None
        sort = self.form.sort[self.sort.GetSelection()].value if self.form.sort else ""
        try:
            values = validated_numbers(self.form.numbers, {
                name: field.GetValue() for name, field in self.number_controls.items()
            })
            if sort == DISTANCE_SORT and not city:
                raise ValueError("Sortowanie od najbliższych wymaga wybrania miejscowości.")
        except (ValueError, InvalidOperation) as error:
            wx.MessageBox(str(error), "Zakupocholik — sprawdź filtry", wx.OK | wx.ICON_WARNING, self)
            return
        self.result_url = self.form.results_url(
            self.selections, values, sort, city, distance, cleared=self.cleared,
        )
        self.EndModal(wx.ID_OK)
