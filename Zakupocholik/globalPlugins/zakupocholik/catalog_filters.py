from collections import Counter
from dataclasses import dataclass
from html.parser import HTMLParser
import re
from urllib.error import HTTPError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .sites import Choice, FilterGroup, NumberFilter, OLX_HOSTS, _get


OLX_FLAGS = (
    ("courier", "Tylko z przesyłką"),
    ("search[photos]", "Tylko ze zdjęciem"),
)
OLX_FLAGS_GROUP = "opcje_ogloszen"
OLX_CONDITION_KEY = "search[filter_enum_state]"
OLX_CONDITION_PARAM = re.compile(r"search\[filter_enum_state\]\[[0-9]*\]")
CONDITION_LABEL = "Stan przedmiotu"
VINTED_STATUS_KEY = "status_ids[]"
VINTED_STATUS_LABELS = {
    "6": "Nowy z metką",
    "1": "Nowy bez metki",
    "2": "Bardzo dobry",
    "3": "Dobry",
    "4": "Zadowalający",
    "7": "Nie w pełni sprawny",
}
VINTED_SORT = (
    ("relevance", "Trafność"),
    ("price_high_to_low", "Cena od najwyższej"),
    ("price_low_to_high", "Cena od najniższej"),
    ("newest_first", "Od najnowszych"),
)


@dataclass(frozen=True)
class CatalogFilterForm:
    url: str
    site: str
    groups: tuple[FilterGroup, ...]
    numbers: tuple[NumberFilter, ...]
    sort: tuple[Choice, ...] = ()
    flag_group: FilterGroup | None = None

    def results_url(self, selections, numbers, sort, _city, _distance, *, cleared=False):
        parts = urlsplit(self.url)
        original = parse_qsl(parts.query, keep_blank_values=True)
        managed = {field.name for field in self.numbers}
        offered = set()
        updated = [(field.name, numbers[field.name]) for field in self.numbers if field.name in numbers]
        if self.flag_group:
            selected = selections.get(self.flag_group.name, set())
            managed.update(choice.value for choice in self.flag_group.choices)
            updated.extend((choice.value, "1") for choice in self.flag_group.choices if choice.value in selected)
        for group in self.groups:
            selected = selections.get(group.name, set())
            if group.name == OLX_CONDITION_KEY:
                available = {choice.value for choice in group.choices}
                old = [value for key, value in original if OLX_CONDITION_PARAM.fullmatch(key)]
                if selected != available.intersection(old):
                    managed.update(key for key, _ in original if OLX_CONDITION_PARAM.fullmatch(key))
                    states = [choice.value for choice in group.choices if choice.value in selected]
                    states.extend(value for value in old if value not in available)
                    updated.extend((f"{group.name}[{index}]", value) for index, value in enumerate(states))
                continue
            offered.update((group.name, choice.value) for choice in group.choices)
            updated.extend((group.name, choice.value) for choice in group.choices if choice.value in selected)
        if self.sort:
            managed.add("order")
            if any(choice.value == sort for choice in self.sort) and (
                sort != "relevance" or (not cleared and ("order", sort) in original)
            ):
                updated.append(("order", sort))

        query = [(key, value) for key, value in original if key not in managed and (key, value) not in offered]
        query.extend(updated)
        if Counter(query) == Counter(original):
            return self.url
        path = parts.path
        if self.site == "Vinted" and (category := re.fullmatch(r"/catalog/([0-9]+)(?:-[^/]+)?/?", path)):
            # Opening filter results uses /catalog with the category in the query.
            if not any(key == "catalog[]" for key, _ in query):
                query.append(("catalog[]", category[1]))
            path = "/catalog"
        if self.site == "Vinted" and numbers and not any(key == "currency" for key, _ in query):
            query.append(("currency", "PLN"))
        pagination = {"page", "time"} if self.site == "Vinted" else {"page"}
        query = [(key, value) for key, value in query if key not in pagination]
        return urlunsplit((parts.scheme, parts.netloc, path, urlencode(query), ""))


