from collections import OrderedDict
from dataclasses import dataclass
import re
from time import monotonic
from urllib.parse import urldefrag, urlsplit

import api
import controlTypes
from comtypes import COMError
import core
from exceptions import CallCancelled
import gui
import speech
import textInfos
from textInfos.offsets import Offsets
import ui
from virtualBuffers import VBufStorage_findMatch_word
import winUser
import wx

from .browser import browser_page, has_olx_listing_filters
from .sites import (
    LOCAL_HOSTS, OLX_HOSTS, VINTED_HOSTS, allegro_listing, is_offer_link, listing_results_key, offer_site,
    results_site, site_url,
)


NUMBER = r"[0-9][0-9 \u00a0\u202f]*"
VINTED_PRICE = rf"{NUMBER}[.,][0-9]{{2}}\s*zł"
VINTED_PRICES = re.compile(rf",\s*({VINTED_PRICE}),\s*({VINTED_PRICE})\s*$")
OLX_PRICE = re.compile(rf"{NUMBER}(?:[.,][0-9]{{2}})?\s*zł(?:\s+do negocjacji)?", re.I)
OLX_LOCATION = re.compile(r"(.+?)\s+[–-]\s+(?:Odświeżono|Dzisiaj|Wczoraj|[0-9]{1,2}\s+[a-ząćęłńóśźż]+)\b", re.I)
OLX_STATES = frozenset({
    "nowe", "używane", "uszkodzone", "powystawowe / outlet", "odnowione / poleasingowe",
})
RESULT_COUNTS = {
    "OLX": re.compile(rf"Znaleźliśmy\s+(?:ponad\s+)?(?P<number>{NUMBER})\s*ogłosze(?:nie|nia|ń)", re.I),
    "Vinted": re.compile(rf"(?<![0-9]){NUMBER}\s*\+?\s*wynik(?:ów|i|\b)", re.I),
    "Allegro Lokalnie": re.compile(rf"(?<![0-9])(?P<number>{NUMBER})(?P<noun>\s*ogłosze(?:nie|nia|ń))", re.I),
}
EMPTY_RESULTS = {
    "OLX": re.compile(r"nie znaleźliśmy (?:żadnych )?ogłoszeń|nie ma ogłoszeń", re.I),
    "Vinted": re.compile(r"nie znaleźliśmy pasujących przedmiotów|nie znaleziono (?:żadnych )?przedmiotów"
                         r"|nie ma żadnych wyników|brak wyników", re.I),
    "Allegro Lokalnie": re.compile(r"nie możemy znaleźć żadnego ogłoszenia", re.I),
}


def listing_site(url):
    return "Allegro Lokalnie" if allegro_listing(url) else offer_site(url)


def _offer_key(url):
    parts = urlsplit(url)
    return parts.hostname.removeprefix("www.") if parts.hostname else "", parts.path.rstrip("/")


def _link_fields(item):
    document_id, item_id = map(str, item.vbufFieldIdentifier)
    fields = [part.field for part in item.textInfo.getTextWithFields()
              if isinstance(part, textInfos.FieldCommand) and part.command == "controlStart"]
    link = next((field for field in fields if field.get("controlIdentifier_docHandle") == document_id
                 and field.get("controlIdentifier_ID") == item_id), {})
    return link, fields


def _card_url(link, site, fields):
    url = link.get("IAccessible::value")
    if site == "Allegro Lokalnie":
        parts = site_url(url, LOCAL_HOSTS)
        if parts and parts.path.startswith("/oferta/") and "mlc-itembox" in _classes(link):
            return url
    elif is_offer_link(url, site):
        if (site != "OLX" or link.get("IAccessible2::attribute_explicit-name") == "true"
                or any(field.get("IAccessible2::attribute_tag") == "h4" for field in fields)):
            return url
    return None


