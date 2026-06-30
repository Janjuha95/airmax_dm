"""Context-managed Postgres connection built from DATABASE_URL.

We parse the URL ourselves and pass discrete kwargs rather than handing the DSN string to
libpq, because libpq splits userinfo on the *first* '@' — so a literal '@' in the password
mis-parses the host. urllib.parse.urlsplit splits on the *last* '@' and is correct.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from urllib.parse import unquote, urlsplit

import psycopg2
from dotenv import load_dotenv

from .config import BASE_DIR


def load_env() -> Path | None:
    """Load .env (note: on this machine it's a `.env` directory containing a `.env` file)."""
    for candidate in (BASE_DIR / ".env" / ".env", BASE_DIR / ".env", BASE_DIR / ".env.txt"):
        if candidate.is_file():
            load_dotenv(candidate)
            return candidate
    load_dotenv()
    return None


def _dsn_kwargs() -> dict:
    load_env()
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set — copy .env.example to .env and fill it in")
    u = urlsplit(url)
    return dict(
        host=u.hostname,
        port=u.port or 5432,
        user=unquote(u.username) if u.username else None,
        password=unquote(u.password) if u.password else None,
        dbname=u.path.lstrip("/") or None,
    )


@contextmanager
def connection() -> Iterator["psycopg2.extensions.connection"]:
    """Yield a psycopg2 connection and always close it."""
    conn = psycopg2.connect(**_dsn_kwargs())
    try:
        yield conn
    finally:
        conn.close()
