from functools import cache
import re
from urllib.parse import urlsplit

import browseMode
from comtypes import COMError
import controlTypes
from exceptions import CallCancelled
from NVDAObjects import NVDAObject
import speech
import textInfos
from textInfos.offsets import Offsets
from virtualBuffers import VBufStorage_findMatch_word, VirtualBuffer

from .sites import OLX_HOSTS, OLX_OFFER_PATHS, VINTED_HOSTS, is_offer_link, site_url


LEVEL = 4
DESCRIPTION_LEVEL = 2
DESCRIPTION_CLASS = "u-overflow-hidden"
VINTED_ACTIONS = frozenset({"Kup teraz", "Zaproponuj cenę", "Zapytaj", "Wyślij wiadomość"})
VINTED_MEMBER = re.compile(r"^https://(?:www\.)?vinted\.pl/member/[0-9]+(?:[/?#-]|$)")
OLX_STOREFRONT_HOST = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.olx\.pl")
OLX_STOREFRONT_PATHS = frozenset({"", "/", "/home", "/home/"})
OLX_PROFILE_PATH = re.compile(r"/(?:d/)?oferty/uzytkownik/([a-zA-Z0-9_-]+)/?")
VINTED_PROFILE_PATH = re.compile(r"/member/([0-9]+)(?:-[^/]+)?/?")
VINTED_ITEM_PATH = re.compile(r"/items/([0-9]+)(?:-[^/]+)?/?")
VINTED_DETAILS = re.compile(
    r",\s+(?:(?:marka|stan|rozmiar):\s*|(?=\d[\d\s.,]*\s*zł(?:\s*,\s*\d|\s+(?:łącznie|zawiera))))",
    re.IGNORECASE,
)
SELLER_PREFIX = re.compile(
    r"^(?:seller profile of|profil sprzedającego|profil sprzedającej|profil użytkownika|profil użytkowniczki)\s+",
    re.IGNORECASE,
)
VINTED_SPOTLIGHT = re.compile(
    r"^(?:Promocja\s+)?Ekspozycja użytkowniczki lub użytkownika\s+(.+?)(?:\.\s+(?:Ocena|Liczba opinii):|\.?$)",
    re.IGNORECASE,
)
PLACEHOLDER_NAMES = frozenset({
    "awatar", "avatar", "logo", "obraz", "zdjęcie", "zdjęcie profilowe", "profil", "moje konto", "twoje konto",
    "mój profil", "profil sprzedającego", "profil sprzedającej", "profil użytkownika", "profil użytkowniczki",
    "seller profile", "zobacz profil", "wyświetl profil", "zobacz ogłoszenia", "zobacz wszystkie ogłoszenia",
    "wszystkie ogłoszenia", "przejdź do treści", "obserwuj", "przestań obserwować", "więcej", "pokaż więcej",
})
NAVIGATION_LANDMARKS = frozenset({"navigation", "banner", "contentinfo"})
NON_TITLE_CONTENT = frozenset({controlTypes.Role.GRAPHIC, controlTypes.Role.BUTTON})


class _OfferHeading(browseMode.TextInfoQuickNavItem):
    def __init__(self, document, text_info, identifier, title, level=LEVEL, description=None):
        super().__init__("heading", document, text_info)
        self.title = title
        self.level = level
        self.description = description
        self.vbufFieldIdentifier = identifier

    @property
    def label(self):
        return self.title

    @property
    def obj(self):
        return self.document.getNVDAObjectFromIdentifier(*self.vbufFieldIdentifier)

    def isChild(self, parent):
        if isinstance(parent, _OfferHeading):
            return self.level > parent.level
        try:
            fields = parent.textInfo._getControlFieldAttribs(*parent.vbufFieldIdentifier)
            return self.level > int(fields["level"])
        except (AttributeError, LookupError, TypeError, ValueError):
            return super().isChild(parent)

    def report(self, readUnit=None):
        sequence = speech.getPropertiesSpeech(
            reason=controlTypes.OutputReason.QUICKNAV,
            name=self.title,
            role=controlTypes.Role.HEADING,
            roleText=_("heading level %d") % self.level,
        )
        if self.description:
            sequence.append(self.description)
        speech.speak(sequence)


def _is_olx_storefront(url):
    if not isinstance(url, str):
        return False
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return False
    if not host or host in OLX_HOSTS or not OLX_STOREFRONT_HOST.fullmatch(host):
        return False
    parts = site_url(url, {host})
    return parts is not None and parts.path in OLX_STOREFRONT_PATHS


