# matochmat-lunch-mcp

An MCP server that fetches today's lunch guides from [matochmat.se](https://www.matochmat.se) for Swedish cities.

## Tools

| Tool | Description |
|------|-------------|
| `list_cities` | Returns all supported city slugs and their display names |
| `get_lunch_guide(city)` | Today's full lunch list for every restaurant in a city |
| `get_restaurant_menu(city, restaurant)` | Full weekly lunch menu for one specific restaurant |

## Installation

### 1. Clone / copy the project

```bash
cd matochmat-lunch-mcp
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
    "matochmat-lunch": {
      "command": "python",
      "args": ["/FULL/PATH/TO/matochmat-lunch-mcp/server.py"]
    }
  }
}
```

Replace `/FULL/PATH/TO/` with the actual path to the project folder.

If you're using a virtual environment or `uv`:

```json
{
  "mcpServers": {
    "matochmat-lunch": {
      "command": "uv",
      "args": [
        "run",
        "--with", "mcp[cli]",
        "--with", "httpx",
        "--with", "beautifulsoup4",
        "python",
        "/FULL/PATH/TO/matochmat-lunch-mcp/server.py"
      ]
    }
  }
}
```

## Configure in Claude Code (CLI)

```bash
claude mcp add matochmat-lunch -- python /FULL/PATH/TO/matochmat-lunch-mcp/server.py
```

## Example usage

Once connected, you can ask Claude things like:

- "What's for lunch in Umeå today?"
- "Show me the lunch options in Stockholm"
- "What does Bistro Le Garage serve this week?"
- "Find a vegetarian lunch in Göteborg"

## Supported cities

Run `list_cities` to get the full list. Some examples:

`umea`, `stockholm`, `goteborg`, `malmo`, `linkoping`, `lulea`, `skelleftea`, `ostersund`, `gavle`, `sundsvall`, `vasteras`, `orebro`, `uppsala`, `helsingborg`, `lund`, `karlskrona`, `jonkoping`, `vaxjo`, `boras` and many more.

## Deploy to Railway (free)

Railway lets you host the MCP server in the cloud so any machine can use it without a local Python install.

### Steps

1. **Push the project to GitHub** (or use Railway's CLI directly)

2. **Create a Railway project** at [railway.app](https://railway.app) → *New Project* → *Deploy from GitHub repo* → select this repo

3. **Set the environment variable** (already in `railway.toml`, but double-check in the Railway dashboard):
   ```
   MCP_TRANSPORT=sse
   ```

4. **Generate a public domain** in the Railway dashboard:
   *Settings* → *Networking* → *Generate Domain*
   You'll get a URL like `https://matochmat-lunch-mcp-production.up.railway.app`

5. **Connect Claude Desktop** — update `claude_desktop_config.json`:
   ```json
   {
     "mcpServers": {
       "matochmat-lunch": {
         "url": "https://YOUR-APP.up.railway.app/sse"
       }
     }
   }
   ```

6. **Connect Claude Code (CLI)**:
   ```bash
   claude mcp add --transport sse matochmat-lunch https://YOUR-APP.up.railway.app/sse
   ```

The health check endpoint is at `/health` — Railway uses it to confirm the service is up.

> **Cost:** Railway gives $5 free credit per month. This server is very lightweight (only active during tool calls), so it should comfortably stay within the free tier for personal use.

## Notes

- Menus are updated daily by the restaurants themselves on matochmat.se.
- City slugs use Swedish characters replaced with ASCII (e.g. `umea` for Umeå, `goteborg` for Göteborg).
- The server fetches live data on every tool call — no caching.
