"""
mcp-lunch
---------
An MCP server that fetches today's lunch guides for Swedish cities.

Tools exposed:
  • list_cities          – list all available city slugs
  • get_lunch_guide      – today's full lunch list for a city
  • get_restaurant_menu  – full weekly lunch menu for one restaurant
"""

import re
import time
import math
import base64
import json
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

    # Drop entries that are clearly not dishes (e.g. lone "Stängt" with no price)
    # but keep them as a status signal
    return dishes


def _simple_text_parse(html: str) -> str:
    """Return clean text from HTML, used for restaurant detail pages."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    # Remove scripts and styles
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    # Collapse blank lines
    lines = [l.strip() for l in text.splitlines()]
    lines = [l for l in lines if l]
    return "\n".join(lines)


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

    Args:
        city: City slug (e.g. "umea", "stockholm", "goteborg").
              Use list_cities() to get valid slugs.

    Returns:
        A JSON string — parse with JSON.parse() (JS) or json.loads() (Python).
        The parsed value is a list of restaurant objects, each containing:
          - name (str): Restaurant name
          - slug (str): Restaurant slug for use with get_restaurant_menu and get_logos
          - city (str): City slug
          - logo (str): Relative path to logo image (prepend base URL, or use get_logos for base64)
          - url (str): Direct link to the restaurant's page
          - dishes (list): Today's dishes, each with:
              - name (str): Dish name or description line
              - price (int|None): Price in SEK, null for description lines
              - vegetarian (bool): Whether the dish is vegetarian
              - tags (list[str]): Dietary tags e.g. ["vegetarisk", "glutenfri"]
              - closed (bool): True if restaurant is closed today

        Note: dishes with price=null are description lines for the preceding priced dish.
        Note: when called via callMcpTool in a Cowork artifact the response arrives as
              {content: [{type:"text", text:"<this JSON string>"}]} — read content[0].text
              and JSON.parse it to get the array.
    """
    city = city.lower().strip()

    cache_key = f"lunch:{city}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return json.dumps(cached, ensure_ascii=False)

    url = f"{BASE_URL}/lunch/{city}/"
    try:
        html = _fetch(url)
    except httpx.HTTPStatusError as e:
        return json.dumps([{"error": f"Could not fetch lunch guide for '{city}': {e}"}])

    restaurants = _parse_lunch_page(html)
    if not restaurants:
        return json.dumps([{"error": f"No lunch data found for city '{city}'. Try a different slug."}])

    _cache_set(cache_key, restaurants)
    return json.dumps(restaurants, ensure_ascii=False)


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

    # Re-use cached lunch data when possible
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

    # Get (or fetch) the full restaurant list
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
    url = f"{BASE_URL}/lunch/{city}/{restaurant}/"

    try:
        html = _fetch(url)
    except httpx.HTTPStatusError as e:
        return f"Error: Could not fetch menu for '{restaurant}' in '{city}': {e}"

    text = _simple_text_parse(html)
    return text


# ---------------------------------------------------------------------------
# Image proxy — pure ASGI, does NOT buffer responses (safe for streaming MCP)
# ---------------------------------------------------------------------------


class ImageProxyMiddleware:
    """
    Intercepts GET /image-proxy?path=/assets/... and proxies the image.
    All other requests are passed straight through to the MCP app unchanged.
    """

    def __init__(self, app):
        self.app = app

    _LANDING = (
        "<!doctype html>"
        '<html lang="sv">'
        "<head><meta charset=\"utf-8\"><title>Lunch-guide MCP</title>"
        "<style>body{font-family:system-ui,sans-serif;max-width:520px;margin:4rem auto;padding:0 1.5rem;color:#222}"
        "h1{font-size:1.4rem;font-weight:500;margin-bottom:.5rem}"
        "p{color:#555;line-height:1.7}code{background:#f4f4f4;padding:2px 6px;border-radius:4px;font-size:.9em}</style>"
        "</head><body>"
        "<h1>Lunch-guide MCP</h1>"
        "<p>MCP-server som listar svenska restaurangers lunchmenyer.</p>"
        "<p>Anslut via:<br><code>https://teamleader.se/lunchguide</code></p>"
        "<p>Tillgangliga verktyg: <code>list_cities</code> &middot; <code>get_lunch_guide</code> &middot; "
        "<code>get_lunch_near</code> &middot; <code>get_logos</code> &middot; <code>get_restaurant_menu</code></p>"
        "</body></html>"
    ).encode("utf-8")

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        req_path: str = scope.get("path", "")

        # ── Landing page ────────────────────────────────────────────────────
        if req_path in ("", "/"):
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [[b"content-type", b"text/html; charset=utf-8"],
                            [b"content-length", str(len(self._LANDING)).encode()]],
            })
            await send({"type": "http.response.body", "body": self._LANDING})
            return

        # ── Image proxy ─────────────────────────────────────────────────────
        if req_path == "/image-proxy":
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

        # ── Path alias: /lunchguide → /mcp ─────────────────────────────────
        if req_path == "/lunchguide" or req_path.startswith("/lunchguide/"):
            new_path = "/mcp" + req_path[len("/lunchguide"):]
            scope = {**scope, "path": new_path, "raw_path": new_path.encode()}

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