def _heading_destination(url, site):
    parts = site_url(url, OLX_HOSTS if site == "OLX" else VINTED_HOSTS)
    if parts is None:
        return None
    if site == "OLX":
        if profile := OLX_PROFILE_PATH.fullmatch(parts.path):
            return "profile", profile[1]
        if any(parts.path.startswith(prefix) and parts.path != prefix for prefix in OLX_OFFER_PATHS):
            path = parts.path.removeprefix("/d")
            return "offer", path.rstrip("/")
    else:
        if profile := VINTED_PROFILE_PATH.fullmatch(parts.path):
            return "profile", profile[1]
        if item := VINTED_ITEM_PATH.fullmatch(parts.path):
            return "offer", item[1]
    return None


def _link_content(parts):
    fields = []
    text = []
    plain_text = []
    suppressed = []
    for part in parts:
        if isinstance(part, str):
            text.append(part)
            if not suppressed or not suppressed[-1]:
                plain_text.append(part)
        elif isinstance(part, textInfos.FieldCommand):
            if part.command == "controlStart":
                fields.append(part.field)
                suppressed.append(bool(suppressed and suppressed[-1]) or part.field.get("role") in NON_TITLE_CONTENT)
            elif part.command == "controlEnd" and suppressed:
                suppressed.pop()
    return fields, "".join(plain_text), "".join(text)


def _first_title(text, site, kind):
    for line in text.splitlines():
        label = " ".join(line.split()).strip(" \u200b\ufffc")
        if kind == "profile":
            label = SELLER_PREFIX.sub("", label)
        elif site == "Vinted":
            label = VINTED_DETAILS.split(label, maxsplit=1)[0].strip()
        if (label and label.casefold() not in PLACEHOLDER_NAMES
                and not label.startswith(("/", "https://", "http://", "Aby uzyskać brakujące opisy obrazów"))
                and not re.fullmatch(r"[\d\s.,]+\s*zł", label, re.IGNORECASE)):
            return label
    return None


def _link_heading(item, site, current):
    fields, plain, text = _link_content(item.textInfo.getTextWithFields())
    document_id, node_id = map(str, item.vbufFieldIdentifier)
    link = next((field for field in fields if field.get("controlIdentifier_docHandle") == document_id
                 and field.get("controlIdentifier_ID") == node_id), None)
    if not link or link.get("role") != controlTypes.Role.LINK:
        return None
    destination = _heading_destination(link.get("IAccessible::value"), site)
    if not destination or destination == current:
        return None
    kind, _ = destination
    if any(field.get("landmark") in NAVIGATION_LANDMARKS for field in fields):
        return None
    if any(field.get("role") == controlTypes.Role.HEADING for field in fields):
        return destination, 4, None
    if title := _first_title(plain, site, kind):
        # Chrome can expose an explicit link name as text even for a photo.
        priority = 2 if link.get("IAccessible2::attribute_explicit-name") == "true" else 3
        return destination, priority, title
    if kind == "profile" and site == "Vinted":
        for field in reversed(fields):
            if spotlight := VINTED_SPOTLIGHT.match(field.get("name") or ""):
                return destination, 2, spotlight[1]
    title = _first_title(link.get("name") or text, site, kind)
    return (destination, 1, title) if title else None


def _entry_headings(document, site, url, occupied):
    current = _heading_destination(url, site)
    paths = ("oferta", "uzytkownik") if site == "OLX" else ("items", "member")
    links = document._iterNodesByAttribs({
        "IAccessible::value": [VBufStorage_findMatch_word(path) for path in paths],
    }, nodeType="shopHeadingLink")
    selected = {}
    for link in links:
        if link.vbufFieldIdentifier in occupied:
            continue
        try:
            candidate = _link_heading(link, site, current)
        except (CallCancelled, COMError, LookupError, OSError):
            continue
        if candidate:
            destination, priority, title = candidate
            if destination not in selected or priority > selected[destination][0]:
                selected[destination] = (priority, title, link)
    return tuple(sorted(
        _OfferHeading(document, link.textInfo, link.vbufFieldIdentifier, title)
        for _, title, link in selected.values() if title
    ))