class _OLXConditionParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.depth = 0
        self.panel = 0
        self.group = 0
        self.select = False
        self.option = None
        self.options = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "div":
            self.depth += 1
            if attrs.get("id") == "listing-filters":
                self.panel = self.depth
            if (self.panel and attrs.get("role") == "group"
                    and (attrs.get("aria-label") or "").strip().casefold() in {"stan", "stan przedmiotu"}):
                self.group = self.depth
        if self.group and tag == "select":
            self.select = "multiple" in attrs and "disabled" not in attrs
        if self.select and tag == "option":
            value = attrs.get("value") or ""
            if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", value) and "disabled" not in attrs:
                self.option = (value, [])

    def handle_data(self, data):
        if self.option:
            self.option[1].append(data)

    def handle_endtag(self, tag):
        if tag == "option" and self.option:
            value, text = self.option
            label = " ".join("".join(text).split())
            if label:
                self.options.setdefault(value, label)
            self.option = None
        if tag == "select":
            self.select = False
        if tag == "div":
            if self.depth == self.group:
                self.group = 0
            if self.depth == self.panel:
                self.panel = 0
            self.depth -= 1


def load_olx_conditions(url):
    try:
        html, final_url = _get(url, "text/html", 8 * 1024 * 1024, hosts=OLX_HOSTS, site="OLX", timeout=4)
    except HTTPError as error:
        error.close()
        raise
    requested = urlsplit(url)
    final = urlsplit(final_url)
    if (requested.path.removeprefix("/d/").lstrip("/").rstrip("/")
            != final.path.removeprefix("/d/").lstrip("/").rstrip("/")
            or Counter(parse_qsl(requested.query)) != Counter(parse_qsl(final.query))):
        return ()
    parser = _OLXConditionParser()
    parser.feed(html)
    return tuple(parser.options.items())


def build_catalog_form(url, site, labels, *, conditions=()):
    values = parse_qsl(urlsplit(url).query, keep_blank_values=True)
    current = dict(values)
    labels = {label.partition(",")[0].strip().casefold() for label in labels}
    groups = ()
    sort = ()
    flag_group = None
    if site == "OLX":
        if conditions:
            selected = {value for key, value in values if OLX_CONDITION_PARAM.fullmatch(key)}
            groups = (FilterGroup(OLX_CONDITION_KEY, CONDITION_LABEL, tuple(
                Choice(value, label, value in selected) for value, label in conditions
            )),)
        flags = tuple(Choice(key, name, current.get(key) == "1") for key, name in OLX_FLAGS
                      if name.casefold() in labels)
        if flags:
            flag_group = FilterGroup(OLX_FLAGS_GROUP, "Opcje ogłoszeń", flags)
        bounds = (
            ("search[filter_float_price:from]", "Cena od", "from"),
            ("search[filter_float_price:to]", "Cena do", "to"),
        )
        bounds = tuple((key, name, end) for key, name, end in bounds if name.casefold() in labels)
    else:
        if "stan" in labels or conditions:
            selected = {value for key, value in values if key == VINTED_STATUS_KEY}
            available = conditions or tuple((value, label) for value, label in VINTED_STATUS_LABELS.items()
                                            if value != "7" or value in selected)
            groups = (FilterGroup(VINTED_STATUS_KEY, CONDITION_LABEL, tuple(
                Choice(value, label, value in selected) for value, label in available
            )),)
        order = current.get("order", "relevance")
        if "sortuj według" in labels and any(value == order for value, _ in VINTED_SORT):
            sort = tuple(Choice(value, label, value == order) for value, label in VINTED_SORT)
        bounds = (
            ("price_from", "Cena od", "from"),
            ("price_to", "Cena do", "to"),
        ) if "cena" in labels else ()
    numbers = tuple(NumberFilter(key, name, current.get(key, ""), "0", "", "0.01", "price", end)
                    for key, name, end in bounds)
    return CatalogFilterForm(url, site, groups, numbers, sort, flag_group)
