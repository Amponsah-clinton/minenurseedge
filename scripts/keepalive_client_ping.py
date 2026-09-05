"""
Supabase keep-alive ping (client-library method).

Free-tier Supabase projects auto-pause after a week with no API activity.
This does a trivial, read-only query through the official supabase-py client
so Supabase sees it as normal API usage and won't pause the project.

Run manually:  python scripts/keepalive_client_ping.py
Run on a schedule via .github/workflows/supabase-keepalive.yml
"""

import os
import sys

from supabase import create_client

TABLE = os.getenv("KEEPALIVE_TABLE", "profiles")


def main() -> int:
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_KEY")

    if not url or not key:
        print("Missing SUPABASE_URL or SUPABASE_SERVICE_KEY/SUPABASE_KEY", file=sys.stderr)
        return 1

    client = create_client(url, key)
    result = client.table(TABLE).select("id").limit(1).execute()

    print(f"[keepalive_client_ping] OK — queried '{TABLE}', rows returned: {len(result.data)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
