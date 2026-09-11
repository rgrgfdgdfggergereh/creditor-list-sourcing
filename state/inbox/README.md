# Inbox

Drop creditor PDFs here for `python -m creditor_sourcing ingest` to read:

- Form 5604 documents purchased from ASIC Connect
- Worrells Initial Advice / Notice of Plan PDFs

Name each file `<matter_id>__<company name>.pdf`. The matter id is the first
column of the **Purchase Queue** tab and of `state/purchase_queue.json`; the
Cloudflare dashboard names uploads this way automatically.

PDFs are gitignored — this is a staging area, not storage.
