"""
Maps Lead Scraper — PyQt6 GUI
==============================
Desktop front-end for the Google Maps local-pack lead scraper.

This wraps the ORIGINAL Playwright/CDP Google-Maps scraping engine
(concurrent tabs, lazy-scroll pagination, "end of list" detection,
per-query timeout + salvage, structured card parsing) in a QThread
worker so the UI never freezes, streams leads into a live table as
they're discovered, and exposes every tunable as a proper
Configuration tab you can save/load as JSON — no code editing needed.

NEW: optional second-pass enrichment step. For each lead, visits the
"Website" link from its Google Maps place page and tries to pull a
contact email off that site (mailto: links first, then a text-regex
scan of the homepage, then a best-effort "Contact" page if nothing
is found). This is a second, separate scrape of the business's own
website (not Google), so it's slower and has a real miss rate —
not every business has a linked website, and not every website
publishes an email anywhere Claude/this script can see.

Requirements:
    pip install PyQt6 playwright pandas phonenumbers psutil requests --break-system-packages
    (Playwright just needs to connect over CDP to a real Chrome
    install — no playwright install browsers required.)
"""

import asyncio
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import psutil
from datetime import datetime
from login_dialog import require_login, consume_tokens, refresh_balance, fetch_settings, logout

import pandas as pd
import phonenumbers
import requests
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStatusBar,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QToolBar,
    QVBoxLayout,
    QWidget,
    QDoubleSpinBox,
)

from PyQt6.QtGui import QPalette, QColor
from playwright.async_api import async_playwright

# ---------------------------------------------------------------------------
# Constants — Google Maps specific selectors (unchanged from the original
# scraping engine; Maps result cards are div[role='article'] wrapping an
# a.hfpxzc link, inside a lazy-loading div[role='feed']).
# ---------------------------------------------------------------------------

CARD_LINK_SELECTOR = "a.hfpxzc"
FEED_SELECTOR = "div[role='feed']"
COLUMNS = [
    "name", "rating", "reviews", "category", "address", "phone", "status", "hours",
    "website", "email", "maps_url",
]

APP_CONFIG_PATH = os.path.expanduser("~/.lead_scraper_config.json")

# Email enrichment constants
EMAIL_RE = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9.-]+")
EMAIL_NOISE_DOMAINS = (
    "wixpress.com", "sentry.io", "schema.org", "example.com", "godaddy.com",
    "gstatic.com", "googleapis.com", "cloudflare.com", "w3.org", "png", "jpg",
    "jpeg", "gif", "svg", "webp",
)
CONTACT_LINK_TEXT_CANDIDATES = ["Contact", "Contact Us", "Get in Touch", "About"]


def _is_noise_email(email: str) -> bool:
    lowered = email.lower()
    return any(noise in lowered for noise in EMAIL_NOISE_DOMAINS)


# ---------------------------------------------------------------------------
# Token cost model — how many "weighted lead units" a single lead costs.
# A lead with a found email is the most valuable (1 unit); a lead with only
# a phone number is worth half that (0.5); a lead with neither still costs
# a small floor amount (0.2), since it can't be reasoned away as truly free.
# The admin sets a leads_per_token ratio (e.g. 10) that converts these
# weighted units into an actual token cost: token_cost = weighted_units / leads_per_token.
# ---------------------------------------------------------------------------

def lead_cost_units(row) -> float:
    email = str(row.get("email", "")).strip()
    phone = str(row.get("phone", "")).strip()
    if email:
        return 1.0
    if phone:
        return 0.5
    return 0.2


def total_cost_units(df: pd.DataFrame) -> float:
    if df.empty:
        return 0.0
    return sum(lead_cost_units(row) for _, row in df.iterrows())


# ---------------------------------------------------------------------------
# Auto-detection — find Chrome, a safe profile dir, and a free debug port
# without asking the user to know or type any of it.
# ---------------------------------------------------------------------------

def detect_chrome_executable():
    """Search common install locations per-OS and fall back gracefully."""
    system = platform.system()

    if system == "Windows":
        candidates = [
            os.path.join(os.environ.get("PROGRAMFILES", r"C:\Program Files"),
                          "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
                          "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""),
                          "Google", "Chrome", "Application", "chrome.exe"),
        ]
        for path in candidates:
            if path and os.path.isfile(path):
                return path
        found = shutil.which("chrome.exe") or shutil.which("chrome")
        return found or candidates[0]

    if system == "Darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            os.path.expanduser(
                "~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
            ),
        ]
        for path in candidates:
            if os.path.isfile(path):
                return path
        found = shutil.which("google-chrome") or shutil.which("chromium")
        return found or candidates[0]

    # Linux / everything else
    for name in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium"):
        found = shutil.which(name)
        if found:
            return found
    return "google-chrome"


def detect_default_profile_dir():
    """A dedicated, writable, OS-appropriate scratch profile — never the
    user's real Chrome profile, so it can't collide with logins/extensions."""
    return os.path.join(tempfile.gettempdir(), "lead_scraper_chrome_profile")


def find_free_port(preferred=9222, attempts=50):
    """Return preferred if it's free, otherwise the next free port after it."""
    for port in range(preferred, preferred + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.2)
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return preferred

DEFAULT_CONFIG = {
    "queries": [
        "dentist Biñan Laguna",
        "dental clinic Biñan Laguna",
        "dentist Santa Rosa Laguna",
        "dental clinic Santa Rosa Laguna",
        "dentist Carmona Cavite",
    ],
    "max_scrolls_per_query": 30,
    "scroll_wait_ms": 1000,
    "max_concurrent_tabs": 3,
    "query_timeout_seconds": 120,
    "stale_rounds_threshold": 5,
    "headless": False,
    "close_after_run": True,
    "chrome_debug_port": 9222,
    "chrome_executable": detect_chrome_executable(),
    "user_data_dir": detect_default_profile_dir(),
    "output_path": os.path.join(os.getcwd(), "leads_maps.csv"),
    "sort_alphabetically": False,
    "phone_default_region": "PH",
    "enable_email_enrichment": False,
    "email_max_concurrent_tabs": 5,
    "website_timeout_seconds": 15,
}


# ---------------------------------------------------------------------------
# Parsing a single Maps card (structured DOM, not raw text lines) — identical
# logic to the original engine, kept as a free async function.
# ---------------------------------------------------------------------------

async def parse_card(card, default_region="PH"):
    """card = the div[role='article'] wrapping one listing.

    default_region: ISO 3166-1 alpha-2 country code (e.g. "PH", "US", "IN")
    used as the assumed country for phone numbers written without an
    explicit "+<country code>" prefix. Numbers that DO include a "+"
    prefix (e.g. "+1 415 555 1234", "+91 98765 43210") are parsed
    correctly regardless of this setting — it only disambiguates
    locally-formatted numbers.
    """
    data = {
        "name": "", "rating": "", "reviews": "", "category": "",
        "address": "", "phone": "", "status": "", "hours": "",
        "website": "", "email": "", "maps_url": "",
    }

    name_el = await card.query_selector(".qBF1Pd, .fontHeadlineSmall")
    if name_el:
        data["name"] = (await name_el.inner_text()).strip()

    link_el = await card.query_selector(CARD_LINK_SELECTOR)
    if link_el:
        href = await link_el.get_attribute("href")
        if href:
            data["maps_url"] = href

    # Some result cards expose a "Website" link icon right in the list
    # view — grab it here so enrichment doesn't need to reopen Maps later.
    website_el = (
        await card.query_selector('a[data-value="Website"]')
        or await card.query_selector('a[aria-label^="Website:"]')
        or await card.query_selector('a[data-item-id="authority"]')
    )
    if website_el:
        website_href = await website_el.get_attribute("href")
        if website_href:
            data["website"] = website_href

    rating_el = await card.query_selector("span.MW4etd")
    
    reviews_el = await card.query_selector("span.UY7F9")
    if rating_el:
        data["rating"] = (await rating_el.inner_text()).strip()
    if reviews_el:
        data["reviews"] = re.sub(r"[()]", "", (await reviews_el.inner_text())).strip()

    # Category / address / status / hours live in W4Efsd rows below the title.
    # Maps duplicates this text for screen readers, so we dedupe first.
    info_rows = await card.query_selector_all("div.W4Efsd")
    seen_text = set()
    full_text_parts = []
    for row in info_rows:
        t = (await row.inner_text()).strip()
        if t and t not in seen_text:
            seen_text.add(t)
            full_text_parts.append(t)
    combined = " · ".join(full_text_parts)
    combined = re.sub(r"\s*·\s*", " · ", combined)

    raw_segments = [s.strip() for s in combined.split("·") if s.strip()]
    rating_review_pattern = re.compile(
        r"^\d\.\d\s*(\(\d+\))?$|^No reviews$", re.IGNORECASE
    )

    segments = []
    seen_seg = set()
    for seg in raw_segments:
        if rating_review_pattern.match(seg):
            continue
        # Belt-and-suspenders: whatever exact string we already captured as
        # the rating (from span.MW4etd) should never also end up as a
        # "category" or address fragment, regardless of how Maps formatted
        # the combined rating+review text this time around.
        if data["rating"] and seg == data["rating"]:
            continue
        if seg in seen_seg:
            continue
        seen_seg.add(seg)
        segments.append(seg)

    address_parts = []
    for seg in segments:
        phone_match = next(iter(phonenumbers.PhoneNumberMatcher(seg, default_region)), None)
        if phone_match:
            data["phone"] = phonenumbers.format_number(
                phone_match.number, phonenumbers.PhoneNumberFormat.NATIONAL
            )
            remainder = (seg[:phone_match.start] + seg[phone_match.end:]).strip(" ·")
            if remainder:
                address_parts.append(remainder)
        elif re.match(r"^(Open|Closed|Opens|Closes)", seg, re.IGNORECASE):
            data["status"] = "Open" if seg.lower().startswith("open") else seg
        elif re.search(r"\d{1,2}(:\d{2})?\s*[AP]M\b", seg, re.IGNORECASE):
            data["hours"] = seg
        elif not data["category"] and len(seg) < 40 and "," not in seg and not re.search(r"\d{3,}", seg):
            data["category"] = seg
        else:
            address_parts.append(seg)

    address = " ".join(dict.fromkeys(address_parts))
    data["address"] = address.strip()
    return data


