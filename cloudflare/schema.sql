-- Purchase queue mirrored from state/purchase_queue.json each weekly run.
CREATE TABLE IF NOT EXISTS purchase_queue (
  matter_id        TEXT PRIMARY KEY,
  company_name     TEXT NOT NULL,
  acn              TEXT,
  document_number  TEXT,
  lodged_date      TEXT,
  appointment_type TEXT,
  asic_connect_url TEXT,
  queued_at        TEXT,
  purchased_at     TEXT,
  uploaded_key     TEXT
);

CREATE INDEX IF NOT EXISTS idx_queue_outstanding
  ON purchase_queue (purchased_at, queued_at);
