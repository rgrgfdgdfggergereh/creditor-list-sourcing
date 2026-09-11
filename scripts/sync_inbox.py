"""Move data between the Actions run and the Cloudflare purchase dashboard.

    push   send the purchase queue to the dashboard so it shows what to buy
    pull   download PDFs a human uploaded there into state/inbox for ingest

The dashboard exists because the ASIC purchase is deliberately manual: a human
opens one page, buys the listed documents, and drops the PDFs back on the same
page. Nothing here spends money.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
QUEUE = ROOT / "state" / "purchase_queue.json"
INBOX = ROOT / "state" / "inbox"
TIMEOUT = 60


def _endpoint() -> tuple[str, dict[str, str]]:
    base = os.environ.get("QUEUE_ENDPOINT", "").rstrip("/")
    token = os.environ.get("QUEUE_TOKEN", "")
    if not base or not token:
        raise SystemExit("QUEUE_ENDPOINT and QUEUE_TOKEN are not set - skipping")
    return base, {"Authorization": f"Bearer {token}"}


def push() -> int:
    base, headers = _endpoint()
    rows = json.loads(QUEUE.read_text()) if QUEUE.exists() else []
    response = requests.post(
        f"{base}/api/queue", headers=headers, json={"queue": rows}, timeout=TIMEOUT
    )
    response.raise_for_status()
    print(f"Published {len(rows)} queued documents to the dashboard.")
    return 0


def pull() -> int:
    base, headers = _endpoint()
    listing = requests.get(f"{base}/api/uploads", headers=headers, timeout=TIMEOUT)
    listing.raise_for_status()
    uploads = listing.json().get("uploads", [])

    INBOX.mkdir(parents=True, exist_ok=True)
    pulled = 0
    for item in uploads:
        key = item["key"]
        target = INBOX / Path(key).name
        if target.exists():
            continue
        blob = requests.get(
            f"{base}/api/uploads/{key}", headers=headers, timeout=TIMEOUT
        )
        blob.raise_for_status()
        target.write_bytes(blob.content)
        pulled += 1
    print(f"Pulled {pulled} purchased document(s) into {INBOX}.")
    return 0


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "push"
    raise SystemExit(push() if action == "push" else pull())
