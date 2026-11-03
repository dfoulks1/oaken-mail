-- Attachments live in the quarantine folder, not in raw.db: a stored message holds a stub where the
-- body was. One row per distinct attachment (its bytes, hashed), with what a scan has said of it.
-- Nothing is released from quarantine until a scan says ``clean``.
CREATE TABLE quarantined_files (
    sha256       TEXT PRIMARY KEY,
    rel_path     TEXT NOT NULL,
    size         INTEGER NOT NULL,
    content_type TEXT NOT NULL,
    flagged      INTEGER NOT NULL DEFAULT 0,
    scan_status  TEXT NOT NULL DEFAULT 'pending'
        CHECK (scan_status IN ('pending', 'clean', 'infected', 'error')),
    scan_detail  TEXT NOT NULL DEFAULT '',
    scanner      TEXT NOT NULL DEFAULT '',
    scanned_at   TEXT,
    created_at   TEXT NOT NULL
);
