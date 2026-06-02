"""
mcp-lunch  —  PATCHED to serve the teamleader.se landing page at /
---------------------------------------------------------------------
Drop-in replacement for `freddan-teamleader/mcp-lunch/server.py`.

Only diff vs. the upstream version:
  • Adds `import mimetypes` and `from pathlib import Path`.
  • Replaces the inline `_LANDING` placeholder in ImageProxyMiddleware
    with a static-file handler that serves `site/index.html` at `/`,
    plus `site/canvas.html`, `site/design-canvas.jsx`, and anything under
    `site/variants/*.html`.
  • Falls through to a small inline notice if the `site/` folder is
    missing (so this file still works even before you copy the assets in).

Everything else — MCP tools, image proxy, `/lunchguide → /mcp` alias,
SSE transport — is unchanged.

Layout expected on disk:
    mcp-lunch/
      server.py          ← this file
      requirements.txt
      railway.toml
      site/
        index.html       ← Aurora landing
        canvas.html      ← (optional) 3-variant review canvas
        design-canvas.jsx← (optional) only needed if canvas.html is kept
        variants/        ← (optional)
          aurora.html
          sunset.html
          acid.html

An MCP server that fetches today's lunch guides for Swedish cities.

Tools exposed:
  • list_cities          – list all available city slugs
  • get_lunch_guide      – today's full lunch list for a city
  • get_restaurant_menu  – full weekly lunch menu for one restaurant
"""

import re
import os
import time
import math
import base64
import json
import mimetypes
import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import httpx
from urllib.parse import parse_qs
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

# ---------------------------------------------------------------------------
# MCP app
# ---------------------------------------------------------------------------

mcp = FastMCP(
    name="lunch-guide",
    instructions=(
        "Use this server to look up Swedish restaurant lunch menus. "
        "Call list_cities first to get valid city slugs, then get_lunch_guide for today's "
        "menus in a city, or get_restaurant_menu for a specific restaurant's full week."
    ),
    # Mount directly on /lunchguide so no path rewriting is needed.
    streamable_http_path="/lunchguide",
    # Stateless mode: every POST is self-contained — no session state needed.
    # This prevents 404s when a session is lost due to Railway restarts or
    # load-balancer connection rotation.
    stateless_http=True,
    # DNS-rebinding protection is disabled here because we sit behind
    # Railway's TLS-terminating proxy, which already enforces origin security.
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    ),
)

BASE_URL = "https://www.matochmat.se"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "sv-SE,sv;q=0.9,en;q=0.8",
}

# ---------------------------------------------------------------------------
# Known cities (from sitemap, trimmed to most common ones)
# ---------------------------------------------------------------------------

CITIES: dict[str, str] = {
    "pitea": "Piteå",
    "lulea": "Luleå",
    "skelleftea": "Skellefteå",
    "umea": "Umeå",
    "kiruna": "Kiruna",
    "boden": "Boden",
    "sundsvall": "Sundsvall",
    "ornskoldsvik": "Örnsköldsvik",
    "gavle": "Gävle",
    "ostersund": "Östersund",
    "hudiksvall": "Hudiksvall",
    "norrtalje": "Norrtälje",
    "soderhamn": "Söderhamn",
    "bollnas": "Bollnäs",
    "falun": "Falun",
    "borlange": "Borlänge",
    "vasteras": "Västerås",
    "orebro": "Örebro",
    "eskilstuna": "Eskilstuna",
    "karlskoga": "Karlskoga",
    "stockholm": "Stockholm",
    "stockholm-gardet": "Stockholm Gärdet",
    "uppsala": "Uppsala",
    "linkoping": "Linköping",
    "norrkoping": "Norrköping",
    "jonkoping": "Jönköping",
    "vaxjo": "Växjö",
    "kalmar": "Kalmar",
    "goteborg": "Göteborg",
    "boras": "Borås",
    "trollhattan": "Trollhättan",
    "uddevalla": "Uddevalla",
    "malmo": "Malmö",
    "helsingborg": "Helsingborg",
    "lund": "Lund",
    "kristianstad": "Kristianstad",
    "karlskrona": "Karlskrona",
    "karlshamn": "Karlshamn",
    "ystad": "Ystad",
    "trelleborg": "Trelleborg",
    "angelholm": "Ängelholm",
    "falkenberg": "Falkenberg",
    "varberg": "Varberg",
    "halmstad": "Halmstad",
    "motala": "Motala",
    "ljusdal": "Ljusdal",
    "timra": "Timrå",
    "alvsbyn": "Älvsbyn",
}


# ---------------------------------------------------------------------------
# Simple in-memory cache  (single-process, TTL-based)
# ---------------------------------------------------------------------------

_cache: dict[str, tuple[float, object]] = {}
CACHE_TTL = 1800  # 30 minutes — lunch data changes at most once a day


def _cache_get(key: str) -> object | None:
    if key in _cache:
        ts, val = _cache[key]
        if time.time() - ts < CACHE_TTL:
            return val
        del _cache[key]
    return None


def _cache_set(key: str, val: object) -> None:
    _cache[key] = (time.time(), val)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fetch(url: str) -> str:
    """Fetch a URL and return the text content."""
    with httpx.Client(follow_redirects=True, timeout=15) as client:
        resp = client.get(url, headers=HEADERS)
        resp.raise_for_status()
        return resp.text


def _preprocess_dish_lines(lines: list[str], restaurant_name: str) -> list[str]:
    """
    Clean up raw text lines before dish parsing:
    - Drop navigation fragments that the site splits across spans
    - Drop the restaurant name itself (appears as a text anchor after the logo)
    - Join split price tokens: "139" + "kr" → "139 kr"
    """
    NAV = frozenset({
        "veckansluncher", "veckans", "luncher",
        "hitta hit", "hitta", "hit",
        "visa alla lunchrätter", "visa fler",
    })
    name_lower = restaurant_name.lower()
    result: list[str] = []
    i = 0
    while i < len(lines):
        curr = lines[i]
        curr_lower = curr.lower()

        # Skip navigation fragments and the restaurant name itself
        if curr_lower in NAV or curr_lower == name_lower:
            i += 1
            continue

        # Join bare number + "kr" on next line → "NNN kr"
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if re.match(r"^\d+$", curr) and nxt.lower() == "kr":
            result.append(f"{curr} kr")
            i += 2
            continue

        # Drop lone orphan "kr"
        if curr_lower == "kr":
            i += 1
            continue

        result.append(curr)
        i += 1
    return result


