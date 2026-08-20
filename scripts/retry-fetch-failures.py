#!/usr/bin/env python3

import sqlite3
import re
from pathlib import Path

DB = "work/control/rdistro.db"
CAMPAIGN = "gen3-bootstrap"

PATTERNS = [
    r"Failed to fetch",
    r"Could not resolve",
    r"Temporary failure resolving",
    r"Connection failed",
    r"Network is unreachable",
    r"Hash Sum mismatch",
    r"Unable to connect",
]

conn = sqlite3.connect(DB)
cur = conn.cursor()

cur.execute("""
SELECT source, failure_category, latest_log
FROM jobs
WHERE campaign = ?
AND status = 'FAILED'
""", (CAMPAIGN,))

matches = []

for source, category, log_path in cur.fetchall():

    if not log_path:
        continue

    path = Path(log_path)

    if not path.exists():
        continue

    try:
        text = path.read_text(errors="ignore")
    except Exception:
        continue

    if any(re.search(p, text, re.I) for p in PATTERNS):
        matches.append((source, category, str(path)))

print(f"Found {len(matches)} transient fetch failures:")

for source, category, path in matches:
    print(f"  {source} [{category}]")
    print(f"      {path}")

for source, _, _ in matches:
    cur.execute("""
    UPDATE jobs
    SET
        status='RETRY',
        failure_category=NULL,
        last_error=NULL
    WHERE campaign=?
      AND source=?
    """, (CAMPAIGN, source))

conn.commit()

print("Requeued.")