def _description_field(item):
    stack = []
    wrapper_id = str(item.vbufFieldIdentifier[1])
    in_sidebar = False
    seen_title = False
    seen_details = False
    for part in item.textInfo.getTextWithFields():
        if not isinstance(part, textInfos.FieldCommand):
            continue
        if part.command == "controlEnd":
            if stack:
                stack.pop()
            continue
        if part.command != "controlStart":
            continue
        field = part.field
        tag = field.get("IAccessible2::attribute_tag")
        classes = (field.get("IAccessible2::attribute_class") or "").split()
        in_sidebar = in_sidebar or field.get("IAccessible2::attribute_id") == "sidebar"
        if in_sidebar and tag == "h1":
            seen_title = True
        elif seen_title and field.get("role") == controlTypes.Role.HEADING:
            break
        if seen_title:
            if "details-list__item-value" in classes:
                seen_details = True
            # Firefox retains the formatted span. Chrome exposes only the
            # anonymous div following the specification rows.
            if "web_ui__Text__format" in classes or (
                seen_details and stack and stack[-1] == wrapper_id and tag == "div" and not classes
            ):
                return int(field["controlIdentifier_docHandle"]), int(field["controlIdentifier_ID"])
        stack.append(field.get("controlIdentifier_ID"))
    return None


def _description_item(document):
    candidates = document._iterNodesByAttribs({
        "IAccessible2::attribute_class": [VBufStorage_findMatch_word(DESCRIPTION_CLASS)],
        "IAccessible2::attribute_tag": ["div"],
    }, nodeType="vintedDescription")
    for item in candidates:
        try:
            identifier = _description_field(item)
            if identifier is None:
                continue
            offsets = item.textInfo._getOffsetsFromFieldIdentifier(*identifier)
            text_info = document.makeTextInfo(Offsets(*offsets))
            description = text_info.text.strip()
            if description:
                return _OfferHeading(
                    document, text_info, identifier, "Opis oferty", DESCRIPTION_LEVEL, description,
                )
        except (CallCancelled, COMError, LookupError, OSError):
            continue
    return None


def vinted_description(document):
    item = _description_item(document)
    return item.description if item else None


def _vinted_sections(document):
    result = {}
    main = next(document._iterNodesByAttribs({
        "IAccessible2::attribute_class": [VBufStorage_findMatch_word("item-information")],
        "IAccessible2::attribute_tag": ["main"],
    }, nodeType="vintedOffer"), None)
    if main is not None:
        stack = []
        seen_title = False
        seen_details = False
        for part in main.textInfo.getTextWithFields():
            if not isinstance(part, textInfos.FieldCommand):
                continue
            if part.command == "controlEnd":
                if stack:
                    stack.pop()
                continue
            if part.command != "controlStart":
                continue
            field = part.field
            classes = (field.get("IAccessible2::attribute_class") or "").split()
            tag = field.get("IAccessible2::attribute_tag")
            role = field.get("role")
            sidebar = field.get("IAccessible2::attribute_id") == "sidebar" or (stack and stack[-1])
            stack.append(bool(sidebar))
            label = None
            info = None
            if sidebar:
                if tag == "h1":
                    seen_title = True
                if "details-list__item-value" in classes:
                    seen_details = True
                    label = "Parametry przedmiotu"
                if (seen_title and not seen_details and tag == "p"
                        and "web_ui__Text__subtitle" in classes and "Cena" not in result):
                    label = "Cena"
                    info = _field_text_info(document, main, field)
                    if not re.fullmatch(r"[0-9\s]+(?:[.,][0-9]{2})?\s*zł", info.text.strip()):
                        label = None
                if role in {controlTypes.Role.BUTTON, controlTypes.Role.LINK} and "Zakup i kontakt" not in result:
                    info = _field_text_info(document, main, field)
                    if info.text.strip() in VINTED_ACTIONS:
                        label = "Zakup i kontakt"
                if role == controlTypes.Role.LINK and VINTED_MEMBER.match(field.get("IAccessible::value") or ""):
                    label = "Sprzedający"
            elif any(name.endswith("_item-thumbnail") for name in classes):
                label = "Zdjęcia"
            if label and label not in result:
                identifier = _field_identifier(field)
                info = info if info is not None else _field_text_info(document, main, field)
                result[label] = _OfferHeading(document, info, identifier, label, DESCRIPTION_LEVEL)
    description = _description_item(document)
    if description:
        result[description.label] = description
    return sorted(result.values())


def _field_identifier(field):
    return int(field["controlIdentifier_docHandle"]), int(field["controlIdentifier_ID"])


def _field_text_info(document, owner, field):
    offsets = owner.textInfo._getOffsetsFromFieldIdentifier(*_field_identifier(field))
    return document.makeTextInfo(Offsets(*offsets))


