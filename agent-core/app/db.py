import os
import psycopg
from psycopg.rows import dict_row

DATABASE_URL = os.getenv("DATABASE_URL")

def _conn():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)

def fetch_all(query, params=None):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params or ())
            return cur.fetchall()

def fetch_one(query, params=None):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params or ())
            return cur.fetchone()

def execute(query, params=None):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params or ())
            conn.commit()
