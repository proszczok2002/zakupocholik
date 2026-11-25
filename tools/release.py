"""Build Zakupocholik and publish the current release to its Samba web directory."""

import argparse
import ast
from hashlib import sha256
from html import escape
from io import BytesIO
import json
from pathlib import Path
import re
from urllib.request import Request, urlopen
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT / "niepotrzebne" / "wydania"
SERVER = Path(r"K:\proszczok.pl\public_html\zakupocholik")
PUBLIC_URL = "https://proszczok.pl/zakupocholik/"
VERSION = re.compile(r"[0-9]{1,6}(?:\.[0-9]{1,6}){1,3}")
INSTALLER = re.compile(r"Zakupocholik-[0-9]+(?:\.[0-9]+){1,3}\.nvda-addon", re.I)


def build():
    source = ROOT / "Zakupocholik"
    site = ROOT / "site"
    manifest = (source / "manifest.ini").read_text(encoding="utf-8")
    version_match = re.search(r'^version = "([^"]+)"$', manifest, flags=re.MULTILINE)
    notes_match = re.search(r'^changelog = """(.*?)"""', manifest, flags=re.MULTILINE | re.DOTALL)
    if not version_match or not VERSION.fullmatch(version_match[1]) or not notes_match:
        raise ValueError("Manifest musi zawierać prawidłowy numer wersji i changelog.")
    version = version_match[1]
    name = f"Zakupocholik-{version}.nvda-addon"
    bundle = BytesIO()
    with ZipFile(bundle, "w", ZIP_DEFLATED) as archive:
        for path in sorted(source.rglob("*")):
            if not path.is_file() or path.suffix not in {".py", ".ini", ".html", ".txt"}:
                continue
            data = path.read_bytes()
            if path.suffix == ".py":
                ast.parse(data, filename=str(path))
            entry = ZipInfo(path.relative_to(source).as_posix())
            archive.writestr(entry, data, compress_type=ZIP_DEFLATED)
    with ZipFile(bundle) as archive:
        if archive.testzip() is not None:
            raise ValueError("Paczka dodatku nie przeszła kontroli ZIP.")
    payload = bundle.getvalue()
    metadata = {
        "schema": 1,
        "addon": "zakupocholik",
        "version": version,
        "url": PUBLIC_URL + name,
        "size": len(payload),
        "sha256": sha256(payload).hexdigest(),
        "notes": notes_match[1].strip(),
        "notes_en": (site / "release-notes.en.txt").read_text(encoding="utf-8").strip(),
    }
    if not metadata["notes"] or len(metadata["notes"]) > 2000 or not metadata["notes_en"]:
        raise ValueError("Potrzebny jest opis zmian po polsku i angielsku. Polski opis ma limit 2000 znaków.")
    html = (site / "index.template.html").read_text(encoding="utf-8")
    for marker, value in {
        "VERSION": version,
        "NOTES": metadata["notes"],
        "PACKAGE": name,
        "SHA256": metadata["sha256"],
        "SIZE": f"{len(payload) / 1024:.1f} KB".replace(".", ","),
    }.items():
        html = html.replace(f"%%{marker}%%", escape(value))
    stylesheet = (site / "style.css").read_bytes()
    javascript = (site / "site.js").read_text(encoding="utf-8")
    javascript = javascript.replace("%%NOTES_EN_JS%%", json.dumps(metadata["notes_en"], ensure_ascii=False)[1:-1]).encode("utf-8")
    html = html.replace("%%CSS_VERSION%%", sha256(stylesheet).hexdigest()[:12])
    html = html.replace("%%JS_VERSION%%", sha256(javascript).hexdigest()[:12])
    files = {
        name: payload,
        "style.css": stylesheet,
        "site.js": javascript,
        "index.html": html.encode("utf-8"),
        "update.json": (json.dumps(metadata, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    }
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    for filename, data in files.items():
        (dist / filename).write_bytes(data)
    _archive_other_installers(dist, name)
    return metadata, files


def _publish_file(directory, filename, data):
    target = directory / filename
    pending = target.with_suffix(target.suffix + ".partial")
    try:
        pending.write_bytes(data)
        if pending.read_bytes() != data:
            raise OSError(f"Weryfikacja kopii Samba nie powiodła się dla {filename}.")
        pending.replace(target)
    finally:
        pending.unlink(missing_ok=True)


def _archive_other_installers(folder, current_name):
    for old in folder.iterdir():
        if old.name == current_name or not old.is_file() or not INSTALLER.fullmatch(old.name):
            continue
        ARCHIVE.mkdir(parents=True, exist_ok=True)
        data = old.read_bytes()
        backup = ARCHIVE / old.name
        if backup.exists() and backup.read_bytes() != data:
            backup = ARCHIVE / f"{old.stem}-{sha256(data).hexdigest()[:12]}{old.suffix}"
        if not backup.exists():
            _publish_file(ARCHIVE, backup.name, data)
        if backup.read_bytes() != data:
            raise OSError(f"Nie udało się zachować starszej paczki {old.name}.")
        old.unlink()


def _public(filename):
    request = Request(PUBLIC_URL + filename, headers={
        "Cache-Control": "no-cache, no-store",
        "Pragma": "no-cache",
        "User-Agent": "Zakupocholik-Updater/2",
    })
    with urlopen(request, timeout=20) as response:
        if response.status != 200:
            raise OSError(f"Publiczny adres {response.url} zwrócił kod {response.status}.")
        return response.read()


def publish(metadata, files):
    folder = SERVER.resolve(strict=True)
    name = metadata["url"].rsplit("/", 1)[-1]
    existing = folder / name
    if existing.is_file() and existing.read_bytes() != files[name]:
        raise ValueError("Ta wersja paczki jest już na serwerze. Przed zmianą zwiększ numer wersji.")
    live_manifest = folder / "update.json"
    if live_manifest.is_file():
        current = json.loads(live_manifest.read_text(encoding="utf-8"))["version"]
        if tuple(map(int, current.split("."))) > tuple(map(int, metadata["version"].split("."))):
            raise ValueError("Na serwerze jest nowsza wersja Zakupocholika.")
    for filename, data in files.items():
        _publish_file(folder, filename, data)
    for filename, data in files.items():
        public_name = filename
        if filename in {"style.css", "site.js"}:
            public_name += "?v=" + sha256(data).hexdigest()[:12]
        live = _public(public_name)
        if live != data:
            raise OSError(f"Publiczna kopia pliku {filename} nie zgadza się z wydaniem.")
    if _public("") != files["index.html"]:
        raise OSError("Publiczny adres strony nie zwraca opublikowanego index.html.")
    _archive_other_installers(folder, name)
    print("Publiczne pliki i ich zawartość zweryfikowane.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publish", action="store_true", help="Publikuj w katalogu Samba Zakupocholika.")
    arguments = parser.parse_args()
    metadata, files = build()
    print(json.dumps(metadata, ensure_ascii=False, indent=2))
    if arguments.publish:
        publish(metadata, files)


if __name__ == "__main__":
    main()
