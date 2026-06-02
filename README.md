# Lunch Guide MCP

An MCP server that fetches today's lunch menus for Swedish cities.

Menus are updated daily by the restaurants themselves.

## Tools

| Tool | Description |
|------|-------------|
| `list_cities` | Returns all supported city slugs and their display names |
| `get_lunch_guide(city)` | Today's full lunch list for every restaurant in a city |
| `get_restaurant_menu(city, restaurant)` | Full weekly lunch menu for one specific restaurant |

## Installation

### 1. Clone the repo

```bash
git clone git@bitbucket.org:infomaker/mcp-lunch.git
cd mcp-lunch
```

### 2. Install dependencies (uv recommended)

```bash
uv pip install -e .
```

Or with plain pip:

```bash
pip install mcp[cli] httpx beautifulsoup4
```

### 3. Test it works

```bash
python server.py
```

You should see the MCP server start up on stdio.

## Configure in Claude Desktop

Add the following to your `claude_desktop_config.json`
(usually at `~/Library/Application Support/Claude/claude_desktop_config.json` on macOS):

```json
{
  "mcpServers": {
    "lunch-guide": {
      "command": "python",
      "args": ["/FULL/PATH/TO/mcp-lunch/server.py"],
      "env": { "MCP_TRANSPORT": "stdio" }
    }
  }
}
```

If you're using a virtual environment or `uv`:

```json
{
  "mcpServers": {
    "lunch-guide": {
      "command": "uv",
      "args": [
        "run",
        "--with", "mcp[cli]",
        "--with", "httpx",
        "--with", "beautifulsoup4",
        "python",
        "/FULL/PATH/TO/mcp-lunch/server.py"
      ],
      "env": { "MCP_TRANSPORT": "stdio" }
    }
  }
}
```

## Configure in Claude Code (CLI)

```bash
MCP_TRANSPORT=stdio claude mcp add lunch-guide -- python /FULL/PATH/TO/mcp-lunch/server.py
```

## Example usage

Once connected, you can ask things like:

- "What's for lunch in Umeå today?"
- "Show me the lunch options in Stockholm"
- "What does Bistro Le Garage serve this week?"
- "Find a vegetarian lunch in Göteborg"

## Supported cities

Run `list_cities` to get the full list. Some examples:

`umea`, `stockholm`, `goteborg`, `malmo`, `linkoping`, `lulea`, `skelleftea`, `ostersund`, `gavle`, `sundsvall`, `vasteras`, `orebro`, `uppsala`, `helsingborg`, `lund`, `karlskrona`, `jonkoping`, `vaxjo`, `boras` and many more.

## Data sources

Menus are merged from multiple sources:

- **matochmat.se** and **mylunch.se** — aggregator sites covering most cities.
- **Own-site restaurants** — restaurants that publish their menu only on their
  own website get a small dedicated parser. Currently:
  - **Byttan i Parken** (Kalmar) — `byttaniparken.se`, slug `byttan-i-parken`.
  - **Gubben i Matlådan** (Färjestaden) — `gubbenimatladan.se`, slug `gubben-i-matladan`.

Färjestaden has no aggregator coverage, so its guide (`get_lunch_guide("farjestaden")`)
is built entirely from own-site sources.

To add another own-site restaurant, see the *Custom single-restaurant sources*
section in `server.py`: write a `_fetch_<name>_today()` (and optional weekly-text)
function and register it in `CUSTOM_SOURCES` / `_CUSTOM_TODAY_FETCHERS` /
`_CUSTOM_MENU_FETCHERS`. The parsers are text-anchor based (keyed off Swedish
section labels) rather than CSS-class based, so they tolerate minor markup changes.

## Deploy to Railway (free)

Railway lets you host the server in the cloud so any machine can use it without a local Python install.

### Steps

1. **Push the repo to GitHub** (or use Railway's CLI directly)

2. **Create a Railway project** at [railway.app](https://railway.app) → *New Project* → *Deploy from GitHub repo* → select this repo

3. **Set the environment variable** (already in `railway.toml`, but double-check in the Railway dashboard):
   ```
   MCP_TRANSPORT=sse
   ```

4. **Generate a public domain** in the Railway dashboard:
   *Settings* → *Networking* → *Generate Domain*
   You'll get a URL like `https://mcp-lunch-production.up.railway.app`

5. **Connect Claude Desktop** — update `claude_desktop_config.json`:
   ```json
   {
     "mcpServers": {
       "lunch-guide": {
         "url": "https://YOUR-APP.up.railway.app/mcp"
       }
     }
   }
   ```

6. **Connect Claude Code (CLI)**:
   ```bash
   claude mcp add --transport sse lunch-guide https://YOUR-APP.up.railway.app/mcp
   ```

The health check endpoint is at `/health` — Railway uses it to confirm the service is up.

> **Cost:** Railway gives $5 free credit per month. This server is very lightweight (only active during tool calls), so it should comfortably stay within the free tier for personal use.

## Notes

- Menus are updated daily by the restaurants themselves.
- City slugs use ASCII versions of Swedish characters (e.g. `umea` for Umeå, `goteborg` for Göteborg).
- The server fetches live data on every tool call — no caching.
- Transport mode is controlled by the `MCP_TRANSPORT` env var: `sse` (default, for Railway and other hosted environments) or `stdio` (for local use via Claude Desktop / Claude Code).
