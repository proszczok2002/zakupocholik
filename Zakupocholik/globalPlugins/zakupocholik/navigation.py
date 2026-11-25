from html import escape

from comtypes import COMError
import controlTypes
from exceptions import CallCancelled
from textInfos import FieldCommand
import ui
from virtualBuffers import VBufStorage_findMatch_word

from .headings import vinted_description
from .listings import listing_site, offer_summary
from .sites import LOCAL_HOSTS, OLX_HOSTS, VINTED_HOSTS, is_offer_link, site_url


def _text_with_block_breaks(info):
    parts = []
    for part in info.getTextWithFields():
        if isinstance(part, str):
            parts.append(part)
        elif (isinstance(part, FieldCommand) and part.command in {"controlStart", "controlEnd"}
              and part.field.get("isBlock") in {"1", 1}):
            parts.append("\n")
    return "\n".join(line.strip() for line in "".join(parts).splitlines() if line.strip())


def _offer_description(document, site):
    if site == "Vinted":
        return vinted_description(document)
    if site == "Allegro Lokalnie":
        item = next(document._iterNodesByAttribs({
            "IAccessible2::attribute_id": ["offer-description-content"],
        }, nodeType="offerDescription"), None)
        # Chrome can concatenate paragraphs in the raw buffer text.
        return _text_with_block_breaks(item.textInfo) if item else None

    # The OLX description wrapper's data-testid is absent from IA2. Match
    # the text block immediately following the site's "Opis" heading.
    heading = next(
        (item for item in document._iterNodesByType("heading")
         if item.label.strip().casefold() == "opis"),
        None,
    )
    if heading is None:
        return None
    item = next(document._iterNodesByAttribs({
        "IAccessible2::attribute_tag": ["div"],
        "IAccessible2::attribute_class": [VBufStorage_findMatch_word("leading-body-base")],
    }, nodeType="offerDescription", pos=heading.textInfo), None)
    if item and item.textInfo.compareEndPoints(heading.textInfo, "startToEnd") == 0:
        return item.textInfo.text.strip()
    return None


def _offer_title(document, site):
    if site == "OLX":
        # The offer title can be an H4, alongside unrelated seller headings.
        title = next(document._iterNodesByAttribs({
            "IAccessible2::attribute_tag": ["h4"],
            "IAccessible2::attribute_class": [VBufStorage_findMatch_word("css-1hd136p")],
        }, nodeType="offerTitle"), None)
        if title and title.textInfo.text.strip():
            return title.textInfo.text.strip()
    heading = next(document._iterNodesByType("heading1"), None)
    return (heading.label if heading else None) or "Opis oferty"


def focus_search(page):
    site = next((name for name, hosts in (
        ("OLX", OLX_HOSTS), ("Vinted", VINTED_HOSTS), ("Allegro Lokalnie", LOCAL_HOSTS),
    ) if page and site_url(page.url, hosts)), None)
    if not site:
        ui.message("Otwórz OLX, Vinted lub Allegro Lokalnie.")
        return
    document = page.document
    if not document or not document.isReady:
        ui.message("Przenieś fokus do treści strony i spróbuj ponownie.")
        return
    try:
        for item in document._iterNodesByType("formField"):
            field = item.textInfo._getControlFieldAttribs(*item.vbufFieldIdentifier)
            if field.get("IAccessible2::attribute_tag") != "input" or field.get("role") not in {
                controlTypes.Role.EDITABLETEXT, controlTypes.Role.COMBOBOX,
            }:
                continue
            field_id = field.get("IAccessible2::attribute_id")
            label = (field.get("name") or "").casefold()
            if not (
                (site == "OLX" and field_id == "search")
                or (site == "Vinted" and field_id == "search_text")
                or (site == "Allegro Lokalnie" and label in {
                    "fraza wyszukiwania", "wpisz czego szukasz", "czego szukasz?",
                })
            ):
                continue
            obj = item.obj
            if obj is None or obj.states.intersection({controlTypes.State.INVISIBLE, controlTypes.State.UNAVAILABLE}):
                continue
            obj.setFocus()
            ui.message(f"Pole wyszukiwania — {site}.")
            return
    except (CallCancelled, COMError, LookupError, NotImplementedError, OSError):
        pass
    ui.message("Nie znaleziono pola wyszukiwania na tej stronie.")


def read_offer(page):
    site = next((name for name in ("OLX", "Vinted", "Allegro Lokalnie")
                 if page and is_offer_link(page.url, name)), None)
    if site:
        document = page.document
        if document and document.isReady:
            try:
                if description := _offer_description(document, site):
                    title = escape(_offer_title(document, site))
                    content = "<br>".join(escape(line) for line in description.splitlines())
                    ui.browseableMessage(
                        f"<h1>{title}</h1><p>{content}</p>",
                        f"Opis oferty {site}",
                        isHtml=True,
                        copyButton=True,
                        closeButton=True,
                    )
                    return
            except (CallCancelled, COMError, LookupError, OSError):
                pass
        ui.message("Nie znaleziono opisu. Poczekaj, aż strona oferty się załaduje.")
        return
    site = listing_site(page.url) if page else None
    if not site:
        ui.message("Otwórz listę ofert OLX, Vinted lub Allegro Lokalnie.")
        return
    document = page.document
    if document and document.isReady:
        try:
            if summary := offer_summary(document, site):
                ui.message(summary)
                return
        except (CallCancelled, COMError, LookupError, OSError):
            pass
    level = 3 if site == "Allegro Lokalnie" else 4
    ui.message(f"Przejdź klawiszem {level} na tytuł oferty i spróbuj ponownie.")
