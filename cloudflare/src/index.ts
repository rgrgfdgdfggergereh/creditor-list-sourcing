/**
 * Purchase queue dashboard for the creditor sourcing pipeline.
 *
 * The pipeline can tell, for free, that a Form 5604 has been lodged for an
 * insolvent company - but buying the document image costs money per document
 * and is not idempotent, so it is deliberately left to a human. This Worker is
 * that human's single page: it lists exactly what to buy with a click-through
 * to ASIC Connect, and takes the purchased PDFs back by drag and drop.
 *
 * Routes
 *   GET  /                      dashboard (protect with Cloudflare Access)
 *   POST /api/queue             replace the queue           [bearer]
 *   GET  /api/uploads           list uploaded PDFs          [bearer]
 *   GET  /api/uploads/:key      download one PDF            [bearer]
 *   POST /api/upload/:matterId  receive a purchased PDF     [bearer or Access]
 */

export interface Env {
  DB: D1Database;
  DOCS: R2Bucket;
  QUEUE_TOKEN: string;
  ORG_NAME: string;
}

interface QueueRow {
  matter_id: string;
  company_name: string;
  acn: string | null;
  document_number: string | null;
  lodged_date: string | null;
  appointment_type: string | null;
  asic_connect_url: string | null;
  queued_at: string | null;
  purchased_at?: string | null;
  uploaded_key?: string | null;
}

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });

function authorised(request: Request, env: Env): boolean {
  const header = request.headers.get("authorization") || "";
  const token = header.replace(/^Bearer\s+/i, "");
  if (!env.QUEUE_TOKEN || !token) return false;
  // Constant-time-ish comparison: length check plus full-string XOR.
  if (token.length !== env.QUEUE_TOKEN.length) return false;
  let diff = 0;
  for (let i = 0; i < token.length; i++) {
    diff |= token.charCodeAt(i) ^ env.QUEUE_TOKEN.charCodeAt(i);
  }
  return diff === 0;
}

/** Cloudflare Access puts a signed JWT on every request it lets through. */
function viaAccess(request: Request): boolean {
  return Boolean(request.headers.get("cf-access-jwt-assertion"));
}

const esc = (value: unknown): string =>
  String(value ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!,
  );

