from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from html.parser import HTMLParser
import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


OLX_HOSTS = frozenset({"olx.pl", "www.olx.pl"})
VINTED_HOSTS = frozenset({"vinted.pl", "www.vinted.pl"})
LOCAL_HOSTS = frozenset({"allegrolokalnie.pl", "www.allegrolokalnie.pl"})
OLX_OFFER_PATHS = ("/d/oferta/", "/oferta/")
START_SORT = "startingTime-desc"
DISTANCE_SORT = "distanceFrom-asc"
ALL_SOURCES = frozenset({"lokalnie", "allegro"})


class FilterLoadError(ValueError):
    pass


def site_url(url, hosts):
    if not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url)
        if (parts.scheme == "https" and parts.hostname in hosts
                and parts.port in (None, 443) and parts.username is None):
            return parts
    except ValueError:
        return None
    return None


def _has_offer_path(parts, prefixes):
    return bool(parts and any(parts.path.startswith(path) and parts.path != path for path in prefixes))


def offer_site(url):
    parts = site_url(url, OLX_HOSTS)
    if parts and not parts.path.startswith(OLX_OFFER_PATHS):
        return "OLX"
    parts = site_url(url, VINTED_HOSTS)
    if parts and not parts.path.startswith("/items/"):
        return "Vinted"
    return None


def is_offer_link(url, site):
    if site == "OLX":
        return _has_offer_path(site_url(url, OLX_HOSTS), OLX_OFFER_PATHS)
    if site == "Vinted":
        parts = site_url(url, VINTED_HOSTS)
        return bool(parts and re.match(r"^/items/[0-9]+(?:[-/]|$)", parts.path))
    if site == "Allegro Lokalnie":
        return _has_offer_path(site_url(url, LOCAL_HOSTS), ("/oferta/",))
    return False


def allegro_listing(url):
    parts = site_url(url, LOCAL_HOSTS)
    return bool(parts and (parts.path == "/oferty" or parts.path.startswith("/oferty/")))


def results_site(url):
    if not isinstance(url, str) or any(ord(character) < 32 or ord(character) == 127 for character in url):
        return None
    if allegro_listing(url):
        return "Allegro Lokalnie"
    parts = site_url(url, VINTED_HOSTS)
    if parts and (parts.path == "/catalog" or parts.path.startswith("/catalog/")):
        return "Vinted"
    parts = site_url(url, OLX_HOSTS)
    if parts and parts.path.strip("/") not in {"", "d"} and not parts.path.startswith(OLX_OFFER_PATHS):
        return "OLX"
    return None


def listing_results_key(url):
    parts = urlsplit(url)
    host = parts.hostname.removeprefix("www.")
    path = parts.path.rstrip("/")
    params = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
              if key != "page" and (host != "vinted.pl" or key != "time")]
    if host == "olx.pl" and (path == "/d/oferty" or path.startswith("/d/oferty/")):
        path = path[2:]
    if host == "vinted.pl" and (category := re.fullmatch(r"/catalog/([0-9]+)(?:-[^/]+)?", path)):
        if not any(key == "catalog[]" for key, _ in params):
            params.append(("catalog[]", category[1]))
        path = "/catalog"
    return host, path, sorted(params)


@dataclass(frozen=True)
class Choice:
    value: str
    label: str
    selected: bool = False


@dataclass(frozen=True)
class FilterGroup:
    name: str
    label: str
    choices: tuple[Choice, ...]


@dataclass(frozen=True)
class NumberFilter:
    name: str
    label: str
    value: str
    minimum: str
    maximum: str
    step: str
    range_group: str
    range_end: str


@dataclass(frozen=True)
class City:
    slug: str
    label: str


@dataclass(frozen=True)
class ListingLink:
    label: str
    url: str


