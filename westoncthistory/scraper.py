#!/usr/bin/env python3
"""
westoncthistory.org archival mirror scraper
=============================================

Crawls https://westoncthistory.org/ (or any start URL on that domain),
downloads every internal page and asset it can find, rewrites all
internal links so the result works when browsed offline, and writes a
crawl report / error log when it's done.

This tool exists to build a local reference copy of the site for a
content migration project. It identifies itself with a descriptive
User-Agent and defaults to a polite 1 request/second crawl rate.

Usage:
    python scraper.py
    python scraper.py --start-url https://westoncthistory.org/ --delay 1.0
    python scraper.py --output-dir ../westoncthistory-mirror --max-pages 500

See README.md for details.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from urllib.parse import (
    urljoin,
    urlparse,
    urlunparse,
    parse_qsl,
    unquote,
)
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------
# Configuration defaults
# --------------------------------------------------------------------------

DEFAULT_START_URL = "https://westoncthistory.org/"
DEFAULT_OUTPUT_DIR = "westoncthistory-mirror"
DEFAULT_DELAY_SECONDS = 1.0
DEFAULT_TIMEOUT = (10, 25)  # (connect, read) seconds
DEFAULT_MAX_PAGES = 5000  # safety cap so a bug can't crawl forever

DEFAULT_CONTACT_EMAIL = "stephen@youell.net"
DEFAULT_USER_AGENT = (
    "WestonCTHistoryArchivalMirrorBot/1.0 "
    "(+Archival crawl for site migration reference; "
    "run on behalf of the site owner; contact: {contact})"
)

# Extensions we treat as "pages" to parse for links (HTML-ish content).
# Anything else with a recognizable extension is treated as an asset.
PAGE_EXTENSIONS = {"", ".php", ".html", ".htm", ".asp", ".aspx", ".jsp", ".cfm"}

# Schemes we never try to fetch.
SKIPPED_SCHEMES = {"mailto", "tel", "javascript", "data", "sms", "fax"}

# Attributes worth inspecting per tag, and what kind of resource they hold.
# kind is one of: "page", "asset", "css", "iframe"
LINK_ATTRS = [
    ("a", "href", "auto"),       # page or asset depending on extension
    ("img", "src", "asset"),
    ("img", "data-src", "asset"),  # common lazy-load pattern
    ("source", "src", "asset"),
    ("script", "src", "asset"),
    ("link", "href", "link-auto"),  # stylesheet/icon vs. other <link> rels
    ("embed", "src", "asset"),
    ("area", "href", "auto"),
    ("iframe", "src", "iframe"),
]

CSS_URL_RE = re.compile(r"url\(\s*(['\"]?)(?P<url>[^'\")]+)\1\s*\)", re.IGNORECASE)


# --------------------------------------------------------------------------
# Small data holders
# --------------------------------------------------------------------------

@dataclass
class Task:
    url: str
    kind: str          # "page", "asset", "css"
    referrer: str = ""


@dataclass
class CrawlStats:
    pages_ok: dict = field(default_factory=dict)     # url -> local path (posix str)
    assets_ok: dict = field(default_factory=dict)    # url -> local path (posix str)
    errors: list = field(default_factory=list)       # (url, referrer, reason)
    external_skipped: list = field(default_factory=list)  # (url, referrer, reason)
    redirects: list = field(default_factory=list)    # (from_url, to_url)
    robots_disallowed: list = field(default_factory=list)  # (url, referrer)


# --------------------------------------------------------------------------
# URL helpers
# --------------------------------------------------------------------------

def normalize_host(host: str) -> str:
    """Fold www.<domain> and <domain> together for comparison purposes."""
    host = (host or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def strip_fragment(url: str) -> str:
    parts = urlparse(url)
    return urlunparse(parts._replace(fragment=""))


def sanitize_component(text: str) -> str:
    """Make a string safe to use as a single path component / filename."""
    text = unquote(text)
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text or "_"


class Mirror:
    """Everything to do with mapping site URLs to local files on disk."""

    def __init__(self, output_dir: Path, allowed_domain: str):
        self.output_dir = output_dir
        self.allowed_domain = allowed_domain

    def is_internal(self, url: str) -> bool:
        scheme = urlparse(url).scheme
        if scheme and scheme not in ("http", "https"):
            return False
        host = normalize_host(urlparse(url).netloc)
        return host == self.allowed_domain

    # -- page URLs, e.g. /about-us.php?pageid=210 -> about-us/pageid-210.html --

    def page_local_path(self, url: str) -> PurePosixPath:
        parsed = urlparse(url)
        segments = [s for s in unquote(parsed.path).split("/") if s]

        if not segments:
            dir_segments: list[str] = []
        else:
            last = segments[-1]
            stem, ext = os.path.splitext(last)
            if ext.lower() in PAGE_EXTENSIONS:
                dir_segments = segments[:-1] + ([stem] if stem else [])
            else:
                # A "page" URL with an unrecognized extension - keep the
                # full path as directories rather than lose information.
                dir_segments = segments[:-1] + [last]
            dir_segments = [sanitize_component(s) for s in dir_segments]

        if parsed.query:
            pairs = parse_qsl(parsed.query, keep_blank_values=True)
            q_str = sanitize_component(
                "-".join(f"{k}-{v}" if v != "" else f"{k}" for k, v in pairs)
            )
            filename = f"{q_str}.html"
        else:
            filename = "index.html"

        return PurePosixPath(*dir_segments, filename) if dir_segments else PurePosixPath(filename)

    # -- asset URLs, e.g. /img/logo.png -> img/logo.png (structure preserved) --

    def asset_local_path(self, url: str) -> PurePosixPath:
        parsed = urlparse(url)
        segments = [sanitize_component(s) for s in unquote(parsed.path).split("/") if s]
        if not segments:
            segments = ["file"]

        if parsed.query:
            stem, ext = os.path.splitext(segments[-1])
            q_str = sanitize_component(parsed.query)
            segments[-1] = f"{stem}__{q_str}{ext}" if ext else f"{segments[-1]}__{q_str}"

        return PurePosixPath(*segments)

    def local_path_for(self, url: str, kind: str) -> PurePosixPath:
        if kind == "page":
            return self.page_local_path(url)
        return self.asset_local_path(url)

    def disk_path(self, rel_path: PurePosixPath) -> Path:
        return self.output_dir / Path(*rel_path.parts)

    @staticmethod
    def relative_href(from_rel: PurePosixPath, to_rel: PurePosixPath) -> str:
        """Relative link from the file at from_rel to the file at to_rel."""
        rel = os.path.relpath(str(to_rel), start=str(from_rel.parent) if from_rel.parent != PurePosixPath(".") else ".")
        return rel.replace(os.sep, "/")


# --------------------------------------------------------------------------
# The crawler itself
# --------------------------------------------------------------------------

class SiteMirrorCrawler:
    def __init__(
        self,
        start_url: str,
        output_dir: Path,
        delay: float,
        timeout,
        user_agent: str,
        max_pages: int,
        logger: logging.Logger,
        respect_robots: bool = True,
    ):
        self.start_url = strip_fragment(start_url)
        self.allowed_domain = normalize_host(urlparse(self.start_url).netloc)
        self.mirror = Mirror(output_dir, self.allowed_domain)
        self.delay = delay
        self.timeout = timeout
        self.max_pages = max_pages
        self.log = logger

        self.user_agent = user_agent
        # robots.txt matches rules against the leading product token of the
        # UA (everything before the first "/"), same as most crawlers.
        self.robots_token = user_agent.split("/")[0].strip()
        self.respect_robots = respect_robots
        self.robots = RobotFileParser()

        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent})

        self.queue: deque[Task] = deque()
        self.queued_or_done: set[str] = set()  # normalized urls already queued/handled
        self.stats = CrawlStats()
        self._last_request_time = 0.0

    # -- polite fetching --------------------------------------------------

    def _throttle(self):
        elapsed = time.monotonic() - self._last_request_time
        wait = self.delay - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_request_time = time.monotonic()

    def _get(self, url: str):
        self._throttle()
        return self.session.get(url, timeout=self.timeout, allow_redirects=True)

    # -- robots.txt ----------------------------------------------------

    def load_robots_txt(self):
        """Fetch and parse robots.txt for the start URL's host, if any.
        Also picks up a Crawl-delay directive (raising --delay if the
        site asks for something slower). Best-effort: if robots.txt can't
        be fetched at all, we proceed as if everything is allowed."""
        if not self.respect_robots:
            self.log.info("robots.txt checking disabled (--ignore-robots)")
            return

        robots_url = urljoin(self.start_url, "/robots.txt")
        try:
            resp = self._get(robots_url)
        except requests.RequestException as exc:
            self.log.warning(
                "robots.txt unreachable (%s) - proceeding without robots.txt restrictions", exc
            )
            return

        if resp.status_code == 200:
            self.robots.parse(resp.text.splitlines())
            self.log.info("Loaded robots.txt from %s (User-agent token: %s)", robots_url, self.robots_token)

            delay = self.robots.crawl_delay(self.robots_token)
            if delay:
                delay = float(delay)
                if delay > self.delay:
                    self.log.info(
                        "robots.txt specifies Crawl-delay: %.1fs - raising delay from %.1fs",
                        delay,
                        self.delay,
                    )
                    self.delay = delay
        elif resp.status_code in (401, 403):
            # RFC 9309: treat an unreadable-due-to-auth robots.txt as a
            # blanket disallow, same as major crawlers do.
            self.log.warning(
                "robots.txt returned HTTP %s - treating as full disallow", resp.status_code
            )
            self.robots.parse(["User-agent: *", "Disallow: /"])
        else:
            self.log.info(
                "No robots.txt found (HTTP %s) - proceeding without robots.txt restrictions",
                resp.status_code,
            )

    # -- classification ----------------------------------------------------

    def classify_link(self, url: str) -> str:
        """Return 'page' or 'asset' for an <a href> / generic link target."""
        path = urlparse(url).path
        _, ext = os.path.splitext(path)
        if ext.lower() in PAGE_EXTENSIONS:
            return "page"
        return "asset"

    # -- enqueue helpers ----------------------------------------------------

    def _dedupe_key(self, url: str) -> str:
        parsed = urlparse(url)
        return urlunparse(
            (
                "https",
                normalize_host(parsed.netloc),
                parsed.path,
                "",
                parsed.query,
                "",
            )
        )

    def maybe_enqueue(self, url: str, kind: str, referrer: str) -> bool:
        """Queue an internal URL for fetching if we haven't seen it yet.
        Returns True if it was (or already had been) queued as internal,
        False if it's not internal or robots.txt disallows fetching it."""
        key = self._dedupe_key(url)
        if key in self.queued_or_done:
            return True
        if not self.mirror.is_internal(url):
            return False
        if self.respect_robots and not self.robots.can_fetch(self.robots_token, url):
            self.queued_or_done.add(key)
            self.stats.robots_disallowed.append((url, referrer))
            self.log.info("SKIP   %s (disallowed by robots.txt)", url)
            return False
        self.queued_or_done.add(key)
        self.queue.append(Task(url=url, kind=kind, referrer=referrer))
        return True

    def note_external_or_skipped(self, url: str, referrer: str, reason: str):
        key = self._dedupe_key(url) + f"|{reason}"
        if key in self.queued_or_done:
            return
        self.queued_or_done.add(key)
        self.stats.external_skipped.append((url, referrer, reason))

    # -- link resolution / rewriting ----------------------------------------

    def resolve_and_route(
        self,
        raw_url: str,
        base_url: str,
        referrer: str,
        forced_kind: str | None,
        referrer_kind: str = "page",
    ):
        """Given a raw href/src found on `referrer`, decide what to do with
        it and return the string that should replace it in the HTML/CSS
        (or None to leave it untouched)."""
        raw_url = (raw_url or "").strip()
        if not raw_url or raw_url.startswith("#"):
            return None  # same-page anchor, nothing to do

        scheme = urlparse(raw_url).scheme
        if scheme in SKIPPED_SCHEMES:
            self.note_external_or_skipped(raw_url, referrer, f"skipped scheme ({scheme})")
            return None

        absolute = strip_fragment(urljoin(base_url, raw_url))
        fragment = urlparse(urljoin(base_url, raw_url)).fragment

        if not self.mirror.is_internal(absolute):
            self.note_external_or_skipped(absolute, referrer, "external domain")
            return None  # leave the original (absolute) URL in place

        if forced_kind == "iframe":
            # Embedded widgets/calendars etc: don't follow even if internal,
            # per crawl policy - just note it and move on.
            self.note_external_or_skipped(absolute, referrer, "iframe (not followed)")
            return None

        kind = forced_kind if forced_kind not in (None, "auto") else self.classify_link(absolute)
        if kind == "link-auto":
            kind = self.classify_link(absolute)
        if kind == "asset" and absolute.lower().split("?")[0].endswith(".css"):
            kind = "css"

        enqueued = self.maybe_enqueue(absolute, kind, referrer)
        if not enqueued:
            # Internal but robots.txt disallows fetching it: leave a live,
            # absolute link to the real page rather than a relative path
            # to a local file we were told not to download.
            return absolute + (f"#{fragment}" if fragment else "")

        target_rel = self.mirror.local_path_for(absolute, "page" if kind == "page" else "asset")
        referrer_rel = (
            self.mirror.local_path_for(referrer, referrer_kind)
            if referrer
            else PurePosixPath("index.html")
        )
        href = Mirror.relative_href(referrer_rel, target_rel)
        if fragment:
            href += f"#{fragment}"
        return href

    # -- page processing ------------------------------------------------

    def process_page(self, task: Task):
        url = task.url
        try:
            resp = self._get(url)
        except requests.RequestException as exc:
            self.stats.errors.append((url, task.referrer, f"request failed: {exc}"))
            self.log.warning("ERROR  %s (%s)", url, exc)
            return

        final_url = strip_fragment(resp.url)
        if final_url != strip_fragment(url):
            self.stats.redirects.append((url, final_url))
            if not self.mirror.is_internal(final_url):
                self.note_external_or_skipped(final_url, url, "redirected off-domain")
                self.log.info("REDIRECT->EXTERNAL %s -> %s", url, final_url)
                return

        if resp.status_code >= 400:
            self.stats.errors.append((url, task.referrer, f"HTTP {resp.status_code}"))
            self.log.warning("ERROR  %s -> HTTP %s", url, resp.status_code)
            return

        content_type = resp.headers.get("Content-Type", "")
        if "text/html" not in content_type and "application/xhtml" not in content_type:
            # Turned out not to be an HTML page after all (e.g. a .php
            # endpoint that streams a file) - handle as a raw asset instead.
            self._save_binary(final_url, resp.content, task.referrer)
            return

        soup = BeautifulSoup(resp.text, "html.parser")
        self._rewrite_links(soup, final_url)

        rel_path = self.mirror.page_local_path(final_url)
        disk_path = self.mirror.disk_path(rel_path)
        self._write_file(disk_path, str(soup).encode("utf-8"), url, task.referrer)
        if disk_path.exists():
            self.stats.pages_ok[final_url] = str(rel_path)
            self.log.info("OK     %s -> %s", final_url, rel_path)

    def _rewrite_links(self, soup: BeautifulSoup, page_url: str):
        for tag_name, attr, forced_kind in LINK_ATTRS:
            for tag in soup.find_all(tag_name):
                if not tag.has_attr(attr):
                    continue
                if forced_kind == "link-auto":
                    rel = " ".join(tag.get("rel", [])).lower()
                    if any(r in rel for r in ("stylesheet", "icon")):
                        kind = "auto"
                    else:
                        # e.g. rel="canonical", rel="alternate", rel="dns-prefetch"
                        continue
                else:
                    kind = forced_kind

                new_href = self.resolve_and_route(tag[attr], page_url, page_url, kind)
                if new_href is not None:
                    tag[attr] = new_href

        # Inline <style> blocks and style="" attributes can reference
        # background images/fonts via url(...).
        for style_tag in soup.find_all("style"):
            if style_tag.string:
                style_tag.string.replace_with(
                    self._rewrite_css_text(style_tag.string, page_url, referrer_kind="page")
                )
        for tag in soup.find_all(style=True):
            tag["style"] = self._rewrite_css_text(tag["style"], page_url, referrer_kind="page")

    # -- CSS processing ----------------------------------------------------

    def process_css(self, task: Task):
        url = task.url
        try:
            resp = self._get(url)
        except requests.RequestException as exc:
            self.stats.errors.append((url, task.referrer, f"request failed: {exc}"))
            self.log.warning("ERROR  %s (%s)", url, exc)
            return

        if resp.status_code >= 400:
            self.stats.errors.append((url, task.referrer, f"HTTP {resp.status_code}"))
            self.log.warning("ERROR  %s -> HTTP %s", url, resp.status_code)
            return

        final_url = strip_fragment(resp.url)
        rewritten = self._rewrite_css_text(resp.text, final_url, referrer_kind="asset")
        rel_path = self.mirror.asset_local_path(final_url)
        disk_path = self.mirror.disk_path(rel_path)
        self._write_file(disk_path, rewritten.encode("utf-8"), url, task.referrer)
        if disk_path.exists():
            self.stats.assets_ok[final_url] = str(rel_path)
            self.log.info("OK     %s -> %s (css)", final_url, rel_path)

    def _rewrite_css_text(self, css_text: str, css_url: str, referrer_kind: str = "asset") -> str:
        def repl(match: re.Match) -> str:
            raw = match.group("url").strip()
            if raw.startswith("data:"):
                return match.group(0)
            new_href = self.resolve_and_route(
                raw, css_url, css_url, "asset", referrer_kind=referrer_kind
            )
            if new_href is None:
                return match.group(0)
            return f'url("{new_href}")'

        return CSS_URL_RE.sub(repl, css_text)

    # -- generic asset processing -------------------------------------------

    def process_asset(self, task: Task):
        url = task.url
        try:
            resp = self._get(url)
        except requests.RequestException as exc:
            self.stats.errors.append((url, task.referrer, f"request failed: {exc}"))
            self.log.warning("ERROR  %s (%s)", url, exc)
            return

        if resp.status_code >= 400:
            self.stats.errors.append((url, task.referrer, f"HTTP {resp.status_code}"))
            self.log.warning("ERROR  %s -> HTTP %s", url, resp.status_code)
            return

        final_url = strip_fragment(resp.url)
        self._save_binary(final_url, resp.content, task.referrer, original_url=url)

    def _save_binary(self, url: str, content: bytes, referrer: str, original_url: str | None = None):
        rel_path = self.mirror.asset_local_path(url)
        disk_path = self.mirror.disk_path(rel_path)
        self._write_file(disk_path, content, original_url or url, referrer)
        if disk_path.exists():
            self.stats.assets_ok[url] = str(rel_path)
            self.log.info("OK     %s -> %s", url, rel_path)

    # -- disk I/O -----------------------------------------------------------

    def _write_file(self, disk_path: Path, data: bytes, url: str, referrer: str):
        try:
            disk_path.parent.mkdir(parents=True, exist_ok=True)
            disk_path.write_bytes(data)
        except OSError as exc:
            self.stats.errors.append((url, referrer, f"could not write {disk_path}: {exc}"))
            self.log.warning("ERROR  writing %s (%s)", disk_path, exc)

    # -- main loop ------------------------------------------------------

    def run(self):
        self.log.info("Starting crawl of %s (allowed domain: %s)", self.start_url, self.allowed_domain)
        self.load_robots_txt()

        if not self.maybe_enqueue(self.start_url, "page", ""):
            self.log.warning(
                "Start URL %s is disallowed by robots.txt - nothing to crawl. "
                "Pass --ignore-robots to override.",
                self.start_url,
            )
            return

        pages_seen = 0
        while self.queue:
            if pages_seen >= self.max_pages:
                self.log.warning(
                    "Reached --max-pages limit (%d); stopping crawl early.", self.max_pages
                )
                break
            task = self.queue.popleft()
            pages_seen += 1

            if task.kind == "page":
                self.process_page(task)
            elif task.kind == "css":
                self.process_css(task)
            else:
                self.process_asset(task)

        self.log.info(
            "Crawl finished: %d pages, %d assets, %d errors, %d external/skipped links, "
            "%d disallowed by robots.txt",
            len(self.stats.pages_ok),
            len(self.stats.assets_ok),
            len(self.stats.errors),
            len(self.stats.external_skipped),
            len(self.stats.robots_disallowed),
        )


