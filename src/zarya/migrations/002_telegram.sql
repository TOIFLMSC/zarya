CREATE TABLE telegram_state (bot_id TEXT PRIMARY KEY, next_offset INTEGER,
    last_received REAL NOT NULL DEFAULT 0);
CREATE TABLE telegram_access (bot_id TEXT NOT NULL, scope TEXT NOT NULL,
    subject_id TEXT NOT NULL, title TEXT NOT NULL, username TEXT,
    state TEXT NOT NULL DEFAULT 'pending', version INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL, member_state TEXT NOT NULL DEFAULT 'unknown',
    PRIMARY KEY(bot_id, scope, subject_id));
ALTER TABLE events ADD COLUMN disposition TEXT NOT NULL DEFAULT 'accepted';
ALTER TABLE jobs ADD COLUMN access_version INTEGER NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN next_attempt REAL NOT NULL DEFAULT 0;
ALTER TABLE outbox ADD COLUMN bot_id TEXT;
ALTER TABLE outbox ADD COLUMN scope TEXT;
ALTER TABLE outbox ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE outbox ADD COLUMN next_attempt REAL NOT NULL DEFAULT 0;
ALTER TABLE outbox ADD COLUMN message_id INTEGER;
ALTER TABLE outbox ADD COLUMN error_code TEXT;
ALTER TABLE outbox ADD COLUMN access_version INTEGER NOT NULL DEFAULT 0;
CREATE TABLE telegram_migrations (bot_id TEXT NOT NULL, old_id TEXT NOT NULL,
    new_id TEXT NOT NULL, PRIMARY KEY(bot_id, old_id));
CREATE UNIQUE INDEX one_job_per_event ON jobs(event_id, kind);
CREATE UNIQUE INDEX one_outbox_per_job ON outbox(job_id);
CREATE INDEX jobs_ready ON jobs(state, next_attempt);
CREATE INDEX outbox_ready ON outbox(bot_id, state, next_attempt);