def most_complete(series):
    non_empty = [v for v in series if str(v).strip()]
    return max(non_empty, key=len) if non_empty else ""


# ---------------------------------------------------------------------------
# Email enrichment — visits the Maps place page to grab the "Website" link,
# then visits that site and looks for a contact email. Runs as a second
# pass, under its own concurrency limit, after the main Maps scrape.
# ---------------------------------------------------------------------------

async def get_place_website(context, maps_place_url, timeout_seconds=20):
    """Open a Maps place page and pull the linked business website, if any."""
    if not maps_place_url:
        return ""
    page = await context.new_page()
    try:
        await page.goto(maps_place_url, wait_until="domcontentloaded",
                         timeout=timeout_seconds * 1000)
        link = await page.query_selector('a[data-item-id="authority"]')
        if not link:
            link = await page.query_selector('a[aria-label^="Website:"]')
        if link:
            href = await link.get_attribute("href")
            return href or ""
        return ""
    except Exception:
        return ""
    finally:
        await page.close()

FACEBOOK_DOMAINS = ("facebook.com", "fb.com", "m.facebook.com")

def _is_facebook_url(url: str) -> bool:
    return any(d in url.lower() for d in FACEBOOK_DOMAINS)

async def get_email_from_website(context, website_url, timeout_seconds=15):
    """Visit a business's own website and try to find a contact email.

    Facebook pages get a dedicated path: no mailto: links exist there, JS
    hydration means domcontentloaded fires before content is rendered, and
    any listed email usually lives on the Page Transparency / About sub-page
    rather than the root URL a Maps "Website" link points to.

    Order of attempts (non-Facebook):
      1. Explicit mailto: link (most reliable signal)
      2. Regex scan of visible homepage text
      3. Same regex scan on a linked "Contact"-style page, if present

    Returns "" if nothing usable is found — this is expected for a
    meaningful fraction of sites, not a bug.
    """
    if not website_url:
        return ""

    if _is_facebook_url(website_url):
        return await _get_email_from_facebook(context, website_url, timeout_seconds)

    page = await context.new_page()
    try:
        await page.goto(website_url, wait_until="domcontentloaded",
                         timeout=timeout_seconds * 1000)
        # Give client-rendered sites a moment to hydrate before scanning.
        # networkidle can hang on sites with persistent polling/analytics
        # connections, so cap it separately and fall through on timeout
        # rather than losing the whole attempt.
        try:
            await page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass

        mailto = await page.query_selector('a[href^="mailto:"]')
        if mailto:
            href = await mailto.get_attribute("href") or ""
            email = href.replace("mailto:", "").split("?")[0].strip()
            if email and not _is_noise_email(email):
                return email

        text = await page.inner_text("body")
        matches = [m for m in EMAIL_RE.findall(text) if not _is_noise_email(m)]
        if matches:
            return matches[0]

        for label in CONTACT_LINK_TEXT_CANDIDATES:
            contact_link = await page.query_selector(f"a:has-text('{label}')")
            if contact_link:
                href = await contact_link.get_attribute("href")
                if not href:
                    continue
                try:
                    await page.goto(href, wait_until="domcontentloaded",
                                     timeout=timeout_seconds * 1000)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=5000)
                    except Exception:
                        pass
                except Exception:
                    continue
                mailto = await page.query_selector('a[href^="mailto:"]')
                if mailto:
                    href2 = await mailto.get_attribute("href") or ""
                    email = href2.replace("mailto:", "").split("?")[0].strip()
                    if email and not _is_noise_email(email):
                        return email
                text = await page.inner_text("body")
                matches = [m for m in EMAIL_RE.findall(text) if not _is_noise_email(m)]
                if matches:
                    return matches[0]
                break

        return ""
    except Exception:
        return ""
    finally:
        await page.close()


async def _get_email_from_facebook(context, page_url, timeout_seconds=15):
    """Facebook-specific path: go straight to the Page Transparency /
    About-Contact sub-page, which is where an email (if listed at all)
    actually lives, and wait for hydration since Facebook's initial HTML
    is a near-empty shell."""
    # Normalize to a bare page URL (strip query params, trailing slash)
    # then build the contact-info sub-page path Facebook uses.
    base = page_url.split("?")[0].rstrip("/")
    candidates = [
        base + "/about_contact_and_basic_info",
        base + "/about_profile_transparency",
        page_url,  # fallback: scan the root page itself
    ]

    page = await context.new_page()
    try:
        for url in candidates:
            try:
                await page.goto(url, wait_until="domcontentloaded",
                                 timeout=timeout_seconds * 1000)
            except Exception:
                continue

            # Facebook is a heavy SPA — give it real time to hydrate.
            # networkidle is unreliable on FB (persistent background
            # requests), so use a fixed settle delay instead.
            await page.wait_for_timeout(3000)

            text = await page.inner_text("body")
            matches = [m for m in EMAIL_RE.findall(text) if not _is_noise_email(m)]
            if matches:
                return matches[0]

        return ""
    except Exception:
        return ""
    finally:
        await page.close()


# ---------------------------------------------------------------------------
# Worker thread — runs the async Playwright/CDP engine off the GUI thread.
# ---------------------------------------------------------------------------