@dataclass(frozen=True)
class FilterForm:
    url: str
    clear_url: str
    groups: tuple[FilterGroup, ...]
    numbers: tuple[NumberFilter, ...]
    sort: tuple[Choice, ...]
    city: City | None
    distances: tuple[int, ...]
    distance: int | None
    categories: tuple[ListingLink, ...]
    active_filters: tuple[ListingLink, ...]

    def results_url(self, selections, numbers, sort, city, distance, *, cleared=False):
        managed = {field.name for field in self.numbers}
        managed.update({"page", "sort", "city", "odleglosc"})
        offered = {(group.name, choice.value) for group in self.groups for choice in group.choices}
        base = urlsplit(self.clear_url)
        source = base if cleared else urlsplit(self.url)
        query = [(key, value) for key, value in parse_qsl(source.query, keep_blank_values=True)
                 if key not in managed and (key, value) not in offered]
        for group in self.groups:
            values = selections.get(group.name, set())
            if group.name == "zrodlo" and values == ALL_SOURCES:
                continue
            query.extend((group.name, choice.value) for choice in group.choices if choice.value in values)
        query.extend((field.name, numbers[field.name]) for field in self.numbers if numbers.get(field.name))
        if sort and sort != START_SORT:
            query.append(("sort", sort))
        path = base.path
        if city:
            path = path.rstrip("/") + "/" + quote(city.slug, safe="")
            if distance:
                query.append(("odleglosc", str(distance)))
        return urlunsplit((base.scheme, base.netloc, quote(path, safe="/%"), urlencode(query), ""))


def validated_numbers(fields, values):
    numbers = {}
    ranges = defaultdict(dict)
    for field in fields:
        text = values.get(field.name, "").strip().replace(",", ".")
        if not text:
            continue
        if len(text) > 24 or not re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", text):
            raise ValueError(f"{field.label} — wpisz liczbę.")
        number = Decimal(text)
        if field.minimum and number < Decimal(field.minimum):
            raise ValueError(f"{field.label} — najmniejsza wartość to {field.minimum}.")
        if field.maximum and number > Decimal(field.maximum):
            raise ValueError(f"{field.label} — największa wartość to {field.maximum}.")
        if field.step and field.step != "any":
            step = Decimal(field.step)
            if step > 0 and (number - Decimal(field.minimum or "0")) % step:
                raise ValueError(f"{field.label} — dozwolony krok to {field.step}.")
        if field.range_group and field.range_end:
            ranges[field.range_group][field.range_end] = number
        numbers[field.name] = format(number, "f")
    for limits in ranges.values():
        if "from" in limits and "to" in limits and limits["from"] > limits["to"]:
            raise ValueError("Dolna granica zakresu musi być mniejsza od górnej lub jej równa.")
    return numbers


