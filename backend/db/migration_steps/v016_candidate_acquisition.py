from typing import Final

MIGRATION_NAME: Final = "016_candidate_acquisition"

_ATTEMPTS = """CREATE TABLE IF NOT EXISTS source_acquisition_attempts (
    attempt_id TEXT PRIMARY KEY,
    extraction_id TEXT NOT NULL,
    source TEXT NOT NULL,
    indicator TEXT NOT NULL,
    candidate_id TEXT,
    route TEXT NOT NULL CHECK (route IN ('primary', 'candidate')),
    status TEXT NOT NULL CHECK (status IN (
        'running', 'success', 'network_error', 'validation_error',
        'deferred', 'semantic_conflict', 'persistence_error', 'internal_error'
    )),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    request_count INTEGER CHECK (request_count IS NULL OR request_count >= 0),
    retry_count INTEGER CHECK (retry_count IS NULL OR retry_count >= 0),
    payload_sha256 TEXT,
    reference_min TEXT,
    reference_max TEXT,
    error_code TEXT,
    error_category TEXT
)"""

_ORIGINS = """CREATE TABLE IF NOT EXISTS source_observation_origins (
    origin_id TEXT PRIMARY KEY,
    extraction_id TEXT NOT NULL,
    source TEXT NOT NULL,
    contract_id TEXT NOT NULL,
    identity_json TEXT NOT NULL,
    reference_date TEXT NOT NULL,
    value_sha256 TEXT NOT NULL,
    route TEXT NOT NULL CHECK (route IN ('primary', 'candidate')),
    candidate_id TEXT,
    source_payload_sha256 TEXT NOT NULL,
    source_published_at TEXT,
    captured_at TEXT NOT NULL,
    contract_version TEXT,
    profile_semantic_sha256 TEXT,
    parser_semantic_sha256 TEXT,
    proof_sha256 TEXT,
    UNIQUE (extraction_id, source, contract_id, identity_json, value_sha256, route)
)"""

_COMMITS = """CREATE TABLE IF NOT EXISTS source_acquisition_commits (
    extraction_id TEXT PRIMARY KEY,
    selected_route TEXT NOT NULL CHECK (selected_route IN ('primary', 'candidate')),
    candidate_id TEXT,
    observation_count INTEGER NOT NULL CHECK (observation_count >= 0),
    source_payload_sha256 TEXT NOT NULL,
    committed_at TEXT NOT NULL
)"""

_CANARY_BINDINGS = """CREATE TABLE IF NOT EXISTS source_canary_bindings (
    operation_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    as_of TEXT NOT NULL,
    plan_sha256 TEXT NOT NULL,
    proof_sha256 TEXT NOT NULL,
    contract_version TEXT NOT NULL,
    profile_semantic_sha256 TEXT NOT NULL,
    parser_semantic_sha256 TEXT NOT NULL,
    actual_reference_period TEXT,
    captured_at TEXT NOT NULL,
    request_count INTEGER NOT NULL CHECK (request_count >= 0),
    inserted_count INTEGER NOT NULL CHECK (inserted_count >= 0),
    updated_count INTEGER NOT NULL CHECK (updated_count >= 0),
    bound_at TEXT NOT NULL
)"""

SQLITE_STATEMENTS: Final[tuple[str, ...]] = (
    _ATTEMPTS,
    _ORIGINS,
    _COMMITS,
    _CANARY_BINDINGS,
    "CREATE INDEX IF NOT EXISTS source_acquisition_attempts_extraction_idx ON source_acquisition_attempts (extraction_id, started_at)",
    "CREATE INDEX IF NOT EXISTS source_observation_origins_identity_idx ON source_observation_origins (source, contract_id, reference_date)",
    "CREATE INDEX IF NOT EXISTS source_canary_bindings_candidate_idx ON source_canary_bindings (candidate_id, as_of, bound_at)",
)

POSTGRES_STATEMENTS: Final[tuple[str, ...]] = SQLITE_STATEMENTS