def _parse_lunch_page(html: str) -> list[dict]:
    """
    Parse the rendered HTML from a lunch city page.
    Returns a list of restaurant dicts, each with:
      name, slug, logo, url, dishes (list of {name, price, vegetarian, tags})

    Strategy:
    1. Collect slug → href and slug → logo_url from anchor/img pairs.
    2. Replace <img alt="..."> with alt text so get_text() surfaces the
       "Name i City lunchmeny" section markers.
    3. Split the full page text into per-restaurant sections.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    # ── 1. Collect slug → href and slug → logo BEFORE replacing img tags ──
    slug_to_href: dict[str, str] = {}
    slug_to_logo: dict[str, str] = {}
    for a in soup.find_all("a", href=re.compile(r"^/lunch/[^/]+/[^/]+/?$")):
        img = a.find("img")
        href = a.get("href", "")
        parts = [p for p in href.strip("/").split("/") if p]
        if len(parts) != 3:
            continue
        slug = parts[2]
        if img:
            alt = img.get("alt", "")
            if "veckansluncher" in alt.lower():
                continue
            if slug not in slug_to_href:
                slug_to_href[slug] = href
                slug_to_logo[slug] = img.get("src", "")
        else:
            link_text = a.get_text(strip=True)
            if "veckansluncher" not in link_text.lower() and slug not in slug_to_href:
                slug_to_href[slug] = href

    # ── 2. Inline img alt text for get_text() section detection ─────────
    for img in soup.find_all("img"):
        img.replace_with(img.get("alt", ""))

    # Infer city slug from any collected href
    city_slug = ""
    if slug_to_href:
        p = next(iter(slug_to_href.values())).strip("/").split("/")
        city_slug = p[1] if len(p) >= 2 else ""

    # ── 3. Split page text into per-restaurant sections ──────────────────
    LUNCHMENY_RE = re.compile(r"^(.+?)\s+i\s+\S.*?\s+lunchmeny\b", re.IGNORECASE)
    GLOBAL_SKIP = frozenset({"veckansluncher", "hitta hit", "visa alla lunchrätter"})

    lines = [l.strip() for l in soup.get_text("\n").splitlines() if l.strip()]
    sections: list[tuple[str, list[str]]] = []
    cur_name: str | None = None
    cur_lines: list[str] = []

    for line in lines:
        m = LUNCHMENY_RE.match(line)
        if m:
            if cur_name is not None:
                sections.append((cur_name, cur_lines))
            cur_name = m.group(1).strip()
            cur_lines = []
        elif cur_name is not None and line.lower() not in GLOBAL_SKIP:
            cur_lines.append(line)

    if cur_name is not None:
        sections.append((cur_name, cur_lines))

    # ── 4. Match sections to slugs and build result ──────────────────────
    def _to_slug(name: str) -> str:
        s = name.lower()
        for fr, to in [("å","a"),("ä","a"),("ö","o"),("é","e"),("è","e"),
                       ("ê","e"),("ü","u"),("ï","i"),("ó","o"),("ú","u")]:
            s = s.replace(fr, to)
        return re.sub(r"[^a-z0-9]+", "-", s).strip("-")

    seen: set[str] = set()
    restaurants: list[dict] = []

    for raw_name, dish_lines in sections:
        key = raw_name.lower()
        if key in seen:
            continue
        seen.add(key)

        guess = _to_slug(raw_name)
        best_slug: str | None = None
        best_href: str | None = None

        if guess in slug_to_href:
            best_slug, best_href = guess, slug_to_href[guess]
        else:
            for slug, href in slug_to_href.items():
                if guess[:8] == slug[:8]:
                    best_slug, best_href = slug, href
                    break

        if best_slug is None:
            best_slug = guess
            best_href = f"/lunch/{city_slug}/{guess}/"

        restaurants.append({
            "name": raw_name,
            "slug": best_slug,
            "city": city_slug,
            "logo": slug_to_logo.get(best_slug, ""),
            "url": f"{BASE_URL}{best_href}",
            "dishes": _parse_dishes(_preprocess_dish_lines(dish_lines, raw_name)),
        })

    return restaurants


def _parse_dishes(lines: list[str]) -> list[dict]:
    """
    Convert a flat list of text lines into structured dish objects.

    The site renders dishes as:
      <dish name / description>
      <price> kr
      Vegetarisk          ← optional tag on the preceding dish
      Laktosfri           ← optional tag on the preceding dish

    "Stängt" means the restaurant is closed today.
    """
    price_re = re.compile(r"^(\d[\d\s]*)\s*kr$", re.IGNORECASE)
    # Labels that annotate the *previous* dish rather than starting a new one
    tag_re = re.compile(
        r"^(vegetarisk|laktosfri|glutenfri|vegan|vegansk)$", re.IGNORECASE
    )
    dishes = []
    current: dict | None = None

    for line in lines:
        # Skip very long lines (HTML artefacts / navigation text)
        if len(line) > 300:
            continue

        price_match = price_re.match(line)
        if price_match:
            price = int(price_match.group(1).replace(" ", ""))
            if current is not None:
                current["price"] = price
            # else: orphan price – ignore

        elif tag_re.match(line):
            # This is a dietary tag for the *previous* dish
            tag = line.lower()
            if current is not None:
                if tag in ("vegetarisk", "vegan", "vegansk"):
                    current["vegetarian"] = True
                current.setdefault("tags", []).append(tag)

        else:
            # New dish – flush the previous one first
            if current is not None:
                dishes.append(current)

            current = {
                "name": line,
                "price": None,
                "vegetarian": False,
                "tags": [],
                "closed": line.lower() == "stängt",
            }

    if current is not None:
        dishes.append(current)

    return dishes


def _simple_text_parse(html: str) -> str:
    """Return clean text from HTML, used for restaurant detail pages."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    lines = [l.strip() for l in text.splitlines()]
    lines = [l for l in lines if l]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Slack notifier
# ---------------------------------------------------------------------------

def _notify_slack(message: str) -> None:
    """Post a message to Slack via webhook. Fails silently."""
    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook:
        return
    try:
        httpx.post(webhook, json={"text": message}, timeout=5)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# LLM dish cleaner (Claude Haiku, opt-in via ANTHROPIC_API_KEY)
# ---------------------------------------------------------------------------

_llm_quota_exceeded = False  # module-level flag — set on 429/529, cleared on restart