class _FormParser(HTMLParser):
    """Read the active public listing's server-rendered filter form."""

    def __init__(self):
        super().__init__()
        self.in_form = False
        self.in_filters = False
        self.finished = False
        self.clear_path = ""
        self.city_data = {}
        self.fields = []
        self.fieldsets = []
        self.labels = {}
        self.sort = []
        self.label = None
        self.legend = None
        self.option = None
        self.in_sort = False
        self.categories = []
        self.active_filters = []
        self.category_label = None
        self.category_count = None
        self.active_filter = None
        self.active_label = False

    def handle_starttag(self, tag, attributes):
        if self.finished:
            return
        attrs = dict(attributes)
        if tag == "form" and "data-offer-listing-form" in attrs:
            self.in_form = True
        if self.in_form and "data-search-sidebar-filters" in attrs:
            self.in_filters = True
        if not self.in_filters:
            return
        if attrs.get("data-testid") == "filters-submit-button":
            self.finished = True
            return
        classes = (attrs.get("class") or "").split()
        if tag == "a" and attrs.get("href"):
            parent = "clear-link" in classes and not self.clear_path
            if parent or "categories__category" in classes:
                label, count = [], []
                self.categories.append((attrs["href"], label, count, parent))
                self.category_label = label
        elif tag == "span" and "categories__category__counter" in classes and self.categories:
            self.category_count = self.categories[-1][2]
        elif tag == "button" and attrs.get("data-listing-filter") and "active-filter" in classes:
            target = _redirect_url(attrs["data-listing-filter"])
            if target:
                self.active_filter = []
                self.active_filters.append((target, self.active_filter))
        elif tag == "span" and "active-filter__value" in classes and self.active_filter is not None:
            self.active_label = True
        if "data-search-sidebar-clear-button" in attrs:
            self.clear_path = _redirect_url(attrs["data-search-sidebar-clear-button"])
        if attrs.get("data-city-filter"):
            self.city_data = json.loads(attrs["data-city-filter"])
        if tag == "fieldset":
            self.fieldsets.append([])
        elif tag == "legend" and self.fieldsets:
            self.legend = self.fieldsets[-1]
        elif tag == "label" and attrs.get("for"):
            self.label = self.labels.setdefault(attrs["for"], [])
        elif tag == "input" and "disabled" not in attrs and attrs.get("name"):
            if attrs.get("type") in {"checkbox", "number"}:
                self.fields.append((attrs, self.fieldsets[-1] if self.fieldsets else []))
        elif tag == "select" and attrs.get("name") == "sort" and "disabled" not in attrs:
            self.in_sort = True
        elif tag == "option" and self.in_sort and "disabled" not in attrs:
            self.option = (attrs, [])

    def handle_endtag(self, tag):
        if not self.in_form or self.finished:
            return
        if tag == "form":
            self.in_form = False
            self.in_filters = False
        elif tag == "a":
            self.category_label = None
        elif tag == "button":
            self.active_filter = None
            self.active_label = False
        elif tag == "span":
            self.category_count = None
            self.active_label = False
        elif tag == "fieldset" and self.fieldsets:
            self.fieldsets.pop()
        elif tag == "legend":
            self.legend = None
        elif tag == "label":
            self.label = None
        elif tag == "option" and self.option:
            attrs, text = self.option
            label = _text(text)
            self.sort.append(Choice(attrs.get("value", label), label, "selected" in attrs))
            self.option = None
        elif tag == "select":
            self.in_sort = False

    def handle_data(self, data):
        if not self.in_filters or self.finished:
            return
        if self.category_label is not None:
            self.category_label.append(data)
        if self.category_count is not None:
            self.category_count.append(data)
        if self.active_label:
            self.active_filter.append(data)
        if self.legend is not None:
            self.legend.append(data)
        if self.label is not None:
            self.label.append(data)
        if self.option:
            self.option[1].append(data)


def _text(parts):
    return " ".join("".join(parts).replace("Rozwiń/zwiń listę", "").split())


def _redirect_url(data):
    try:
        target = json.loads(data)["redirectUrl"]
    except (LookupError, TypeError, ValueError):
        return ""
    return target if isinstance(target, str) else ""


def _listing_links(url, entries):
    links = []
    for target, label in entries:
        if target and label:
            destination = urljoin(url, target)
            if allegro_listing(destination):
                links.append(ListingLink(label, destination))
    return tuple(links)


def _category_entries(categories):
    for target, text, count, parent in categories:
        label = _text(text)
        if not label:
            continue
        if parent:
            label = f"Wróć do kategorii {label}"
        elif count := _text(count):
            label = f"{label} — liczba ofert: {count}"
        yield target, label