def offer_link_at(document, site, position):
    item = next(document._iterNodesByType("link", "up", position), None)
    if item:
        link, fields = _link_fields(item)
        if url := _card_url(link, site, fields):
            return item, url
    return None


def _classes(field):
    return (field.get("IAccessible2::attribute_class") or "").split()


def _text(value):
    return " ".join(value.split())


def _field_text_info(item, field):
    identifier = int(field["controlIdentifier_docHandle"]), int(field["controlIdentifier_ID"])
    start, end = item.textInfo._getOffsetsFromFieldIdentifier(*identifier)
    return item.document.makeTextInfo(Offsets(start, end))


def _local_fields(item):
    result = {}
    for part in item.textInfo.getTextWithFields():
        if not isinstance(part, textInfos.FieldCommand) or part.command != "controlStart":
            continue
        field = part.field
        classes = _classes(field)
        key = None
        if "mlc-itembox__title" in classes:
            key = "title"
        elif "ml-offer-price" in classes or "mlc-itembox__price__offer-price" in classes:
            key = "price"
        elif "mlc-itembox__price" in classes:
            key = "priceBlock"
        elif field.get("IAccessible2::attribute_tag") == "address":
            key = "location"
        if key and key not in result:
            result[key] = _field_text_info(item, field)
    return result


def _local_summary(item):
    fields = _local_fields(item)
    if "title" not in fields:
        return None
    parts = [_text(fields["title"].text).rstrip(".")]
    price = next((value for key in ("price", "priceBlock")
                  if key in fields and (value := _text(fields[key].text))), None)
    if not price:
        after_title = fields["title"].copy()
        after_title.collapse(end=True)
        after_title.setEndPoint(item.textInfo, "endToEnd")
        # Some virtual buffers flatten the price markup to ordinary text.
        values = _text(after_title.text)
        amounts = [match for match in OLX_PRICE.finditer(values)
                   if not re.search(r"dostaw\w*(?:\s+od)?\s*$", values[:match.start()], re.I)]
        if len(amounts) == 1:
            price = _text(amounts[0][0])
    if price:
        parts.append(f"Cena: {price}")
    if "location" in fields and (location := _text(fields["location"].text)):
        parts.append(f"Miejscowość: {location}")
    state = next((_text(part) for part in item.textInfo.getTextWithFields() if isinstance(part, str)
                  and _text(part).casefold().startswith("stan:")), None)
    if state:
        parts.append(state)
    return ". ".join(parts)


def _olx_summary(item):
    parts = [_text(item.textInfo.text)]
    _, fields = _link_fields(item)
    card = next((field for field in reversed(fields) if field.get("IAccessible2::attribute_tag") == "div"
                 and (field.get("IAccessible2::attribute_id") or "").isdigit()), None)
    if card is None:
        return parts[0]
    card_info = _field_text_info(item, card)
    after_title = item.textInfo.copy()
    after_title.collapse(end=True)
    after_title.setEndPoint(card_info, "endToEnd")
    price = state = location = None
    for part in after_title.getTextWithFields():
        if not isinstance(part, str):
            continue
        value = _text(part)
        if not price and OLX_PRICE.fullmatch(value):
            price = f"Cena: {value}"
        elif not price and value.casefold() in {"za darmo", "zamienię"}:
            price = value
        if not state and value.casefold() in OLX_STATES:
            state = f"Stan: {value}"
        if not location and (match := OLX_LOCATION.match(value)):
            location = f"Miejscowość: {match[1]}"
    return ". ".join(part for part in (*parts, price, state, location) if part)


def _vinted_summary(item):
    label = _text(item.textInfo.text)
    if match := VINTED_PRICES.search(label):
        base, total = (price.replace(".", ",") for price in match.groups())
        return f"{label[:match.start()]}. Cena przedmiotu {base}. Cena z opłatą Vinted {total}."
    return label


OFFER_SUMMARIES = {"OLX": _olx_summary, "Vinted": _vinted_summary, "Allegro Lokalnie": _local_summary}


