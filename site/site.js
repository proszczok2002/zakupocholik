"use strict";

const english = {
    skip: "Skip to main content",
    home: "Home",
    eyebrow: "NVDA add-on",
    lead: "Jump between listing titles and sections by heading. Search OLX and Vinted and set Allegro Lokalnie filters using the keyboard.",
    badgesLabel: "Key information",
    badgeLanguage: "Polish interface",
    latest: "Downloads",
    version: "Version hosted on proszczok.pl:",
    download: "Download the latest version from GitHub",
    downloadLocal: "Download a copy from proszczok.pl",
    githubReleases: "Releases on GitHub",
    size: "Size:",
    checksum: "Package SHA-256:",
    information: "About the add-on",
    offers: "OLX and Vinted",
    offerList: "The add-on creates level 4 headings for recognized listing and profile cards, including listings in seller profiles and recommendations.",
    vintedSections: "On a Vinted listing, jump to photos, price, item details, description, purchase and contact, and seller information.",
    filters: "Allegro Lokalnie filters",
    filterList: "Choose categories, price, location and attributes available for the current results.",
    filterSearch: "Type part of an option name to find it in the current filter group. Searching preserves your selections.",
    filterRemove: "Remove an individual active filter or clear all filters.",
    filterFallback: "If the request fails, the add-on can try to read the current tab's source in Chrome and Firefox. You can also load filters from a saved HTML page.",
    shortcuts: "Essential shortcuts",
    guideMenu: "For the full Polish guide, choose Tools → Zakupocholik → Instrukcja i skróty in NVDA.",
    shortcutOffers: "Listing titles and profile names on OLX and Vinted.",
    shortcutSections: "Title and sections of a Vinted listing.",
    shortcutFilters: "Set filters for the current OLX, Vinted or Allegro Lokalnie results, including item condition when available. On OLX and Vinted, you can also jump to the site's other filters.",
    shortcutFavorites: "Save and open favorite results with their filters, even when the browser is closed. Press F2 to rename the selected favorite or Delete to remove it after confirmation.",
    shortcutResultList: "Browse the loaded offers with the arrow keys and press Enter to open the selected offer.",
    shortcutSearch: "Search field.",
    shortcutRead: "Reads a summary in search results. On an OLX, Vinted or Allegro Lokalnie listing, opens the title and seller's description with a Copy button.",
    shortcutBack: "Return to the last opened listing title on the same results page.",
    shortcutCount: "Read the result count when the site provides it. The add-on also announces it after applying filters.",
    install: "Installation",
    installOne: "Download and open the .nvda-addon file.",
    installTwo: "Confirm installation and restart NVDA.",
    installThree: "Find the commands under NVDA → Tools → Zakupocholik.",
    renumber: "Version 1.0 starts the numbering for releases with the updater. To switch from an earlier release, open the downloaded package manually.",
    updates: "Requirements, privacy and secure updates",
    updateSchedule: "Zakupocholik checks after NVDA starts and every 60 minutes by default. Change the interval or check manually from the Zakupocholik submenu.",
    updatePrompt: "The update dialog shows the release notes. Choose Yes to download the package from proszczok.pl over HTTPS, verify its SHA-256 checksum and install it in NVDA. NVDA will then ask whether to restart.",
    feedback: "The report option in the Zakupocholik menu asks for a nickname and a description. When you choose Send, the server forwards your nickname, description and add-on version to the author.",
    support: "The menu also links to the author's website and lets you support the author via PayPal or Revolut.",
    privacy_link: "Privacy policy",
    rights: "All rights reserved.",
};

const toggle = document.getElementById("language-toggle");
const translations = [...document.querySelectorAll("[data-i18n], [data-i18n-aria]")].map(element => {
    const attribute = element.dataset.i18nAria ? "aria-label" : null;
    const key = element.dataset.i18nAria || element.dataset.i18n;
    const polish = attribute ? element.getAttribute(attribute) : element.textContent;
    return { element, attribute, polish, english: english[key] };
});
const note = document.getElementById("release-note");
const initialNotes = { pl: note.textContent, en: "%%NOTES_EN_JS%%" };
let language = "pl";
let release;

function renderRelease() {
    note.textContent = release
        ? (language === "en" ? release.notes_en : release.notes)
        : initialNotes[language];
    if (!release) return;
    document.getElementById("release-version").textContent = release.version;
    document.getElementById("release-checksum").textContent = release.sha256;
    const size = new Intl.NumberFormat(language, { maximumFractionDigits: 1 }).format(release.size / 1024);
    document.getElementById("release-size").textContent = size + " KB";
    document.getElementById("download-link").href = release.url;
}

function setLanguage(nextLanguage) {
    language = nextLanguage;
    document.documentElement.lang = language;
    for (const entry of translations) {
        const value = language === "en" ? entry.english : entry.polish;
        if (entry.attribute) entry.element.setAttribute(entry.attribute, value);
        else entry.element.textContent = value;
    }
    toggle.textContent = language === "pl" ? "EN" : "PL";
    toggle.setAttribute("aria-label", language === "pl" ? "Switch to English" : "Przełącz na język polski");
    renderRelease();
    try {
        localStorage.setItem("siteLanguage", language);
    } catch {
        // The switch still works when browser storage is unavailable.
    }
}

toggle.addEventListener("click", () => setLanguage(language === "pl" ? "en" : "pl"));

const requestedLanguage = new URLSearchParams(window.location.search).get("lang");
let savedLanguage = "pl";
try { savedLanguage = localStorage.getItem("siteLanguage"); } catch (_) {}
setLanguage(requestedLanguage === "en" || (requestedLanguage !== "pl" && savedLanguage === "en") ? "en" : "pl");

fetch("update.json", { cache: "no-store" })
    .then(response => {
        if (!response.ok) throw new Error("Update metadata unavailable");
        return response.json();
    })
    .then(metadata => {
        if (metadata.schema !== 1 || metadata.addon !== "zakupocholik"
            || !/^[0-9]+(?:\.[0-9]+){1,3}$/.test(metadata.version)
            || metadata.url !== "https://proszczok.pl/zakupocholik/Zakupocholik-" + metadata.version + ".nvda-addon"
            || !/^[0-9a-f]{64}$/.test(metadata.sha256)
            || !Number.isSafeInteger(metadata.size) || metadata.size <= 0
            || typeof metadata.notes !== "string" || typeof metadata.notes_en !== "string") {
            return;
        }
        release = metadata;
        renderRelease();
    })
    .catch(() => {
        // The release information in the HTML remains available offline.
    });
