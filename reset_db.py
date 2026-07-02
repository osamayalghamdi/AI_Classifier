"""Reset all incidents from the incident store DB."""
import sqlite3

DB_PATH = "/data/incidents.db"

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row

# Show before
before = conn.execute("SELECT COUNT(*) as cnt FROM incidents").fetchone()["cnt"]
print(f"Before: {before} incidents")

conn.execute("DELETE FROM incidents")
conn.commit()

# VACUUM outside transaction
conn.isolation_level = None
conn.execute("VACUUM")

# Show after
after = conn.execute("SELECT COUNT(*) as cnt FROM incidents").fetchone()["cnt"]
print(f"After: {after} incidents")
conn.close()
