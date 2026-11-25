from dataclasses import dataclass

from comtypes import COMError
import controlTypes
import core
from exceptions import CallCancelled
import gui
import textInfos
from textInfos.offsets import Offsets
import ui
from virtualBuffers import VBufStorage_findMatch_word
import winUser
import wx

from .browser import browser_page
from .catalog_filters import VINTED_STATUS_LABELS, build_catalog_form
from .dialog import FiltersDialog
from .sites import VINTED_HOSTS, is_offer_link, site_url


ID = "IAccessible2::attribute_id"
CLASS = "IAccessible2::attribute_class"
Role = controlTypes.Role
State = controlTypes.State
UNAVAILABLE = frozenset({State.INVISIBLE, State.UNAVAILABLE})
FIELD_ROLES = frozenset({Role.BUTTON, Role.MENUBUTTON, Role.TOGGLEBUTTON, Role.CHECKBOX,
                         Role.RADIOBUTTON, Role.COMBOBOX, Role.EDITABLETEXT})


@dataclass(frozen=True)
class FilterTarget:
    label: str
    identifier: tuple[int, int]
    condition: bool = False


def _attributes(item):
    return item.textInfo._getControlFieldAttribs(*item.vbufFieldIdentifier)


def _element_by_id(document, identifier):
    return next(document._iterNodesByAttribs({ID: [identifier]}, nodeType="shopFilterRegion"), None)


def _text(value):
    return " ".join((value or "").split())


def _fields(document, start, end):
    for item in document._iterNodesByType("formField", pos=start):
        if item.textInfo._startOffset >= end:
            break
        field = _attributes(item)
        if field.get("role") not in FIELD_ROLES or UNAVAILABLE.intersection(field.get("states", ())):
            continue
        value = _text(item.textInfo.text)
        label = _text(field.get("name")) or value
        if label:
            yield item, field, label, value


def _olx_filters(document):
    panel = _element_by_id(document, "listing-filters")
    if panel is None:
        return
    start = panel.textInfo
    heading = next(document._iterNodesByType("heading1", pos=start), None)
    end = min(start._endOffset, heading.textInfo._startOffset) if heading else start._endOffset
    for identifier in ("photos", "courier"):
        item = _element_by_id(document, identifier)
        if item and item.textInfo._startOffset < start._startOffset:
            field = _attributes(item)
            label = _text(field.get("name")) or _text(item.textInfo.text)
            if (label and field.get("role") == Role.CHECKBOX
                    and not UNAVAILABLE.intersection(field.get("states", ()))):
                if State.CHECKED in field.get("states", ()):
                    label += ", zaznaczone"
                yield FilterTarget(label, item.vbufFieldIdentifier)
    previous_end = start._startOffset
    range_label = ""
    for item, field, label, value in _fields(document, start, end):
        role = field["role"]
        condition = role in {Role.BUTTON, Role.MENUBUTTON} and any(
            f" {text.casefold()}".endswith((" stan", " stan przedmiotu")) for text in (label, value)
        )
        if role in {Role.COMBOBOX, Role.EDITABLETEXT}:
            if label in {"Od", "Do"}:
                offset = item.textInfo._startOffset
                if label == "Od" or previous_end < offset:
                    range_label = ""
                if previous_end < offset <= previous_end + 100:
                    range_label = _text(document.makeTextInfo(Offsets(previous_end, offset)).text)
                label = f"{range_label or 'Wartość'} {label.lower()}"
            else:
                range_label = ""
            if value and value.casefold() not in {"od", "do", label.casefold()}:
                label += f", {value}"
        elif label.startswith("Wszystkie "):
            label = f"{label.removeprefix('Wszystkie ')}, wszystkie"
        elif State.CHECKED in field.get("states", ()) or State.PRESSED in field.get("states", ()):
            label += ", zaznaczone"
        previous_end = item.textInfo._endOffset
        # In this panel an unqualified Clear button belongs to a nearby input.
        if label != "Wyczyść":
            yield FilterTarget(label, item.vbufFieldIdentifier, condition)


def _vinted_filters(document):
    container = _element_by_id(document, "content")
    if container is None:
        return
    start = container.textInfo
    end = min(start._endOffset, start._startOffset + 12000)
    for link in document._iterNodesByType("link", pos=start):
        if link.textInfo._startOffset >= end:
            break
        if is_offer_link(_attributes(link).get("IAccessible::value"), "Vinted"):
            end = link.textInfo._startOffset
            break
    info = document.makeTextInfo(Offsets(start._startOffset, end))
    selected = []
    for part in info.getTextWithFields():
        if (isinstance(part, textInfos.FieldCommand) and part.command == "controlStart"
                and any(name.endswith("_catalog-selected-filter") for name in (part.field.get(CLASS) or "").split())):
            selected.append(info._getOffsetsFromFieldIdentifier(
                int(part.field["controlIdentifier_docHandle"]), int(part.field["controlIdentifier_ID"]),
            ))
    for item, field, label, value in _fields(document, start, end):
        classes = (field.get(CLASS) or "").split()
        if field.get(ID) in {"price_from", "price_to"}:
            if value and value != label:
                label += f", {value}"
        elif field["role"] in {Role.BUTTON, Role.MENUBUTTON} and "web_ui__Chip__chip" in classes:
            offset = item.textInfo._startOffset
            if any(begin <= offset < finish for begin, finish in selected):
                label = f"Usuń aktywny filtr: {label}"
            elif not {State.COLLAPSED, State.EXPANDED}.intersection(field.get("states", ())):
                continue
        elif field["role"] not in {Role.BUTTON, Role.MENUBUTTON} or label not in {
            "Filtry", "Wyczyść filtry", "Resetuj filtry",
        }:
            continue
        yield FilterTarget(label, item.vbufFieldIdentifier, label.casefold() == "stan")