def parse_filter_form(html, url):
    parser = _FormParser()
    parser.feed(html)
    clear_url = urljoin(url, parser.clear_path)
    if not (parser.finished and parser.clear_path and allegro_listing(clear_url)):
        raise FilterLoadError("Allegro Lokalnie nie udostępniło formularza filtrów dla tej listy.")
    groups = {}
    numeric = []
    for attrs, legend in parser.fields:
        name = attrs["name"]
        group_label = _text(legend) or name.replace("_", " ")
        label = _text(parser.labels.get(attrs.get("id"), []))
        if attrs["type"] == "checkbox":
            value = attrs.get("value", "on")
            if name == "zrodlo":
                group_label = "Źródło ofert"
            if name == "dostawa_smart":
                label = "Darmowa dostawa z Allegro Smart!"
            choices = groups.setdefault(name, (group_label, []))[1]
            choices.append(Choice(value, label or value, "checked" in attrs))
        else:
            label = " ".join(part for part in (group_label, label.rstrip(":")) if part)
            numeric.append(NumberFilter(
                name, label, attrs.get("value", ""), attrs.get("min", ""),
                attrs.get("max", ""), attrs.get("step", ""), attrs.get("data-attrib-id", ""),
                attrs.get("data-attrib-range", ""),
            ))
    city_data = parser.city_data.get("city")
    city = City(city_data["slug"], city_data["name"]) if city_data else None
    distance_data = parser.city_data.get("location_max_distance") or {}
    return FilterForm(
        url, clear_url, tuple(FilterGroup(name, label, tuple(values)) for name, (label, values) in groups.items()),
        tuple(numeric), tuple(parser.sort), city, tuple(distance_data.get("available") or ()), distance_data.get("selected"),
        tuple(link for link in _listing_links(url, _category_entries(parser.categories))
              if urlsplit(link.url).path.rstrip("/") != urlsplit(url).path.rstrip("/")),
        _listing_links(url, ((target, _text(text)) for target, text in parser.active_filters)),
    )


class _ListingRedirects(HTTPRedirectHandler):
    def __init__(self, hosts, site):
        self.hosts = hosts
        self.site = site

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not site_url(newurl, self.hosts):
            raise ValueError(f"{self.site} skierowało żądanie poza własną domenę.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _get(url, accept, limit, *, hosts=LOCAL_HOSTS, site="Allegro Lokalnie", timeout=10):
    if not site_url(url, hosts):
        raise ValueError(f"Nieprawidłowy adres {site}.")
    request = Request(url, headers={"Accept": accept, "Accept-Language": "pl-PL,pl;q=0.9",
                                    "User-Agent": "Zakupocholik/1.0"})
    with build_opener(_ListingRedirects(hosts, site)).open(request, timeout=timeout) as response:
        data = response.read(limit + 1)
        if len(data) > limit:
            raise ValueError(f"Odpowiedź {site} jest za duża.")
        return data.decode("utf-8"), response.url


def load_filters(url):
    for attempt in range(2):
        try:
            html, final_url = _get(url, "text/html", 8 * 1024 * 1024)
            return parse_filter_form(html, final_url)
        except HTTPError as error:
            error.close()
            if error.code in {403, 429}:
                raise FilterLoadError(
                    "Allegro Lokalnie chwilowo odrzuciło pobranie filtrów. Spróbuj ponownie za chwilę.",
                ) from error
            if error.code not in {408, 500, 502, 503, 504} or attempt or (
                error.headers and error.headers.get("Retry-After")
            ):
                raise FilterLoadError("Allegro Lokalnie nie odpowiedziało na pobranie filtrów. Spróbuj ponownie.") from error
        except (TimeoutError, ConnectionError, URLError) as error:
            if attempt:
                raise FilterLoadError("Nie udało się połączyć z Allegro Lokalnie. Spróbuj ponownie.") from error
        time.sleep(0.5)


def search_cities(name):
    url = "https://allegrolokalnie.pl/api/cities/?" + urlencode({"name": name})
    text, _ = _get(url, "application/json", 512 * 1024)
    cities = []
    for city in json.loads(text)["cities"]:
        label = ", ".join(dict.fromkeys(city[key] for key in ("name", "county", "state") if city.get(key)))
        if label and city.get("slug"):
            cities.append(City(city["slug"], label))
    return cities
