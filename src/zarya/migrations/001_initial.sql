CREATE TABLE admin (id INTEGER PRIMARY KEY CHECK (id = 1), password_hash TEXT NOT NULL);
CREATE TABLE settings (id INTEGER PRIMARY KEY CHECK (id = 1), version INTEGER NOT NULL,
    document TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE settings_versions (version INTEGER PRIMARY KEY, document TEXT NOT NULL,
    updated_at TEXT NOT NULL);
CREATE TABLE users (telegram_id TEXT PRIMARY KEY, username TEXT, display_name TEXT);
CREATE TABLE chats (telegram_id TEXT PRIMARY KEY, kind TEXT NOT NULL,
    title TEXT NOT NULL, access_state TEXT NOT NULL DEFAULT 'pending');
CREATE TABLE access_grants (scope TEXT NOT NULL, subject_id TEXT NOT NULL,
    state TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(scope, subject_id));
CREATE TABLE events (id INTEGER PRIMARY KEY, bot_id TEXT NOT NULL, update_id INTEGER NOT NULL,
    chat_id TEXT, payload TEXT NOT NULL, received_at TEXT NOT NULL, UNIQUE(bot_id, update_id));
CREATE TABLE jobs (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, event_id INTEGER REFERENCES events(id),
    state TEXT NOT NULL, payload TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
    lease_until TEXT, created_at TEXT NOT NULL);
CREATE TABLE outbox (id INTEGER PRIMARY KEY, job_id INTEGER REFERENCES jobs(id),
    chat_id TEXT NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE model_calls (id INTEGER PRIMARY KEY, job_id INTEGER REFERENCES jobs(id),
    provider TEXT NOT NULL, model TEXT NOT NULL, state TEXT NOT NULL,
    usage_json TEXT, created_at TEXT NOT NULL);
