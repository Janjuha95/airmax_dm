"""db.py — load DATABASE_URL from .env and hand out psycopg2 connections.

On this machine the env file lives at .env/.env (a `.env` *directory* containing a
`.env` file), which python-dotenv won't find by default, so we point at it explicitly.
We never print the URL (it carries the password).
"""

import os
from pathlib import Path
from urllib.parse import urlsplit, unquote

import psycopg2
from dotenv import load_dotenv

_HERE = Path(__file__).resolve().parent


def load_env() -> Path | None:
    """Load the .env file from the known candidate locations; return the path used."""
    for candidate in (_HERE / ".env" / ".env", _HERE / ".env", _HERE / ".env.txt"):
        if candidate.is_file():
            load_dotenv(candidate)
            return candidate
    load_dotenv()  # fall back to python-dotenv's default search
    return None


def database_url() -> str:
    load_env()
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set — check .env/.env")
    return url


def connect():
    """Return a new psycopg2 connection from DATABASE_URL.

    We parse the URL ourselves and pass discrete keyword args, rather than letting
    libpq parse the DSN string. libpq splits userinfo on the *first* '@', so a literal
    '@' in the password mis-parses the host; urlsplit splits on the *last* '@' and is
    correct. unquote also decodes any percent-encoded characters.
    """
    u = urlsplit(database_url())
    return psycopg2.connect(
        host=u.hostname,
        port=u.port or 5432,
        user=unquote(u.username) if u.username else None,
        password=unquote(u.password) if u.password else None,
        dbname=u.path.lstrip("/") or None,
    )