class ScraperWorker(QThread):
    log = pyqtSignal(str)
    card_found = pyqtSignal(dict)
    query_progress = pyqtSignal(int, int, str)   # completed, total, last query
    query_detail = pyqtSignal(str)                # fine-grained per-query status
    enrich_progress = pyqtSignal(int, int, str)   # completed, total, last name
    finished_ok = pyqtSignal(pd.DataFrame)
    failed = pyqtSignal(str)

    def __init__(self, config: dict):
        super().__init__()
        self.cfg = dict(config)
        self._stop_requested = False
        self._chrome_process = None
        # Running tally of "weighted lead units" spent so far this run
        # (email=1, phone-only=0.5, neither=0.2) — compared against
        # cfg["token_budget_leads"] to auto-stop a user account that has
        # run out of tokens partway through. None/absent budget = no cap
        # (admin accounts, or a backend that couldn't be reached).
        self._spent_units = 0.0

    def request_stop(self):
        self._stop_requested = True

    def _add_units_and_maybe_stop(self, delta: float):
        if delta <= 0:
            self._spent_units += delta
            return
        self._spent_units += delta
        budget = self.cfg.get("token_budget_leads")
        if budget is not None and self._spent_units >= budget and not self._stop_requested:
            self._stop_requested = True
            self.log.emit(
                f"Token balance exhausted (used ~{self._spent_units:.1f}/"
                f"{budget:.1f} weighted leads) — stopping the run. "
                f"Leads found so far are kept."
            )

    # -- Chrome lifecycle ---------------------------------------------------

    def _kill_chrome_on_port(self, port):
        """Kill only the Chrome process (and its children) that was launched
        with this specific --remote-debugging-port — never other Chrome
        windows the user has open. Works the same way on every OS."""
        marker = f"--remote-debugging-port={port}"
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                cmdline = proc.info["cmdline"] or []
                if any(marker in arg for arg in cmdline):
                    parent = psutil.Process(proc.info["pid"])
                    children = parent.children(recursive=True)
                    for child in children:
                        child.terminate()
                    parent.terminate()
                    gone, alive = psutil.wait_procs([parent, *children], timeout=3)
                    for p in alive:
                        p.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

    def _launch_chrome(self):
        port = self.cfg["chrome_debug_port"]
        self.log.emit("Closing any stale Chrome debug sessions…")
        self._kill_chrome_on_port(port)
        time.sleep(1)

        # Self-heal: if the configured port is still occupied (e.g. another
        # app is using it), silently move to the next free one instead of
        # failing to connect later.
        free_port = find_free_port(preferred=port)
        if free_port != port:
            self.log.emit(f"Port {port} was busy — auto-selected free port {free_port} instead.")
            self.cfg["chrome_debug_port"] = free_port
            port = free_port

        user_data_dir = self.cfg["user_data_dir"]
        os.makedirs(user_data_dir, exist_ok=True)
        lock_file = os.path.join(user_data_dir, "SingletonLock")
        if os.path.exists(lock_file):
            os.remove(lock_file)

        args = [
            self.cfg["chrome_executable"],
            f"--remote-debugging-port={port}",
            f"--user-data-dir={user_data_dir}",
            # Google Maps fingerprints navigator.webdriver / automation
            # signals and serves a reduced card layout when it suspects a
            # headless/automated browser — review counts in particular
            # tend to disappear from that reduced layout. These flags hide
            # the tell-tale signs so headless mode renders the same full
            # card markup as a normal visible Chrome window.
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process",
            "--lang=en-US,en",
        ]
        if self.cfg["headless"]:
            args.insert(1, "--headless=new")
            args.append("--window-size=1920,1080")
            # Headless Chrome's default UA string literally contains the
            # word "Headless" — an easy signal for Maps to key off. Strip
            # it so the UA matches a normal desktop Chrome build.
            args.append(
                "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )

        self.log.emit(f"Launching Chrome ({'headless' if self.cfg['headless'] else 'visible'}) "
                       f"on port {port}…")
        self._chrome_process = subprocess.Popen(args)
        time.sleep(2)

    def _close_chrome(self):
        """Kill the whole Chrome process tree we launched (renderer/GPU/utility
        children survive Popen.terminate(), so match on the debug port)."""
        self.log.emit("Closing Chrome…")
        self._kill_chrome_on_port(self.cfg["chrome_debug_port"])
        if self._chrome_process is not None:
            try:
                self._chrome_process.wait(timeout=3)
            except Exception:
                pass

    # -- Single-query scrape (Google Maps) -----------------------------------

    async def _scrape_query_inner(self, page, query):
        url = f"https://www.google.com/maps/search/{query.replace(' ', '+')}"
        await page.goto(url, wait_until="domcontentloaded")

        try:
            await page.wait_for_selector(FEED_SELECTOR, timeout=15000)
        except Exception:
            self.log.emit(f"  no results feed loaded for: {query}")
            return []

        feed = await page.query_selector(FEED_SELECTOR)

        prev_count = 0
        stale_rounds = 0
        reached_end = False
        loop_start = time.time()
        max_scrolls = self.cfg["max_scrolls_per_query"]
        wait_ms = self.cfg["scroll_wait_ms"]
        stale_threshold = self.cfg["stale_rounds_threshold"]

        for scroll_i in range(max_scrolls):
            if self._stop_requested:
                self.log.emit(f"  [{query}] stop requested — halting scroll early")
                break

            try:
                if feed:
                    await feed.evaluate("el => el.scrollBy(0, el.scrollHeight)")
            except Exception as e:
                self.log.emit(f"  [{query}] feed handle stale at scroll {scroll_i + 1}, "
                               f"re-fetching ({e.__class__.__name__})")
                feed = await page.query_selector(FEED_SELECTOR)
                if feed:
                    await feed.evaluate("el => el.scrollBy(0, el.scrollHeight)")

            await page.wait_for_timeout(wait_ms)

            end_marker = await page.query_selector("text=You've reached the end of the list.")
            if end_marker:
                reached_end = True
                self.log.emit(f"  [{query}] reached end of list after {scroll_i + 1} scrolls")
                break

            current_count = len(await page.query_selector_all(CARD_LINK_SELECTOR))

            if (scroll_i + 1) % 10 == 0:
                elapsed = time.time() - loop_start
                self.query_detail.emit(
                    f"[{query}] scroll {scroll_i + 1}/{max_scrolls} — "
                    f"{current_count} cards — {elapsed:.0f}s elapsed"
                )

            if current_count <= prev_count:
                stale_rounds += 1
                if stale_rounds >= stale_threshold:
                    break
            else:
                stale_rounds = 0
            prev_count = current_count

        if not reached_end and not self._stop_requested:
            total_elapsed = time.time() - loop_start
            self.log.emit(f"  [{query}] stopped at scroll limit ({max_scrolls}) after "
                           f"{total_elapsed:.0f}s — list may not be exhausted")

        cards = await page.query_selector_all("div[role='article']")
        query_results = []
        region = self.cfg.get("phone_default_region", "PH")
        for card in cards:
            result = await parse_card(card, region)
            if result["name"]:
                query_results.append(result)

        self.log.emit(f"  {query}: {len(query_results)} cards")
        return query_results

    async def scrape_query(self, page, query, semaphore):
        """Runs the real scrape under a hard timeout so a stalled page can
        never block the rest of the run indefinitely."""
        timeout = self.cfg["query_timeout_seconds"]
        async with semaphore:
            if self._stop_requested:
                return []
            try:
                return await asyncio.wait_for(
                    self._scrape_query_inner(page, query), timeout=timeout
                )
            except asyncio.TimeoutError:
                self.log.emit(f"  [{query}] TIMED OUT after {timeout}s — salvaging loaded cards")
                try:
                    cards = await page.query_selector_all("div[role='article']")
                    results = []
                    region = self.cfg.get("phone_default_region", "PH")
                    for card in cards:
                        result = await parse_card(card, region)
                        if result["name"]:
                            results.append(result)
                    self.log.emit(f"  [{query}] salvaged {len(results)} cards after timeout")
                    return results
                except Exception as e:
                    self.log.emit(f"  [{query}] salvage attempt also failed: {e}")
                    return []

    async def _run_one(self, page, query, semaphore):
        result = await self.scrape_query(page, query, semaphore)
        return query, result

    async def run_all_queries(self, context, queries):
        semaphore = asyncio.Semaphore(self.cfg["max_concurrent_tabs"])
        pages = [await context.new_page() for _ in queries]

        tasks = [asyncio.ensure_future(self._run_one(p, q, semaphore)) for p, q in zip(pages, queries)]

        all_results = []
        completed = 0
        total = len(tasks)
        for fut in asyncio.as_completed(tasks):
            try:
                query, r = await fut
            except Exception as e:
                completed += 1
                self.log.emit(f"  FAILED with unhandled error: {e!r}")
                self.query_progress.emit(completed, total, "(error)")
                continue

            completed += 1
            self.query_progress.emit(completed, total, query)
            for card in r:
                self.card_found.emit(card)
            all_results.extend(r)

            # Provisional running cost: email is never known yet at this
            # stage, so each card is worth 0.5 (has a phone) or 0.2 (doesn't).
            # This lets a user's run stop itself as soon as it's clearly
            # burned through their balance, without waiting for enrichment.
            delta = sum(
                0.5 if str(card.get("phone", "")).strip() else 0.2
                for card in r
            )
            self._add_units_and_maybe_stop(delta)

        if self.cfg["close_after_run"]:
            for page in pages:
                try:
                    await page.close()
                except Exception:
                    pass

        return all_results

    # -- Email enrichment pass (optional second pass) ------------------------


    @staticmethod
    def _dedup(all_results):
        df = pd.DataFrame(all_results, columns=COLUMNS)
        df = df[df["name"].str.strip() != ""]
        df = df.drop_duplicates()
        if not df.empty:
            df = df.groupby(
                ["name", "phone"], as_index=False, dropna=False, sort=False
            ).agg(most_complete)
        return df

    async def enrich_with_emails(self, context, results):
        """For each lead that has a maps_url, fetch its website link and try
        to pull a contact email from that site. Runs under its own
        concurrency cap since it's roughly 2 extra page loads per lead."""
        max_concurrent = max(1, self.cfg.get("email_max_concurrent_tabs", 5))
        timeout_seconds = self.cfg.get("website_timeout_seconds", 15)
        semaphore = asyncio.Semaphore(max_concurrent)

        total = len(results)
        completed = 0
        with_website = sum(1 for c in results if c.get("website"))

        async def _one(card):
            nonlocal completed
            async with semaphore:
                if self._stop_requested:
                    return
                try:
                    website = card.get("website", "")
                    # No Maps place-page fallback — if the card didn't expose
                    # a website link, skip this lead entirely rather than
                    # reopening Maps to look for one.
                    prior_cost = 0.5 if str(card.get("phone", "")).strip() else 0.2
                    if website:
                        card["email"] = await get_email_from_website(
                            context, website, timeout_seconds
                        )
                        status = "email found" if card["email"] else "no email on site"
                        self.log.emit(f"  [{card.get('name', '?')}] {status} — {website}")
                    else:
                        self.log.emit(f"  [{card.get('name', '?')}] no website link — skipped")
                    new_cost = 1.0 if card.get("email") else prior_cost
                    self._add_units_and_maybe_stop(new_cost - prior_cost)
                except Exception as e:
                    self.log.emit(f"  enrichment failed for {card.get('name', '?')}: {e}")
                finally:
                    completed += 1
                    self.enrich_progress.emit(completed, total, card.get("name", ""))

        self.log.emit(
            f"Enriching {total} leads with website/email lookups "
            f"(max {max_concurrent} concurrent)…"
        )
        await asyncio.gather(*(_one(c) for c in results))
        found = sum(1 for c in results if c.get("email"))
        self.log.emit(
            f"Email enrichment done — {with_website}/{total} leads had a website link, "
            f"found emails for {found}/{with_website if with_website else total} of those."
        )
        return results


    async def _async_main(self):
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(
                f"http://localhost:{self.cfg['chrome_debug_port']}"
            )
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            await context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            self.log.emit(f"Searching {len(self.cfg['queries'])} queries "
                        f"(max {self.cfg['max_concurrent_tabs']} tabs at a time)…")
            all_results = await self.run_all_queries(context, self.cfg["queries"])

            self.log.emit(f"\nTotal raw cards across all queries: {len(all_results)}")
            df = self._dedup(all_results)
            self.log.emit(f"Unique leads after dedup: {len(df)}")

            if self.cfg.get("enable_email_enrichment", False) and not df.empty and not self._stop_requested:
                deduped_records = df.to_dict("records")
                enriched_records = await self.enrich_with_emails(context, deduped_records)
                df = pd.DataFrame(enriched_records, columns=COLUMNS)

            return df

    # -- Thread entry point ---------------------------------------------------

    def run(self):
        try:
            # Belt-and-suspenders: regular user accounts always run
            # headless, no matter what a stale saved config says.
            if self.cfg.get("role") != "admin":
                self.cfg["headless"] = True

            self._launch_chrome()
            df = asyncio.run(self._async_main())

            if self.cfg.get("sort_alphabetically", False) and not df.empty:
                df = df.sort_values(
                    by="name", key=lambda col: col.str.lower(), kind="stable"
                ).reset_index(drop=True)

            self.finished_ok.emit(df)
        except Exception as e:
            self.failed.emit(str(e))
        finally:
            if self.cfg.get("close_after_run", True):
                self._close_chrome()
            else:
                self.log.emit("Leaving Chrome (and tabs) open — 'Close after run' is unchecked.")

# ---------------------------------------------------------------------------
# Stylesheet — professional dark theme with tab styling
# ---------------------------------------------------------------------------

STYLE_SHEET = """
QMainWindow { background-color: #14161c; }
QWidget { color: #e6e6e6; font-family: 'Segoe UI', 'Inter', sans-serif; font-size: 13px; }

QToolBar {
    background-color: #1b1e26;
    border: none;
    padding: 8px;
    spacing: 8px;
    border-bottom: 1px solid #2a2f3a;
}

QLabel#appTitle { font-size: 16px; font-weight: 700; color: #ffffff; }
QLabel#appSubtitle { font-size: 11px; color: #7a8194; }
QLabel#sectionLabel {
    font-weight: 600; font-size: 11px; color: #8b93a7;
    text-transform: uppercase; letter-spacing: 0.6px; padding-top: 4px;
}
QLabel#tokenBalance {
    font-weight: 700; font-size: 13px; color: #4c7cff;
    padding: 4px 10px; background-color: #1c2233; border-radius: 6px;
}
QLabel#roleBadge {
    font-weight: 700; font-size: 10px; color: #8b93a7;
    padding: 3px 8px; background-color: #232834; border-radius: 6px;
    text-transform: uppercase; letter-spacing: 0.6px;
}

QTabWidget::pane { border: 1px solid #2a2f3a; border-radius: 8px; top: -1px; background-color: #181b22; }
QTabBar::tab {
    background-color: #1b1e26; color: #8b93a7; padding: 10px 20px;
    border: 1px solid #2a2f3a; border-bottom: none;
    border-top-left-radius: 8px; border-top-right-radius: 8px; margin-right: 2px;
    font-weight: 600;
}
QTabBar::tab:selected { background-color: #181b22; color: #ffffff; border-bottom: 2px solid #4c7cff; }
QTabBar::tab:hover:!selected { color: #c4c9d4; }

QGroupBox {
    border: 1px solid #2a2f3a; border-radius: 8px; margin-top: 14px;
    padding-top: 12px; font-weight: 600; color: #b8bfcf;
}
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 6px; }

QListWidget, QPlainTextEdit, QTableWidget, QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {
    background-color: #20242e; border: 1px solid #2f3542; border-radius: 6px;
    padding: 5px; selection-background-color: #3d6bff;
}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus { border: 1px solid #4c7cff; }

QTableWidget { gridline-color: #2a2f3a; alternate-background-color: #1c2027; }
QTableWidget::item { padding: 4px; }
QTableWidget::item:selected { background-color: #3d6bff; color: #ffffff; }
QTableWidget::item:hover { background-color: #232a3a; }

QTableWidget::item {
    padding: 4px;
}
QTableWidget::item:selected {
    background-color: #3d6bff;
    color: #ffffff;
}
QTableWidget::item:hover {
    background-color: #232a3a;
}

QHeaderView::section {
    background-color: #232834; color: #b8bfcf; padding: 8px;
    border: none; border-bottom: 1px solid #2f3542; font-weight: 600;
}
QHeaderView {
    background-color: #232834;
}
QTableCornerButton::section {
    background-color: #232834;
    border: none;
    border-bottom: 1px solid #2f3542;
}

QPushButton {
    background-color: #3d6bff; color: white; border: none; border-radius: 6px;
    padding: 9px 18px; font-weight: 600;
}
QPushButton:hover { background-color: #5680ff; }
QPushButton:pressed { background-color: #2f57d6; }
QPushButton:disabled { background-color: #2b3040; color: #6b7180; }

QPushButton#secondary { background-color: #232834; color: #e6e6e6; border: 1px solid #34394a; }
QPushButton#secondary:hover { background-color: #2c313f; }

QPushButton#danger { background-color: #e5484d; }
QPushButton#danger:hover { background-color: #f16469; }

QStatusBar { background-color: #1b1e26; color: #8b93a7; border-top: 1px solid #2a2f3a; }

QProgressBar {
    background-color: #20242e; border: 1px solid #2f3542; border-radius: 6px;
    text-align: center; color: #e6e6e6; height: 18px;
}
QProgressBar::chunk { background-color: #4c7cff; border-radius: 5px; }

QCheckBox::indicator { width: 16px; height: 16px; }

QScrollBar:vertical { background: #181b22; width: 10px; margin: 0; }
QScrollBar::handle:vertical { background: #34394a; border-radius: 5px; min-height: 24px; }
QScrollBar::handle:vertical:hover { background: #454b5e; }

QMessageBox {
    background-color: #181b22;
}
QMessageBox QLabel {
    color: #e6e6e6;
    background-color: transparent;
}
QMessageBox QPushButton {
    background-color: #3d6bff;
    color: white;
    border: none;
    border-radius: 6px;
    padding: 8px 18px;
    font-weight: 600;
    min-width: 70px;
}
QMessageBox QPushButton:hover {
    background-color: #5680ff;
}
"""


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self, session: dict):
        super().__init__()
        self.session = session  # {"token", "email", "role", "tokens_balance"}
        self.is_admin = session.get("role") == "admin"

        self.setWindowTitle(f"Maps Lead Scraper — {session.get('email', '')}"
                            f"{'  (admin)' if self.is_admin else ''}")
        self.resize(1320, 800)
        self.setStyleSheet(STYLE_SHEET)

        self.worker = None
        self.results_df = pd.DataFrame(columns=COLUMNS)
        self.last_autosave_path = None
        self.config = dict(DEFAULT_CONFIG)

        # Backend-controlled settings: how many weighted lead-units one
        # token buys, the QR code to display, and the payment blurb. Falls
        # back to sane defaults if the backend can't be reached.
        self.backend_settings = fetch_settings(self.session.get("token")) or {}
        self.leads_per_token = float(self.backend_settings.get("leads_per_token", 10.0) or 10.0)

        self._build_toolbar()
        self._build_central()
        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("Ready")

        # Populate widgets from the (in-memory) default config, then try
        # loading a previously saved one from disk.
        self._populate_query_list(self.config["queries"])
        self._apply_config_to_fields(self.config)
        if os.path.exists(APP_CONFIG_PATH):
            self._load_config(APP_CONFIG_PATH, silent=True)
        self.auto_detect_chrome_settings(silent=True)
        self._enforce_role_restrictions()
        self._update_balance_label()

    # -- Toolbar --------------------------------------------------------------

    def _build_toolbar(self):
        toolbar = QToolBar("Main")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        title_box = QWidget()
        title_layout = QVBoxLayout(title_box)
        title_layout.setContentsMargins(6, 0, 20, 0)
        title_layout.setSpacing(0)
        title = QLabel("🗺  Maps Lead Scraper")
        title.setObjectName("appTitle")
        subtitle = QLabel("Google Maps local-pack lead generation")
        subtitle.setObjectName("appSubtitle")
        title_layout.addWidget(title)
        title_layout.addWidget(subtitle)
        toolbar.addWidget(title_box)

        self.role_badge = QLabel("ADMIN" if self.is_admin else "USER")
        self.role_badge.setObjectName("roleBadge")
        toolbar.addWidget(self.role_badge)

        toolbar.addSeparator()

        self.balance_label = QLabel("")
        self.balance_label.setObjectName("tokenBalance")
        toolbar.addWidget(self.balance_label)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        toolbar.addWidget(spacer)

        self.start_btn = QPushButton("▶  Start Scrape")
        self.start_btn.clicked.connect(self.start_scrape)
        toolbar.addWidget(self.start_btn)

        self.stop_btn = QPushButton("■  Stop")
        self.stop_btn.setObjectName("danger")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_scrape)
        toolbar.addWidget(self.stop_btn)

        toolbar.addSeparator()

        self.export_btn = QPushButton("Export CSV")
        self.export_btn.setObjectName("secondary")
        self.export_btn.clicked.connect(self.export_csv)
        self.export_btn.setEnabled(False)
        toolbar.addWidget(self.export_btn)

        toolbar.addSeparator()

        self.logout_btn = QPushButton("Log Out")
        self.logout_btn.setObjectName("secondary")
        self.logout_btn.clicked.connect(self.handle_logout)
        toolbar.addWidget(self.logout_btn)

    def _update_balance_label(self):
        if self.is_admin:
            self.balance_label.setText("Unlimited (admin)")
        else:
            bal = self.session.get("tokens_balance", 0.0)
            self.balance_label.setText(
                f"{bal:g} token{'s' if bal != 1 else ''}  "
                f"(~{bal * self.leads_per_token:g} weighted leads)"
            )

    # -- Central widget: tabs ---------------------------------------------------

    def _build_central(self):
        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)

        self.tabs.addTab(self._build_dashboard_tab(), "🔍  Search && Results")
        self.tabs.addTab(self._build_config_tab(), "⚙️  Configuration")

        if self.is_admin:
            self.tabs.addTab(self._build_admin_tab(), "🛠  Admin")
        else:
            self.tabs.addTab(self._build_buy_tokens_tab(), "💳  Buy Tokens")

    # -- Tab 1: dashboard -------------------------------------------------------

    def _build_dashboard_tab(self):
        root = QWidget()
        outer_layout = QVBoxLayout(root)
        outer_layout.setContentsMargins(14, 14, 14, 14)
        outer_layout.setSpacing(10)

        # Vertical splitter: top row (queries + results) above, log docked below
        v_splitter = QSplitter(Qt.Orientation.Vertical)
        outer_layout.addWidget(v_splitter)

        top_row = QWidget()
        layout = QHBoxLayout(top_row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        layout.addWidget(splitter)

        # Left: query list
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setSpacing(8)

        left_layout.addWidget(self._label("Search Queries"))
        self.query_list = QListWidget()
        self.query_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        left_layout.addWidget(self.query_list, stretch=1)

        query_input_row = QHBoxLayout()
        self.query_input = QLineEdit()
        self.query_input.setPlaceholderText("e.g. dentist Cabuyao Laguna")
        self.query_input.returnPressed.connect(self.add_query)
        add_btn = QPushButton("Add")
        add_btn.setObjectName("secondary")
        add_btn.clicked.connect(self.add_query)
        remove_btn = QPushButton("Remove")
        remove_btn.setObjectName("secondary")
        remove_btn.clicked.connect(self.remove_query)
        query_input_row.addWidget(self.query_input)
        query_input_row.addWidget(add_btn)
        query_input_row.addWidget(remove_btn)
        left_layout.addLayout(query_input_row)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("Idle")
        left_layout.addWidget(self.progress_bar)

        self.enrich_progress_bar = QProgressBar()
        self.enrich_progress_bar.setRange(0, 1)
        self.enrich_progress_bar.setValue(0)
        self.enrich_progress_bar.setFormat("Email enrichment: idle")
        left_layout.addWidget(self.enrich_progress_bar)

        left.setMinimumWidth(320)
        splitter.addWidget(left)

        # Right: results table
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setSpacing(8)

        header_row = QHBoxLayout()
        header_row.addWidget(self._label("Results"))
        header_row.addStretch()
        self.results_count_label = QLabel("0 leads")
        header_row.addWidget(self.results_count_label)
        right_layout.addLayout(header_row)

        self.table = QTableWidget(0, len(COLUMNS) + 1)
        self.table.setHorizontalHeaderLabels(["Keep"] + [c.replace("_", " ").title() for c in COLUMNS])
        self.table.setColumnWidth(0, 50)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSortingEnabled(True)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        pal = self.table.palette()
        pal.setColor(QPalette.ColorRole.Base, QColor("#20242e"))
        pal.setColor(QPalette.ColorRole.AlternateBase, QColor("#1c2027"))
        self.table.setPalette(pal)
        self.table.itemChanged.connect(self._on_table_item_changed)
        right_layout.addWidget(self.table, stretch=1)

        selection_row = QHBoxLayout()
        self.selection_cost_label = QLabel("")
        selection_row.addWidget(self.selection_cost_label)
        selection_row.addStretch()
        select_all_btn = QPushButton("Select All")
        select_all_btn.setObjectName("secondary")
        select_all_btn.clicked.connect(lambda: self._set_all_checked(True))
        select_none_btn = QPushButton("Select None")
        select_none_btn.setObjectName("secondary")
        select_none_btn.clicked.connect(lambda: self._set_all_checked(False))
        self.confirm_selection_btn = QPushButton("✓  Confirm && Charge Selected")
        self.confirm_selection_btn.clicked.connect(self.confirm_selection)
        self.confirm_selection_btn.setEnabled(False)
        selection_row.addWidget(select_all_btn)
        selection_row.addWidget(select_none_btn)
        selection_row.addWidget(self.confirm_selection_btn)
        right_layout.addLayout(selection_row)

        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

        v_splitter.addWidget(top_row)

        # Bottom: docked activity log
        log_panel = QWidget()
        log_layout = QVBoxLayout(log_panel)
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_layout.setSpacing(6)

        log_header_row = QHBoxLayout()
        log_header_row.addWidget(self._label("Activity Log"))
        log_header_row.addStretch()
        clear_btn = QPushButton("Clear")
        clear_btn.setObjectName("secondary")
        clear_btn.clicked.connect(lambda: self.log_box.clear())
        log_header_row.addWidget(clear_btn)
        log_layout.addLayout(log_header_row)

        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setFont(QFont("Consolas", 10))
        log_layout.addWidget(self.log_box)

        v_splitter.addWidget(log_panel)
        v_splitter.setStretchFactor(0, 4)
        v_splitter.setStretchFactor(1, 1)
        v_splitter.setSizes([560, 160])

        return root

    # -- Tab 2: configuration -----------------------------------------------

    def _build_config_tab(self):
        root = QWidget()
        outer = QVBoxLayout(root)
        outer.setContentsMargins(18, 18, 18, 18)
        outer.setSpacing(14)

        row = QHBoxLayout()
        row.setSpacing(14)
        outer.addLayout(row)

        # -- Scraping behaviour group
        behaviour_group = QGroupBox("Scraping Behaviour")
        form1 = QFormLayout(behaviour_group)
        form1.setSpacing(10)

        self.max_scrolls_spin = QSpinBox()
        self.max_scrolls_spin.setRange(1, 200)
        form1.addRow("Max scrolls per query", self.max_scrolls_spin)

        self.scroll_wait_spin = QSpinBox()
        self.scroll_wait_spin.setRange(100, 10000)
        self.scroll_wait_spin.setSingleStep(100)
        self.scroll_wait_spin.setSuffix(" ms")
        form1.addRow("Wait between scrolls", self.scroll_wait_spin)

        self.stale_rounds_spin = QSpinBox()
        self.stale_rounds_spin.setRange(1, 20)
        form1.addRow("Stale rounds before giving up", self.stale_rounds_spin)

        self.max_concurrent_spin = QSpinBox()
        self.max_concurrent_spin.setRange(1, 10)
        form1.addRow("Max concurrent tabs", self.max_concurrent_spin)

        self.query_timeout_spin = QSpinBox()
        self.query_timeout_spin.setRange(10, 600)
        self.query_timeout_spin.setSuffix(" s")
        form1.addRow("Per-query timeout", self.query_timeout_spin)

        row.addWidget(behaviour_group)

        # -- Chrome / browser group
        chrome_group = QGroupBox("Chrome / Browser  (auto-detected)")
        chrome_layout = QVBoxLayout(chrome_group)
        chrome_layout.setSpacing(10)

        form2 = QFormLayout()
        form2.setContentsMargins(0, 0, 0, 0)
        form2.setSpacing(10)

        self.headless_check = QCheckBox("Run headless (no visible browser)")
        form2.addRow(self.headless_check)

        self.close_after_check = QCheckBox("Close Chrome && tabs after run")
        form2.addRow(self.close_after_check)

        self.chrome_exe_edit = QLineEdit()
        self.chrome_exe_edit.setReadOnly(True)
        form2.addRow("Chrome executable", self.chrome_exe_edit)

        self.profile_dir_edit = QLineEdit()
        self.profile_dir_edit.setReadOnly(True)
        form2.addRow("Chrome profile dir", self.profile_dir_edit)

        self.debug_port_spin = QSpinBox()
        self.debug_port_spin.setRange(1024, 65535)
        self.debug_port_spin.setReadOnly(True)
        self.debug_port_spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        form2.addRow("Debug port", self.debug_port_spin)

        chrome_layout.addLayout(form2)

        redetect_row = QHBoxLayout()
        redetect_btn = QPushButton("🔄  Re-detect")
        redetect_btn.setObjectName("secondary")
        redetect_btn.clicked.connect(self.auto_detect_chrome_settings)
        redetect_row.addWidget(redetect_btn)
        redetect_row.addStretch()
        chrome_layout.addLayout(redetect_row)

        self.headless_hint = QLabel(
            "Detected automatically for this machine — Chrome is located "
            "and a free debug port is picked at launch time. Click "
            "Re-detect if you install/move Chrome."
        )
        self.headless_hint.setWordWrap(True)
        self.headless_hint.setStyleSheet("color: #7a8194; font-size: 11px;")
        chrome_layout.addWidget(self.headless_hint)

        row.addWidget(chrome_group)

        # -- Email enrichment group
        enrich_group = QGroupBox("Website / Email Enrichment")
        enrich_layout = QVBoxLayout(enrich_group)
        enrich_layout.setSpacing(10)

        self.enable_email_check = QCheckBox(
            "After scraping, visit each lead's website to find a contact email"
        )
        enrich_layout.addWidget(self.enable_email_check)

        form_enrich = QFormLayout()
        form_enrich.setContentsMargins(0, 0, 0, 0)
        form_enrich.setSpacing(10)

        self.email_concurrent_spin = QSpinBox()
        self.email_concurrent_spin.setRange(1, 20)
        form_enrich.addRow("Max concurrent website visits", self.email_concurrent_spin)

        self.website_timeout_spin = QSpinBox()
        self.website_timeout_spin.setRange(5, 120)
        self.website_timeout_spin.setSuffix(" s")
        form_enrich.addRow("Per-website timeout", self.website_timeout_spin)

        enrich_layout.addLayout(form_enrich)

        enrich_hint_text = (
            "For each lead, opens its Google Maps 'Website' link, then checks "
            "that site for a mailto: link or a visible email address (falling "
            "back to a Contact page if present). This roughly doubles run "
            "time and won't find an email for every lead — many small "
            "businesses have no website, or no email listed anywhere on it."
        )
        if not self.is_admin:
            enrich_hint_text += (
                f" Token cost: a lead with an email found costs 1 token per "
                f"{self.leads_per_token:g}, a phone-only lead costs half that, "
                f"and a lead with neither still costs a small amount (0.2 units)."
            )
        enrich_hint = QLabel(enrich_hint_text)
        enrich_hint.setWordWrap(True)
        enrich_hint.setStyleSheet("color: #7a8194; font-size: 11px;")
        enrich_layout.addWidget(enrich_hint)

        row.addWidget(enrich_group)

        # -- Output group
        output_group = QGroupBox("Output")
        form3 = QFormLayout(output_group)
        form3.setSpacing(10)

        output_row = QHBoxLayout()
        self.output_path_edit = QLineEdit()
        output_browse_btn = QPushButton("Browse…")
        output_browse_btn.setObjectName("secondary")
        output_browse_btn.clicked.connect(self._browse_output_path)
        output_row.addWidget(self.output_path_edit)
        output_row.addWidget(output_browse_btn)
        form3.addRow("CSV output path", output_row)

        self.sort_alpha_check = QCheckBox("Sort leads alphabetically by name (display && save)")
        self.sort_alpha_check.stateChanged.connect(self._on_sort_toggle)
        form3.addRow(self.sort_alpha_check)

        self.phone_region_combo = QComboBox()
        self.phone_region_combo.setEditable(True)
        self.phone_region_combo.addItems([
            "PH", "US", "IN", "GB", "AU", "CA", "SG", "MY", "ID", "TH",
            "VN", "AE", "SA", "NZ", "DE", "FR", "ES", "IT", "JP", "KR",
            "CN", "BR", "MX", "ZA", "NG",
        ])
        form3.addRow("Default phone region", self.phone_region_combo)

        phone_hint = QLabel(
            "2-letter country code used only for phone numbers written "
            "without a '+' country code (e.g. a local '0917 ...' number). "
            "Numbers that already include '+1', '+91', etc. are always "
            "parsed correctly regardless of this setting — this just "
            "resolves ambiguous local-format numbers for whichever "
            "country your queries are targeting."
        )
        phone_hint.setWordWrap(True)
        phone_hint.setStyleSheet("color: #7a8194; font-size: 11px;")
        form3.addRow(phone_hint)

        output_hint = QLabel(
            "Auto-saved after every run with a timestamp appended "
            "(e.g. leads_maps_2026-07-19_174230.csv) — never overwrites a previous run. "
            "'Export CSV' saves an extra copy wherever you choose. When "
            "alphabetical sorting is on, both the table and every saved CSV "
            "are ordered A→Z by lead name."
        )
        output_hint.setWordWrap(True)
        output_hint.setStyleSheet("color: #7a8194; font-size: 11px;")
        form3.addRow(output_hint)

        outer.addWidget(output_group)

        # -- Config file management
        cfg_group = QGroupBox("Configuration File")
        cfg_layout = QHBoxLayout(cfg_group)
        cfg_layout.setSpacing(10)

        save_cfg_btn = QPushButton("💾  Save Config As…")
        save_cfg_btn.clicked.connect(self.save_config_as)
        load_cfg_btn = QPushButton("📂  Load Config…")
        load_cfg_btn.setObjectName("secondary")
        load_cfg_btn.clicked.connect(self.load_config_from_dialog)
        reset_cfg_btn = QPushButton("↺  Reset to Defaults")
        reset_cfg_btn.setObjectName("secondary")
        reset_cfg_btn.clicked.connect(self.reset_config_to_defaults)

        cfg_layout.addWidget(save_cfg_btn)
        cfg_layout.addWidget(load_cfg_btn)
        cfg_layout.addWidget(reset_cfg_btn)
        cfg_layout.addStretch()
        self.config_path_label = QLabel(f"Auto-loads from: {APP_CONFIG_PATH}")
        self.config_path_label.setStyleSheet("color: #7a8194;")
        cfg_layout.addWidget(self.config_path_label)

        outer.addWidget(cfg_group)
        outer.addStretch()
        return root

    # -- Tab: Buy Tokens (user accounts only) --------------------------------

    def _build_buy_tokens_tab(self):
        root = QWidget()
        outer = QVBoxLayout(root)
        outer.setContentsMargins(18, 18, 18, 18)
        outer.setSpacing(14)

        row = QHBoxLayout()
        row.setSpacing(14)
        outer.addLayout(row)

        # -- Left: balance + how it works
        left_group = QGroupBox("Your Balance")
        left_layout = QVBoxLayout(left_group)
        left_layout.setSpacing(10)

        self.buy_balance_label = QLabel("")
        self.buy_balance_label.setStyleSheet(
            "font-size: 22px; font-weight: 700; color: #4c7cff;"
        )
        left_layout.addWidget(self.buy_balance_label)

        refresh_row = QHBoxLayout()
        refresh_balance_btn = QPushButton("🔄  Refresh Balance")
        refresh_balance_btn.setObjectName("secondary")
        refresh_balance_btn.clicked.connect(self.refresh_balance_from_backend)
        refresh_row.addWidget(refresh_balance_btn)
        refresh_row.addStretch()
        left_layout.addLayout(refresh_row)

        how_it_works = QLabel(
            f"Every scrape spends tokens based on what it actually finds:\n\n"
            f"•  A lead with an email found costs 1 token per {self.leads_per_token:g} leads\n"
            f"•  A lead with only a phone number costs half that\n"
            f"•  A lead with neither a phone nor an email still costs a small amount (0.2 units)\n\n"
            f"If your balance runs out partway through a run, the run stops "
            f"itself automatically and keeps whatever it already found."
        )
        how_it_works.setWordWrap(True)
        how_it_works.setStyleSheet("color: #b8bfcf;")
        left_layout.addWidget(how_it_works)
        left_layout.addStretch()

        row.addWidget(left_group, stretch=1)

        # -- Right: payment QR + instructions
        right_group = QGroupBox("Buy More Tokens")
        right_layout = QVBoxLayout(right_group)
        right_layout.setSpacing(10)

        self.qr_label = QLabel("No QR code has been set up yet — check back later "
                                "or contact the admin directly.")
        self.qr_label.setWordWrap(True)
        self.qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.qr_label.setMinimumHeight(220)
        self.qr_label.setStyleSheet(
            "background-color: #20242e; border: 1px solid #2f3542; "
            "border-radius: 8px; color: #7a8194;"
        )
        right_layout.addWidget(self.qr_label)

        self.payment_instructions_label = QLabel("")
        self.payment_instructions_label.setWordWrap(True)
        self.payment_instructions_label.setStyleSheet("color: #b8bfcf;")
        right_layout.addWidget(self.payment_instructions_label)

        refresh_qr_btn = QPushButton("🔄  Refresh Payment Info")
        refresh_qr_btn.setObjectName("secondary")
        refresh_qr_btn.clicked.connect(self.refresh_payment_info)
        right_layout.addWidget(refresh_qr_btn)

        row.addWidget(right_group, stretch=1)
        outer.addStretch()

        self._load_qr_and_instructions()
        return root

    def _load_qr_and_instructions(self):
        qr_url = self.backend_settings.get("qr_code_url")
        instructions = self.backend_settings.get("payment_instructions") or (
            "Scan the QR code, pay for the number of tokens you want, then "
            "message the admin with your proof of payment and the email you "
            "signed up with. Tokens are added manually after payment is "
            "confirmed — this can take a little while."
        )
        self.payment_instructions_label.setText(instructions)

        if qr_url:
            try:
                resp = requests.get(qr_url, timeout=10)
                if resp.status_code == 200:
                    pixmap = QPixmap()
                    if pixmap.loadFromData(resp.content):
                        self.qr_label.setPixmap(
                            pixmap.scaled(
                                240, 240,
                                Qt.AspectRatioMode.KeepAspectRatio,
                                Qt.TransformationMode.SmoothTransformation,
                            )
                        )
                        return
            except requests.RequestException:
                pass
        self.qr_label.setText(
            "No QR code has been set up yet — check back later or "
            "contact the admin directly."
        )

    def refresh_payment_info(self):
        self.backend_settings = fetch_settings(self.session.get("token")) or self.backend_settings
        self.leads_per_token = float(self.backend_settings.get("leads_per_token", 10.0) or 10.0)
        self._load_qr_and_instructions()
        self.statusBar().showMessage("Payment info refreshed.")

    def refresh_balance_from_backend(self):
        bal = refresh_balance(self.session.get("token"))
        if bal is None:
            self.statusBar().showMessage("Couldn't reach the server to refresh your balance.")
            return
        self.session["tokens_balance"] = bal
        self._update_balance_label()
        self.buy_balance_label.setText(
            f"{bal:g} token{'s' if bal != 1 else ''}  "
            f"(~{bal * self.leads_per_token:g} weighted leads)"
        )
        self.statusBar().showMessage("Balance refreshed.")

    # -- Tab: Admin (admin accounts only) ------------------------------------

    def _build_admin_tab(self):
        root = QWidget()
        outer = QVBoxLayout(root)
        outer.setContentsMargins(18, 18, 18, 18)
        outer.setSpacing(14)

        row = QHBoxLayout()
        row.setSpacing(14)
        outer.addLayout(row)

        # -- Credit tokens
        credit_group = QGroupBox("Credit Tokens To A User")
        credit_form = QFormLayout(credit_group)
        credit_form.setSpacing(10)

        self.credit_email_edit = QLineEdit()
        self.credit_email_edit.setPlaceholderText("user@example.com")
        credit_form.addRow("User email", self.credit_email_edit)

        self.credit_amount_spin = QDoubleSpinBox()
        self.credit_amount_spin.setRange(-100000, 100000)
        self.credit_amount_spin.setDecimals(2)
        self.credit_amount_spin.setValue(1.0)
        credit_form.addRow("Tokens to add (negative to correct)", self.credit_amount_spin)

        credit_btn = QPushButton("💰  Credit Tokens")
        credit_btn.clicked.connect(self._handle_credit_tokens)
        credit_form.addRow(credit_btn)

        credit_hint = QLabel(
            "This is the manual step: once you see the payment notification "
            "outside the app (bank/e-wallet SMS or message), enter the "
            "buyer's account email and how many tokens their payment covers, "
            "then click Credit Tokens."
        )
        credit_hint.setWordWrap(True)
        credit_hint.setStyleSheet("color: #7a8194; font-size: 11px;")
        credit_form.addRow(credit_hint)

        row.addWidget(credit_group)

        # -- Settings
        settings_group = QGroupBox("Token && Payment Settings")
        settings_form = QFormLayout(settings_group)
        settings_form.setSpacing(10)

        self.leads_per_token_spin = QDoubleSpinBox()
        self.leads_per_token_spin.setRange(0.1, 100000)
        self.leads_per_token_spin.setDecimals(2)
        self.leads_per_token_spin.setValue(self.leads_per_token)
        settings_form.addRow("Leads per token", self.leads_per_token_spin)

        self.qr_url_edit = QLineEdit()
        self.qr_url_edit.setPlaceholderText("https://example.com/payment-qr.png")
        self.qr_url_edit.setText(self.backend_settings.get("qr_code_url") or "")
        settings_form.addRow("QR code image URL", self.qr_url_edit)

        self.instructions_edit = QLineEdit()
        self.instructions_edit.setText(self.backend_settings.get("payment_instructions") or "")
        settings_form.addRow("Payment instructions", self.instructions_edit)

        save_settings_btn = QPushButton("💾  Save Settings")
        save_settings_btn.clicked.connect(self._handle_save_settings)
        settings_form.addRow(save_settings_btn)

        settings_hint = QLabel(
            "Host your QR code image anywhere reachable by URL (e.g. a "
            "Google Drive/Imgur direct-image link) and paste that link here "
            "— every user's app will pick it up next time they open or "
            "refresh the Buy Tokens tab."
        )
        settings_hint.setWordWrap(True)
        settings_hint.setStyleSheet("color: #7a8194; font-size: 11px;")
        settings_form.addRow(settings_hint)

        row.addWidget(settings_group)

        # -- Users table
        users_group = QGroupBox("Users")
        users_layout = QVBoxLayout(users_group)
        users_layout.setSpacing(10)

        refresh_users_row = QHBoxLayout()
        refresh_users_btn = QPushButton("🔄  Refresh Users")
        refresh_users_btn.setObjectName("secondary")
        refresh_users_btn.clicked.connect(self._refresh_users_table)
        refresh_users_row.addWidget(refresh_users_btn)
        refresh_users_row.addStretch()
        users_layout.addLayout(refresh_users_row)

        self.users_table = QTableWidget(0, 3)
        self.users_table.setHorizontalHeaderLabels(["Email", "Role", "Tokens Balance"])
        self.users_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.users_table.horizontalHeader().setStretchLastSection(True)
        pal2 = self.users_table.palette()
        pal2.setColor(QPalette.ColorRole.Base, QColor("#20242e"))
        pal2.setColor(QPalette.ColorRole.AlternateBase, QColor("#1c2027"))
        self.users_table.setPalette(pal2)
        users_layout.addWidget(self.users_table)

        outer.addWidget(users_group, stretch=1)

        self._refresh_users_table()
        return root

    def _handle_credit_tokens(self):
        email = self.credit_email_edit.text().strip()
        amount = self.credit_amount_spin.value()
        if not email:
            QMessageBox.warning(self, "Missing email", "Enter the user's account email.")
            return
        try:
            resp = requests.post(
                f"{os.environ.get('AUTH_API_URL', 'https://lead-scraper-auth.onrender.com')}/admin/credit-tokens",
                json={"email": email, "tokens": amount},
                headers={"Authorization": f"Bearer {self.session.get('token')}"},
                timeout=15,
            )
        except requests.RequestException as e:
            QMessageBox.critical(self, "Request failed", str(e))
            return
        if resp.status_code == 200:
            data = resp.json()
            self.statusBar().showMessage(
                f"Credited {amount:g} tokens to {data['email']} — new balance "
                f"{data['tokens_balance']:g}."
            )
            self._refresh_users_table()
        else:
            try:
                detail = resp.json().get("detail", "Failed")
            except Exception:
                detail = f"Failed ({resp.status_code})"
            QMessageBox.critical(self, "Credit failed", detail)

    def _handle_save_settings(self):
        payload = {
            "leads_per_token": self.leads_per_token_spin.value(),
            "qr_code_url": self.qr_url_edit.text().strip() or None,
            "payment_instructions": self.instructions_edit.text().strip() or None,
        }
        try:
            resp = requests.put(
                f"{os.environ.get('AUTH_API_URL', 'https://lead-scraper-auth.onrender.com')}/admin/settings",
                json=payload,
                headers={"Authorization": f"Bearer {self.session.get('token')}"},
                timeout=15,
            )
        except requests.RequestException as e:
            QMessageBox.critical(self, "Request failed", str(e))
            return
        if resp.status_code == 200:
            self.backend_settings = resp.json()
            self.leads_per_token = float(self.backend_settings.get("leads_per_token", 10.0) or 10.0)
            self.statusBar().showMessage("Settings saved.")
        else:
            try:
                detail = resp.json().get("detail", "Failed")
            except Exception:
                detail = f"Failed ({resp.status_code})"
            QMessageBox.critical(self, "Save failed", detail)

    def _refresh_users_table(self):
        try:
            resp = requests.get(
                f"{os.environ.get('AUTH_API_URL', 'https://lead-scraper-auth.onrender.com')}/admin/users",
                headers={"Authorization": f"Bearer {self.session.get('token')}"},
                timeout=15,
            )
        except requests.RequestException:
            self.statusBar().showMessage("Couldn't reach the server to load users.")
            return
        if resp.status_code != 200:
            self.statusBar().showMessage("Couldn't load users.")
            return
        users = resp.json()
        self.users_table.setRowCount(0)
        for u in users:
            r = self.users_table.rowCount()
            self.users_table.insertRow(r)
            self.users_table.setItem(r, 0, QTableWidgetItem(u["email"]))
            self.users_table.setItem(r, 1, QTableWidgetItem(u["role"]))
            self.users_table.setItem(r, 2, QTableWidgetItem(f"{u['tokens_balance']:g}"))

    @staticmethod
    def _label(text):
        lbl = QLabel(text)
        lbl.setObjectName("sectionLabel")
        return lbl

    # -- Role restrictions ----------------------------------------------------

    def _enforce_role_restrictions(self):
        """Regular user accounts always run headless and can't change it —
        only admins get the visible-browser option. Called after every
        place that could otherwise re-enable/uncheck it (initial load,
        config load, reset-to-defaults)."""
        if not self.is_admin:
            self.headless_check.setChecked(True)
            self.headless_check.setEnabled(False)
            self.headless_check.setToolTip(
                "User accounts always scrape headless. Admins can toggle this."
            )
            self.headless_hint.setText(
                "User accounts always run headless (no visible browser window) "
                "— this isn't configurable from a regular account. "
                + self.headless_hint.text()
            )

    # -- Query list management -----------------------------------------------

    def _populate_query_list(self, queries):
        self.query_list.clear()
        for q in queries:
            self.query_list.addItem(QListWidgetItem(q))

    def add_query(self):
        text = self.query_input.text().strip()
        if text:
            self.query_list.addItem(QListWidgetItem(text))
            self.query_input.clear()

    def remove_query(self):
        for item in self.query_list.selectedItems():
            self.query_list.takeItem(self.query_list.row(item))

    # -- Alphabetical sort toggle --------------------------------------------

    def _on_sort_toggle(self, _state=None):
        """Live-reflect the checkbox against whatever results are already
        on screen, so flipping it doesn't require re-running a scrape."""
        if not self.results_df.empty:
            self.repopulate_table(self._sort_df_if_needed(self.results_df))

    def _sort_df_if_needed(self, df: pd.DataFrame) -> pd.DataFrame:
        if df.empty or not self.sort_alpha_check.isChecked():
            return df
        return df.sort_values(
            by="name", key=lambda col: col.str.lower(), kind="stable"
        ).reset_index(drop=True)

    # -- Configuration <-> UI --------------------------------------------------

    def _apply_config_to_fields(self, cfg):
        self.max_scrolls_spin.setValue(cfg["max_scrolls_per_query"])
        self.scroll_wait_spin.setValue(cfg["scroll_wait_ms"])
        self.stale_rounds_spin.setValue(cfg["stale_rounds_threshold"])
        self.max_concurrent_spin.setValue(cfg["max_concurrent_tabs"])
        self.query_timeout_spin.setValue(cfg["query_timeout_seconds"])
        self.headless_check.setChecked(cfg["headless"])
        self.close_after_check.setChecked(cfg["close_after_run"])
        self.debug_port_spin.setValue(cfg["chrome_debug_port"])
        self.chrome_exe_edit.setText(cfg["chrome_executable"])
        self.profile_dir_edit.setText(cfg["user_data_dir"])
        self.output_path_edit.setText(cfg["output_path"])
        self.sort_alpha_check.setChecked(cfg.get("sort_alphabetically", False))
        self.phone_region_combo.setCurrentText(cfg.get("phone_default_region", "PH"))
        self.enable_email_check.setChecked(cfg.get("enable_email_enrichment", False))
        self.email_concurrent_spin.setValue(cfg.get("email_max_concurrent_tabs", 5))
        self.website_timeout_spin.setValue(cfg.get("website_timeout_seconds", 15))
        self._enforce_role_restrictions()

    def _gather_config_from_fields(self):
        queries = [self.query_list.item(i).text() for i in range(self.query_list.count())]
        return {
            "queries": queries,
            "max_scrolls_per_query": self.max_scrolls_spin.value(),
            "scroll_wait_ms": self.scroll_wait_spin.value(),
            "max_concurrent_tabs": self.max_concurrent_spin.value(),
            "query_timeout_seconds": self.query_timeout_spin.value(),
            "stale_rounds_threshold": self.stale_rounds_spin.value(),
            "headless": True if not self.is_admin else self.headless_check.isChecked(),
            "close_after_run": self.close_after_check.isChecked(),
            "chrome_debug_port": self.debug_port_spin.value(),
            "chrome_executable": self.chrome_exe_edit.text().strip() or detect_chrome_executable(),
            "user_data_dir": self.profile_dir_edit.text().strip() or detect_default_profile_dir(),
            "output_path": self.output_path_edit.text().strip() or DEFAULT_CONFIG["output_path"],
            "sort_alphabetically": self.sort_alpha_check.isChecked(),
            "phone_default_region": self.phone_region_combo.currentText().strip().upper() or "PH",
            "enable_email_enrichment": self.enable_email_check.isChecked(),
            "email_max_concurrent_tabs": self.email_concurrent_spin.value(),
            "website_timeout_seconds": self.website_timeout_spin.value(),
            "role": self.session.get("role", "user"),
        }

    def auto_detect_chrome_settings(self, silent=False):
        """Re-run detection and refresh the read-only Chrome fields. Also
        called once on startup so the fields are populated before the user
        even opens the Configuration tab."""
        self.chrome_exe_edit.setText(detect_chrome_executable())
        self.profile_dir_edit.setText(detect_default_profile_dir())
        self.debug_port_spin.setValue(find_free_port(preferred=self.debug_port_spin.value() or 9222))
        if not silent:
            self.statusBar().showMessage("Chrome settings re-detected.")

    def _browse_output_path(self):
        path, _ = QFileDialog.getSaveFileName(self, "Select CSV output path",
                                               self.output_path_edit.text(), "CSV Files (*.csv)")
        if path:
            self.output_path_edit.setText(path)

    def save_config_as(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save configuration", APP_CONFIG_PATH,
                                               "JSON Files (*.json)")
        if path:
            self._save_config(path)

    def _save_config(self, path):
        cfg = self._gather_config_from_fields()
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2, ensure_ascii=False)
            self.statusBar().showMessage(f"Configuration saved to {path}")
        except Exception as e:
            QMessageBox.critical(self, "Save failed", str(e))

    def load_config_from_dialog(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load configuration", APP_CONFIG_PATH,
                                               "JSON Files (*.json)")
        if path:
            self._load_config(path)

    def _load_config(self, path, silent=False):
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            merged = dict(DEFAULT_CONFIG)
            merged.update(cfg)
            self.config = merged
            self._populate_query_list(merged["queries"])
            self._apply_config_to_fields(merged)
            if not silent:
                self.statusBar().showMessage(f"Configuration loaded from {path}")
        except Exception as e:
            if not silent:
                QMessageBox.critical(self, "Load failed", str(e))

    def reset_config_to_defaults(self):
        self.config = dict(DEFAULT_CONFIG)
        self._populate_query_list(self.config["queries"])
        self._apply_config_to_fields(self.config)
        self.statusBar().showMessage("Configuration reset to defaults")

    # -- Scrape control -----------------------------------------------------

    def start_scrape(self):
        self.export_btn.setEnabled(False)
        cfg = self._gather_config_from_fields()
        if not cfg["queries"]:
            QMessageBox.warning(self, "No queries", "Add at least one search query first.")
            return

        # Token budget: admins are unlimited; users are capped at whatever
        # their current balance converts to in weighted lead-units. Every
        # lead now costs at least 0.2 units, so a 0-balance user's run will
        # stop itself almost immediately, on the very first card found.
        if self.is_admin:
            cfg["token_budget_leads"] = None
        else:
            bal = self.session.get("tokens_balance", 0.0)
            if bal <= 0:
                QMessageBox.warning(
                    self, "No tokens left",
                    "Your token balance is 0. Every lead costs at least a small "
                    "amount now, so there's no way to run a scrape and keep "
                    "anything from it. Buy more tokens from the Buy Tokens tab, "
                    "or ask your admin to credit your account.",
                )
                return
            cfg["token_budget_leads"] = bal * self.leads_per_token

        self.table.setRowCount(0)
        self.results_df = pd.DataFrame(columns=COLUMNS)
        self.results_count_label.setText("0 leads")
        self.log_box.clear()

        self.progress_bar.setRange(0, len(cfg["queries"]))
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat(f"0 / {len(cfg['queries'])} queries")

        self.enrich_progress_bar.setRange(0, 1)
        self.enrich_progress_bar.setValue(0)
        self.enrich_progress_bar.setFormat(
            "Email enrichment: enabled, pending" if cfg["enable_email_enrichment"]
            else "Email enrichment: disabled"
        )

        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.statusBar().showMessage("Scraping…")

        self.worker = ScraperWorker(cfg)
        self.worker.log.connect(self.append_log)
        self.worker.query_detail.connect(self.append_log)
        self.worker.card_found.connect(self.add_row)
        self.worker.query_progress.connect(self.update_progress)
        self.worker.enrich_progress.connect(self.update_enrich_progress)
        self.worker.finished_ok.connect(self.on_finished)
        self.worker.failed.connect(self.on_failed)
        self.worker.finished.connect(self.on_thread_finished)
        self.worker.start()

    def stop_scrape(self):
        if self.worker is not None:
            self.append_log("Stopping — will finish in-flight scrolls then halt…")
            self.worker.request_stop()
            self.stop_btn.setEnabled(False)

    def update_progress(self, current, total, query):
        self.progress_bar.setValue(current)
        self.progress_bar.setFormat(f"{current} / {total} queries — last: {query}")
        self.statusBar().showMessage(f"Completed {current}/{total}: {query}")

    def update_enrich_progress(self, current, total, name):
        if self.enrich_progress_bar.maximum() != total:
            self.enrich_progress_bar.setRange(0, max(total, 1))
        self.enrich_progress_bar.setValue(current)
        self.enrich_progress_bar.setFormat(f"Email enrichment: {current}/{total} — last: {name}")

    def append_log(self, text):
        self.log_box.appendPlainText(text)

    def add_row(self, card: dict):
        self.table.blockSignals(True)
        self.table.setSortingEnabled(False)
        row = self.table.rowCount()
        self.table.insertRow(row)
        keep_item = QTableWidgetItem()
        keep_item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
        keep_item.setCheckState(Qt.CheckState.Checked)
        self.table.setItem(row, 0, keep_item)
        for col, key in enumerate(COLUMNS):
            self.table.setItem(row, col + 1, QTableWidgetItem(str(card.get(key, ""))))
        self.table.horizontalHeader().setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        self.table.setSortingEnabled(True)
        self.table.blockSignals(False)
        self.results_count_label.setText(f"{row + 1} leads (raw, before dedup)")

    def on_finished(self, df: pd.DataFrame):
        df = self._sort_df_if_needed(df)
        self.results_df = df
        self.repopulate_table(df)
        self.results_count_label.setText(f"{len(df)} leads (deduped) — select which to keep below")
        self.statusBar().showMessage(f"Done — {len(df)} unique leads found. Review and confirm your selection.")
        self.export_btn.setEnabled(False)   # ← add this
        
    def on_failed(self, message):
        QMessageBox.critical(self, "Scrape failed", message)
        self.statusBar().showMessage("Failed.")

    def on_thread_finished(self):
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)

    def repopulate_table(self, df: pd.DataFrame):
        self.table.blockSignals(True)
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        for _, row in df.iterrows():
            r = self.table.rowCount()
            self.table.insertRow(r)
            keep_item = QTableWidgetItem()
            keep_item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            keep_item.setCheckState(Qt.CheckState.Checked)
            self.table.setItem(r, 0, keep_item)
            for col, key in enumerate(COLUMNS):
                self.table.setItem(r, col + 1, QTableWidgetItem(str(row.get(key, ""))))
        self.table.horizontalHeader().setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        self.table.setSortingEnabled(True)
        self.table.blockSignals(False)
        self._update_selection_summary()

    def _get_checked_row_indices(self):
        rows = []
        for r in range(self.table.rowCount()):
            item = self.table.item(r, 0)
            if item is not None and item.checkState() == Qt.CheckState.Checked:
                rows.append(r)
        return rows

    def _set_all_checked(self, checked: bool):
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        self.table.blockSignals(True)
        for r in range(self.table.rowCount()):
            item = self.table.item(r, 0)
            if item is not None:
                item.setCheckState(state)
        self.table.blockSignals(False)
        self._update_selection_summary()

    def _on_table_item_changed(self, item):
        if item.column() == 0:
            self._update_selection_summary()

    def _update_selection_summary(self):
        if self.results_df.empty:
            self.selection_cost_label.setText("")
            self.confirm_selection_btn.setEnabled(False)
            return

        checked_rows = self._get_checked_row_indices()
        units = 0.0
        for r in checked_rows:
            row_dict = {key: self.table.item(r, col + 1).text() for col, key in enumerate(COLUMNS)}
            units += lead_cost_units(row_dict)

        if self.is_admin:
            self.selection_cost_label.setText(f"{len(checked_rows)} leads selected (admin — free)")
            self.selection_cost_label.setStyleSheet("color: #4c7cff; font-weight: 600;")
            self.confirm_selection_btn.setEnabled(len(checked_rows) > 0)
        else:
            cost = units / self.leads_per_token if self.leads_per_token else 0.0
            bal = self.session.get("tokens_balance", 0.0)
            over = cost > bal
            color = "#e5484d" if over else "#4c7cff"
            self.selection_cost_label.setText(
                f"{len(checked_rows)} leads selected — costs {cost:.2f} tokens (balance: {bal:g})"
            )
            self.selection_cost_label.setStyleSheet(f"color: {color}; font-weight: 600;")
            self.confirm_selection_btn.setEnabled(len(checked_rows) > 0 and not over)

    def confirm_selection(self):
        checked_rows = self._get_checked_row_indices()
        if not checked_rows:
            QMessageBox.information(self, "Nothing selected", "Check at least one lead to keep.")
            return

        kept_df = self.results_df.iloc[checked_rows].reset_index(drop=True)
        units = total_cost_units(kept_df)

        if not self.is_admin:
            token_cost = units / self.leads_per_token if self.leads_per_token else 0.0
            bal = self.session.get("tokens_balance", 0.0)
            if token_cost > bal:
                QMessageBox.warning(
                    self, "Not enough tokens",
                    f"Selected leads cost {token_cost:.2f} tokens but you only have "
                    f"{bal:g}. Uncheck some leads or buy more tokens.",
                )
                return
            new_balance = consume_tokens(self.session.get("token"), token_cost)
            if new_balance is not None:
                self.session["tokens_balance"] = new_balance
                self._update_balance_label()
                self.append_log(
                    f"Charged {token_cost:.2f} tokens for {len(kept_df)} selected leads "
                    f"({units:g} weighted lead-units) — new balance {new_balance:g}."
                )
            else:
                self.append_log(
                    "Couldn't reach the server to charge tokens for this selection — "
                    "your on-screen balance may be stale."
                )

        # dedented — now runs for admins too
        self.results_df = kept_df
        self.repopulate_table(kept_df)
        self.results_count_label.setText(f"{len(kept_df)} leads kept")
        self.confirm_selection_btn.setEnabled(False)
        self.export_btn.setEnabled(True)

        self.statusBar().showMessage(f"Kept {len(kept_df)} leads. Use Export CSV to save them.")
    # -- Export ---------------------------------------------------------------

    def export_csv(self):
        if self.results_df.empty:
            QMessageBox.information(self, "Nothing to export", "Run a scrape first.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save leads as CSV", "leads.csv", "CSV Files (*.csv)")
        if path:
            df = self._sort_df_if_needed(self.results_df)
            df.to_csv(path, index=False, encoding="utf-8-sig")
            self.statusBar().showMessage(f"Saved to {path}")

    # -- Cleanup ----------------------------------------------------------------

    def closeEvent(self, event):
        if self.worker is not None and self.worker.isRunning():
            self.worker.request_stop()
            self.worker.wait(3000)
        # Persist current settings for next launch.
        try:
            self._save_config(APP_CONFIG_PATH)
        except Exception:
            pass
        event.accept()

    def handle_logout(self):
        if self.worker is not None and self.worker.isRunning():
            QMessageBox.warning(self, "Scrape in progress",
                                 "Stop the current scrape before logging out.")
            return
        confirm = QMessageBox.question(
            self, "Log out",
            "Log out of this account? You'll need to sign in again to use the app.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        logout()
        QApplication.instance().exit(RESTART_CODE)

RESTART_CODE = 1000

def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    while True:
        session = require_login(app)
        if session is None:
            sys.exit(0)

        window = MainWindow(session)
        window.show()
        exit_code = app.exec()

        window.close()  # <-- add this line

        if exit_code != RESTART_CODE:
            sys.exit(exit_code)
        # else: loop back and show the login dialog again, same QApplication

if __name__ == "__main__":
    main()