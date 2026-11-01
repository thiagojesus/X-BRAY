"""Private app sessions, explicit admin memberships and one-use OAuth flows."""

MIGRATION_NAME = "022_admin_auth"

SQLITE_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS admin_memberships (
        user_id TEXT PRIMARY KEY,
        granted_at TEXT NOT NULL,
        granted_by TEXT,
        revoked_at TEXT,
        reason TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS admin_sessions (
        id TEXT PRIMARY KEY,
        session_token_hash TEXT NOT NULL UNIQUE,
        user_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        last_activity_at TEXT NOT NULL,
        idle_expires_at TEXT NOT NULL,
        absolute_expires_at TEXT NOT NULL,
        revoked_at TEXT,
        csrf_token_hash TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS admin_oauth_flows (
        id TEXT PRIMARY KEY,
        flow_handle_hash TEXT NOT NULL UNIQUE,
        state_hash TEXT NOT NULL UNIQUE,
        browser_binding_hash TEXT NOT NULL,
        verifier_ciphertext BLOB NOT NULL,
        nonce BLOB NOT NULL,
        key_version TEXT NOT NULL,
        callback_url TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        consumed_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS admin_auth_events (
        id TEXT PRIMARY KEY,
        user_id TEXT,
        event TEXT NOT NULL,
        created_at TEXT NOT NULL,
        reason TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS admin_sessions_user_idx ON admin_sessions(user_id, revoked_at)",
    "CREATE INDEX IF NOT EXISTS admin_sessions_cleanup_idx ON admin_sessions(absolute_expires_at, revoked_at)",
    "CREATE INDEX IF NOT EXISTS admin_flows_cleanup_idx ON admin_oauth_flows(expires_at, consumed_at)",
    "CREATE INDEX IF NOT EXISTS admin_flows_browser_rate_idx ON admin_oauth_flows(browser_binding_hash, created_at)",
    "CREATE INDEX IF NOT EXISTS admin_events_cleanup_idx ON admin_auth_events(created_at)",
)

POSTGRES_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS admin_memberships (
        user_id UUID PRIMARY KEY,
        granted_at TIMESTAMPTZ NOT NULL,
        granted_by UUID,
        revoked_at TIMESTAMPTZ,
        reason TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS admin_sessions (
        id UUID PRIMARY KEY,
        session_token_hash TEXT NOT NULL UNIQUE,
        user_id UUID NOT NULL,
        created_at TIMESTAMPTZ NOT NULL,
        last_activity_at TIMESTAMPTZ NOT NULL,
        idle_expires_at TIMESTAMPTZ NOT NULL,
        absolute_expires_at TIMESTAMPTZ NOT NULL,
        revoked_at TIMESTAMPTZ,
        csrf_token_hash TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS admin_oauth_flows (
        id UUID PRIMARY KEY,
        flow_handle_hash TEXT NOT NULL UNIQUE,
        state_hash TEXT NOT NULL UNIQUE,
        browser_binding_hash TEXT NOT NULL,
        verifier_ciphertext BYTEA NOT NULL,
        nonce BYTEA NOT NULL,
        key_version TEXT NOT NULL,
        callback_url TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL,
        expires_at TIMESTAMPTZ NOT NULL,
        consumed_at TIMESTAMPTZ
    )""",
    """CREATE TABLE IF NOT EXISTS admin_auth_events (
        id UUID PRIMARY KEY,
        user_id UUID,
        event TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL,
        reason TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS admin_sessions_user_idx ON admin_sessions(user_id, revoked_at)",
    "CREATE INDEX IF NOT EXISTS admin_sessions_cleanup_idx ON admin_sessions(absolute_expires_at, revoked_at)",
    "CREATE INDEX IF NOT EXISTS admin_flows_cleanup_idx ON admin_oauth_flows(expires_at, consumed_at)",
    "CREATE INDEX IF NOT EXISTS admin_flows_browser_rate_idx ON admin_oauth_flows(browser_binding_hash, created_at)",
    "CREATE INDEX IF NOT EXISTS admin_events_cleanup_idx ON admin_auth_events(created_at)",
    "ALTER TABLE admin_memberships ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE admin_sessions ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE admin_oauth_flows ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE admin_auth_events ENABLE ROW LEVEL SECURITY",
    """DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
            REVOKE ALL ON admin_memberships, admin_sessions, admin_oauth_flows, admin_auth_events FROM anon;
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
            REVOKE ALL ON admin_memberships, admin_sessions, admin_oauth_flows, admin_auth_events FROM authenticated;
        END IF;
    END $$""",
)
