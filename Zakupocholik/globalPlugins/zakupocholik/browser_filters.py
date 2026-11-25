from time import monotonic

from comInterfaces import IAccessible2Lib as IA2
import textInfos
from textInfos.offsets import Offsets

from .sites import FilterLoadError, parse_filter_form


SIDEBAR = "data-search-sidebar-filters"
SUBMIT = "filters-submit-button"
MAX_SOURCE_LENGTH = 8 * 1024 * 1024
MAX_SIDEBAR_LENGTH = 512 * 1024
MAX_SOURCE_LINES = 100_000


def _line_source(fields):
    lines = []
    in_source_line = False
    for part in fields:
        if isinstance(part, str):
            if in_source_line:
                lines.append(part)
        elif part.field.get("IAccessible2::attribute_class") == "line-content":
            in_source_line = part.command == "controlStart"
            if part.command == "controlEnd":
                lines.append("\n")
    return "".join(lines)


def _utf16_length(text):
    return len(text.encode("utf-16-le", errors="surrogatepass")) // 2


def chrome_filter_source(document, url):
    if not document.isReady:
        return None
    cells = document._iterNodesByAttribs({
        "IAccessible2::attribute_tag": ["td"],
        "IAccessible2::attribute_class": ["line-content"],
    }, nodeType="chromeSourceLine")
    if next(cells, None) is None:
        return None

    text_info = document.makeTextInfo(textInfos.POSITION_ALL)
    if text_info._endOffset > MAX_SOURCE_LENGTH:
        raise FilterLoadError("Karta źródła jest za duża, aby odczytać filtry.")
    source = text_info.text
    sidebar = source.find(SIDEBAR)
    start = source.rfind("<div", 0, sidebar) if sidebar >= 0 else -1
    submit = source.find(SUBMIT, sidebar) if start >= 0 else -1
    end = source.find(">", submit) if submit >= 0 else -1
    if end < 0 or end - start > MAX_SIDEBAR_LENGTH:
        raise FilterLoadError("W źródle bieżącej karty nie znaleziono formularza filtrów.")
    offsets = Offsets(_utf16_length(source[:start]), _utf16_length(source[:end + 1]))
    fields = document.makeTextInfo(offsets).getTextWithFields()
    html = "<form data-offer-listing-form>" + _line_source(fields) + "</form>"
    return parse_filter_form(html, url)


def _expand_source_line(accessible, text, depth=0):
    if "\ufffc" not in text:
        return text
    if depth >= 12:
        raise FilterLoadError("Firefox nie udostępnił całego tekstu formularza.")
    links = accessible.QueryInterface(IA2.IAccessibleHypertext)
    fragments = text.split("\ufffc")
    if links.nHyperlinks != len(fragments) - 1:
        raise FilterLoadError("Firefox nie udostępnił całego tekstu formularza.")
    parts = [fragments[0]]
    for index, suffix in enumerate(fragments[1:]):
        child = links.hyperlink(index).QueryInterface(IA2.IAccessibleText)
        value = child.text(0, -1)
        if value is None and child.nCharacters:
            raise FilterLoadError("Firefox nie udostępnił wartości pola formularza.")
        parts.extend((_expand_source_line(child, value or "", depth + 1), suffix))
    return "".join(parts)


def firefox_filter_source(document, url):
    """Read the Firefox source tab in slices so NVDA can process input between them."""
    # Gecko's browse-mode buffer can omit source lines. The document still
    # exposes each numbered line and the highlighted attribute values via IA2.
    root = document.rootNVDAObject.IAccessibleObject.QueryInterface(IA2.IAccessibleHypertext)
    row_count = root.nHyperlinks
    if not 0 < row_count <= MAX_SOURCE_LINES:
        raise FilterLoadError("Firefox nie udostępnił źródła tej karty lub źródło jest za duże.")
    slice_end = monotonic() + 0.04
    lines = []
    length = 0
    submit_started = False
    for index in range(row_count):
        if monotonic() >= slice_end:
            yield None
            slice_end = monotonic() + 0.04
        row = root.hyperlink(index).QueryInterface(IA2.IAccessibleText)
        line = row.text(0, -1)
        if not lines and SIDEBAR not in line:
            continue
        markup = _expand_source_line(row, line)
        number, space, markup = markup.partition(" ")
        if not space or number != str(index + 1):
            raise FilterLoadError("Nie udało się odczytać wiersza źródła Firefoksa.")
        lines.append(markup.rstrip("\r\n") + "\n")
        length += len(markup)
        if length > MAX_SIDEBAR_LENGTH:
            raise FilterLoadError("Formularz filtrów w źródle Firefoksa jest za duży.")
        _, separator, following = markup.partition(SUBMIT)
        # The submit button can span several lines.
        if (submit_started and ">" in markup) or (separator and ">" in following):
            yield parse_filter_form("<form data-offer-listing-form>" + "".join(lines), url)
            return
        submit_started = submit_started or bool(separator)
    raise FilterLoadError("W źródle bieżącej karty nie znaleziono formularza filtrów.")