function dashboard(rows: QueueRow[], org: string): string {
  const outstanding = rows.filter((r) => !r.purchased_at);
  const done = rows.filter((r) => r.purchased_at);
  const row = (r: QueueRow) => `
    <tr>
      <td class="co">${esc(r.company_name)}</td>
      <td class="mono">${esc(r.acn ?? "—")}</td>
      <td class="mono">${esc(r.document_number ?? "see register")}</td>
      <td class="mono">${esc(r.lodged_date ?? "—")}</td>
      <td>${
        r.asic_connect_url
          ? `<a class="btn" href="${esc(r.asic_connect_url)}" target="_blank" rel="noopener">Open on ASIC&nbsp;↗</a>`
          : "—"
      }</td>
      <td>${
        r.purchased_at
          ? `<span class="done">received</span>`
          : `<label class="up">Upload PDF<input type="file" accept="application/pdf" data-matter="${esc(r.matter_id)}" hidden></label>`
      }</td>
    </tr>`;

  return `<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ASIC purchase queue</title>
<style>
  :root { color-scheme: light dark; --bg:#fbfbfa; --fg:#16181d; --mut:#5b6170;
          --line:#e2e4e9; --card:#fff; --accent:#1f3864; --ok:#0f7b4f; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#12141a; --fg:#e8eaef; --mut:#9aa1b1; --line:#282c36;
            --card:#1a1d25; --accent:#7fa6e8; --ok:#4ac08a; }
  }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--fg);
         font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif; }
  .wrap { max-width:1080px; margin:0 auto; padding-block:40px; padding-inline:20px; }
  h1 { font-size:1.45rem; margin:0 0 4px; letter-spacing:-0.01em; }
  .sub { color:var(--mut); margin:0 0 28px; }
  .stats { display:flex; flex-wrap:wrap; gap:12px; margin-bottom:28px; }
  .stat { background:var(--card); border:1px solid var(--line); border-radius:10px;
          padding:14px 18px; min-width:150px; flex:1 1 150px; }
  .stat b { display:block; font-size:1.7rem; line-height:1.1; }
  .stat span { color:var(--mut); font-size:.82rem; }
  .scroll { overflow-x:auto; background:var(--card);
            border:1px solid var(--line); border-radius:10px; }
  table { border-collapse:collapse; width:100%; min-width:760px; }
  th,td { text-align:left; padding:11px 14px; border-bottom:1px solid var(--line);
          vertical-align:middle; }
  th { font-size:.74rem; text-transform:uppercase; letter-spacing:.06em;
       color:var(--mut); font-weight:600; }
  tr:last-child td { border-bottom:0; }
  .co { font-weight:600; }
  .mono { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:.86rem;
          color:var(--mut); }
  .btn, .up { display:inline-block; border:1px solid var(--line); border-radius:7px;
              padding:5px 11px; font-size:.84rem; text-decoration:none;
              color:var(--accent); cursor:pointer; white-space:nowrap; }
  .up:hover, .btn:hover { border-color:var(--accent); }
  .done { color:var(--ok); font-size:.84rem; font-weight:600; }
  h2 { font-size:.82rem; text-transform:uppercase; letter-spacing:.06em;
       color:var(--mut); margin:34px 0 10px; }
  .empty { padding:34px; text-align:center; color:var(--mut); }
  .note { color:var(--mut); font-size:.86rem; margin-top:22px; }
</style>
<div class="wrap">
  <h1>ASIC purchase queue</h1>
  <p class="sub">${esc(org)} — Form 5604 creditor lists ready to buy.</p>
  <div class="stats">
    <div class="stat"><b>${outstanding.length}</b><span>waiting to be purchased</span></div>
    <div class="stat"><b>${done.length}</b><span>received this cycle</span></div>
  </div>
  <div class="scroll">
    ${
      outstanding.length
        ? `<table><thead><tr><th>Insolvent company</th><th>ACN</th><th>Document</th>
           <th>Lodged</th><th>Buy</th><th>Return PDF</th></tr></thead>
           <tbody>${outstanding.map(row).join("")}</tbody></table>`
        : `<p class="empty">Nothing to buy. The next weekly run will refresh this.</p>`
    }
  </div>
  ${
    done.length
      ? `<h2>Received</h2><div class="scroll"><table><tbody>${done
          .map(row)
          .join("")}</tbody></table></div>`
      : ""
  }
  <p class="note">Buy the document on ASIC Connect, then upload the PDF here.
     The next pipeline run reads it, extracts the creditors and adds them to the
     prospect workbook.</p>
</div>
<script>
document.addEventListener("change", async (event) => {
  const input = event.target;
  if (!(input instanceof HTMLInputElement) || input.type !== "file") return;
  const file = input.files && input.files[0];
  if (!file) return;
  const label = input.closest("label");
  if (label) label.textContent = "Uploading…";
  const response = await fetch("/api/upload/" + input.dataset.matter, {
    method: "POST",
    headers: { "content-type": "application/pdf" },
    body: file,
  });
  if (label) label.textContent = response.ok ? "Received ✓" : "Failed — retry";
  if (response.ok) setTimeout(() => location.reload(), 700);
});
</script>`;
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+$/, "") || "/";

    // --- Dashboard (Cloudflare Access protects the route) --------------------
    if (path === "/" && request.method === "GET") {
      const { results } = await env.DB.prepare(
        "SELECT * FROM purchase_queue ORDER BY purchased_at IS NOT NULL, queued_at DESC",
      ).all<QueueRow>();
      return new Response(dashboard(results ?? [], env.ORG_NAME), {
        headers: { "content-type": "text/html; charset=utf-8" },
      });
    }

    // --- Replace the queue (from the weekly Actions run) ---------------------
    if (path === "/api/queue" && request.method === "POST") {
      if (!authorised(request, env)) return json({ error: "unauthorised" }, 401);
      const body = (await request.json()) as { queue?: QueueRow[] };
      const rows = body.queue ?? [];

      // Upsert rather than delete-and-insert: purchased_at and uploaded_key are
      // set by humans on this side and must survive a queue refresh.
      const statements = rows.map((r) =>
        env.DB.prepare(
          `INSERT INTO purchase_queue
             (matter_id, company_name, acn, document_number, lodged_date,
              appointment_type, asic_connect_url, queued_at)
           VALUES (?1,?2,?3,?4,?5,?6,?7,?8)
           ON CONFLICT(matter_id) DO UPDATE SET
             company_name=excluded.company_name,
             document_number=excluded.document_number,
             lodged_date=excluded.lodged_date,
             asic_connect_url=excluded.asic_connect_url`,
        ).bind(
          r.matter_id, r.company_name, r.acn ?? null, r.document_number ?? null,
          r.lodged_date ?? null, r.appointment_type ?? null,
          r.asic_connect_url ?? null, r.queued_at ?? null,
        ),
      );
      if (statements.length) await env.DB.batch(statements);

      // Anything no longer queued upstream has been ingested - clear it out.
      const keep = rows.map((r) => r.matter_id);
      await env.DB.prepare(
        keep.length
          ? `DELETE FROM purchase_queue WHERE uploaded_key IS NULL
               AND matter_id NOT IN (${keep.map(() => "?").join(",")})`
          : "DELETE FROM purchase_queue WHERE uploaded_key IS NULL",
      ).bind(...keep).run();

      return json({ ok: true, queued: rows.length });
    }

    // --- Receive a purchased PDF --------------------------------------------
    const upload = path.match(/^\/api\/upload\/([A-Za-z0-9_-]{4,64})$/);
    if (upload && request.method === "POST") {
      if (!authorised(request, env) && !viaAccess(request)) {
        return json({ error: "unauthorised" }, 401);
      }
      const matterId = upload[1];
      const row = await env.DB.prepare(
        "SELECT company_name FROM purchase_queue WHERE matter_id = ?1",
      ).bind(matterId).first<{ company_name: string }>();
      if (!row) return json({ error: "unknown matter" }, 404);

      // The ingest step reads the matter id back off the filename.
      const safe = row.company_name.replace(/[^A-Za-z0-9 ]+/g, "").trim().slice(0, 60);
      const key = `${matterId}__${safe || "company"}.pdf`;
      await env.DOCS.put(key, request.body, {
        httpMetadata: { contentType: "application/pdf" },
      });
      await env.DB.prepare(
        "UPDATE purchase_queue SET purchased_at = ?2, uploaded_key = ?3 WHERE matter_id = ?1",
      ).bind(matterId, new Date().toISOString(), key).run();
      return json({ ok: true, key });
    }

    // --- Hand the PDFs back to the pipeline ----------------------------------
    if (path === "/api/uploads" && request.method === "GET") {
      if (!authorised(request, env)) return json({ error: "unauthorised" }, 401);
      const listing = await env.DOCS.list({ limit: 1000 });
      return json({
        uploads: listing.objects.map((o) => ({
          key: o.key, size: o.size, uploaded: o.uploaded,
        })),
      });
    }

    const download = path.match(/^\/api\/uploads\/(.+)$/);
    if (download && request.method === "GET") {
      if (!authorised(request, env)) return json({ error: "unauthorised" }, 401);
      const object = await env.DOCS.get(decodeURIComponent(download[1]));
      if (!object) return json({ error: "not found" }, 404);
      return new Response(object.body, {
        headers: { "content-type": "application/pdf" },
      });
    }

    return json({ error: "not found" }, 404);
  },
};
