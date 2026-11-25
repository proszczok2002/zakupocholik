import json
import threading
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

import addonHandler
import config
import gui
from gui import guiHelper
import ui
import wx

from .updater import SECTION


REPORT_URL = "https://proszczok.pl/YT%20Downloader%20by%20Proszczok/report.php"
MAX_NICK_LENGTH = 80
MAX_DESCRIPTION_LENGTH = 3500
MAX_RESPONSE_SIZE = 16 * 1024
TITLE = "Zgłoszenie — Zakupocholik"

config.conf.spec[SECTION]["reportNick"] = "string(default='')"


class ReportError(RuntimeError):
    pass


class _ReportRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ReportError("Serwer przekierował zgłoszenie. Spróbuj ponownie później.")


def send_report(nick, description, version):
    payload = {"nick": nick, "description": description, "version": f"Zakupocholik {version}"}
    request = Request(REPORT_URL, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers={
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "application/json",
        "User-Agent": f"Zakupocholik/{version}",
    }, method="POST")
    try:
        with build_opener(_ReportRedirects()).open(request, timeout=15) as response:
            raw = response.read(MAX_RESPONSE_SIZE + 1)
    except HTTPError as error:
        error.close()
        if error.code == 429:
            raise ReportError("Serwer ograniczył liczbę zgłoszeń. Odczekaj chwilę i spróbuj ponownie.") from error
        if error.code == 400:
            raise ReportError("Serwer odrzucił zgłoszenie. Sprawdź nick i opis.") from error
        raise ReportError("Serwer zgłoszeń jest chwilowo niedostępny.") from error
    except OSError as error:
        raise ReportError("Nie udało się połączyć z serwerem zgłoszeń.") from error
    if len(raw) > MAX_RESPONSE_SIZE:
        raise ReportError("Odpowiedź serwera jest za duża.")
    try:
        reply = json.loads(raw)
    except (ValueError, UnicodeError) as error:
        raise ReportError("Serwer nie potwierdził wysłania zgłoszenia.") from error
    if not isinstance(reply, dict) or reply.get("ok") is not True:
        raise ReportError("Serwer nie potwierdził wysłania zgłoszenia.")


class _ReportDialog(wx.Dialog):
    def __init__(self, caption, message, value, *, multiline):
        super().__init__(gui.mainFrame, title=caption)
        helper = guiHelper.BoxSizerHelper(self, orientation=wx.VERTICAL)
        instruction = wx.StaticText(self, label=message)
        instruction.Wrap(self.FromDIP(450))
        helper.addItem(instruction)
        label = "Opis błędu lub propozycji" if multiline else "Nick"
        style = wx.TE_MULTILINE if multiline else 0
        self.field = helper.addLabeledControl(
            label, wx.TextCtrl, value=value, style=style,
            size=self.FromDIP((450, 145 if multiline else -1)),
        )
        buttons = wx.StdDialogButtonSizer()
        confirm = wx.Button(self, wx.ID_OK, label="Wyślij" if multiline else "Dalej")
        buttons.AddButton(confirm)
        buttons.AddButton(wx.Button(self, wx.ID_CANCEL, label="Anuluj"))
        buttons.Realize()
        helper.addDialogDismissButtons(buttons)
        self.SetSizerAndFit(helper.sizer)
        self.CentreOnParent()
        confirm.SetDefault()
        self.field.SetInsertionPointEnd()
        self.field.SetFocus()


class Feedback:
    def __init__(self):
        self._active = True
        self._busy = False
        self._draft = ""

    def close(self):
        self._active = False

    def open(self):
        if not self._active:
            return
        if self._busy:
            ui.message("Zgłoszenie jest już otwarte lub trwa jego wysyłanie.")
            return
        self._busy = True
        sending = False
        try:
            report = self._collect()
            if report is None:
                return
            nick, description = report
            addon = addonHandler.getCodeAddon()
            version = addon.manifest["version"] if addon else ""
            if not version:
                gui.messageBox("Nie udało się odczytać wersji dodatku.", TITLE, wx.OK | wx.ICON_ERROR)
                return
            config.conf[SECTION]["reportNick"] = nick
            self._draft = description
            ui.message("Wysyłam zgłoszenie.")
            threading.Thread(target=self._send, args=(nick, description, version),
                             name="Zakupocholik report", daemon=True).start()
            sending = True
        finally:
            if not sending:
                self._busy = False

    def _collect(self):
        gui.mainFrame.prePopup()
        try:
            nick = self._prompt("Zgłoszenie — nick", "Podaj nick użytkownika do zgłoszenia.",
                                config.conf[SECTION]["reportNick"], MAX_NICK_LENGTH)
            if nick is None:
                return None
            description = self._prompt(
                "Zgłoszenie — błąd lub propozycja",
                "Opisz problem lub propozycję. Przycisk Wyślij przekazuje autorowi nick, opis i wersję dodatku.",
                self._draft, MAX_DESCRIPTION_LENGTH, multiline=True,
            )
            if description is not None:
                return nick, description
        finally:
            gui.mainFrame.postPopup()
        return None

    def _prompt(self, caption, message, value, limit, *, multiline=False):
        with _ReportDialog(caption, message, value, multiline=multiline) as dialog:
            while dialog.ShowModal() == wx.ID_OK:
                text = dialog.field.GetValue().strip()
                if 1 <= len(text) <= limit:
                    return text
                gui.messageBox(f"Wpisz od 1 do {limit} znaków.", caption, wx.OK | wx.ICON_WARNING)
                dialog.field.SetFocus()
        return None

    def _send(self, nick, description, version):
        try:
            send_report(nick, description, version)
        except ReportError as error:
            wx.CallAfter(self._sent, str(error))
        else:
            wx.CallAfter(self._sent, None)

    def _sent(self, error):
        self._busy = False
        if self._active:
            if error is None:
                self._draft = ""
                gui.messageBox("Zgłoszenie zostało wysłane.", TITLE, wx.OK | wx.ICON_INFORMATION)
            else:
                gui.messageBox(error + " Możesz ponownie otworzyć formularz i spróbować wysłać zapisany opis.",
                               TITLE, wx.OK | wx.ICON_ERROR)
