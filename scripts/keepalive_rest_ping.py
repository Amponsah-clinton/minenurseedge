"""
Supabase keep-alive ping (raw REST method).

Independent, dependency-free backup for keepalive_client_ping.py: hits the
PostgREST endpoint directly with urllib (stdlib only) so this still works
even if the supabase-py client is broken/unavailable.

Run manually:  python scripts/keepalive_rest_ping.py
Run on a schedule via .github/workflows/supabase-keepalive.yml
"""

import json
import os
import sys
import urllib.error
import urllib.request

TABLE = os.getenv("KEEPALIVE_TABLE", "profiles")


def main() -> int:
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_KEY")

    if not url or not key:
        print("Missing SUPABASE_URL or SUPABASE_SERVICE_KEY/SUPABASE_KEY", file=sys.stderr)
        return 1

    endpoint = f"{url.rstrip('/')}/rest/v1/{TABLE}?select=id&limit=1"
    request = urllib.request.Request(
        endpoint,
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = json.loads(response.read().decode("utf-8"))
            print(f"[keepalive_rest_ping] OK — status {response.status}, rows returned: {len(body)}")
            return 0
    except urllib.error.HTTPError as exc:
        print(f"[keepalive_rest_ping] HTTP {exc.code}: {exc.read().decode('utf-8', 'ignore')}", file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"[keepalive_rest_ping] Request failed: {exc.reason}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
