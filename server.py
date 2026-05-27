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
import httpx
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
# Helpers
# ---------------------------------------------------------------------------


def _fetch(url: str) -> str:
    """Fetch a URL and return the text content."""
    with httpx.Client(follow_redirects=True, timeout=15) as client:
        resp = client.get(url, headers=HEADERS)
        resp.raise_for_status()
        return resp.text


def _parse_lunch_page(html: str) -> list[dict]:
    """
    Parse the rendered HTML from a lunch city page.
    Returns a list of restaurant dicts, each with:
      name, slug, url, dishes (list of {name, price, vegetarian, tags})

    Strategy: replace <img alt="..."> with the alt text so that
    get_text() surfaces the "Name i City lunchmeny" marker that
    the site embeds in every restaurant logo's alt attribute.
    Then split the full page text into per-restaurant sections.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    # Inline img alt text so get_text() picks up the "lunchmeny" markers
    for img in soup.find_all("img"):
        img.replace_with(img.get("alt", ""))

    # ── Collect unique slug → href (skip "Veckansluncher" navigation links) ──
    slug_to_href: dict[str, str] = {}
    for a in soup.find_all("a", href=re.compile(r"^/lunch/[^/]+/[^/]+/?$")):
        if "veckansluncher" in a.get_text(strip=True).lower():
            continue
        href = a.get("href", "")
        parts = [p for p in href.strip("/").split("/") if p]
        if len(parts) == 3 and parts[2] not in slug_to_href:
            slug_to_href[parts[2]] = href

    # Infer city slug from any collected href
    city_slug = ""
    if slug_to_href:
        first = next(iter(slug_to_href.values()))
        p = first.strip("/").split("/")
        city_slug = p[1] if len(p) >= 2 else ""

    # ── Split page text into sections by "Name i City lunchmeny" lines ──
    LUNCHMENY_RE = re.compile(
        r"^(.+?)\s+i\s+\S.*?\s+lunchmeny\b", re.IGNORECASE
    )
    SKIP = frozenset({"veckansluncher", "hitta hit", "visa alla lunchrätter"})

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
        elif cur_name is not None and line.lower() not in SKIP:
            cur_lines.append(line)

    if cur_name is not None:
        sections.append((cur_name, cur_lines))

    # ── Match each section to a known slug ──────────────────────────────
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
        best_slug = slug_to_href.get(guess)
        best_href: str | None = None

        if best_slug:
            best_href = slug_to_href[guess]
            best_slug = guess
        else:
            # Substring fallback
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
            "url": f"{BASE_URL}{best_href}",
            "dishes": _parse_dishes(dish_lines),
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
def get_lunch_guide(city: str) -> list[dict]:
    """
    Get today's lunch menus for all restaurants in a Swedish city.

    Args:
        city: City slug (e.g. "umea", "stockholm", "goteborg").
              Use list_cities() to get valid slugs.

    Returns:
        A list of restaurant objects, each containing:
          - name (str): Restaurant name
          - slug (str): Restaurant slug for use with get_restaurant_menu
          - url (str): Direct link to the restaurant's page
          - dishes (list): Today's dishes, each with:
              - name (str): Dish name and description
              - price (int|None): Price in SEK
              - vegetarian (bool): Whether the dish is vegetarian
    """
    city = city.lower().strip()
    if city not in CITIES:
        # Try anyway – new cities may have been added to the site
        pass

    url = f"{BASE_URL}/lunch/{city}/"
    try:
        html = _fetch(url)
    except httpx.HTTPStatusError as e:
        return [{"error": f"Could not fetch lunch guide for '{city}': {e}"}]

    restaurants = _parse_lunch_page(html)
    if not restaurants:
        return [{"error": f"No lunch data found for city '{city}'. Try a different slug."}]

    return restaurants


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
        uvicorn.run(mcp.streamable_http_app(), host="0.0.0.0", port=port)
