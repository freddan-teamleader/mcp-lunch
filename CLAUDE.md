# CLAUDE.md

Guidance for Claude (and humans) working in this repo.

## What this is

`lunch-guide` — an MCP server that fetches Swedish restaurant lunch menus.
Single-file core: **`server.py`** (FastMCP). Everything (tools, parsers,
static landing, image proxy) lives there. `price_tracker.py` is a separate
batch job for historical price snapshots.

### MCP tools exposed
| Tool | Purpose |
|------|---------|
| `list_cities()` | slug → display name map |
| `get_lunch_guide(city)` | today's menus, all restaurants in a city (merged sources) |
| `get_restaurant_menu(city, restaurant)` | one restaurant's full week (plain text) |
| `get_lunch_near(city, lat, lon, radius_km)` | today's menus near a point (Nominatim geocoding) |
| `get_logos(city)` | base64 data-URL logos per restaurant slug |
| `compare_city_prices(month?)` | avg/min/max prices from the price DB (needs `DATABASE_URL`) |

## Dev commands

```bash
# Always use the project venv
.venv/bin/python -m py_compile server.py        # syntax check
.venv/bin/python server.py                      # run (stdio if MCP_TRANSPORT=stdio)

# Interactive parser testing (no network needed — monkeypatch _fetch):
.venv/bin/python3 -i
>>> import server
>>> server._fetch = lambda url: "<html>...</html>"   # feed saved HTML
>>> server._parse_byttan_weekly(server._fetch(""))
```

`MCP_TRANSPORT` controls transport: `sse` (default, hosted) or `stdio` (local
Claude Desktop / Claude Code). HTTP server runs via uvicorn wrapped in
`ImageProxyMiddleware` (static landing at `/`, image proxy, `/lunchguide` alias).

## Data sources

Three kinds, merged in `get_lunch_guide`:
1. **matochmat.se** — `_parse_lunch_page()` (city page → restaurants).
2. **mylunch.se** — `_parse_mylunch_page()` + per-restaurant `_parse_mylunch_restaurant_today()`.
   Optional LLM cleanup of noisy dishes via Claude Haiku when `ANTHROPIC_API_KEY` is set.
3. **Own-site restaurants** — restaurants not on either aggregator, parsed from
   their own website. This is the part to know well (see next section).

## Adding an own-site restaurant (the `_fetch_x_today` pattern)

Restaurants that publish menus only on their own site get a small dedicated
parser. See the **"Custom single-restaurant sources"** section in `server.py`.
Byttan i Parken (`byttaniparken.se`) is the reference implementation.

Three functions per restaurant:

- **`_parse_<name>_weekly(html) -> dict`** — the actual parser. Returns
  `{"days": {"tisdag": [dish,...], ..., "helg": [...]}, "standing": [dish,...]}`.
  Keep it **text-anchor based**: drive the state machine off the Swedish section
  labels in `get_text()` lines (e.g. `"veckans lunch"`, weekday names,
  `"helglunch"`, next section `"bistro"`) — NOT CSS classes, which break on
  redesigns. Reusable helper `_is_desc()` distinguishes description lines
  (lead with "Med ..." or end in ".") from dish-name lines.

- **`_fetch_<name>_today(city_slug) -> dict | None`** — picks today's day
  (`_today_sv()`), assembles dishes, returns a restaurant dict in the standard
  shape (below). Returns `None` on fetch failure so the rest of the guide still works.

- **`_<name>_weekly_text() -> str`** — full week as plain text for `get_restaurant_menu`.

Then register in the three maps at the bottom of that section:

```python
CUSTOM_SOURCES = { "kalmar": ["byttan"] }          # city slug → source keys
_CUSTOM_TODAY_FETCHERS = { "byttan": _fetch_byttan_today }   # key → today fn
_CUSTOM_MENU_FETCHERS  = { "byttan-i-parken": _byttan_weekly_text }  # restaurant slug → weekly fn
```

Wiring (already done — don't duplicate):
- `get_lunch_guide` calls `_fetch_custom_sources(city)` and appends results,
  deduped against aggregator names by normalized name.
- `get_restaurant_menu` checks `_CUSTOM_MENU_FETCHERS[restaurant]` before
  falling through to the matochmat URL fetch.

## Standard restaurant dict shape

Every source (aggregator or own-site) must produce this shape so the JSON stays uniform:

```python
{
  "name": str,            # display name
  "slug": str,            # url-safe id (also used by get_restaurant_menu)
  "city": str,            # city slug
  "logo": str,            # url or "" (own-site can use an absolute https url)
  "url": str,             # link to the menu page
  "source": str,          # "matochmat.se" | "mylunch.se" | "<ownsite>.se"
  "dishes": [
    {
      "name": str,        # dish (own-site joins description as "Name – desc")
      "price": int|None,  # SEK, None for info/header lines
      "vegetarian": bool,
      "tags": list[str],  # e.g. ["vegetarisk", "glutenfri"]
      "closed": bool,     # True = restaurant/lunch closed that day
    }, ...
  ],
}
```

## Logos

Aim to give **every restaurant a usable logo** in the `logo` field.

- Aggregator restaurants: logos are relative paths under matochmat
  (`/assets/uploads/...`); `get_logos(city)` turns them into base64 data URLs.
- Own-site restaurants: use an absolute `https://` URL to a logo asset.
  Prefer a mark that reads on **both light and dark backgrounds** — avoid
  variants tuned for one background (e.g. a cream/white mark disappears on the
  light card surfaces most clients render). For Byttan we use
  `byttan_emblem.svg` (the colored emblem), not the cream or black-only variants.
- `_fetch_logo_data_url(path)` is the shared helper: relative paths get the
  matochmat prefix, absolute URLs are fetched as-is. Both `get_logos` and
  `get_logo` use it, so own-site logos resolve correctly.
- **`get_logos(city)`** returns every logo in one dict — for a city with many
  restaurants this can exceed the ~1 MB tool-response limit. To embed a logo in
  a size-constrained context (e.g. an inline chat widget), use
  **`get_logo(city, restaurant)`** which returns a single restaurant's data URL.

## Caching & TTLs

- `_cache` (in-memory): lunch data + logos, `CACHE_TTL = 1800` (30 min).
  Note: README historically said "no caching" — the code DOES cache 30 min.
- `_SITEMAP_CACHE`: mylunch sitemap, 24 h. `_geo_cache`: Nominatim, per process.
- Custom sources are fetched inside `get_lunch_guide`, so they share the 30-min
  city cache once merged.

## Gotchas

- **Byttan onsdag** is usually `"Byttan abonnerad"` → parsed as a `closed: True`
  dish. That's correct, not a bug.
- **Monday** has no weekday lunch at Byttan (served tis–fre); `_fetch_byttan_today`
  returns an info line + the standing soup instead.
- Own-site pages may be JS-rendered. `httpx` only sees server HTML — if a site
  ships an empty SPA shell, the parser gets nothing. Byttan is static today.
- City slugs are ASCII (`umea`, `goteborg`). `_today_sv()` returns lowercase
  Swedish weekday; weekend maps to the `"helg"` key.

## Deploy

Railway, `MCP_TRANSPORT=sse`. Optional env: `ANTHROPIC_API_KEY` (mylunch LLM
cleanup), `DATABASE_URL` (price tracker), `SLACK_WEBHOOK_URL` /
`SLACK_PRICE_CHECK_TOKEN` (Slack-triggered price check). See README for the
full Railway walkthrough.
