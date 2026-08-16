PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS visual_evidence_records (
    sequence_no INTEGER PRIMARY KEY AUTOINCREMENT,
    stream_kind TEXT NOT NULL CHECK (stream_kind IN ('lineage','validation')),
    algorithm_key TEXT NOT NULL,
    record_id TEXT NOT NULL,
    record_digest TEXT NOT NULL CHECK (length(record_digest)=64),
    canonical_record_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(stream_kind,algorithm_key,record_id)
);

CREATE TABLE IF NOT EXISTS visual_evidence_receipts (
    idempotency_key TEXT PRIMARY KEY,
    request_fingerprint TEXT NOT NULL CHECK (length(request_fingerprint)=64),
    stream_kind TEXT NOT NULL,
    algorithm_key TEXT NOT NULL,
    record_id TEXT NOT NULL,
    record_digest TEXT NOT NULL CHECK (length(record_digest)=64),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS visual_evidence_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    stream_kind TEXT NOT NULL CHECK (stream_kind IN ('lineage','validation')),
    algorithm_key TEXT NOT NULL,
    watermark INTEGER NOT NULL CHECK (watermark>=0),
    membership_digest TEXT NOT NULL CHECK (length(membership_digest)=64),
    total INTEGER NOT NULL CHECK (total>=0),
    created_at TEXT NOT NULL,
    UNIQUE(stream_kind,algorithm_key,watermark,membership_digest)
);

CREATE TABLE IF NOT EXISTS visual_evidence_snapshot_items (
    snapshot_id TEXT NOT NULL REFERENCES visual_evidence_snapshots(snapshot_id),
    ordinal INTEGER NOT NULL CHECK (ordinal>=0),
    record_sequence_no INTEGER NOT NULL REFERENCES visual_evidence_records(sequence_no),
    record_digest TEXT NOT NULL CHECK (length(record_digest)=64),
    PRIMARY KEY(snapshot_id,ordinal),
    UNIQUE(snapshot_id,record_sequence_no)
);

CREATE TRIGGER IF NOT EXISTS visual_evidence_records_no_update
BEFORE UPDATE ON visual_evidence_records
BEGIN SELECT RAISE(ABORT,'visual evidence record is immutable'); END;
CREATE TRIGGER IF NOT EXISTS visual_evidence_records_no_delete
BEFORE DELETE ON visual_evidence_records
BEGIN SELECT RAISE(ABORT,'visual evidence record is immutable'); END;
CREATE TRIGGER IF NOT EXISTS visual_evidence_snapshot_items_no_update
BEFORE UPDATE ON visual_evidence_snapshot_items
BEGIN SELECT RAISE(ABORT,'visual evidence snapshot item is immutable'); END;
CREATE TRIGGER IF NOT EXISTS visual_evidence_snapshot_items_no_delete
BEFORE DELETE ON visual_evidence_snapshot_items
BEGIN SELECT RAISE(ABORT,'visual evidence snapshot item is immutable'); END;
