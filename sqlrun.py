"""Safe read-only SQL execution, shared by the Explore tab, the SQL tab and the Ask tab."""
import re
import time

import pandas as pd

import db

TIMEOUT_SECONDS = 10


def run_query(sql: str, params=(), max_rows: int = 500) -> pd.DataFrame:
    cleaned = sql.strip().rstrip(";").strip()
    if not re.match(r"(?is)^(select|with)\b", cleaned) or ";" in cleaned:
        raise ValueError("Only a single SELECT statement is allowed.")
    con = db.connect(read_only=True)
    try:
        con.execute("PRAGMA query_only = ON")
        deadline = time.time() + TIMEOUT_SECONDS
        con.set_progress_handler(lambda: 1 if time.time() > deadline else 0, 10000)
        cur = con.execute(cleaned, tuple(params))
        cols = [c[0] for c in cur.description]
        rows = cur.fetchmany(max_rows)
    finally:
        con.close()
    return pd.DataFrame(rows, columns=cols)