def read_vinted_conditions(document):
    rows = document._iterNodesByAttribs(
        {ID: [VBufStorage_findMatch_word("status_ids-list-item")]}, nodeType="shopCondition",
    )
    options = {}
    for row in rows:
        field = _attributes(row)
        value = (field.get(ID) or "").removeprefix("status_ids-list-item-")
        if field.get("role") != Role.BUTTON or not (value.isascii() and value.isdecimal() and len(value) <= 8):
            continue
        if UNAVAILABLE.intersection(field.get("states", ())):
            continue
        label = _vinted_condition_label(row.textInfo) or VINTED_STATUS_LABELS.get(value)
        if label:
            options.setdefault(value, label)
    return tuple(options.items())


def _vinted_condition_label(info):
    in_title = False
    text = []
    for part in info.getTextWithFields():
        if isinstance(part, str) and in_title:
            text.append(part)
        elif isinstance(part, textInfos.FieldCommand):
            if part.command == "controlStart" and "web_ui__Cell__title" in (part.field.get(CLASS) or "").split():
                in_title = True
            elif part.command == "controlEnd" and in_title and text:
                break
    return _text("".join(text))


def catalog_filter_targets(page, site):
    document = page.document
    if site == "Vinted":
        parts = site_url(page.url, VINTED_HOSTS)
        if not parts or not (parts.path == "/catalog" or parts.path.startswith("/catalog/")):
            ui.message("Otwórz listę wyników Vinted.")
            return ()
    if document is None or not document.isReady:
        ui.message("Przenieś fokus do treści strony i spróbuj ponownie.")
        return ()
    try:
        targets = tuple(_olx_filters(document) if site == "OLX" else _vinted_filters(document))
    except (CallCancelled, COMError, LookupError, OSError, ValueError):
        targets = ()
    if not targets:
        ui.message(f"Nie znaleziono filtrów na tej stronie {site}.")
    return targets


def open_catalog_filters(page, site, targets, conditions):
    form = build_catalog_form(page.url, site, (target.label for target in targets), conditions=conditions)
    if form.flag_group or form.groups or form.numbers or form.sort:
        gui.mainFrame.prePopup()
        dialog = None
        try:
            dialog = FiltersDialog(gui.mainFrame, form, site=site, more_filters=True)
            result = dialog.ShowModal()
            if result == wx.ID_OK:
                return dialog.result_url
            if result != wx.ID_HELP:
                return
        finally:
            if dialog is not None:
                dialog.closed = True
                dialog.Destroy()
            gui.mainFrame.postPopup()
    _show_filter_list(page, site, targets)


def _show_filter_list(page, site, targets):
    gui.mainFrame.prePopup()
    try:
        with wx.SingleChoiceDialog(
            gui.mainFrame,
            "Wybierz filtr strzałkami i naciśnij Enter, aby przejść do niego na stronie.",
            f"Zakupocholik — filtry {site}",
            [target.label for target in targets],
        ) as dialog:
            dialog.SetSelection(0)
            selected = targets[dialog.GetSelection()] if dialog.ShowModal() == wx.ID_OK else None
    finally:
        gui.mainFrame.postPopup()
    if selected is not None:
        core.callLater(100, _focus_filter, page, selected, 6)


def _focus_filter(page, target, attempts):
    current = browser_page() if winUser.getForegroundWindow() == page.window else None
    if current is None:
        if attempts:
            core.callLater(100, _focus_filter, page, target, attempts - 1)
        return
    if not page.matches(current) or current.document is not page.document:
        ui.message("Karta wyników się zmieniła. Otwórz listę filtrów ponownie.")
        return
    try:
        obj = page.document.getNVDAObjectFromIdentifier(*target.identifier)
        if obj:
            states = obj.states
            if State.FOCUSABLE in states and not UNAVAILABLE.intersection(states):
                obj.setFocus()
                return
    except (CallCancelled, COMError, LookupError, NotImplementedError, OSError):
        pass
    if attempts:
        core.callLater(100, _focus_filter, page, target, attempts - 1)
    else:
        ui.message("Wybrany filtr jest już niedostępny. Otwórz listę filtrów ponownie.")
