"""
price_tracker.py
----------------
Monthly price tracker for Swedish restaurant lunch menus.

Fetches lunch data for configured cities, stores prices in Postgres,
and logs any price changes since the previous snapshot.

Usage:
    python price_tracker.py

Environment variables:
    DATABASE_URL  – PostgreSQL connection string (set automatically by Railway)
    TRACK_CITIES  – comma-separated city slugs to track (default: kalmar,karlskrona)
"""

import logging
import os
import sys
from datetime import date

import psycopg2
from psycopg2.extras import execute_values

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# Resolved lazily in main() so that importing this module never raises KeyError.
DATABASE_URL: str | None = None
# If TRACK_CITIES is set, use that – otherwise track all available cities.
_env_cities = os.environ.get("TRACK_CITIES", "")
TRACK_CITIES = _env_cities.split(",") if _env_cities else None


# ---------------------------------------------------------------------------
# Database setup
# ---------------------------------------------------------------------------

DDL = """
CREATE TABLE IF NOT EXISTS lunch_prices (
    id          SERIAL PRIMARY KEY,
    city        TEXT        NOT NULL,
    restaurant  TEXT        NOT NULL,
    dish        TEXT        NOT NULL,
    price       INTEGER     NOT NULL,
    snapshot_date DATE      NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (city, restaurant, dish, snapshot_date)
);

CREATE TABLE IF NOT EXISTS price_changes (
    id              SERIAL PRIMARY KEY,
    city            TEXT        NOT NULL,
    restaurant      TEXT        NOT NULL,
    dish            TEXT        NOT NULL,
    old_price       INTEGER     NOT NULL,
    new_price       INTEGER     NOT NULL,
    change_date     DATE        NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


def get_conn():
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL environment variable is not set")
    return psycopg2.connect(url)


def ensure_tables(conn):
    with conn.cursor() as cur:
        cur.execute(DDL)
    conn.commit()
    log.info("Tables OK")


# ---------------------------------------------------------------------------
# Snapshot logic
# ---------------------------------------------------------------------------


def fetch_restaurants(city: str) -> list[dict]:
    """Fetch lunch data by calling matochmat.se directly (no MCP import needed)."""
    sys.path.insert(0, os.path.dirname(__file__))
    from server import _fetch, _parse_lunch_page

    try:
        html = _fetch(f"https://www.matochmat.se/lunch/{city}/")
        return _parse_lunch_page(html)
    except Exception as e:
        return [{"error": str(e)}]


def get_previous_prices(conn, city: str) -> dict[tuple, int]:
    """Return {(city, restaurant, dish): price} for the most recent snapshot."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (city, restaurant, dish)
                city, restaurant, dish, price
            FROM lunch_prices
            WHERE city = %s
            ORDER BY city, restaurant, dish, snapshot_date DESC
        """,
            (city,),
        )
        return {(row[0], row[1], row[2]): row[3] for row in cur.fetchall()}


def save_snapshot(conn, city: str, restaurants: list[dict], today: date):
    """Insert today's prices, skip duplicates (UNIQUE constraint)."""
    rows = []
    for r in restaurants:
        name = r["name"]
        for dish in r.get("dishes", []):
            if dish.get("price") is None:
                continue
            rows.append((city, name, dish["name"], dish["price"], today))

    if not rows:
        log.warning(f"No priced dishes found for {city}")
        return

    with conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO lunch_prices (city, restaurant, dish, price, snapshot_date)
            VALUES %s
            ON CONFLICT (city, restaurant, dish, snapshot_date) DO NOTHING
        """,
            rows,
        )
    conn.commit()
    log.info(f"Saved {len(rows)} dish prices for {city}")


def detect_changes(conn, city: str, restaurants: list[dict], prev: dict[tuple, int], today: date):
    """Compare today's prices with previous snapshot, log and save changes."""
    changes = []
    for r in restaurants:
        name = r["name"]
        for dish in r.get("dishes", []):
            if dish.get("price") is None:
                continue
            key = (city, name, dish["name"])
            old = prev.get(key)
            new = dish["price"]
            if old is not None and old != new:
                diff = new - old
                sign = "▲" if diff > 0 else "▼"
                log.info(f"{sign} PRICE CHANGE: {name} / {dish['name']}: {old} kr → {new} kr ({diff:+d} kr)")
                changes.append((city, name, dish["name"], old, new, today))

    if changes:
        with conn.cursor() as cur:
            execute_values(
                cur,
                """
                INSERT INTO price_changes
                    (city, restaurant, dish, old_price, new_price, change_date)
                VALUES %s
            """,
                changes,
            )
        conn.commit()
        log.info(f"Recorded {len(changes)} price changes for {city}")
    else:
        log.info(f"No price changes detected for {city}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    today = date.today()
    log.info(f"Price tracker starting — snapshot date: {today}")

    from server import list_cities

    cities = TRACK_CITIES or list(list_cities().keys())
    log.info(f"Tracking {len(cities)} cities: {', '.join(cities)}")

    conn = get_conn()
    ensure_tables(conn)

    for city in cities:
        city = city.strip()
        log.info(f"--- {city} ---")
        try:
            restaurants = fetch_restaurants(city)
            if restaurants and "error" in restaurants[0]:
                log.error(f"MCP error for {city}: {restaurants[0]['error']}")
                continue
            prev = get_previous_prices(conn, city)
            detect_changes(conn, city, restaurants, prev, today)
            save_snapshot(conn, city, restaurants, today)
        except Exception as e:
            log.exception(f"Failed to process {city}: {e}")

    conn.close()
    log.info("Done.")


if __name__ == "__main__":
    main()