def _clean_dishes_with_llm(dishes: list[dict], restaurant_name: str) -> list[dict]:
    """
    Use Claude Haiku to clean up raw dish data from mylunch.se.

    Only runs when ANTHROPIC_API_KEY is set and quota has not been exceeded.
    Falls back to raw dishes on any error.

    Returns a cleaned list of dish dicts in the same format.
    """
    global _llm_quota_exceeded

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key or _llm_quota_exceeded:
        return dishes

    raw = "\n".join(
        f"- {d['name']}" + (f" ({d['price']} kr)" if d.get("price") else "")
        for d in dishes
    )

    prompt = f"""You are extracting structured lunch menu data for the restaurant "{restaurant_name}".

Below is raw text scraped from a Swedish lunch guide website. It may contain:
- Actual dish names and prices (keep these)
- Restaurant descriptions, opening hours, marketing text (remove these)
- Navigation fragments, footers, form labels (remove these)

Return ONLY a JSON array of dish objects. Each object must have:
  "name": string (the dish name, cleaned up, in Swedish)
  "price": integer or null (price in SEK, null if not found)
  "vegetarian": boolean (true if dish is vegetarian/vegan)
  "tags": array of strings (dietary tags: "vegetarisk", "vegansk", "glutenfri", "laktosfri")
  "closed": false

If there are no real dishes, return an empty array [].
Return ONLY valid JSON, no markdown, no explanation.

Raw text:
{raw}"""

    try:
        resp = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=15,
        )

        if resp.status_code in (429, 529):
            _llm_quota_exceeded = True
            msg = (
                f":warning: *mcp-lunch*: Anthropic API quota exceeded (HTTP {resp.status_code}). "
                f"LLM dish cleaning disabled until next restart."
            )
            import logging
            logging.getLogger(__name__).warning(msg)
            _notify_slack(msg)
            return dishes

        resp.raise_for_status()
        data = resp.json()
        text = data["content"][0]["text"].strip()
        # Strip markdown code fences if present
        text = re.sub(r"^```[a-z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
        cleaned = json.loads(text)
        if not isinstance(cleaned, list):
            return dishes
        # Ensure required fields
        result = []
        for item in cleaned:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            result.append({
                "name": str(item.get("name", "")),
                "price": int(item["price"]) if item.get("price") else None,
                "vegetarian": bool(item.get("vegetarian", False)),
                "tags": list(item.get("tags", [])),
                "closed": False,
            })
        return result if result else dishes

    except Exception:
        return dishes

MYLUNCH_BASE = "https://www.mylunch.se"

# Maps our city slugs to mylunch.se city slugs (most are identical)
MYLUNCH_CITY_MAP: dict[str, str] = {
    "pitea": "pitea",
    "lulea": "lulea",
    "skelleftea": "skelleftea",
    "umea": "umea",
    "kiruna": "kiruna",
    "boden": "boden",
    "sundsvall": "sundsvall",
    "ornskoldsvik": "ornskoldsvik",
    "gavle": "gavle",
    "ostersund": "ostersund",
    "hudiksvall": "hudiksvall",
    "norrtalje": "norrtalje",
    "soderhamn": "soderhamn",
    "bollnas": "bollnas",
    "falun": "falun",
    "borlange": "borlange",
    "vasteras": "vasteras",
    "orebro": "orebro",
    "eskilstuna": "eskilstuna",
    "karlskoga": "karlskoga",
    "stockholm": "stockholm",
    "uppsala": "uppsala",
    "linkoping": "linkoping",
    "norrkoping": "norrkoping",
    "jonkoping": "jonkoping",
    "vaxjo": "vaxjo",
    "kalmar": "kalmar",
    "goteborg": "goteborg",
    "boras": "boras",
    "malmo": "malmo",
    "helsingborg": "helsingborg",
    "lund": "lund",
    "kristianstad": "kristianstad",
    "karlskrona": "karlskrona",
    "karlshamn": "karlshamn",
    "halmstad": "halmstad",
    "varberg": "varberg",
}


def _parse_mylunch_page(html: str, city_slug: str) -> list[dict]:
    """
    Parse mylunch.se city page into the same restaurant/dish format as matochmat.

    Structure: each restaurant is a div.mi containing:
      div.mih > a[href] > h2   — name and link
      div.miib > a > img       — logo
      div.mim > div.mim-table  — dishes as div.mim-row rows
        p.mim-txt   = actual dish (has price in p.mim-prc sibling)
        p.mim-txt0  = header/separator line (skip)
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    price_re = re.compile(r"(\d{2,4})\s*[Kk]r", re.IGNORECASE)
    tag_re = re.compile(
        r"\b(GF|LF|MF|vegetarisk|vegansk|vegan|glutenfri|laktosfri)\b", re.IGNORECASE
    )
    tag_map = {
        "gf": "glutenfri", "lf": "laktosfri", "mf": "mjölkfri",
        "vegetarisk": "vegetarisk", "vegansk": "vegansk", "vegan": "vegansk",
        "glutenfri": "glutenfri", "laktosfri": "laktosfri",
    }

    restaurants = []
    seen_names: set[str] = set()

    for mi in soup.find_all("div", class_="mi"):
        # Name and URL
        mih = mi.find("div", class_="mih")
        if not mih:
            continue
        a = mih.find("a")
        h2 = mih.find("h2")
        if not h2:
            continue
        name = h2.get_text(strip=True)
        if not name or name.lower() in seen_names:
            continue
        seen_names.add(name.lower())

        href = a.get("href", "") if a else ""
        url = f"{MYLUNCH_BASE}{href}" if href.startswith("/") else href

        # Logo
        miib = mi.find("div", class_="miib")
        logo_url = ""
        if miib:
            img = miib.find("img")
            if img:
                logo_url = img.get("src", "")

        # Dishes from mim-row — only rows with p.mim-txt (not mim-txt0) are real dishes
        dishes = []
        for row in mi.find_all("div", class_="mim-row"):
            txt_el = row.find("p", class_="mim-txt")
            if not txt_el:
                continue  # skip headers (mim-txt0)
            dish_name = txt_el.get_text(strip=True)
            if not dish_name or len(dish_name) > 250:
                continue

            # Price from sibling p.mim-prc
            prc_el = row.find("p", class_="mim-prc")
            price = None
            if prc_el:
                pm = price_re.search(prc_el.get_text())
                if pm:
                    price = int(pm.group(1))

            # Dietary tags from dish name
            tags = []
            is_veg = False
            for m in tag_re.finditer(dish_name):
                t = tag_map.get(m.group(1).lower(), m.group(1).lower())
                if t not in tags:
                    tags.append(t)
                if t in ("vegetarisk", "vegansk"):
                    is_veg = True

            dishes.append({
                "name": dish_name,
                "price": price,
                "vegetarian": is_veg,
                "tags": tags,
                "closed": False,
            })

        if not dishes:
            continue

        # Build slug from name
        slug = name.lower()
        for fr, to in [("å","a"),("ä","a"),("ö","o"),("é","e"),(" ","-")]:
            slug = slug.replace(fr, to)
        slug = re.sub(r"[^a-z0-9-]+", "", slug).strip("-")

        restaurants.append({
            "name": name,
            "slug": slug,
            "city": city_slug,
            "logo": logo_url,
            "url": url,
            "source": "mylunch.se",
            "dishes": dishes,
        })

    return restaurants


_SITEMAP_CACHE: dict[str, tuple[float, list[str]]] = {}
SITEMAP_TTL = 86400  # 24 h — restaurant list rarely changes

_SV_DAYS = ["måndag", "tisdag", "onsdag", "torsdag", "fredag", "lördag", "söndag"]


def _today_sv() -> str:
    return _SV_DAYS[datetime.date.today().weekday()]


def _get_mylunch_slugs(city: str) -> list[str]:
    """Return restaurant slugs for `city` from the mylunch.se sitemap (cached 24 h)."""
    ts, slugs = _SITEMAP_CACHE.get(city, (0, []))
    if time.time() - ts < SITEMAP_TTL:
        return slugs
    try:
        xml = _fetch(f"{MYLUNCH_BASE}/sitemap-companies.xml")
        slugs = re.findall(
            rf"https://www\.mylunch\.se/{re.escape(city)}/([^/]+)/lunch/", xml
        )
    except Exception:
        slugs = []
    _SITEMAP_CACHE[city] = (time.time(), slugs)
    return slugs


def _parse_mylunch_restaurant_today(html: str, slug: str, city_slug: str) -> dict | None:
    """
    Parse an individual mylunch.se restaurant page for today's menu.
    Returns None if the restaurant has no menu today.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    # Name from h1
    h1 = soup.find("h1")
    name = h1.get_text(strip=True) if h1 else slug.replace("-", " ").title()
    # Strip " - Lunch" suffix that individual pages sometimes add
    name = re.sub(r"\s*[-–]\s*lunch\s*$", "", name, flags=re.IGNORECASE).strip()

    url = f"{MYLUNCH_BASE}/{city_slug}/{slug}/lunch/"

    # Restaurant-level price (used when per-dish price is missing)
    rest_price: int | None = None
    for price_el in soup.find_all("div", class_="midp"):
        p = price_el.find("p")
        if p:
            pm = re.search(r"(\d{2,4})", p.get_text())
            if pm:
                rest_price = int(pm.group(1))
                break

    # Logo
    img = soup.find("img", class_="miibm")
    logo_url = img.get("src", "") if img else ""

    _SKIP_HEADER = re.compile(
        r"^\*+[\s*]+$|nybakat|kaffe ingår|sallad ingår|bröd ingår"
        r"|ingår$|^\s*$",
        re.I,
    )
    # Swedish day names — used to identify per-day sections
    _DAY_RE = re.compile(
        r"^(måndag|tisdag|onsdag|torsdag|fredag|lördag|söndag)\b", re.I
    )

    def _rows_to_dishes(rows: list, default_price: int | None) -> list[dict]:
        dishes_out = []
        for row in rows:
            p0 = row.find("p", class_="mim-txt0")
            if not p0:
                continue
            dish_name = p0.get_text(strip=True)
            if not dish_name or _SKIP_HEADER.search(dish_name):
                continue
            prc_el = row.find("p", class_="mim-prc")
            price: int | None = None
            if prc_el:
                pm2 = re.search(r"(\d{2,4})", prc_el.get_text())
                if pm2:
                    price = int(pm2.group(1))
            if price is None:
                price = default_price
            is_veg = bool(re.search(r"\bvegetar|\bvegan", dish_name, re.I))
            dishes_out.append({
                "name": dish_name,
                "price": price,
                "vegetarian": is_veg,
                "tags": ["vegetarisk"] if is_veg else [],
                "closed": False,
            })
        return dishes_out

    all_mims = soup.find_all("div", class_="mim")
    today = _today_sv()

    # Strategy 1: find today's day-specific section
    today_mim = None
    for mim in all_mims:
        first_row = mim.find("div", class_="mim-row")
        if first_row and today in first_row.get_text("", strip=True).lower():
            today_mim = mim
            break

    if today_mim is not None:
        # Day-specific menu: skip the date header row
        rows = today_mim.find_all("div", class_="mim-row")[1:]
        dishes = _rows_to_dishes(rows, rest_price)
    else:
        # Strategy 2: no day-specific sections — use the first mim block that
        # does NOT start with a day-name header (generic "Dagens lunch" / "Veckans rätter").
        # Also extract price from the header line if present.
        dishes = []
        for mim in all_mims:
            all_rows = mim.find_all("div", class_="mim-row")
            if not all_rows:
                continue
            first_text = all_rows[0].get_text("", strip=True).lower()
            # Skip if this block starts with a different day name
            if _DAY_RE.match(first_text) and today not in first_text:
                continue
            # Extract price embedded in the header (e.g. "Dagens lunch ... 195Kr")
            header_pm = re.search(r"(\d{2,4})\s*kr", first_text, re.I)
            block_price = int(header_pm.group(1)) if header_pm else rest_price
            # Skip header row; use rest
            candidate = _rows_to_dishes(all_rows[1:], block_price)
            if candidate:
                dishes = candidate
                # Update rest_price if we found one in the block header
                if block_price and rest_price is None:
                    rest_price = block_price
                break

    if not dishes:
        return None

    return {
        "name": name,
        "slug": slug,
        "city": city_slug,
        "logo": logo_url,
        "url": url,
        "source": "mylunch.se",
        "dishes": dishes,
    }


def _fetch_mylunch_one(args: tuple[str, str]) -> dict | None:
    slug, city = args
    try:
        html = _fetch(f"{MYLUNCH_BASE}/{city}/{slug}/lunch/")
        return _parse_mylunch_restaurant_today(html, slug, city)
    except Exception:
        return None


def _fetch_mylunch_full(city: str, exclude_slugs: set[str]) -> list[dict]:
    """
    Fetch all restaurants for `city` by hitting each individual page concurrently.
    `exclude_slugs` are already known from the city page and will be skipped.
    """
    slugs = [s for s in _get_mylunch_slugs(city) if s not in exclude_slugs]
    if not slugs:
        return []
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=20) as pool:
        for r in as_completed(pool.submit(_fetch_mylunch_one, (s, city)) for s in slugs):
            item = r.result()
            if item:
                results.append(item)
    return results