def offer_summary(document, site):
    result = offer_link_at(document, site, document.selection)
    if result:
        item, _ = result
        return OFFER_SUMMARIES[site](item)
    return None


def _search_result_offers(document, site):
    path = "items" if site == "Vinted" else "oferta"
    offers = {}
    for item in document._iterNodesByAttribs(
        {"IAccessible::value": [VBufStorage_findMatch_word(path)]}, nodeType="shopOfferLink",
    ):
        try:
            link, fields = _link_fields(item)
            if not (url := _card_url(link, site, fields)):
                continue
            key = _offer_key(url)
            if key not in offers and (label := OFFER_SUMMARIES[site](item)):
                offers[key] = (label, url)
        except (LookupError, ValueError):
            # A card can disappear from the virtual buffer during enumeration.
            continue
    return tuple(offers.values())


def _count_node(document, *classes):
    return next(document._iterNodesByAttribs({
        "IAccessible2::attribute_class": [VBufStorage_findMatch_word(name) for name in classes],
    }, nodeType="shopResultCount"), None)


def result_count(document, site):
    limit = min(8000, document.makeTextInfo(textInfos.POSITION_ALL)._endOffset)
    start = 0
    if site == "OLX":
        grid = _count_node(document, "listing-grid-container", "listing-item-badge-portal-root")
        if grid and 0 < grid.textInfo._startOffset <= 64_000:
            limit = grid.textInfo._startOffset
        filters = next(document._iterNodesByAttribs({
            "IAccessible2::attribute_id": ["listing-filters"],
        }, nodeType="shopResultCount"), None)
        if filters and 0 < filters.textInfo._endOffset < limit:
            start = filters.textInfo._endOffset
    elif site == "Vinted":
        links = document._iterNodesByAttribs({
            "IAccessible::value": [VBufStorage_findMatch_word("items")],
        }, nodeType="shopResultCount")
        first_offer = next((link for link in links if is_offer_link(_link_fields(link)[0].get("IAccessible::value"), site)), None)
        if first_offer and 0 < first_offer.textInfo._startOffset <= 64_000:
            limit = first_offer.textInfo._startOffset
    header = document.makeTextInfo(Offsets(start, limit))
    if site == "Allegro Lokalnie":
        counter = next(document._iterNodesByAttribs({
            "IAccessible2::attribute_class": [VBufStorage_findMatch_word("mlc-listing__offer-count")],
        }, nodeType="shopResultCount"), None)
        match = RESULT_COUNTS[site].search(counter.textInfo.text) if counter else None
    elif site == "Vinted":
        # The count is its own text node. Searching whole buffer text can
        # accidentally glue digits from a search phrase to the result count.
        match = next((found for part in header.getTextWithFields() if isinstance(part, str)
                      and (found := RESULT_COUNTS[site].fullmatch(part.strip().rstrip(".")))), None)
    else:
        match = RESULT_COUNTS[site].search(header.text)
    if match:
        label = _text(match[0])
        if (match.groupdict().get("number") or label.split()[0]).strip() == "0":
            return "Brak wyników."
        if site == "Vinted" and "+" in label:
            return f"Ponad {label.replace('+', '').strip()}."
        if site == "Allegro Lokalnie":
            return f"Znaleźliśmy {_text(match['number'])} {match['noun'].strip()}."
        return f"{label[:1].upper()}{label[1:]}."
    if EMPTY_RESULTS[site].search(header.text):
        return "Brak wyników."
    return None


def _restore(document, site, wanted):
    for link in document._iterNodesByType("link"):
        field, fields = _link_fields(link)
        url = _card_url(field, site, fields)
        if url and _offer_key(url) == wanted:
            if site == "Allegro Lokalnie":
                info = _local_fields(link).get("title", link.textInfo)
            else:
                info = link.textInfo
            info = info.copy()
            info.collapse()
            speech.cancelSpeech()
            document._set_selection(info, reason=controlTypes.OutputReason.QUICKNAV)
            spoken = info.copy()
            spoken.expand(textInfos.UNIT_LINE)
            ui.message(_text(spoken.text))
            return True
    return False


