"""
Upravljanje SQLite baze podatkov za API ključe Veritas IDV storitve.
"""

import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

# Pot do SQLite datoteke – v isti mapi kot ta modul
DB_PATH = Path(__file__).parent / "veritas_keys.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS api_keys (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    key         TEXT    NOT NULL UNIQUE,
    project     TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1
);
"""


def _connect() -> sqlite3.Connection:
    """Odpre in vrne SQLite povezavo z WAL načinom za boljšo hkratnost."""
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


@contextmanager
def _db():
    """Kontekstni upravljalnik, ki zagotovi zapiranje povezave."""
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Ustvari tabelo, če še ne obstaja. Kliče se ob zagonu."""
    with _db() as conn:
        conn.executescript(_SCHEMA)


def generate_api_key(project: str) -> str:
    """
    Ustvari kriptografsko varen API ključ, ga shrani in vrne.
    Format: vrt_<64 hex znakov>
    """
    key = "vrt_" + secrets.token_hex(32)
    now = datetime.utcnow().isoformat()
    with _db() as conn:
        conn.execute(
            "INSERT INTO api_keys (key, project, created_at) VALUES (?, ?, ?)",
            (key, project, now),
        )
    return key


def validate_api_key(key: str) -> Optional[str]:
    """
    Preveri API ključ in vrne ime projekta ali None, če ključ ni veljaven.
    """
    with _db() as conn:
        row = conn.execute(
            "SELECT project FROM api_keys WHERE key = ? AND active = 1",
            (key,),
        ).fetchone()
    return row["project"] if row else None


def list_api_keys() -> list[dict]:
    """Vrne seznam vseh aktivnih API ključev z metapodatki (brez samega ključa)."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT id, project, created_at, active FROM api_keys ORDER BY id DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def revoke_api_key(key: str) -> bool:
    """Deaktivira API ključ. Vrne True, če je bil ključ najden in deaktiviran."""
    with _db() as conn:
        cursor = conn.execute(
            "UPDATE api_keys SET active = 0 WHERE key = ? AND active = 1",
            (key,),
        )
    return cursor.rowcount > 0