@cache
def _buffer_class(base):
    class OfferTextInfo(base.TextInfo):
        def getControlFieldSpeech(
            self, attrs, ancestorAttrs, fieldType, formatConfig=None, extraDetail=False, reason=None,
        ):
            native = super().getControlFieldSpeech(attrs, ancestorAttrs, fieldType, formatConfig, extraDetail, reason)
            if (reason not in {controlTypes.OutputReason.CARET, controlTypes.OutputReason.SAYALL}
                    or attrs.get("role") != controlTypes.Role.LINK):
                return native
            url = attrs.get("IAccessible::value")
            if not (_heading_destination(url, "OLX") or _heading_destination(url, "Vinted")):
                return native
            data = self.obj._shopping_headings()
            identifier = attrs.get("controlIdentifier_docHandle"), attrs.get("controlIdentifier_ID")
            if data is None or identifier not in data[3]:
                return native
            # Read the heading along with the original link so Enter and link navigation keep working.
            field = textInfos.ControlField(
                role=controlTypes.Role.HEADING,
                level=LEVEL,
                _startOfNode=attrs.get("_startOfNode", False),
                _endOfNode=attrs.get("_endOfNode", False),
            )
            heading = super().getControlFieldSpeech(field, ancestorAttrs, fieldType, formatConfig, extraDetail, reason)
            return heading + native

    class OfferBuffer(base):
        TextInfo = OfferTextInfo

        def _activatePosition(self, obj=None, info=None):
            navigation = getattr(self.rootNVDAObject, "_shopping_navigation", None)
            if navigation is not None and self.isReady:
                try:
                    position = info if info is not None else self.makeTextInfo(obj) if obj else self.selection
                    navigation.remember_activation(self, position)
                except (CallCancelled, COMError, LookupError, OSError):
                    pass
            return super()._activatePosition(obj=obj, info=info)

        def event_treeInterceptor_gainFocus(self):
            super().event_treeInterceptor_gainFocus()
            navigation = getattr(self.rootNVDAObject, "_shopping_navigation", None)
            if navigation is not None:
                navigation.schedule(self)

        def _handleUpdate(self):
            self._shopping_heading_cache = None
            super()._handleUpdate()
            navigation = getattr(self.rootNVDAObject, "_shopping_navigation", None)
            if navigation is not None:
                navigation.schedule(self)

        def _shopping_headings(self, *, sections_only=False):
            if not self.isReady:
                return None
            try:
                url = self.documentURL
                if site_url(url, OLX_HOSTS) or _is_olx_storefront(url):
                    site = "OLX"
                elif site_url(url, VINTED_HOSTS):
                    site = "Vinted"
                else:
                    return None
                vinted_offer = site == "Vinted" and is_offer_link(url, site)
                if sections_only and not vinted_offer:
                    return None
                # Newly loaded cards can be visible in the buffer before its update callback.
                cache_key = url, self.makeTextInfo(textInfos.POSITION_ALL)._endOffset
                cached = getattr(self, "_shopping_heading_cache", None)
                if cached and cached[0] == cache_key:
                    return cached
                try:
                    sections = _vinted_sections(self) if vinted_offer else ()
                except (CallCancelled, COMError, LookupError, OSError):
                    sections = ()
                occupied = {section.vbufFieldIdentifier for section in sections}
                entries = _entry_headings(self, site, url, occupied)
                entry_ids = {tuple(map(str, entry.vbufFieldIdentifier)) for entry in entries}
                self._shopping_heading_cache = (cache_key, sections, entries, entry_ids)
                return self._shopping_heading_cache
            except (CallCancelled, COMError, LookupError, OSError):
                return None

        def _iterNodesByType(self, itemType, direction="next", pos=None):
            native = super()._iterNodesByType(itemType, direction, pos)
            if (itemType not in {"heading", f"heading{DESCRIPTION_LEVEL}", f"heading{LEVEL}"}
                    or direction not in {"next", "previous"}):
                return native
            cached = self._shopping_headings(sections_only=itemType == f"heading{DESCRIPTION_LEVEL}")
            if cached is None:
                return native
            _, sections, entries, _ = cached
            if itemType == f"heading{DESCRIPTION_LEVEL}":
                groups = (sections,)
            elif itemType == f"heading{LEVEL}":
                groups = (entries,)
            else:
                groups = (sections, entries)
            step = 1 if direction == "next" else -1
            items = [
                (heading for heading in group[::step]
                 if pos is None or heading.textInfo.compareEndPoints(pos, "startToStart") * step > 0)
                for group in groups
            ]
            return browseMode.mergeQuickNavItemIterators((native, *items), direction)

    return OfferBuffer


class ShopDocument(NVDAObject):
    """Use NVDA's own heading commands and Elements List on shopping pages."""

    def _get_treeInterceptorClass(self):
        base = super().treeInterceptorClass
        if not isinstance(base, type) or not issubclass(base, VirtualBuffer):
            return base
        return _buffer_class(base)