@dataclass
class Visit:
    listing: str
    offer: tuple[str, str]
    entered_offer: bool = False
    return_deadline: float | None = None


class ListingNavigation:
    def __init__(self):
        self.active = True
        self.visits = OrderedDict()
        self.counts = OrderedDict()
        self._count_request = None
        self._offer_dialog = None

    def close(self):
        self.active = False
        self.visits.clear()
        self.counts.clear()
        self._count_request = None

    def show_offers(self, page):
        if not self.active:
            return
        if self._offer_dialog is not None:
            self._offer_dialog.Raise()
            if winUser.getForegroundWindow() != self._offer_dialog.GetHandle():
                winUser.setForegroundWindow(self._offer_dialog.GetHandle())
            return
        site = results_site(page.url) if page else None
        if not site:
            ui.message("Otwórz listę wyników OLX, Vinted lub Allegro Lokalnie.")
            return
        document = page.document
        if not (document and document.isReady):
            ui.message("Poczekaj na wczytanie wyników i spróbuj ponownie.")
            return
        try:
            if site == "OLX" and not has_olx_listing_filters(document):
                ui.message("Otwórz listę wyników OLX.")
                return
            offers = _search_result_offers(document, site)
        except (CallCancelled, COMError, LookupError, OSError):
            ui.message("Nie udało się odczytać wyników. Spróbuj ponownie.")
            return
        if not offers:
            ui.message("Na tej stronie nie ma wczytanych ofert.")
            return

        selected_url = None
        gui.mainFrame.prePopup()
        try:
            with wx.SingleChoiceDialog(
                gui.mainFrame,
                "Wybierz ofertę strzałkami i naciśnij Enter, aby ją otworzyć.",
                f"Wyniki wyszukiwania — {site}",
                [label for label, _ in offers],
            ) as dialog:
                self._offer_dialog = dialog
                try:
                    dialog.SetSelection(0)
                    dialog.FindWindow(wx.ID_OK).SetLabel("Otwórz ofertę")
                    if dialog.ShowModal() == wx.ID_OK:
                        selection = dialog.GetSelection()
                        if selection != wx.NOT_FOUND:
                            selected_url = offers[selection][1]
                finally:
                    self._offer_dialog = None
        finally:
            gui.mainFrame.postPopup()
        if selected_url:
            core.callLater(100, self._open_result, page, site, selected_url, 6)

    def _open_result(self, page, site, url, attempts):
        if not self.active:
            return
        current = browser_page() if winUser.getForegroundWindow() == page.window else None
        if current is None and attempts:
            core.callLater(100, self._open_result, page, site, url, attempts - 1)
            return
        if not page.matches(current) or current.document is not page.document:
            ui.message("Karta wyników się zmieniła. Otwórz listę ofert ponownie.")
            return
        try:
            for item in page.document._iterNodesByAttribs({"IAccessible::value": [url]}, nodeType="shopOfferLink"):
                link, fields = _link_fields(item)
                if _card_url(link, site, fields) == url:
                    self._remember(self.visits, page.window, Visit(urldefrag(page.url)[0], _offer_key(url)))
                    item.activate()
                    return
        except (CallCancelled, COMError, LookupError, OSError):
            pass
        ui.message("Wybranej oferty nie ma już w bieżących wynikach.")

    def remember_activation(self, document, position):
        site = listing_site(document.documentURL)
        if not site:
            return
        page = browser_page()
        if not page or page.document is not document:
            return
        result = offer_link_at(document, site, position)
        if result:
            _, url = result
            self._remember(self.visits, page.window, Visit(urldefrag(page.url)[0], _offer_key(url)))

    def _remember(self, mapping, window, value):
        mapping[window] = value
        mapping.move_to_end(window)
        if len(mapping) > 16:
            mapping.popitem(last=False)

    def schedule(self, document):
        if not self.active or not document.isReady or getattr(document, "_shop_listing_pending", False):
            return
        try:
            focus = api.getFocusObject()
            if (focus and focus.treeInterceptor is document
                    and site_url(document.documentURL, OLX_HOSTS | VINTED_HOSTS | LOCAL_HOSTS)):
                document._shop_listing_pending = True
                wx.CallLater(300, self._observe, document)
        except (CallCancelled, COMError, LookupError, OSError):
            pass

    def _observe(self, document):
        document._shop_listing_pending = False
        if not self.active or not document.isReady:
            return
        page = browser_page()
        if not page or page.document is not document:
            return
        try:
            url = urldefrag(page.url)[0]
            site = listing_site(url)
            visit = self.visits.get(page.window)
            if visit and _offer_key(url) == visit.offer:
                visit.entered_offer = True
                visit.return_deadline = None
            elif visit and visit.entered_offer:
                if site and not document.passThrough and visit.listing == url:
                    now = monotonic()
                    if visit.return_deadline is None:
                        visit.return_deadline = now + 3
                    if now < visit.return_deadline:
                        if _restore(document, site, visit.offer):
                            visit.entered_offer = False
                            return
                    else:
                        visit.entered_offer = False
                elif url != visit.listing:
                    visit.entered_offer = False
            if site:
                self._announce_count(page, site)
        except (CallCancelled, COMError, LookupError, OSError):
            pass

    def _announce_count(self, page, site):
        if count := result_count(page.document, site):
            key = urldefrag(page.url)[0], count
            if self.counts.get(page.window) != key:
                self._remember(self.counts, page.window, key)
                ui.message(count)
        return count

    def wait_for_filtered_count(self, page, address):
        request = self._count_request = object()
        wx.CallLater(400, self._check_filtered_count, request, page, address, monotonic() + 8)

    def _check_filtered_count(self, request, previous, address, deadline):
        if (not self.active or self._count_request is not request or monotonic() > deadline
                or winUser.getForegroundWindow() != previous.window):
            return
        try:
            page = browser_page()
            if (page and page.document and page.document.isReady and page.document is not previous.document
                    and (site := listing_site(page.url)) and listing_results_key(page.url) == listing_results_key(address)
                    and self._announce_count(page, site)):
                return
        except (CallCancelled, COMError, LookupError, OSError, ValueError):
            pass
        wx.CallLater(400, self._check_filtered_count, request, previous, address, deadline)

    def restore(self, page):
        document = page.document if page else None
        site = listing_site(page.url) if page else None
        visit = self.visits.get(page.window) if page else None
        if not (site and document and document.isReady):
            ui.message("Otwórz listę wyników OLX, Vinted lub Allegro Lokalnie.")
            return
        if not visit or visit.listing != urldefrag(page.url)[0]:
            ui.message("Nie zapamiętano otwartej oferty z tej listy.")
            return
        if document.passThrough:
            ui.message("Włącz tryb przeglądania i spróbuj ponownie.")
            return
        try:
            if _restore(document, site, visit.offer):
                return
        except (CallCancelled, COMError, LookupError, OSError):
            pass
        ui.message("Tej oferty nie ma w bieżących wynikach.")

    def speak_count(self, page):
        site = listing_site(page.url) if page else None
        document = page.document if page else None
        if not (site and document and document.isReady):
            ui.message("Otwórz listę wyników OLX, Vinted lub Allegro Lokalnie.")
            return
        try:
            count = result_count(document, site)
        except (CallCancelled, COMError, LookupError, OSError):
            count = None
        ui.message(count or "Strona nie podaje liczby wyników.")
