"""Bezpieczne aktualizacje dodatku z serwera proszczok.pl."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
from pathlib import PurePosixPath
import re
import shutil
import tempfile
import threading
from typing import NamedTuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import build_opener, HTTPRedirectHandler, Request
import zipfile

import addonHandler
import config
import gui
from gui import guiHelper
import ui
import wx
from logHandler import log


WEBSITE_URL = "https://proszczok.pl/zakupocholik/"
UPDATE_METADATA_URL = WEBSITE_URL + "update.json"
EXPECTED_ADDON_NAME = "zakupocholik"
ALLOWED_UPDATE_HOSTS = frozenset(("proszczok.pl",))
NETWORK_TIMEOUT_SECONDS = 8
MAX_METADATA_BYTES = 64 * 1024
MAX_ADDON_BYTES = 25 * 1024 * 1024
MAX_ARCHIVE_FILES = 500
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
_VERSION_PATTERN = re.compile(r"^[0-9]{1,6}(?:\.[0-9]{1,6}){1,3}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
_MANIFEST_VALUE_PATTERN = r'^\s*{key}\s*=\s*"([^"\r\n]+)"\s*$'
SECTION = "zakupocholik"
DEFAULT_MINUTES = 60
MAX_MINUTES = 10080
TITLE = "Aktualizacje — Zakupocholik"

config.conf.spec[SECTION] = {
    "automaticUpdates": "boolean(default=True)",
    "updateIntervalMinutes": f"integer(default={DEFAULT_MINUTES}, min=1, max={MAX_MINUTES})",
}


class UpdateError(RuntimeError):
    """Błąd bezpiecznego sprawdzania albo pobierania aktualizacji."""


class UpdateInfo(NamedTuple):
    version: str
    url: str
    sha256: str
    size: int
    notes: str


def _version_key(version: str) -> tuple[int, int, int, int]:
    if not isinstance(version, str) or not _VERSION_PATTERN.fullmatch(version):
        raise UpdateError("Nieprawidłowy numer wersji aktualizacji.")
    parts = [int(part) for part in version.split(".")]
    if any(part > 999999 for part in parts):
        raise UpdateError("Numer wersji aktualizacji jest zbyt duży.")
    return tuple((parts + [0, 0, 0, 0])[:4])


def _validate_https_url(url: str) -> str:
    if not isinstance(url, str) or len(url) > 2048:
        raise UpdateError("Nieprawidłowy adres aktualizacji.")
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError as error:
        raise UpdateError("Nieprawidłowy port serwera aktualizacji.") from error
    if (
        parsed.scheme.casefold() != "https"
        or parsed.hostname is None
        or parsed.hostname.casefold() not in ALLOWED_UPDATE_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or not parsed.path.startswith("/zakupocholik/")
        or parsed.query
        or parsed.fragment
    ):
        raise UpdateError("Aktualizacja musi pochodzić z bezpiecznego adresu proszczok.pl.")
    return url


class _SecureRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_https_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _opener():
    return build_opener(_SecureRedirectHandler())


def _request(url: str, accept: str):
    _validate_https_url(url)
    return Request(
        url,
        headers={
            "Accept": accept,
            "Cache-Control": "no-cache, no-store",
            "Pragma": "no-cache",
            "User-Agent": "Zakupocholik-Updater/2",
        },
        method="GET",
    )


def _content_length(headers, maximum: int) -> int | None:
    value = headers.get("Content-Length")
    if value is None:
        return None
    try:
        length = int(value)
    except (TypeError, ValueError) as error:
        raise UpdateError("Serwer podał nieprawidłowy rozmiar pliku.") from error
    if length < 0 or length > maximum:
        raise UpdateError("Plik aktualizacji przekracza dozwolony rozmiar.")
    return length


def _json_without_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise UpdateError(f"Powtórzone pole metadanych: {key}.")
        result[key] = value
    return result


def _read_metadata() -> UpdateInfo:
    try:
        with _opener().open(
            _request(UPDATE_METADATA_URL, "application/json"),
            timeout=NETWORK_TIMEOUT_SECONDS,
        ) as response:
            _content_length(response.headers, MAX_METADATA_BYTES)
            data = response.read(MAX_METADATA_BYTES + 1)
    except (HTTPError, URLError, OSError, TimeoutError) as error:
        raise UpdateError("Nie udało się połączyć z serwerem aktualizacji.") from error
    if len(data) > MAX_METADATA_BYTES:
        raise UpdateError("Plik informacji o aktualizacji jest zbyt duży.")
    try:
        metadata = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_json_without_duplicates,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise UpdateError("Serwer zwrócił nieprawidłowe informacje o aktualizacji.") from error
    if not isinstance(metadata, dict) or metadata.get("schema") != 1:
        raise UpdateError("Nieobsługiwana wersja informacji o aktualizacji.")
    if metadata.get("addon") != EXPECTED_ADDON_NAME:
        raise UpdateError("Informacje dotyczą innego dodatku.")
    version = metadata.get("version")
    _version_key(version)
    url = _validate_https_url(metadata.get("url"))
    if url != f"{WEBSITE_URL}Zakupocholik-{version}.nvda-addon":
        raise UpdateError("Adres paczki nie odpowiada wersji Zakupocholika.")
    sha256 = metadata.get("sha256")
    if not isinstance(sha256, str) or not _SHA256_PATTERN.fullmatch(sha256):
        raise UpdateError("Nieprawidłowa suma kontrolna aktualizacji.")
    size = metadata.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0 or size > MAX_ADDON_BYTES:
        raise UpdateError("Nieprawidłowy rozmiar aktualizacji.")
    notes = metadata.get("notes", "")
    if not isinstance(notes, str) or len(notes) > 2000:
        raise UpdateError("Nieprawidłowy opis aktualizacji.")
    return UpdateInfo(version, url, sha256.casefold(), size, notes.strip())


def _manifest_value(manifest: str, key: str) -> str:
    match = re.search(
        _MANIFEST_VALUE_PATTERN.format(key=re.escape(key)),
        manifest,
        flags=re.MULTILINE,
    )
    if match is None:
        raise UpdateError(f"Manifest aktualizacji nie zawiera pola {key}.")
    return match.group(1)


def _verify_addon_package(path: str, expected: UpdateInfo) -> None:
    try:
        with zipfile.ZipFile(path, "r") as archive:
            entries = archive.infolist()
            if not entries or len(entries) > MAX_ARCHIVE_FILES:
                raise UpdateError("Paczka aktualizacji ma nieprawidłową liczbę plików.")
            total_size = 0
            manifest_entry = None
            seen_paths = set()
            for entry in entries:
                name = entry.filename.replace("\\", "/")
                parts = PurePosixPath(name).parts
                normalized = name.rstrip("/").casefold()
                unix_file_type = (entry.external_attr >> 16) & 0xF000
                if (
                    not normalized
                    or normalized in seen_paths
                    or name.startswith("/")
                    or ".." in parts
                    or any(":" in part or part.endswith((" ", ".")) for part in parts)
                    or os.path.isreserved(name)
                    or entry.flag_bits & 0x1
                    or unix_file_type == 0xA000
                ):
                    raise UpdateError("Paczka aktualizacji zawiera niedozwoloną albo powtórzoną ścieżkę.")
                seen_paths.add(normalized)
                total_size += entry.file_size
                if total_size > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                    raise UpdateError("Rozpakowana aktualizacja jest zbyt duża.")
                if name == "manifest.ini":
                    if manifest_entry is not None:
                        raise UpdateError("Paczka aktualizacji zawiera więcej niż jeden manifest.")
                    manifest_entry = entry
            if manifest_entry is None or manifest_entry.file_size > MAX_METADATA_BYTES:
                raise UpdateError("Paczka aktualizacji nie zawiera prawidłowego manifestu.")
            if archive.testzip() is not None:
                raise UpdateError("Paczka aktualizacji jest uszkodzona.")
            manifest = archive.read(manifest_entry).decode("utf-8-sig")
    except UpdateError:
        raise
    except (OSError, zipfile.BadZipFile, UnicodeDecodeError, RuntimeError) as error:
        raise UpdateError("Pobrany plik nie jest prawidłową paczką dodatku NVDA.") from error
    if _manifest_value(manifest, "name") != EXPECTED_ADDON_NAME:
        raise UpdateError("Pobrana paczka ma inną nazwę dodatku.")
    package_version = _manifest_value(manifest, "version")
    if package_version != expected.version:
        raise UpdateError("Wersja w pobranej paczce nie zgadza się z informacją serwera.")


def _download_update(info: UpdateInfo) -> tuple[str, str]:
    temporary_dir = tempfile.mkdtemp(prefix="Zakupocholik-update-")
    path = os.path.join(temporary_dir, f"Zakupocholik-{info.version}.nvda-addon")
    digest = hashlib.sha256()
    written = 0
    try:
        with _opener().open(
            _request(info.url, "application/x-nvda-addon, application/octet-stream"),
            timeout=NETWORK_TIMEOUT_SECONDS,
        ) as response, open(path, "xb") as output:
            declared_size = _content_length(response.headers, MAX_ADDON_BYTES)
            if declared_size is not None and declared_size != info.size:
                raise UpdateError("Rozmiar aktualizacji nie zgadza się z informacją serwera.")
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_ADDON_BYTES or written > info.size:
                    raise UpdateError("Pobrana aktualizacja przekracza oczekiwany rozmiar.")
                output.write(chunk)
                digest.update(chunk)
        if written != info.size:
            raise UpdateError("Pobrana aktualizacja ma nieprawidłowy rozmiar.")
        if digest.hexdigest().casefold() != info.sha256:
            raise UpdateError("Suma SHA-256 pobranej aktualizacji jest nieprawidłowa.")
        _verify_addon_package(path, info)
        return temporary_dir, path
    except UpdateError:
        shutil.rmtree(temporary_dir, ignore_errors=True)
        raise
    except (HTTPError, URLError, OSError, TimeoutError) as error:
        shutil.rmtree(temporary_dir, ignore_errors=True)
        raise UpdateError("Nie udało się pobrać aktualizacji.") from error


class UpdateSettingsDialog(wx.Dialog):
    def __init__(self, parent, last_automatic_check):
        super().__init__(parent, title="Ustawienia aktualizacji Zakupocholika")
        settings = config.conf[SECTION]
        helper = guiHelper.BoxSizerHelper(self, orientation=wx.VERTICAL)
        self.automatic = helper.addItem(wx.CheckBox(self, label="Sprawdzaj aktualizacje automatycznie"))
        self.automatic.SetValue(settings["automaticUpdates"])
        self.minutes = helper.addLabeledControl(
            "Sprawdzaj co ile minut", wx.SpinCtrl,
            min=1, max=MAX_MINUTES, initial=settings["updateIntervalMinutes"],
        )
        self.minutes.Enable(self.automatic.GetValue())
        self.automatic.Bind(wx.EVT_CHECKBOX, self._toggle_interval)
        helper.addLabeledControl(
            "Ostatnie sprawdzenie automatyczne", wx.TextCtrl, value=last_automatic_check,
            size=self.FromDIP((390, 58)), style=wx.TE_READONLY | wx.TE_MULTILINE,
        )
        helper.addDialogDismissButtons(self.CreateButtonSizer(wx.OK | wx.CANCEL))
        self.SetSizerAndFit(helper.sizer)
        self.CentreOnParent()
        self.automatic.SetFocus()

    def _toggle_interval(self, event):
        self.minutes.Enable(self.automatic.GetValue())


class UpdateManager:
    def __init__(self):
        self._active = True
        self._busy = False
        self._pending_restart = False
        self._timer = None
        self._interval_minutes = None
        self._last_automatic_check = "Jeszcze nie wykonano w tej sesji NVDA."
        self._sync_schedule(first=True)
        config.post_configProfileSwitch.register(self._profile_switched)

    def close(self):
        self._active = False
        config.post_configProfileSwitch.unregister(self._profile_switched)
        self._schedule()

    def _profile_switched(self, prevConf):
        self._sync_schedule()

    def _sync_schedule(self, *, first=False):
        settings = config.conf[SECTION]
        interval = settings["updateIntervalMinutes"] if settings["automaticUpdates"] else None
        if interval != self._interval_minutes:
            self._interval_minutes = interval
            self._schedule(first=first)

    def _schedule(self, *, first=False):
        if self._timer is not None:
            self._timer.Stop()
            self._timer = None
        if self._active and self._interval_minutes is not None:
            delay = 10000 if first else self._interval_minutes * 60000
            self._timer = wx.CallLater(delay, self._automatic_check)

    def _automatic_check(self):
        self._timer = None
        if not self._active or self._interval_minutes is None:
            return
        self._schedule()
        self.check()

    def settings(self):
        gui.mainFrame.prePopup()
        try:
            with UpdateSettingsDialog(gui.mainFrame, self._last_automatic_check) as dialog:
                if dialog.ShowModal() != wx.ID_OK:
                    return
                settings = config.conf[SECTION]
                settings["automaticUpdates"] = dialog.automatic.GetValue()
                settings["updateIntervalMinutes"] = dialog.minutes.GetValue()
        finally:
            gui.mainFrame.postPopup()
        self._sync_schedule()

    def check(self, *, manual=False):
        if not self._active:
            return
        if self._busy:
            if manual:
                ui.message("Sprawdzanie lub instalowanie aktualizacji już trwa.")
            return
        if self._pending_restart:
            if manual:
                self._prompt_restart()
            return
        self._busy = True
        if manual:
            ui.message("Sprawdzam aktualizacje Zakupocholika.")
        threading.Thread(target=self._check_worker, args=(manual,),
                         name="Zakupocholik update check", daemon=True).start()

    def _check_worker(self, manual):
        try:
            current = addonHandler.getCodeAddon().manifest["version"]
            version = _version_key(current)
            info = _read_metadata()
            update = info if _version_key(info.version) > version else None
        except (RuntimeError, LookupError, OSError, TypeError, ValueError) as error:
            log.debugWarning("Zakupocholik: sprawdzanie aktualizacji", exc_info=True)
            wx.CallAfter(self._checked, manual, None, None, str(error))
        else:
            wx.CallAfter(self._checked, manual, current, update, None)

    def _checked(self, manual, current, info, error):
        downloading = False
        try:
            if not self._active:
                return
            if not manual:
                if error:
                    outcome = error
                elif info is not None:
                    outcome = f"Dostępna wersja {info.version}."
                else:
                    outcome = f"Brak nowszej wersji. Zainstalowana wersja {current}."
                timestamp = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
                self._last_automatic_check = f"{timestamp}. {outcome}"
                log.debug(f"Zakupocholik: automatyczne sprawdzenie aktualizacji. {outcome}")
            if error:
                if manual:
                    self._show_error(error)
                return
            if info is None:
                if manual:
                    gui.messageBox(f"Masz najnowszą wersję Zakupocholika ({current}).",
                                   TITLE, wx.OK | wx.ICON_INFORMATION)
                return
            details = f"Dostępna jest wersja {info.version}. Masz wersję {current}."
            if info.notes:
                details += f"\n\n{info.notes}"
            details += "\n\nCzy pobrać i zainstalować aktualizację?"
            result = gui.messageBox(details, TITLE, wx.YES_NO | wx.NO_DEFAULT | wx.ICON_INFORMATION)
            if result == wx.YES and self._active:
                ui.message("Pobieram aktualizację Zakupocholika.")
                threading.Thread(target=self._download_worker, args=(info,),
                                 name="Zakupocholik update download", daemon=True).start()
                downloading = True
        finally:
            if not downloading:
                self._busy = False

    def _download_worker(self, info):
        try:
            temporary_dir, path = _download_update(info)
        except UpdateError as error:
            wx.CallAfter(self._finish_download, None, None, str(error))
        else:
            wx.CallAfter(self._finish_download, temporary_dir, path, None)

    def _finish_download(self, temporary_dir, path, error):
        try:
            if not self._active:
                return
            if error:
                self._show_error(error)
                return
            from addonStore.install import installAddon
            installAddon(path)
            self._pending_restart = True
            self._prompt_restart()
        except Exception as failure:
            log.exception("Zakupocholik: instalowanie aktualizacji")
            self._show_error(getattr(failure, "displayMessage", None) or str(failure))
        finally:
            self._busy = False
            if temporary_dir is not None:
                shutil.rmtree(temporary_dir, ignore_errors=True)

    def _show_error(self, message):
        gui.messageBox(message, TITLE, wx.OK | wx.ICON_ERROR)

    def _prompt_restart(self):
        from gui.addonGui import promptUserForRestart
        promptUserForRestart()