def _fetch_mylunch(city: str) -> list[dict]:
    """Fetch and parse mylunch.se for a given city slug."""
    ml_city = MYLUNCH_CITY_MAP.get(city)
    if not ml_city:
        return []
    try:
        html = _fetch(f"{MYLUNCH_BASE}/{ml_city}/")
        restaurants = _parse_mylunch_page(html, city)
        # Filter out SVG placeholder logos
        for r in restaurants:
            if r.get("logo", "").startswith("data:image/svg"):
                r["logo"] = ""
        # LLM cleanup — only if ANTHROPIC_API_KEY is set
        if os.environ.get("ANTHROPIC_API_KEY"):
            with ThreadPoolExecutor(max_workers=8) as pool:
                futures = {
                    pool.submit(_clean_dishes_with_llm, r["dishes"], r["name"]): i
                    for i, r in enumerate(restaurants)
                }
                for fut, idx in futures.items():
                    restaurants[idx]["dishes"] = fut.result()
        return restaurants
    except Exception:
        return []


def _merge_sources(matochmat: list[dict], mylunch: list[dict]) -> list[dict]:
    """
    Merge two restaurant lists, deduplicating by name similarity.
    Restaurants only in mylunch get source='mylunch.se'.
    Restaurants in matochmat get source='matochmat.se'.
    """
    def _norm(name: str) -> str:
        return re.sub(r"[^a-z0-9]", "", name.lower())

    existing = {_norm(r["name"]) for r in matochmat}
    merged = [{**r, "source": "matochmat.se"} for r in matochmat]

    for r in mylunch:
        if _norm(r["name"]) not in existing:
            merged.append(r)

    return merged


