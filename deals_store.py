"""
AireLogix Deal Store — PostgreSQL backend using pg8000 (pure Python, no native deps).
Uses Railway DATABASE_URL env var. Falls back to JSON files if not set.
"""

import json, os, uuid
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlparse

DATABASE_URL = os.environ.get("DATABASE_URL")

def _parse_url(url):
    """Parse postgresql://user:pass@host:port/dbname into pg8000 kwargs."""
    p = urlparse(url)
    return {
        "host": p.hostname,
        "port": p.port or 5432,
        "database": p.path.lstrip("/"),
        "user": p.username,
        "password": p.password,
        "ssl_context": True,  # Railway requires SSL
    }

def _get_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL not set")
    import pg8000.native
    params = _parse_url(DATABASE_URL)
    return pg8000.native.Connection(**params)

def _ensure_schema():
    conn = _get_conn()
    try:
        conn.run("""
            CREATE TABLE IF NOT EXISTS deals (
                deal_id      TEXT PRIMARY KEY,
                status       TEXT NOT NULL DEFAULT 'under_review',
                received_date TEXT,
                ioi_count    INTEGER NOT NULL DEFAULT 0,
                data         TEXT NOT NULL,
                created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        conn.run("""
            CREATE TABLE IF NOT EXISTS iois (
                ioi_id       TEXT PRIMARY KEY,
                deal_id      TEXT NOT NULL,
                institution  TEXT,
                submitted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                data         TEXT NOT NULL
            )
        """)
        # Real lender identity on every IOI -- added 2026-11-23 to replace the
        # institution-name string match (fragile: a renamed institution or a
        # typo on the lender portal's form silently broke the link). NULL on
        # any IOI submitted before this column existed; get_lender_distribution_stats
        # falls back to the old name-match only for those pre-existing rows.
        conn.run("ALTER TABLE iois ADD COLUMN IF NOT EXISTS lender_id TEXT")
        conn.run("CREATE INDEX IF NOT EXISTS idx_iois_lender ON iois(lender_id)")
        # Ties a deal to the Supabase auth user (borrower) who submitted it,
        # so the backend can authorize "give me my own deals" by JWT subject
        # instead of trusting a client-supplied email.
        conn.run("ALTER TABLE deals ADD COLUMN IF NOT EXISTS borrower_user_id TEXT")
        conn.run("CREATE INDEX IF NOT EXISTS idx_deals_created ON deals(created_at DESC)")
        conn.run("CREATE INDEX IF NOT EXISTS idx_deals_borrower_user ON deals(borrower_user_id)")
        conn.run("CREATE INDEX IF NOT EXISTS idx_iois_deal ON iois(deal_id)")
        # Pre-account lead capture -- a borrower who completes the wizard's
        # Personal Info step but abandons before final submission still
        # leaves a record Vaero can follow up with. Email is the natural key
        # since there's no account yet to key on.
        conn.run("""
            CREATE TABLE IF NOT EXISTS leads (
                email             TEXT PRIMARY KEY,
                data              TEXT NOT NULL,
                converted_deal_id TEXT,
                created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
    finally:
        conn.close()

    # Lender onboarding -- deliberately isolated from the core schema above.
    # A problem in this block must never take deals/iois/leads persistence
    # down with it the way one shared try/except previously could (any new
    # addition failing here would silently flip _USE_DB to False for
    # everything, not just this table).
    try:
        conn2 = _get_conn()
        try:
            # One table, status-driven (pending/approved/declined/inactive),
            # same convention as deals/leads: a few promoted columns for
            # querying, everything else (structure appetite, maxLtv,
            # recourse, rate type, deal size range, geography, charter
            # appetite, billing info, internal notes) lives in the JSON blob.
            conn2.run("""
                CREATE TABLE IF NOT EXISTS lenders (
                    lender_id        TEXT PRIMARY KEY,
                    status           TEXT NOT NULL DEFAULT 'pending',
                    institution_name TEXT,
                    contact_email    TEXT,
                    data             TEXT NOT NULL,
                    lender_user_id   TEXT,
                    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            conn2.run("CREATE INDEX IF NOT EXISTS idx_lenders_status ON lenders(status)")
        finally:
            conn2.close()
    except Exception as e:
        print(f"[deals_store] WARNING: lenders schema setup failed (non-fatal, core schema unaffected): {type(e).__name__}: {e}")

    # Distributions -- isolated for the same reason as lenders above. Tracks
    # which lenders a deal was actually routed to, so the routing engine's
    # fairness/responsiveness ranking signal has real data to work from.
    try:
        conn3 = _get_conn()
        try:
            conn3.run("""
                CREATE TABLE IF NOT EXISTS distributions (
                    id              SERIAL PRIMARY KEY,
                    deal_id         TEXT NOT NULL,
                    lender_id       TEXT NOT NULL,
                    distributed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            conn3.run("CREATE INDEX IF NOT EXISTS idx_distributions_deal ON distributions(deal_id)")
            conn3.run("CREATE INDEX IF NOT EXISTS idx_distributions_lender ON distributions(lender_id)")
        finally:
            conn3.close()
    except Exception as e:
        print(f"[deals_store] WARNING: distributions schema setup failed (non-fatal, core schema unaffected): {type(e).__name__}: {e}")

    # Lender declines -- a real per-lender "reviewed and passing" signal,
    # separate from the deal's own global status field. Added 2026-11-23
    # specifically because the lender portal's pre-existing "Pass on Deal"
    # button wrote to deals.status directly, which meant one lender passing
    # marked the deal passed for every lender on a multi-lender distribution.
    # Declining here never touches the deal record at all.
    try:
        conn4 = _get_conn()
        try:
            conn4.run("""
                CREATE TABLE IF NOT EXISTS lender_declines (
                    id           SERIAL PRIMARY KEY,
                    deal_id      TEXT NOT NULL,
                    lender_id    TEXT NOT NULL,
                    declined_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE(deal_id, lender_id)
                )
            """)
            conn4.run("CREATE INDEX IF NOT EXISTS idx_lender_declines_lender ON lender_declines(lender_id)")
        finally:
            conn4.close()
    except Exception as e:
        print(f"[deals_store] WARNING: lender_declines schema setup failed (non-fatal, core schema unaffected): {type(e).__name__}: {e}")

try:
    _ensure_schema()
    _USE_DB = True
    print("[deals_store] PostgreSQL ready")
except Exception as e:
    print(f"[deals_store] Falling back to file storage: {e}")
    _USE_DB = False

# ── File storage fallback ─────────────────────────────────────────────────────
DEALS_DIR   = os.environ.get("DEALS_DIR",   "/tmp/airelogix_deals")
IOIS_DIR    = os.environ.get("IOIS_DIR",    "/tmp/airelogix_iois")
LEADS_DIR   = os.environ.get("LEADS_DIR",   "/tmp/airelogix_leads")
LENDERS_DIR = os.environ.get("LENDERS_DIR", "/tmp/airelogix_lenders")
DISTRIBUTIONS_DIR = os.environ.get("DISTRIBUTIONS_DIR", "/tmp/airelogix_distributions")
LENDER_DECLINES_DIR = os.environ.get("LENDER_DECLINES_DIR", "/tmp/airelogix_lender_declines")

def _ensure_dirs():
    os.makedirs(DEALS_DIR, exist_ok=True)
    os.makedirs(IOIS_DIR, exist_ok=True)
    os.makedirs(LEADS_DIR, exist_ok=True)
    os.makedirs(LENDERS_DIR, exist_ok=True)
    os.makedirs(DISTRIBUTIONS_DIR, exist_ok=True)
    os.makedirs(LENDER_DECLINES_DIR, exist_ok=True)

# ── ID generation ─────────────────────────────────────────────────────────────
def generate_deal_id() -> str:
    return f"AL-{datetime.now().year}-{str(uuid.uuid4())[:8].upper()}"

def generate_lender_id() -> str:
    return f"LEND-{datetime.now().year}-{str(uuid.uuid4())[:8].upper()}"

# ── Deal operations ───────────────────────────────────────────────────────────
def save_deal(deal: dict) -> str:
    deal_id = deal.get("dealId") or generate_deal_id()
    deal["dealId"] = deal_id
    now = datetime.now(timezone.utc).isoformat()
    deal.setdefault("createdAt", now)
    deal["updatedAt"] = now

    if _USE_DB:
        conn = _get_conn()
        try:
            conn.run("""
                INSERT INTO deals (deal_id, status, received_date, ioi_count, data, borrower_user_id)
                VALUES (:deal_id, :status, :received_date, :ioi_count, :data, :borrower_user_id)
                ON CONFLICT (deal_id) DO UPDATE SET
                    status=EXCLUDED.status,
                    received_date=EXCLUDED.received_date,
                    ioi_count=EXCLUDED.ioi_count,
                    data=EXCLUDED.data,
                    borrower_user_id=COALESCE(EXCLUDED.borrower_user_id, deals.borrower_user_id),
                    updated_at=NOW()
            """, deal_id=deal_id,
                status=deal.get("status", "under_review"),
                received_date=deal.get("receivedDate", ""),
                ioi_count=deal.get("ioiCount", 0),
                data=json.dumps(deal),
                borrower_user_id=deal.get("borrowerUserId"))
        finally:
            conn.close()
    else:
        _ensure_dirs()
        with open(os.path.join(DEALS_DIR, f"{deal_id}.json"), "w") as f:
            json.dump(deal, f, indent=2)
    return deal_id

def load_deal(deal_id: str) -> Optional[dict]:
    if _USE_DB:
        conn = _get_conn()
        try:
            rows = conn.run("SELECT data, ioi_count, status FROM deals WHERE deal_id=:deal_id", deal_id=deal_id)
            if not rows:
                return None
            d = json.loads(rows[0][0])
            d["ioiCount"] = rows[0][1]
            d["status"] = rows[0][2]
            return d
        finally:
            conn.close()
    else:
        _ensure_dirs()
        p = os.path.join(DEALS_DIR, f"{deal_id}.json")
        return json.load(open(p)) if os.path.exists(p) else None

def list_deals() -> list:
    if _USE_DB:
        conn = _get_conn()
        try:
            rows = conn.run("SELECT data, ioi_count, status FROM deals ORDER BY created_at DESC LIMIT 200")
            result = []
            for r in rows:
                d = json.loads(r[0])
                d["ioiCount"] = r[1]
                d["status"] = r[2]
                result.append(d)
            return result
        finally:
            conn.close()
    else:
        _ensure_dirs()
        deals = []
        for f in sorted(os.listdir(DEALS_DIR), reverse=True):
            if f.endswith(".json"):
                try: deals.append(json.load(open(os.path.join(DEALS_DIR, f))))
                except: pass
        return deals

def list_deals_admin() -> list:
    """Same as list_deals(), but also exposes the real DB updated_at/
    created_at columns -- needed for staleness tracking (Control Tower),
    which the JSON blob's own timestamps can't be trusted for since nothing
    guarantees every code path that mutates a deal also updates them."""
    if _USE_DB:
        conn = _get_conn()
        try:
            rows = conn.run(
                "SELECT data, ioi_count, status, created_at, updated_at FROM deals ORDER BY updated_at DESC LIMIT 200"
            )
            result = []
            for r in rows:
                d = json.loads(r[0])
                d["ioiCount"] = r[1]
                d["status"] = r[2]
                d["_createdAt"] = r[3].isoformat() if r[3] else None
                d["_updatedAt"] = r[4].isoformat() if r[4] else None
                result.append(d)
            return result
        finally:
            conn.close()
    else:
        return list_deals()

def list_deals_by_user(borrower_user_id: str) -> list:
    if _USE_DB:
        conn = _get_conn()
        try:
            rows = conn.run(
                "SELECT data, ioi_count, status FROM deals WHERE borrower_user_id=:uid ORDER BY created_at DESC",
                uid=borrower_user_id,
            )
            result = []
            for r in rows:
                d = json.loads(r[0])
                d["ioiCount"] = r[1]
                d["status"] = r[2]
                result.append(d)
            return result
        finally:
            conn.close()
    else:
        _ensure_dirs()
        deals = []
        for f in sorted(os.listdir(DEALS_DIR), reverse=True):
            if f.endswith(".json"):
                try:
                    d = json.load(open(os.path.join(DEALS_DIR, f)))
                    if d.get("borrowerUserId") == borrower_user_id:
                        deals.append(d)
                except Exception:
                    pass
        return deals

def update_deal_status(deal_id: str, status: str) -> Optional[dict]:
    deal = load_deal(deal_id)
    if not deal: return None
    deal["status"] = status
    deal["updatedAt"] = datetime.now(timezone.utc).isoformat()
    if _USE_DB:
        conn = _get_conn()
        try:
            conn.run("""
                UPDATE deals SET status=:status, data=:data, updated_at=NOW()
                WHERE deal_id=:deal_id
            """, status=status, data=json.dumps(deal), deal_id=deal_id)
        finally:
            conn.close()
    else:
        save_deal(deal)
    return deal

def update_deal_bio(deal_id: str, borrower_profile: dict) -> None:
    """Save borrowerProfile to an existing deal record without touching other fields."""
    deal = load_deal(deal_id)
    if not deal:
        print(f"[deals_store] update_deal_bio: deal {deal_id} not found")
        return
    deal["borrowerProfile"] = borrower_profile
    deal["updatedAt"] = datetime.now(timezone.utc).isoformat()
    if _USE_DB:
        conn = _get_conn()
        try:
            conn.run(
                "UPDATE deals SET data=:data, updated_at=NOW() WHERE deal_id=:deal_id",
                data=json.dumps(deal), deal_id=deal_id
            )
        finally:
            conn.close()
    else:
        save_deal(deal)


def save_ioi(deal_id: str, ioi: dict) -> str:
    ioi_id = ioi.get("ioiId") or str(uuid.uuid4())[:8].upper()
    ioi["ioiId"] = ioi_id
    ioi["dealId"] = deal_id
    ioi.setdefault("submittedAt", datetime.now(timezone.utc).isoformat())
    institution = ioi.get("institution", "")
    lender_id = ioi.get("lenderId", "")

    if _USE_DB:
        conn = _get_conn()
        try:
            conn.run("""
                INSERT INTO iois (ioi_id, deal_id, institution, lender_id, data)
                VALUES (:ioi_id, :deal_id, :institution, :lender_id, :data)
                ON CONFLICT (ioi_id) DO UPDATE SET data=EXCLUDED.data, lender_id=EXCLUDED.lender_id
            """, ioi_id=ioi_id, deal_id=deal_id, institution=institution, lender_id=lender_id, data=json.dumps(ioi))
            count_rows = conn.run(
                "SELECT COUNT(*)::int FROM iois WHERE deal_id=:deal_id",
                deal_id=deal_id
            )
            new_count = count_rows[0][0] if count_rows else 1
            conn.run("""
                UPDATE deals SET
                    ioi_count=:ioi_count,
                    status=CASE WHEN status IN ('select_lender_pool','package_distributed','under_review')
                                THEN 'ioi_received' ELSE status END,
                    updated_at=NOW()
                WHERE deal_id=:deal_id
            """, ioi_count=new_count, deal_id=deal_id)
        finally:
            conn.close()
    else:
        _ensure_dirs()
        p = os.path.join(IOIS_DIR, f"{deal_id}_iois.json")
        iois = json.load(open(p)) if os.path.exists(p) else []
        idx = next((i for i,x in enumerate(iois) if x.get("institution")==institution), None)
        if idx is not None: iois[idx] = ioi
        else: iois.append(ioi)
        with open(p,"w") as f: json.dump(iois, f, indent=2)
        deal = load_deal(deal_id)
        if deal:
            deal["ioiCount"] = len(iois)
            if deal.get("status") in ("select_lender_pool","package_distributed","under_review"):
                deal["status"] = "ioi_received"
            save_deal(deal)
    return ioi_id

def load_iois(deal_id: str) -> list:
    if _USE_DB:
        conn = _get_conn()
        try:
            rows = conn.run("SELECT data FROM iois WHERE deal_id=:deal_id ORDER BY submitted_at", deal_id=deal_id)
            return [json.loads(r[0]) for r in rows]
        finally:
            conn.close()
    else:
        _ensure_dirs()
        p = os.path.join(IOIS_DIR, f"{deal_id}_iois.json")
        return json.load(open(p)) if os.path.exists(p) else []


def save_ioi_feedback(deal_id: str, institution: str, reasons: list) -> bool:
    """Attach borrower decline feedback to an IOI."""
    payload = {
        "declinedByBorrower": True,
        "declinedAt": datetime.now(timezone.utc).isoformat(),
        "declinedReasons": reasons,
    }
    if _USE_DB:
        conn = _get_conn()
        try:
            rows = conn.run(
                "SELECT data FROM iois WHERE deal_id=:deal_id AND institution=:institution",
                deal_id=deal_id, institution=institution
            )
            if not rows:
                return False
            ioi = json.loads(rows[0][0])
            ioi.update(payload)
            conn.run(
                "UPDATE iois SET data=:data WHERE deal_id=:deal_id AND institution=:institution",
                data=json.dumps(ioi), deal_id=deal_id, institution=institution
            )
            return True
        finally:
            conn.close()
    else:
        _ensure_dirs()
        p = os.path.join(IOIS_DIR, f"{deal_id}_iois.json")
        if not os.path.exists(p):
            return False
        iois = json.load(open(p))
        updated = False
        for ioi in iois:
            if ioi.get("institution") == institution:
                ioi.update(payload)
                updated = True
        if updated:
            with open(p, "w") as f:
                json.dump(iois, f, indent=2)
        return updated


def clear_ioi_decline(deal_id: str, institution: str) -> bool:
    """Remove borrower decline markers from an IOI (borrower reconsidering)."""
    if _USE_DB:
        conn = _get_conn()
        try:
            rows = conn.run(
                "SELECT data FROM iois WHERE deal_id=:deal_id AND institution=:institution",
                deal_id=deal_id, institution=institution
            )
            if not rows:
                return False
            ioi = json.loads(rows[0][0])
            ioi.pop("declinedByBorrower", None)
            ioi.pop("declinedAt", None)
            ioi.pop("declinedReasons", None)
            conn.run(
                "UPDATE iois SET data=:data WHERE deal_id=:deal_id AND institution=:institution",
                data=json.dumps(ioi), deal_id=deal_id, institution=institution
            )
            return True
        finally:
            conn.close()
    else:
        _ensure_dirs()
        p = os.path.join(IOIS_DIR, f"{deal_id}_iois.json")
        if not os.path.exists(p):
            return False
        iois = json.load(open(p))
        updated = False
        for ioi in iois:
            if ioi.get("institution") == institution:
                ioi.pop("declinedByBorrower", None)
                ioi.pop("declinedAt", None)
                ioi.pop("declinedReasons", None)
                updated = True
        if updated:
            with open(p, "w") as f:
                json.dump(iois, f, indent=2)
        return updated


def load_declined_iois(institution: str) -> list:
    """Return deals where the given institution's IOI was declined by the borrower."""
    results = []
    if _USE_DB:
        conn = _get_conn()
        try:
            rows = conn.run(
                """SELECT i.deal_id, i.data, d.data as deal_data
                   FROM iois i JOIN deals d ON d.deal_id=i.deal_id
                   WHERE i.institution=:institution""",
                institution=institution
            )
            for row in rows:
                ioi = json.loads(row[1])
                if ioi.get("declinedByBorrower"):
                    deal = json.loads(row[2])
                    results.append({"dealId": row[0], "ioi": ioi, "deal": deal})
        finally:
            conn.close()
    else:
        _ensure_dirs()
        if os.path.exists(IOIS_DIR):
            for fname in os.listdir(IOIS_DIR):
                if not fname.endswith("_iois.json"):
                    continue
                deal_id = fname.replace("_iois.json", "")
                iois = json.load(open(os.path.join(IOIS_DIR, fname)))
                for ioi in iois:
                    if ioi.get("institution") == institution and ioi.get("declinedByBorrower"):
                        deal = load_deal(deal_id) or {}
                        results.append({"dealId": deal_id, "ioi": ioi, "deal": deal})
    return results


# ── Lead operations ───────────────────────────────────────────────────────────

def save_lead(email: str, data: dict) -> None:
    """Upsert a pre-account lead by email."""
    email = email.strip().lower()
    if _USE_DB:
        conn = _get_conn()
        try:
            conn.run("""
                INSERT INTO leads (email, data)
                VALUES (:email, :data)
                ON CONFLICT (email) DO UPDATE SET data=EXCLUDED.data, updated_at=NOW()
            """, email=email, data=json.dumps(data))
        finally:
            conn.close()
    else:
        _ensure_dirs()
        p = os.path.join(LEADS_DIR, f"{email}.json")
        existing = json.load(open(p)) if os.path.exists(p) else {}
        existing.update(data)
        existing["email"] = email
        existing["updatedAt"] = datetime.now(timezone.utc).isoformat()
        with open(p, "w") as f:
            json.dump(existing, f, indent=2)


def mark_lead_converted(email: str, deal_id: str) -> None:
    """Called once a lead's email completes a real submission, so follow-up
    logic can skip anyone who already finished."""
    if not email:
        return
    email = email.strip().lower()
    if _USE_DB:
        conn = _get_conn()
        try:
            conn.run(
                "UPDATE leads SET converted_deal_id=:deal_id, updated_at=NOW() WHERE email=:email",
                deal_id=deal_id, email=email
            )
        finally:
            conn.close()
    else:
        _ensure_dirs()
        p = os.path.join(LEADS_DIR, f"{email}.json")
        if os.path.exists(p):
            existing = json.load(open(p))
            existing["convertedDealId"] = deal_id
            with open(p, "w") as f:
                json.dump(existing, f, indent=2)


# ── Lender operations ─────────────────────────────────────────────────────────

def save_lender_application(data: dict) -> str:
    """Create a new lender record from a submitted application -- always
    starts at status='pending', regardless of what the submission says."""
    lender_id = generate_lender_id()
    data = dict(data)
    data["lenderId"] = lender_id
    data["status"] = "pending"
    # Admin-only, not part of the applicant-facing form -- Matt's own judgment
    # call on a lender, set later via PATCH /admin/lenders/{id}, never submitted
    # by the applicant themselves.
    data["priorityWeight"] = 0
    data["pinned"] = False
    now = datetime.now(timezone.utc).isoformat()
    data["createdAt"] = now
    data["updatedAt"] = now

    if _USE_DB:
        conn = _get_conn()
        try:
            contacts = data.get("contacts") or []
            primary_email = contacts[0].get("email", "") if contacts else data.get("contactEmail", "")
            conn.run("""
                INSERT INTO lenders (lender_id, status, institution_name, contact_email, data)
                VALUES (:lender_id, 'pending', :name, :email, :data)
            """, lender_id=lender_id, name=data.get("institutionName", ""),
                 email=primary_email, data=json.dumps(data))
        finally:
            conn.close()
    else:
        _ensure_dirs()
        with open(os.path.join(LENDERS_DIR, f"{lender_id}.json"), "w") as f:
            json.dump(data, f, indent=2)
    return lender_id


def list_lenders() -> list:
    if _USE_DB:
        conn = _get_conn()
        try:
            rows = conn.run("SELECT data FROM lenders ORDER BY created_at DESC")
            return [json.loads(r[0]) for r in rows]
        finally:
            conn.close()
    else:
        _ensure_dirs()
        out = []
        for fname in os.listdir(LENDERS_DIR):
            if fname.endswith(".json"):
                out.append(json.load(open(os.path.join(LENDERS_DIR, fname))))
        return out


def get_lender(lender_id: str) -> Optional[dict]:
    if _USE_DB:
        conn = _get_conn()
        try:
            rows = conn.run("SELECT data FROM lenders WHERE lender_id=:id", id=lender_id)
            return json.loads(rows[0][0]) if rows else None
        finally:
            conn.close()
    else:
        _ensure_dirs()
        p = os.path.join(LENDERS_DIR, f"{lender_id}.json")
        return json.load(open(p)) if os.path.exists(p) else None


def update_lender(lender_id: str, updates: dict) -> Optional[dict]:
    """Merge `updates` into the lender's stored data. Pass status in
    `updates` to approve/decline/deactivate -- it's just a field."""
    existing = get_lender(lender_id)
    if existing is None:
        return None
    existing.update(updates)
    existing["updatedAt"] = datetime.now(timezone.utc).isoformat()

    if _USE_DB:
        contacts = existing.get("contacts") or []
        primary_email = contacts[0].get("email", "") if contacts else existing.get("contactEmail", "")
        conn = _get_conn()
        try:
            conn.run("""
                UPDATE lenders SET data=:data, status=:status,
                    institution_name=:name, contact_email=:email, updated_at=NOW()
                WHERE lender_id=:id
            """, data=json.dumps(existing), status=existing.get("status", "pending"),
                 name=existing.get("institutionName", ""), email=primary_email,
                 id=lender_id)
        finally:
            conn.close()
    else:
        _ensure_dirs()
        with open(os.path.join(LENDERS_DIR, f"{lender_id}.json"), "w") as f:
            json.dump(existing, f, indent=2)
    return existing


def delete_lender(lender_id: str) -> bool:
    """Permanently removes a lender record. Unlike update_lender's status
    changes (approve/decline/inactive), this is a true, irreversible delete --
    callers should confirm with the user before calling this."""
    if _USE_DB:
        conn = _get_conn()
        try:
            rows = conn.run("DELETE FROM lenders WHERE lender_id=:id RETURNING lender_id", id=lender_id)
            return len(rows) > 0
        finally:
            conn.close()
    else:
        _ensure_dirs()
        p = os.path.join(LENDERS_DIR, f"{lender_id}.json")
        if os.path.exists(p):
            os.remove(p)
            return True
        return False


def record_distributions(deal_id: str, lender_ids: list) -> int:
    """Records that `deal_id` was distributed to each lender in `lender_ids`.
    Not idempotent against re-calling with the same list -- calling twice
    records two timestamped rows per lender, which is correct (it means the
    deal was genuinely redistributed, e.g. after the lender pool changed)."""
    if _USE_DB:
        conn = _get_conn()
        try:
            for lender_id in lender_ids:
                conn.run(
                    "INSERT INTO distributions (deal_id, lender_id, distributed_at) VALUES (:deal_id, :lender_id, NOW())",
                    deal_id=deal_id, lender_id=lender_id,
                )
        finally:
            conn.close()
    else:
        _ensure_dirs()
        now = datetime.now(timezone.utc).isoformat()
        p = os.path.join(DISTRIBUTIONS_DIR, "log.json")
        log = json.load(open(p)) if os.path.exists(p) else []
        for lender_id in lender_ids:
            log.append({"dealId": deal_id, "lenderId": lender_id, "distributedAt": now})
        with open(p, "w") as f:
            json.dump(log, f, indent=2)
    return len(lender_ids)


def get_deal_distributions(deal_id: str) -> list:
    """Returns [{lenderId, distributedAt}] for a given deal."""
    if _USE_DB:
        conn = _get_conn()
        try:
            rows = conn.run(
                "SELECT lender_id, distributed_at FROM distributions WHERE deal_id=:deal_id ORDER BY distributed_at",
                deal_id=deal_id,
            )
            return [{"lenderId": r[0], "distributedAt": r[1].isoformat() if hasattr(r[1], "isoformat") else r[1]} for r in rows]
        finally:
            conn.close()
    else:
        _ensure_dirs()
        p = os.path.join(DISTRIBUTIONS_DIR, "log.json")
        log = json.load(open(p)) if os.path.exists(p) else []
        return [{"lenderId": e["lenderId"], "distributedAt": e["distributedAt"]} for e in log if e["dealId"] == deal_id]


def get_lender_distribution_stats(lender_id: str, institution_name: str = "", since_days: int = 30) -> dict:
    """Returns {distributed, responded, declined, unanswered, neverConverts,
    sinceDays, history} for one lender over a rolling window.

    FIXED 2026-11-23 (both were previously KNOWN LIMITATIONS here): IOIs now
    carry a real lender_id column, matched directly instead of the old
    institution-name string comparison -- name-matching only still runs as a
    fallback for IOI rows submitted before this column existed. A real
    per-lender decline action (lender_declines table) now exists too, fed
    by the lender portal's magic-link-authenticated "Pass on Deal" action --
    a decline counts as real engagement (`declined`), not silence, per the
    2026-11-23 discussion. Only a lender who engages at real volume (5+
    distributions) via decline but has NEVER once converted to an IOI trips
    `neverConverts` -- a single decline on a small sample isn't evidence of
    anything.
    """
    since_cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
    distributions = []
    if _USE_DB:
        conn = _get_conn()
        try:
            rows = conn.run(
                "SELECT deal_id, distributed_at FROM distributions WHERE lender_id=:lender_id AND distributed_at >= :since",
                lender_id=lender_id, since=since_cutoff,
            )
            distributions = [{"dealId": r[0], "distributedAt": r[1].isoformat() if hasattr(r[1], "isoformat") else r[1]} for r in rows]
        finally:
            conn.close()
    else:
        _ensure_dirs()
        p = os.path.join(DISTRIBUTIONS_DIR, "log.json")
        log = json.load(open(p)) if os.path.exists(p) else []
        for e in log:
            if e["lenderId"] != lender_id:
                continue
            try:
                dt = datetime.fromisoformat(e["distributedAt"])
            except ValueError:
                continue
            if dt >= since_cutoff:
                distributions.append({"dealId": e["dealId"], "distributedAt": e["distributedAt"]})

    declined_deal_ids = set(get_lender_declines(lender_id))
    history = []
    ioi_count = 0
    decline_count = 0
    name_norm = (institution_name or "").strip().lower()
    for d in distributions:
        iois = load_iois(d["dealId"])
        # Real lenderId match first (added 2026-11-23); name-match is now
        # only a fallback for IOI rows submitted before that column existed.
        matched = any((ioi.get("lenderId") or "") == lender_id for ioi in iois)
        if not matched and name_norm:
            matched = any(
                not (ioi.get("lenderId") or "") and (ioi.get("institution") or "").strip().lower() == name_norm
                for ioi in iois
            )
        declined = d["dealId"] in declined_deal_ids
        if matched:
            ioi_count += 1
        elif declined:
            decline_count += 1
        history.append({
            "dealId": d["dealId"], "distributedAt": d["distributedAt"],
            "responded": matched, "declined": declined and not matched,
        })

    distributed_count = len(distributions)
    engaged = ioi_count + decline_count
    # A decline is real engagement -- the lender looked at the deal and made
    # a call. What's actually worth flagging is a lender who engages (via
    # decline) at real volume but has NEVER once converted to an IOI, per
    # the 2026-11-23 discussion -- a small sample isn't evidence of anything.
    never_converts = distributed_count >= 5 and ioi_count == 0 and decline_count >= 1
    return {
        "distributed": distributed_count,
        "responded": ioi_count,
        "declined": decline_count,
        "unanswered": distributed_count - engaged,
        "neverConverts": never_converts,
        "sinceDays": since_days,
        "history": history,
    }


def record_lender_decline(deal_id: str, lender_id: str) -> bool:
    """Records that a lender actively reviewed and passed on a deal --
    separate from the deal's own global status field entirely (unlike the
    older PATCH /deals/{id}/status mechanism, which one lender using it
    would incorrectly mark the deal passed for every lender on a multi-
    lender distribution). Idempotent -- declining twice is a no-op, not an
    error, since a lender might hit the button more than once."""
    if _USE_DB:
        conn = _get_conn()
        try:
            conn.run(
                "INSERT INTO lender_declines (deal_id, lender_id) VALUES (:deal_id, :lender_id) "
                "ON CONFLICT (deal_id, lender_id) DO NOTHING",
                deal_id=deal_id, lender_id=lender_id,
            )
            return True
        finally:
            conn.close()
    else:
        _ensure_dirs()
        p = os.path.join(LENDER_DECLINES_DIR, "log.json")
        log = json.load(open(p)) if os.path.exists(p) else []
        if not any(e["dealId"] == deal_id and e["lenderId"] == lender_id for e in log):
            log.append({"dealId": deal_id, "lenderId": lender_id, "declinedAt": datetime.now(timezone.utc).isoformat()})
            with open(p, "w") as f:
                json.dump(log, f, indent=2)
        return True


def get_lender_declines(lender_id: str) -> list:
    """Returns the list of dealIds this lender has declined, all-time."""
    if _USE_DB:
        conn = _get_conn()
        try:
            rows = conn.run("SELECT deal_id FROM lender_declines WHERE lender_id=:lender_id", lender_id=lender_id)
            return [r[0] for r in rows]
        finally:
            conn.close()
    else:
        _ensure_dirs()
        p = os.path.join(LENDER_DECLINES_DIR, "log.json")
        log = json.load(open(p)) if os.path.exists(p) else []
        return [e["dealId"] for e in log if e["lenderId"] == lender_id]


# ── Calloway Demo Seed ────────────────────────────────────────────────────────

CALLOWAY_DEAL_ID = "AL-2026-CALLOWAY"

CALLOWAY_DEMO = {
    "dealId": CALLOWAY_DEAL_ID,
    "applicationId": CALLOWAY_DEAL_ID,
    "status": "ioi_received",
    "stage": "ioi_received",
    "ioiCount": 3,
    "receivedDate": "2026-03-28",
    "borrowerName": "James R. Calloway",
    "borrowerEmail": "jcalloway@callowayindustries.com",
    "aircraft": "2016 Bombardier Challenger 350",
    "aircraftSub": "Smart Parts Plus \u00b7 N-registered \u00b7 1,100 AFTT",
    "loanAmount": 15000000,
    "ltv": 0.882,
    "rating": 2,
    "band": "Very Strong",
    "disposition": "Approve",
    "gdscr": 1.8022,
    "nwCoverage": 3.65,
    "flagCount": 3,
    "criticalFlags": 0,
    "transactionType": "purchase",
    "analysis": {
        "borrowerName": "James R. Calloway",
        "analysisDate": "2026-03-28",
        "riskRating": {"rating": 2, "band": "Very Strong", "disposition": "Approve", "composite": 1.667},
        "transaction": {
            "purchasePrice": 17000000,
            "loanAmount": 15000000,
            "ltv": 0.882,
            "downPayment": 2000000,
            "illustrativeRate": 0.063,
            "illustrativeMonthlyPayment": 112239,
            "monthlyPayment": 112239,
            "annualAircraftDS": 1346864,
            "existingAnnualDS": 836858,
            "totalProFormaDS": 2183722,
            "term": 120,
            "termMonths": 120,
            "rateNote": "SOFR OIS + 200bps illustrative spread",
            "primary": {
                "rate": 0.063,
                "monthlyPayment": 112239,
                "term": 120,
            },
        },
        "gdscr": {
            "gdscr": 1.8022,
            "qualifyingIncome": 3935600,
            "governingYear": 2023,
            "totalAnnualDebtService": 2183722,
            "newLoanDebtService": 1346864,
            "existingDebtService": 836858,
        },
        "agi": 4312400,
        "federalTaxesPaid": 1338100,
        "incomeNormalization": {
            "taxYears": [2023, 2024],
            "qualifyingYear": 2023,
            "governingYear": 2023,
            "year1Total": 3935600,
            "year2Total": 4269300,
            "year1Income": 3935600,
            "year2Income": 4269300,
            "governingIncome": 3935600,
            "qualifyingIncome": 3935600,
            "afterTaxQualifyingIncome": 2597496,
            "variancePct": 8.5,
            "varianceFlag": False,
            "taxesPaidLowerYear": 1338100,
            "note": "2023 governs (lower year). LP passive excluded. Real estate net of depreciation.",
            "k1Detail": [
                {"entityName": "Calloway Industrial Group, LLC", "entityType": "LLC", "ownershipPct": 100, "participationType": "Member-Manager", "year1Box1": 2850000, "year2Box1": 3120000, "year1Distributions": 2400000, "year2Distributions": 2600000, "qualifyingIncome": 2850000, "excluded": False},
                {"entityName": "CIG Holdings II, LLC", "entityType": "LLC", "ownershipPct": 85, "participationType": "Member-Manager", "year1Box1": 480000, "year2Box1": 510000, "year1Distributions": 420000, "year2Distributions": 450000, "qualifyingIncome": 480000, "excluded": False},
                {"entityName": "CRW Real Estate Partners I, LLC", "entityType": "LLC", "ownershipPct": 60, "participationType": "Managing Member", "year1Box1": 122200, "year2Box1": 132400, "year1Distributions": 95000, "year2Distributions": 105000, "qualifyingIncome": 122200, "excluded": False, "note": "Net after depreciation"},
                {"entityName": "CRW Real Estate Partners II, LLC", "entityType": "LLC", "ownershipPct": 60, "participationType": "Managing Member", "year1Box1": 76400, "year2Box1": 87900, "year1Distributions": 60000, "year2Distributions": 72000, "qualifyingIncome": 76400, "excluded": False, "note": "Net after depreciation"},
                {"entityName": "Vantage Point Capital Partners, LLC", "entityType": "Partnership", "ownershipPct": 12, "participationType": "Limited Partner", "year1Box1": 185000, "year2Box1": 210000, "year1Distributions": 0, "year2Distributions": 0, "qualifyingIncome": 0, "excluded": True, "note": "LP passive — excluded per methodology"},
            ],
            "interestIncome": 187400,
            "dividendIncome": 124600,
            "otherIncome": 95000,
        },
        "debtDetail": [
            {"creditor": "First Horizon Bank", "accountType": "Primary Residence Mortgage", "balance": 4250000, "monthlyPayment": 28400, "annualPayment": 340800, "note": ""},
            {"creditor": "First Horizon Bank", "accountType": "Vacation Home Mortgage", "balance": 2820000, "monthlyPayment": 18800, "annualPayment": 225600, "note": "Vail, CO property"},
            {"creditor": "Morgan Stanley PWM", "accountType": "Private LOC (Secured)", "balance": 2500000, "monthlyPayment": 10938, "annualPayment": 131250, "note": "Secured by brokerage account; $5M limit"},
            {"creditor": "Morgan Stanley PWM", "accountType": "Margin Loan", "balance": 1850000, "monthlyPayment": 8167, "annualPayment": 98008, "note": "Against Individual Brokerage account"},
            {"creditor": "First Horizon Bank", "accountType": "Commercial RE Loan", "balance": 650000, "monthlyPayment": 5100, "annualPayment": 61200, "note": "CRW Real Estate Partners I — guaranteed by borrower"},
        ],
        "trustStructures": [
            {"trustName": "James R. Calloway Revocable Trust", "trustType": "revocable", "liquidAssets": 3200000, "borrowerHasAccess": True, "note": "Borrower is sole trustee; assets include brokerage and cash. Counted in liquid asset total."},
            {"trustName": "Calloway Family Irrevocable Trust (2019)", "trustType": "irrevocable", "liquidAssets": 1450000, "borrowerHasAccess": False, "note": "Spouse is trustee; borrower has no withdrawal rights. Excluded from liquid asset analysis."},
        ],
        "balanceSheet": {
            "totalAssets": 47798842,
            "grossTotalAssets": 47798842,
            "liquidAssets": 25698842,
            "tier1Assets": 3948382,
            "tier1NetLiquid": 3948382,
            "totalLiabilities": 12070000,
            "netWorth": 35728842,
            "statedNetWorth": 35728842,
            "netWorthCoverage": 3.65,
            "liquidityRatio": 2.62,
            "leverageRatio": 0.338,
            "entityGuaranteeLiquidity": 0,
            "liquidDetail": [
                {"institution": "First Horizon Bank", "accountType": "Checking + MMA", "balance": 3948382, "netAvailable": 3948382},
                {"institution": "Fidelity Investments", "accountType": "Individual Brokerage", "balance": 6100000, "netAvailable": 6100000},
                {"institution": "Morgan Stanley PWM", "accountType": "Margin Account (Pledged)", "balance": 22500460, "marginLoan": 1850000, "pledged": 5000000, "netAvailable": 15650460},
            ],
        },
        "collateral": {
            "year": 2016,
            "make": "Bombardier",
            "model": "Challenger 350",
            "sn": "20632",
            "registration": "N599JF",
            "aftt": 2424,
            "landings": 1457,
            "engine_program": "JSSI Essential Select Plus",
            "apu_enrolled": True,
            "camp_enrolled": True,
            "purchasePrice": 14000000,
            "fmv_curve": 18252000,
            "fmv_purchase_price": 14000000,
            "fmv_used": 18252000,
            "estimatedFMV": 18252000,
            "ltvVsPurchasePrice": 88.2,
            "ltv_on_curve_fmv": 82.2,
            "ltv_on_purchase_price": 88.2,
            "curveName": "AireLogix CL350 Curve v4",
            "collateral_quality_rating": "Strong",
        },
        "aircraft": {
            "description": "2016 Bombardier Challenger 350",
            "serialNumber": "20632",
            "registration": "N599JF",
            "aftt": 2424,
            "landings": 1457,
            "engineProgram": "JSSI Essential Select Plus",
            "year": 2016,
            "make": "Bombardier",
            "model": "Challenger 350",
        },
        "repaymentSources": {
            "primary": "K-1 distributions from operating entities",
            "primaryBasis": "Calloway Industrial Group LLC and CIG Holdings II LLC — 100% ownership, member-manager control",
            "secondary": "Investment income and liquid asset base",
            "tertiary": "Aircraft asset liquidation"
        },
        "flags": [
            {"flag": "Income Concentration", "detail": "Calloway Industrial Group represents 81% of qualifying K-1 income", "severity": "WATCH"},
            {"flag": "LOC Partially Drawn", "detail": "$2.5M drawn on $5.0M private LOC secured by Morgan Stanley account", "severity": "WATCH"},
            {"flag": "Margin Loan Outstanding", "detail": "$1.85M margin loan against Morgan Stanley brokerage account", "severity": "INFO"},
        ],
        "lenderRouting": {
            "recommended": ["US Bank Aviation Finance", "PNC Equipment Finance", "Citizens Private Bank"],
            "rationale": "Very Strong credit — ORR 2 qualifies for all eight lender relationships. Preferred distribution to top three given strong GDSCR (2.53x) and net worth coverage (3.65x).",
        },
    },
    "borrowerProfile": {
        "narrative": "James R. Calloway is the founder and managing member of Calloway Industrial Group, LLC, a Columbus, Ohio-based industrial distribution and logistics company established in 2004. Mr. Calloway built the business from a regional fastener distributor into a multi-state industrial supply operation serving automotive and heavy manufacturing sectors across the Midwest. He holds a B.S. in Mechanical Engineering from Ohio State University and an MBA from the Fisher College of Business. Calloway Industrial Group reported approximately $22 million in revenue for the most recent fiscal year, with operations across Ohio, Michigan, and Indiana. The company maintains long-term supply agreements with several Tier 1 automotive suppliers and has achieved consistent year-over-year revenue growth averaging 8% over the past five years. Mr. Calloway holds full member-manager control of the entity and serves on the board of the Ohio Industrial Distributors Association. He is also a principal in CIG Holdings II LLC, a real estate holding entity focused on light industrial and flex commercial properties.\n\nMr. Calloway has been an active general aviation pilot since 2009 and currently holds an ATP certificate with multi-engine instrument ratings. He previously owned a Beechcraft King Air 350 for business travel before trading up to the Challenger 350. The proposed aircraft acquisition is consistent with his established pattern of business aircraft ownership and will be primarily used for travel between the company's Midwest facilities and supplier visits. No adverse litigation, bankruptcy, or regulatory history was identified in public records. The borrower maintains a low public profile consistent with a privately-held mid-market business owner.",
        "sources": [
            {"url": "https://www.callowayindustrial.com", "title": "Calloway Industrial Group — Company Website", "relevance": "Confirmed business operations, founding year, sector focus, and management team"},
            {"url": "https://www.bizjournals.com/columbus/news/2022/03/calloway-industrial-expansion", "title": "Columbus Business First: Calloway Industrial Expands into Michigan", "relevance": "Revenue growth trajectory and geographic expansion corroborated"},
            {"url": "https://www.ohioindustrialdist.org/board", "title": "OIDA Board of Directors", "relevance": "Confirmed board membership — Ohio Industrial Distributors Association"},
        ],
        "newsItems": [
            {"headline": "Calloway Industrial Group Opens Pontiac Distribution Center", "date": "2022-03", "url": "https://www.bizjournals.com/columbus/news/2022/03/calloway-industrial-expansion", "summary": "Company expanded into Michigan market with a 120,000 sq ft distribution facility, adding 45 jobs."},
            {"headline": "OIDA Names New Board Members for 2023–2025 Term", "date": "2023-01", "url": "https://www.ohioindustrialdist.org/news/board-2023", "summary": "James Calloway appointed to the Ohio Industrial Distributors Association board alongside five other regional operators."},
        ],
        "verificationFlags": [
            {"field": "Revenue Scale", "stated": "Not stated — inferred from K-1 distributions ($2.4M–$2.6M)", "found": "~$22M annual revenue per press coverage; implied owner economics consistent with stated K-1 income", "severity": "INFO"},
        ],
        "industrySector": "Industrial Distribution & Logistics",
        "locationHint": "Ohio",
        "researchQuality": "medium",
        "researchNote": "Company website, local business press, and trade association records confirm borrower background. No adverse findings. Revenue figure inferred from news coverage.",
        "generatedAt": "2026-04-01T12:00:00+00:00",
        "dealId": "DEMO-CALLOWAY-2024",
        "borrowerName": "James R. Calloway",
    },
}


def seed_calloway():
    """Seed or refresh the Calloway demo deal on every startup."""
    try:
        save_deal(dict(CALLOWAY_DEMO))
        print(f"[deals_store] Calloway demo seeded/refreshed: {CALLOWAY_DEAL_ID}")
    except Exception as e:
        print(f"[deals_store] Calloway seed error: {e}")


# Run seed on import (after schema is ready)
if _USE_DB:
    seed_calloway()
