from dataclasses import dataclass

import api
from comtypes import COMError
import controlTypes
from exceptions import CallCancelled
import treeInterceptorHandler
from virtualBuffers import VirtualBuffer
import winUser


BROWSERS = frozenset({"chrome", "firefox"})


@dataclass(frozen=True)
class BrowserPage:
    browser: str
    window: int
    url: str
    document: VirtualBuffer | None

    def matches(self, other):
        return bool(other and self.window == other.window and self.url == other.url)


def has_olx_listing_filters(document):
    return bool(document and document.isReady and next(document._iterNodesByAttribs(
        {"IAccessible2::attribute_id": ["listing-filters"]}, nodeType="shopListing",
    ), None))


def _document_for_window(window):
    # Toolbar controls do not belong to the page's virtual buffer. Reuse
    # NVDA's ready documents so looking up a tab never walks the browser's
    # accessibility tree or uses text still being typed in the address bar.
    found = None
    for document in tuple(treeInterceptorHandler.runningTable):
        if not isinstance(document, VirtualBuffer) or not document.isReady:
            continue
        root = document.rootNVDAObject
        if winUser.getAncestor(root.windowHandle, winUser.GA_ROOT) != window:
            continue
        if not winUser.isWindowVisible(root.windowHandle) or not document.isAlive:
            continue
        if controlTypes.State.INVISIBLE in root.states:
            continue
        if found is not None:
            return None
        found = document
    return found


def browser_page(focus=None):
    focus = focus if focus is not None else api.getFocusObject()
    if focus is None:
        return None
    try:
        browser = focus.appModule.appName
        if browser not in BROWSERS:
            return None
        window = winUser.getAncestor(focus.windowHandle, winUser.GA_ROOT) or focus.windowHandle
        document = focus.treeInterceptor
        if isinstance(document, VirtualBuffer) and document.documentURL:
            return BrowserPage(browser, window, document.documentURL, document)
        document = _document_for_window(window)
        if document and document.documentURL:
            return BrowserPage(browser, window, document.documentURL, document)
    except (AttributeError, CallCancelled, COMError, LookupError, OSError):
        return None
    return None
