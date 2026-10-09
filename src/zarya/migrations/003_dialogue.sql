DROP INDEX one_outbox_per_job;
ALTER TABLE outbox ADD COLUMN part_index INTEGER NOT NULL DEFAULT 0;
CREATE UNIQUE INDEX one_outbox_per_part ON outbox(job_id,part_index);
CREATE TABLE dialogue_runs (
 id INTEGER PRIMARY KEY, job_id INTEGER UNIQUE REFERENCES jobs(id),
 bot_id TEXT NOT NULL, chat_id TEXT NOT NULL, thread_id INTEGER,
 sender_id TEXT NOT NULL, message_id INTEGER NOT NULL,
 trigger TEXT NOT NULL, state TEXT NOT NULL, snapshot TEXT NOT NULL,
 response TEXT, error_code TEXT, created_at TEXT NOT NULL,
 replay_of INTEGER REFERENCES dialogue_runs(id), mode TEXT NOT NULL DEFAULT 'live'
);
ALTER TABLE model_calls ADD COLUMN run_id INTEGER REFERENCES dialogue_runs(id);
ALTER TABLE model_calls ADD COLUMN settings_version INTEGER;
ALTER TABLE model_calls ADD COLUMN request_json TEXT;
ALTER TABLE model_calls ADD COLUMN response_id TEXT;
ALTER TABLE model_calls ADD COLUMN request_id TEXT;
ALTER TABLE model_calls ADD COLUMN latency_ms INTEGER;
ALTER TABLE model_calls ADD COLUMN cost_usd REAL;
ALTER TABLE model_calls ADD COLUMN pricing_profile TEXT;
ALTER TABLE model_calls ADD COLUMN error_code TEXT;
ALTER TABLE model_calls ADD COLUMN finished_at TEXT;
CREATE UNIQUE INDEX one_call_per_run ON model_calls(run_id);
CREATE TABLE recent_messages (
 bot_id TEXT NOT NULL, chat_id TEXT NOT NULL, message_id INTEGER NOT NULL,
 thread_id INTEGER, sender_id TEXT NOT NULL, name TEXT NOT NULL,
 role TEXT NOT NULL, text TEXT NOT NULL, event_id INTEGER,
 received_at REAL NOT NULL,
 PRIMARY KEY(bot_id,chat_id,message_id)
);
CREATE TABLE active_dialogues (
 bot_id TEXT NOT NULL, chat_id TEXT NOT NULL, thread_id INTEGER NOT NULL DEFAULT 0,
 sender_id TEXT NOT NULL, expires_at REAL NOT NULL, remaining INTEGER NOT NULL,
 PRIMARY KEY(bot_id,chat_id,thread_id,sender_id)
);
CREATE TABLE delivery_limits (
 bot_id TEXT NOT NULL, chat_id TEXT NOT NULL, last_attempt REAL NOT NULL DEFAULT 0,
 blocked_until REAL NOT NULL DEFAULT 0, PRIMARY KEY(bot_id,chat_id)
);
CREATE INDEX recent_chat ON recent_messages(bot_id,chat_id,received_at);
CREATE INDEX runs_recent ON dialogue_runs(bot_id,id);