# ---------------------------------------------------------------------------
# Custom single-restaurant sources
# ---------------------------------------------------------------------------
#
# Some restaurants are not listed on the aggregator sites (matochmat.se /
# mylunch.se) and publish their menu only on their own website. Each such
# restaurant gets a small dedicated parser registered here. The parsers are
# text-anchor based (not CSS-class based) so they survive minor markup
# changes on the source site.
#
# To add another own-site restaurant:
#   1. Write a `_fetch_<name>_today(city_slug) -> dict | None` returning a
#      restaurant dict in the standard shape (today's dishes).
#   2. (optional) Write a weekly-text function for get_restaurant_menu.
#   3. Register both in the maps at the bottom of this section.
# ---------------------------------------------------------------------------

BYTTAN_URL = "https://www.byttaniparken.se/meny"
BYTTAN_SLUG = "byttan-i-parken"
_BYTTAN_WEEKDAYS = ("måndag", "tisdag", "onsdag", "torsdag", "fredag")


def _parse_byttan_weekly(html: str) -> dict:
    """
    Parse Byttan i Parken's single-page menu into a weekly lunch structure.

    Returns:
      {
        "days": { "tisdag": [dish, ...], ..., "helg": [dish, ...] },
        "standing": [dish, ...],   # always-available items (soup)
      }

    Text-anchor based: keys off the Swedish section labels ("Veckans lunch",
    weekday names, "Helglunch") and the next top-level section ("Bistro"),
    rather than fragile CSS classes.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    lines = [l.strip() for l in soup.get_text("\n").splitlines() if l.strip()]

    PRICE_WEEKDAY = 149
    PRICE_WEEKEND = 299

    def _is_desc(s: str) -> bool:
        # Description lines either lead with "Med ..." or read as a full
        # sentence ending in a period. Dish names do neither.
        return s.lower().startswith("med ") or s.rstrip().endswith(".")

    def _slice(after: str, until: set[str]) -> list[str]:
        try:
            i0 = next(i for i, l in enumerate(lines) if l.lower() == after)
        except StopIteration:
            return []
        out: list[str] = []
        for l in lines[i0 + 1:]:
            if l.lower() in until:
                break
            out.append(l)
        return out

    days: dict[str, list[dict]] = {}

    # ── Weekday lunch: between "Veckans lunch" and "Helglunch" ──────────
    cur_day: str | None = None
    veg_next = False
    for l in _slice("veckans lunch", {"helglunch", "bistro"}):
        low = l.lower()
        if low in _BYTTAN_WEEKDAYS:
            cur_day = low
            days.setdefault(cur_day, [])
            veg_next = False
        elif cur_day is None:
            continue
        elif low == "vegetariskt":
            veg_next = True
        elif low == "byttan abonnerad":
            days[cur_day].append({
                "name": "Byttan abonnerad (lunchstängt)",
                "price": None, "vegetarian": False, "tags": [], "closed": True,
            })
        elif _is_desc(l) and days[cur_day]:
            prev = days[cur_day][-1]
            prev["name"] = f'{prev["name"]} – {l.rstrip(".")}'
        else:
            days[cur_day].append({
                "name": l, "price": PRICE_WEEKDAY, "vegetarian": veg_next,
                "tags": ["vegetarisk"] if veg_next else [], "closed": False,
            })
            veg_next = False

    # ── Weekend set menu: between "Helglunch" and "Bistro"/"Sällskap" ───
    helg: list[dict] = []
    for l in _slice("helglunch", {"bistro", "sällskap"}):
        low = l.lower()
        if "inkl." in low or low.startswith("lördag") or low.startswith("helglunch"):
            continue
        if _is_desc(l) and helg:
            helg[-1]["name"] = f'{helg[-1]["name"]} – {l.rstrip(".")}'
        else:
            helg.append({
                "name": l, "price": PRICE_WEEKEND, "vegetarian": False,
                "tags": [], "closed": False,
            })
    if helg:
        days["helg"] = helg

    # ── Always-available items (weekly soup) ────────────────────────────
    standing: list[dict] = []
    for l in lines:
        if l.lower().startswith("veckans soppa"):
            standing.append({
                "name": "Veckans soppa med bröd och sallad",
                "price": 130, "vegetarian": False, "tags": [], "closed": False,
            })
            break

    return {"days": days, "standing": standing}


def _fetch_byttan_today(city_slug: str) -> dict | None:
    """Today's lunch for Byttan i Parken, in the standard restaurant shape."""
    try:
        html = _fetch(BYTTAN_URL)
    except Exception:
        return None

    weekly = _parse_byttan_weekly(html)
    today = _today_sv()

    if today in ("lördag", "söndag"):
        dishes = list(weekly["days"].get("helg", []))
    elif today == "måndag":
        # Lunch is served tis–fre only; café is still open.
        dishes = [{
            "name": "Lunch serveras tis–fre 11.30–14.30 (caféet öppet som vanligt)",
            "price": None, "vegetarian": False, "tags": [], "closed": False,
        }] + list(weekly["standing"])
    else:
        dishes = list(weekly["days"].get(today, [])) + list(weekly["standing"])

    if not dishes:
        return None

    return {
        "name": "Byttan i Parken",
        "slug": BYTTAN_SLUG,
        "city": city_slug,
        "logo": "https://www.byttaniparken.se/assets/logo/byttan_logo_black.png",
        "url": BYTTAN_URL,
        "source": "byttaniparken.se",
        "dishes": dishes,
    }


def _byttan_weekly_text() -> str:
    """Full weekly lunch menu for Byttan i Parken as plain text."""
    try:
        html = _fetch(BYTTAN_URL)
    except Exception as e:
        return f"Error: Could not fetch Byttan i Parken menu: {e}"

    w = _parse_byttan_weekly(html)
    order = [
        ("Tisdag", "tisdag"), ("Onsdag", "onsdag"), ("Torsdag", "torsdag"),
        ("Fredag", "fredag"), ("Lördag & söndag (Helglunch)", "helg"),
    ]
    out = [
        "Byttan i Parken – Veckans lunch",
        "Slottsvägen 6, 392 33 Kalmar (Stadsparken)",
        "Lunch tis–fre 11.30–14.30 · Helglunch lör–sön 11.30–15.00",
        "",
    ]
    for label, key in order:
        out.append(label)
        items = w["days"].get(key, [])
        if items:
            for d in items:
                price = f' ({d["price"]} kr)' if d.get("price") else ""
                out.append(f'  • {d["name"]}{price}')
        else:
            out.append("  • (ingen lunchinfo för dagen)")
        out.append("")
    if w["standing"]:
        out.append("Alltid:")
        for d in w["standing"]:
            out.append(f'  • {d["name"]} ({d["price"]} kr)')
    return "\n".join(out)


# Registry: city slug → list of custom-source keys to merge into get_lunch_guide
CUSTOM_SOURCES: dict[str, list[str]] = {
    "kalmar": ["byttan"],
}

# key → today's-menu fetcher  (used by get_lunch_guide)
_CUSTOM_TODAY_FETCHERS = {
    "byttan": _fetch_byttan_today,
}