# --------------------------------------------------------------------------
# Report generation
# --------------------------------------------------------------------------

def write_report(stats: CrawlStats, output_dir: Path, start_url: str):
    report_path = output_dir / "crawl-report.txt"
    lines = []
    lines.append("westoncthistory.org archival mirror - crawl report")
    lines.append("=" * 60)
    lines.append(f"Start URL:        {start_url}")
    lines.append(f"Generated:        {time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    lines.append(f"Pages saved:      {len(stats.pages_ok)}")
    lines.append(f"Assets saved:     {len(stats.assets_ok)}")
    lines.append(f"Errors:           {len(stats.errors)}")
    lines.append(f"External/skipped: {len(stats.external_skipped)}")
    lines.append(f"Robots-disallowed: {len(stats.robots_disallowed)}")
    lines.append(f"Redirects seen:   {len(stats.redirects)}")
    lines.append("")

    lines.append("-" * 60)
    lines.append(f"PAGES VISITED ({len(stats.pages_ok)})")
    lines.append("-" * 60)
    for url, local in sorted(stats.pages_ok.items()):
        lines.append(f"  {url}\n      -> {local}")

    lines.append("")
    lines.append("-" * 60)
    lines.append(f"ASSETS DOWNLOADED ({len(stats.assets_ok)})")
    lines.append("-" * 60)
    for url, local in sorted(stats.assets_ok.items()):
        lines.append(f"  {url}\n      -> {local}")

    lines.append("")
    lines.append("-" * 60)
    lines.append(f"ERRORS ({len(stats.errors)})")
    lines.append("-" * 60)
    for url, referrer, reason in stats.errors:
        lines.append(f"  {url}")
        lines.append(f"      reason:   {reason}")
        lines.append(f"      found on: {referrer or '(start url)'}")

    lines.append("")
    lines.append("-" * 60)
    lines.append(f"EXTERNAL / SKIPPED LINKS - NOT FOLLOWED ({len(stats.external_skipped)})")
    lines.append("-" * 60)
    for url, referrer, reason in stats.external_skipped:
        lines.append(f"  {url}")
        lines.append(f"      reason:   {reason}")
        lines.append(f"      found on: {referrer or '(start url)'}")

    lines.append("")
    lines.append("-" * 60)
    lines.append(f"DISALLOWED BY ROBOTS.TXT - NOT FOLLOWED ({len(stats.robots_disallowed)})")
    lines.append("-" * 60)
    for url, referrer in stats.robots_disallowed:
        lines.append(f"  {url}")
        lines.append(f"      found on: {referrer or '(start url)'}")

    lines.append("")
    lines.append("-" * 60)
    lines.append(f"REDIRECTS FOLLOWED ({len(stats.redirects)})")
    lines.append("-" * 60)
    for from_url, to_url in stats.redirects:
        lines.append(f"  {from_url}\n      -> {to_url}")

    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_logger(output_dir: Path) -> logging.Logger:
    logger = logging.getLogger("westoncthistory-mirror")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S")

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    logger.addHandler(console)

    output_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(output_dir / "crawl.log", encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    return logger


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Download a full local mirror of westoncthistory.org for migration reference."
    )
    parser.add_argument("--start-url", default=DEFAULT_START_URL, help="URL to start crawling from.")
    parser.add_argument(
        "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        help="Directory to write the mirror into (created if missing).",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_DELAY_SECONDS,
        help="Minimum seconds between requests (politeness delay).",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=DEFAULT_MAX_PAGES,
        help="Safety cap on the total number of URLs fetched.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT[1],
        help="Per-request read timeout in seconds.",
    )
    parser.add_argument(
        "--contact-email",
        default=DEFAULT_CONTACT_EMAIL,
        help="Contact email embedded in the User-Agent string.",
    )
    parser.add_argument(
        "--user-agent",
        default=None,
        help="Override the full User-Agent string instead of building the default one.",
    )
    parser.add_argument(
        "--ignore-robots",
        action="store_true",
        help=(
            "Do not fetch or honor robots.txt. Off by default - the crawler "
            "normally fetches /robots.txt, skips any disallowed URLs, and "
            "raises --delay to match a Crawl-delay directive if one is set."
        ),
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    output_dir = Path(args.output_dir).resolve()
    logger = build_logger(output_dir)

    user_agent = args.user_agent or DEFAULT_USER_AGENT.format(contact=args.contact_email)

    crawler = SiteMirrorCrawler(
        start_url=args.start_url,
        output_dir=output_dir,
        delay=args.delay,
        timeout=(DEFAULT_TIMEOUT[0], args.timeout),
        user_agent=user_agent,
        max_pages=args.max_pages,
        logger=logger,
        respect_robots=not args.ignore_robots,
    )

    try:
        crawler.run()
    except KeyboardInterrupt:
        logger.warning("Interrupted by user - writing report for what was gathered so far.")

    report_path = write_report(crawler.stats, output_dir, crawler.start_url)
    logger.info("Report written to %s", report_path)


if __name__ == "__main__":
    main()
