# westoncthistory — archival mirror scraper

A small Python tool that downloads a full local mirror of
[westoncthistory.org](https://westoncthistory.org/) for use as reference
material during a content migration. It crawls every page reachable from
the homepage, saves the HTML, downloads every image/CSS/JS/PDF/other
static asset it finds, and rewrites all internal links so the resulting
mirror can be browsed **offline**, with no connection to the live site.

It does not use a headless browser — it's plain `requests` +
`BeautifulSoup`, which is enough for this site since it's server-rendered
HTML.

## What it does

- Starts at the homepage and follows every internal `<a href>` link it
  finds, recursively, staying strictly within `westoncthistory.org` (and
  `www.westoncthistory.org`, treated as the same site).
- Downloads every internal image, stylesheet, script, PDF, icon, font,
  etc. it finds via `<img>`, `<link>`, `<script>`, `<source>`, `<embed>`,
  inline `style="..."` attributes, `<style>` blocks, and `url(...)`
  references inside downloaded CSS files.
- Saves each page preserving the site's URL structure, e.g.:
  - `/about-us.php?pageid=210` → `about-us/pageid-210.html`
  - `/events.php` → `events/index.html`
  - `/` → `index.html`
- Saves assets under their original path, e.g. `/img/logo.png` →
  `img/logo.png`.
- Rewrites every internal link/src in the saved HTML and CSS to a
  relative path pointing at the local copy, so the mirror works when
  opened with no network access.
- Leaves external links (social media, embedded calendars, third-party
  widgets, `mailto:`/`tel:` links, etc.) untouched in the HTML and does
  **not** follow them — it just records that they were seen and skipped.
- Sends one request at a time with a **1 second delay** between requests
  by default, and a descriptive `User-Agent` identifying the crawl as an
  archival mirror run for the site owner (with a contact email), so it's
  clear to the site/host what the traffic is and why.
- Writes a full crawl log (`crawl.log`, written live as it runs) and a
  final summary report (`crawl-report.txt`) listing every page visited,
  every asset downloaded, every error (404s, timeouts, etc.), and every
  external/skipped link — all with the page that linked to them.

## Requirements

- Python 3.9+
- `requests`, `beautifulsoup4` (see `requirements.txt`)

## Setup

```bash
cd westoncthistory
python3 -m venv .venv        # optional but recommended
source .venv/bin/activate
pip install -r requirements.txt
```

## Running it

From inside the `westoncthistory/` directory:

```bash
python3 scraper.py
```

This crawls `https://westoncthistory.org/` and writes the mirror to
`./westoncthistory-mirror/` (i.e. `westoncthistory/westoncthistory-mirror/`).

Useful options:

```bash
python3 scraper.py \
  --start-url https://westoncthistory.org/ \
  --output-dir ./westoncthistory-mirror \
  --delay 1.0 \
  --max-pages 5000 \
  --contact-email you@example.com
```

| Flag | Default | Meaning |
|---|---|---|
| `--start-url` | `https://westoncthistory.org/` | Where the crawl begins. |
| `--output-dir` | `westoncthistory-mirror` | Where the mirror is written. |
| `--delay` | `1.0` | Minimum seconds between requests (politeness delay). |
| `--max-pages` | `5000` | Safety cap on total URLs fetched, in case of a crawl-logic bug. |
| `--timeout` | `25` | Per-request read timeout, in seconds. |
| `--contact-email` | `stephen@youell.net` | Embedded in the default User-Agent string. |
| `--user-agent` | *(built from the above)* | Override the User-Agent entirely. |

The crawl can take a while for a full site at 1 request/second — this is
intentional (politeness), not a bug. You can safely `Ctrl-C` it; it will
still write out a report for whatever it managed to gather before
stopping.

## Output layout

```
westoncthistory-mirror/
  index.html                    # homepage
  about-us/
    index.html                  # /about-us.php (no query)
    pageid-210.html             # /about-us.php?pageid=210
  events/
    index.html
  img/
    logo.png
  css/
    style.css                   # url(...) references rewritten too
  docs/
    brochure.pdf
  crawl.log                     # live log of every request as it happened
  crawl-report.txt              # final summary: pages, assets, errors, skipped links
```

## Viewing the mirror locally

Once a crawl has finished (or been interrupted), serve the output
directory with Python's built-in HTTP server and open it in a browser:

```bash
cd westoncthistory-mirror
python3 -m http.server 8000
```

Then visit <http://localhost:8000/> — internal links, images, CSS, and
downloadable PDFs should all work without any connection to the live
site. (Opening the HTML files directly with `file://` mostly works too,
but a local server avoids any browser quirks with relative paths.)

## Checking the crawl report

Always check `crawl-report.txt` after a run:

- **ERRORS** — pages/assets that failed (404, timeout, connection error,
  etc.), along with the page that linked to them. Worth checking by hand
  against the live site before treating the mirror as complete.
- **EXTERNAL / SKIPPED LINKS** — things intentionally not downloaded
  (social media links, embedded calendar widgets, `mailto:` links, etc.),
  with the page each was found on.
- **REDIRECTS FOLLOWED** — any internal redirects the crawler followed
  transparently.

## Known limitations

- Only crawls links present in the HTML/CSS the server returns; it does
  not execute JavaScript, so links injected purely by client-side script
  (rare on this kind of site) won't be discovered. If parts of the site
  turn out to require this, re-run the JS-heavy pages through a headless
  browser separately.
- `<meta http-equiv="refresh">` redirects are not followed automatically.
- Very unusual/ambiguous URL patterns could theoretically collide in the
  output directory naming scheme; check `crawl.log` for any file-write
  errors if the asset/page counts look off.