# restaurant slug → weekly-text fetcher  (used by get_restaurant_menu)
_CUSTOM_MENU_FETCHERS = {
    BYTTAN_SLUG: _byttan_weekly_text,
}


def _fetch_custom_sources(city: str) -> list[dict]:
    """Return today's-menu dicts for any own-site restaurants in `city`."""
    out: list[dict] = []
    for key in CUSTOM_SOURCES.get(city, []):
        fn = _CUSTOM_TODAY_FETCHERS.get(key)
        if fn is None:
            continue
        try:
            r = fn(city)
        except Exception:
            r = None
        if r:
            out.append(r)
    return out


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def list_cities() -> dict:
    """
    List all Swedish cities that have lunch guides available.

    Returns a dict mapping city slug (used in other tools) to display name.
    Example: {"umea": "Umeå", "stockholm": "Stockholm", ...}
    """
    return CITIES


@mcp.tool()
def get_lunch_guide(city: str) -> str:
    """
    Get today's lunch menus for all restaurants in a Swedish city.
    Fetches from multiple sources (matochmat.se + mylunch.se), merged and deduplicated.

    Args:
        city: City slug (e.g. "umea", "stockholm", "goteborg").
              Use list_cities() to get valid slugs.

    Returns:
        A JSON string — parse with JSON.parse() (JS) or json.loads() (Python).
        The parsed value is a list of restaurant objects, each containing:
          - name (str): Restaurant name
          - slug (str): Restaurant slug
          - city (str): City slug
          - logo (str): Relative path to logo image
          - url (str): Direct link to the restaurant's page
          - source (str): "matochmat.se" or "mylunch.se"
          - dishes (list): Today's dishes, each with:
              - name (str): Dish name or description line
              - price (int|None): Price in SEK, null for description lines
              - vegetarian (bool): Whether the dish is vegetarian
              - tags (list[str]): Dietary tags e.g. ["vegetarisk", "glutenfri"]
              - closed (bool): True if restaurant is closed today
    """
    city = city.lower().strip()

    cache_key = f"lunch:{city}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return json.dumps(cached, ensure_ascii=False)

    # Fetch matochmat
    matochmat: list[dict] = []
    try:
        html = _fetch(f"{BASE_URL}/lunch/{city}/")
        matochmat = _parse_lunch_page(html)
    except Exception:
        pass

    # Fetch mylunch
    mylunch = _fetch_mylunch(city)

    merged = _merge_sources(matochmat, mylunch)

    # Own-site restaurants not present on the aggregators
    custom = _fetch_custom_sources(city)
    if custom:
        def _norm(name: str) -> str:
            return re.sub(r"[^a-z0-9]", "", name.lower())
        existing = {_norm(r["name"]) for r in merged}
        for r in custom:
            if _norm(r["name"]) not in existing:
                merged.append(r)

    if not merged:
        return json.dumps([{"error": f"No lunch data found for city '{city}'. Try a different slug."}])

    _cache_set(cache_key, merged)
    return json.dumps(merged, ensure_ascii=False)


@mcp.tool()
def get_logos(city: str) -> str:
    """
    Get base64-encoded logo images for all restaurants in a city.

    Args:
        city: City slug (e.g. "umea", "kalmar"). Use list_cities() for valid slugs.

    Returns:
        A JSON string — parse with JSON.parse() (JS) or json.loads() (Python).
        The parsed value is a dict mapping restaurant slug → data URL string,
        e.g. {"bistro-sjostugan": "data:image/jpeg;base64,..."}
        Use the data URL directly as an <img src> attribute — works in any context
        including sandboxed iframes (no external request needed).
        Restaurants without a logo are omitted from the dict.

        Note: when called via callMcpTool in a Cowork artifact the response arrives as
              {content: [{type:"text", text:"<this JSON string>"}]} — read content[0].text
              and JSON.parse it to get the dict.
    """
    city = city.lower().strip()

    cache_key = f"logos:{city}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return json.dumps(cached, ensure_ascii=False)

    restaurants: list[dict] = _cache_get(f"lunch:{city}") or []  # type: ignore[assignment]
    if not restaurants:
        try:
            html = _fetch(f"{BASE_URL}/lunch/{city}/")
        except httpx.HTTPStatusError:
            return json.dumps({})
        restaurants = _parse_lunch_page(html)
        _cache_set(f"lunch:{city}", restaurants)

    result: dict[str, str] = {}
    with httpx.Client(follow_redirects=True, timeout=10) as client:
        for r in restaurants:
            slug = r.get("slug", "")
            path = r.get("logo", "")
            if not (slug and path):
                continue
            try:
                resp = client.get(f"{BASE_URL}{path}", headers=HEADERS)
                resp.raise_for_status()
                ct = resp.headers.get("content-type", "image/jpeg").split(";")[0]
                b64 = base64.b64encode(resp.content).decode()
                result[slug] = f"data:{ct};base64,{b64}"
            except Exception:
                pass

    _cache_set(cache_key, result)
    return json.dumps(result, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Geocoding helpers (Nominatim, rate-limited, cached forever per session)
# ---------------------------------------------------------------------------

_geo_cache: dict[str, tuple[float, float] | None] = {}


def _geocode(name: str, city_display: str) -> tuple[float, float] | None:
    key = f"{name}|{city_display}"
    if key in _geo_cache:
        return _geo_cache[key]
    try:
        resp = httpx.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": f"{name}, {city_display}, Sverige", "format": "json", "limit": "1"},
            headers={"User-Agent": "mcp-lunch/1.0 (lunch guide)"},
            timeout=5,
            follow_redirects=True,
        )
        data = resp.json()
        coords = (float(data[0]["lat"]), float(data[0]["lon"])) if data else None
    except Exception:
        coords = None
    _geo_cache[key] = coords
    time.sleep(1.1)  # Nominatim rate limit: max 1 req/s
    return coords


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance in km between two WGS84 coordinates."""
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


@mcp.tool()
def get_lunch_near(city: str, lat: float, lon: float, radius_km: float = 1.0) -> str:
    """
    Get today's lunch menus for restaurants within a given radius of a location.

    Args:
        city:       City slug (e.g. "umea", "kalmar"). Use list_cities() for valid slugs.
        lat:        Latitude of the center point (WGS84, decimal degrees).
        lon:        Longitude of the center point (WGS84, decimal degrees).
        radius_km:  Search radius in kilometres. Default 1.0.

    Returns:
        A JSON string — parse with JSON.parse() (JS) or json.loads() (Python).
        Same restaurant/dish structure as get_lunch_guide, but with an added field:
          - distance_km (float): straight-line distance from the given point.
        Sorted nearest-first. Restaurants whose address could not be geocoded are excluded.

        Note: geocoding uses Nominatim (OpenStreetMap) with a 1 s rate limit between
        requests — the first call for a city may take 10–30 s depending on restaurant count.
        Subsequent calls within the same server session are instant (cached in memory).

        Note: when called via callMcpTool in a Cowork artifact the response arrives as
              {content: [{type:"text", text:"<this JSON string>"}]} — read content[0].text
              and JSON.parse it to get the array.
    """
    city = city.lower().strip()
    city_display = CITIES.get(city, city.capitalize())

    restaurants: list[dict] = _cache_get(f"lunch:{city}") or []  # type: ignore[assignment]
    if not restaurants:
        try:
            html = _fetch(f"{BASE_URL}/lunch/{city}/")
        except httpx.HTTPStatusError as e:
            return json.dumps([{"error": str(e)}])
        restaurants = _parse_lunch_page(html)
        _cache_set(f"lunch:{city}", restaurants)

    nearby = []
    for r in restaurants:
        coords = _geocode(r["name"], city_display)
        if coords is None:
            continue
        dist = _haversine(lat, lon, coords[0], coords[1])
        if dist <= radius_km:
            nearby.append({**r, "distance_km": round(dist, 2)})

    nearby.sort(key=lambda x: x["distance_km"])
    return json.dumps(nearby, ensure_ascii=False)


@mcp.tool()
def compare_city_prices(month: str | None = None) -> str:
    """
    Compare average lunch prices across cities using historical snapshot data.

    Requires the price tracker to have run at least once (DATABASE_URL must be set).

    Args:
        month: Optional filter in YYYY-MM format (e.g. "2026-06").
               If omitted, uses all available snapshots.

    Returns:
        A JSON string with a list of city summaries, sorted cheapest to most expensive:
          - city (str): City slug
          - display_name (str): Human-readable city name
          - avg_price (float): Average dish price in SEK
          - min_price (int): Cheapest dish recorded
          - max_price (int): Most expensive dish recorded
          - dish_count (int): Number of priced dishes in the snapshot(s)
          - snapshots (int): Number of monthly snapshots included
    """
    import os
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        return json.dumps({"error": "DATABASE_URL not set — price tracker has not been configured."})

    try:
        import psycopg2
        conn = psycopg2.connect(database_url)
        with conn.cursor() as cur:
            if month:
                cur.execute("""
                    SELECT
                        city,
                        ROUND(AVG(price)::numeric, 1)  AS avg_price,
                        MIN(price)                      AS min_price,
                        MAX(price)                      AS max_price,
                        COUNT(*)                        AS dish_count,
                        COUNT(DISTINCT snapshot_date)   AS snapshots
                    FROM lunch_prices
                    WHERE to_char(snapshot_date, 'YYYY-MM') = %s
                    GROUP BY city
                    ORDER BY avg_price ASC
                """, (month,))
            else:
                cur.execute("""
                    SELECT
                        city,
                        ROUND(AVG(price)::numeric, 1)  AS avg_price,
                        MIN(price)                      AS min_price,
                        MAX(price)                      AS max_price,
                        COUNT(*)                        AS dish_count,
                        COUNT(DISTINCT snapshot_date)   AS snapshots
                    FROM lunch_prices
                    GROUP BY city
                    ORDER BY avg_price ASC
                """)
            rows = cur.fetchall()
        conn.close()
    except Exception as e:
        return json.dumps({"error": f"Database query failed: {e}"})

    result = [
        {
            "city": row[0],
            "display_name": CITIES.get(row[0], row[0].capitalize()),
            "avg_price": float(row[1]),
            "min_price": row[2],
            "max_price": row[3],
            "dish_count": row[4],
            "snapshots": row[5],
        }
        for row in rows
    ]
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
def get_restaurant_menu(city: str, restaurant: str) -> str:
    """
    Get the full weekly lunch menu for a specific restaurant.

    Args:
        city: City slug (e.g. "umea"). Use list_cities() for valid slugs.
        restaurant: Restaurant slug from get_lunch_guide() (e.g. "bistro-le-garage").

    Returns:
        The full weekly menu as plain text, including all days of the week.
    """
    city = city.lower().strip()
    restaurant = restaurant.lower().strip()

    # Own-site restaurants have a dedicated weekly-menu fetcher
    if restaurant in _CUSTOM_MENU_FETCHERS:
        return _CUSTOM_MENU_FETCHERS[restaurant]()

    url = f"{BASE_URL}/lunch/{city}/{restaurant}/"

    try:
        html = _fetch(url)
    except httpx.HTTPStatusError as e:
        return f"Error: Could not fetch menu for '{restaurant}' in '{city}': {e}"

    text = _simple_text_parse(html)
    return text


# ---------------------------------------------------------------------------
# Static landing  +  image proxy  +  /lunchguide alias
# ---------------------------------------------------------------------------
#
# Pure ASGI middleware in front of the MCP app. Does NOT buffer the MCP
# response stream (important for SSE).
#
# ---------------------------------------------------------------------------
# Slack-triggered price check
# ---------------------------------------------------------------------------

def _run_price_check_background() -> None:
    """Run price_tracker.py in a subprocess and notify Slack when done."""
    import subprocess
    import sys
    try:
        result = subprocess.run(
            [sys.executable, str(Path(__file__).parent / "price_tracker.py")],
            capture_output=True, text=True, timeout=600,
        )
        if result.returncode == 0:
            _notify_slack(":white_check_mark: *mcp-lunch*: Priskoll klar! Databasen är uppdaterad.")
        else:
            _notify_slack(
                f":x: *mcp-lunch*: Priskoll misslyckades (exit {result.returncode}).\n"
                f"```{result.stderr[-500:]}```"
            )
    except Exception as e:
        _notify_slack(f":x: *mcp-lunch*: Priskoll kraschade: {e}")


# Routes handled here (everything else falls through to MCP):
#   GET /                         → site/index.html       (Aurora landing)
#   GET /canvas.html              → site/canvas.html      (3-variant review)
#   GET /design-canvas.jsx        → site/design-canvas.jsx
#   GET /variants/<name>.html     → site/variants/<name>.html
#   GET /image-proxy?path=/...    → proxies images from matochmat.se
#   *   /lunchguide(/*)           → rewritten to /mcp(/*)


class ImageProxyMiddleware:
    """ASGI middleware: static landing files, image proxy, /lunchguide alias."""

    _STATIC_DIR = (Path(__file__).parent / "site").resolve()

    # Explicit route table for root-level files
    _STATIC_ROUTES: dict[str, str] = {
        "/": "index.html",
        "/index.html": "index.html",
        "/canvas.html": "canvas.html",
        "/design-canvas.jsx": "design-canvas.jsx",
        "/favicon.ico": "favicon.ico",  # served if you add one, ignored otherwise
    }

    # Tiny fallback page if site/ has not been copied in yet
    _FALLBACK = (
        "<!doctype html><meta charset=\"utf-8\"><title>teamleader.se</title>"
        "<style>body{font-family:system-ui;max-width:560px;margin:6rem auto;"
        "padding:0 1.5rem;color:#222;line-height:1.6}code{background:#f4f4f4;"
        "padding:2px 6px;border-radius:4px;font-size:.9em}</style>"
        "<h1>teamleader.se</h1>"
        "<p>MCP server live at <code>/lunchguide</code>. "
        "Landing page assets not deployed yet — drop <code>site/index.html</code> "
        "into the repo and redeploy.</p>"
    ).encode("utf-8")

    def __init__(self, app):
        self.app = app

    @staticmethod
    async def _iter_body(receive):
        """Async generator that yields body chunks from an ASGI receive channel."""
        while True:
            msg = await receive()
            if msg["type"] == "http.request":
                chunk = msg.get("body", b"")
                if chunk:
                    yield chunk
                if not msg.get("more_body", False):
                    break

    @classmethod
    def _resolve_static(cls, req_path: str) -> Path | None:
        """Return the resolved file path if `req_path` maps to a safe static asset."""
        # Explicit alias map
        if req_path in cls._STATIC_ROUTES:
            return cls._STATIC_DIR / cls._STATIC_ROUTES[req_path]
        # Anything under /variants/, .html only, no traversal
        if req_path.startswith("/variants/") and req_path.endswith(".html"):
            candidate = (cls._STATIC_DIR / req_path.lstrip("/")).resolve()
            try:
                candidate.relative_to(cls._STATIC_DIR)
            except ValueError:
                return None
            return candidate
        return None

    @staticmethod
    def _content_type(path: Path) -> bytes:
        suffix = path.suffix.lower()
        if suffix == ".jsx":
            return b"application/javascript; charset=utf-8"
        if suffix == ".html":
            return b"text/html; charset=utf-8"
        if suffix == ".css":
            return b"text/css; charset=utf-8"
        if suffix == ".svg":
            return b"image/svg+xml"
        mt, _ = mimetypes.guess_type(path.name)
        if not mt:
            return b"application/octet-stream"
        if mt.startswith(("text/", "application/javascript", "application/json")):
            mt = f"{mt}; charset=utf-8"
        return mt.encode()

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        req_path: str = scope.get("path", "")
        method: str = scope.get("method", "GET").upper()

        # ── Static landing files ────────────────────────────────────────────
        if method in ("GET", "HEAD"):
            static_file = self._resolve_static(req_path)
            if static_file is not None:
                if static_file.is_file():
                    body = static_file.read_bytes()
                    cache = (
                        b"no-cache"
                        if static_file.suffix == ".html"
                        else b"public, max-age=3600"
                    )
                    await send({
                        "type": "http.response.start",
                        "status": 200,
                        "headers": [
                            [b"content-type", self._content_type(static_file)],
                            [b"content-length", str(len(body)).encode()],
                            [b"cache-control", cache],
                            [b"x-content-type-options", b"nosniff"],
                            [b"referrer-policy", b"strict-origin-when-cross-origin"],
                        ],
                    })
                    await send({
                        "type": "http.response.body",
                        "body": b"" if method == "HEAD" else body,
                    })
                    return
                # Route is known but file is missing → fall back gracefully on `/`
                if req_path in ("/", "/index.html"):
                    await send({
                        "type": "http.response.start",
                        "status": 200,
                        "headers": [
                            [b"content-type", b"text/html; charset=utf-8"],
                            [b"content-length", str(len(self._FALLBACK)).encode()],
                        ],
                    })
                    await send({"type": "http.response.body", "body": self._FALLBACK})
                    return

        # ── Slack-triggered price check ──────────────────────────────────────
        if req_path == "/lunchguide/run-price-check" and scope["method"] == "POST":
            import threading
            import hashlib
            import hmac
            from urllib.parse import parse_qs as _parse_qs
            body_parts = []
            async for chunk in self._iter_body(receive):
                body_parts.append(chunk)
            raw_body = b"".join(body_parts)

            # Verify request comes from Slack using Signing Secret
            signing_secret = os.environ.get("SLACK_PRICE_CHECK_TOKEN", "").encode()
            if signing_secret:
                headers_dict = {k.decode(): v.decode() for k, v in scope.get("headers", [])}
                timestamp = headers_dict.get("x-slack-request-timestamp", "")
                slack_sig = headers_dict.get("x-slack-signature", "")
                sig_basestring = f"v0:{timestamp}:{raw_body.decode()}".encode()
                expected = "v0=" + hmac.new(signing_secret, sig_basestring, hashlib.sha256).hexdigest()
                if not hmac.compare_digest(expected, slack_sig):
                    await send({"type": "http.response.start", "status": 401,
                                "headers": [[b"content-type", b"text/plain"]]})
                    await send({"type": "http.response.body", "body": b"Unauthorized"})
                    return

            # Respond immediately to Slack (must reply within 3 s)
            ack = ":hourglass: Startar priskoll\u2026".encode("utf-8")
            await send({"type": "http.response.start", "status": 200,
                        "headers": [[b"content-type", b"text/plain; charset=utf-8"]]})
            await send({"type": "http.response.body", "body": ack})
            # Run price_tracker.py in a background thread
            threading.Thread(target=_run_price_check_background, daemon=True).start()
            return

        # ── Image proxy ─────────────────────────────────────────────────────
        if req_path == "/lunchguide/image-proxy":
            qs = parse_qs(scope.get("query_string", b"").decode())
            img_path = (qs.get("path") or [""])[0]

            if img_path and img_path.startswith("/assets/"):
                try:
                    async with httpx.AsyncClient(
                        follow_redirects=True, timeout=10
                    ) as client:
                        resp = await client.get(f"{BASE_URL}{img_path}", headers=HEADERS)
                        resp.raise_for_status()
                    ct = resp.headers.get("content-type", "image/jpeg").encode()
                    body = resp.content
                    await send({
                        "type": "http.response.start",
                        "status": 200,
                        "headers": [
                            [b"content-type", ct],
                            [b"content-length", str(len(body)).encode()],
                            [b"cache-control", b"public, max-age=3600"],
                            [b"access-control-allow-origin", b"*"],
                        ],
                    })
                    await send({"type": "http.response.body", "body": body})
                    return
                except Exception as exc:
                    msg = f"Proxy error: {exc}".encode()
                    await send({
                        "type": "http.response.start",
                        "status": 502,
                        "headers": [[b"content-type", b"text/plain"]],
                    })
                    await send({"type": "http.response.body", "body": msg})
                    return

            await send({
                "type": "http.response.start",
                "status": 400,
                "headers": [[b"content-type", b"text/plain"]],
            })
            await send({"type": "http.response.body", "body": b"Bad request"})
            return

        await self.app(scope, receive, send)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import os

    transport = os.environ.get("MCP_TRANSPORT", "sse")

    if transport == "stdio":
        # Local usage via Claude Desktop / Claude Code
        mcp.run(transport="stdio")
    else:
        # HTTP server — default, used on Railway and any other hosted environment
        import uvicorn
        port = int(os.environ.get("PORT", "8000"))
        mcp_app = mcp.streamable_http_app()
        app = ImageProxyMiddleware(mcp_app)
        uvicorn.run(app, host="0.0.0.0", port=port)
