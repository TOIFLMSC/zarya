CREATE TABLE avatar_runs (
 id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(id),
 bot_id TEXT NOT NULL, chat_id TEXT NOT NULL, scope TEXT NOT NULL, access_version INTEGER NOT NULL,
 user_id TEXT NOT NULL, target_name TEXT NOT NULL, binding TEXT NOT NULL,
 mode TEXT NOT NULL, previous_id INTEGER REFERENCES avatar_runs(id),
 state TEXT NOT NULL, model TEXT NOT NULL, reasoning TEXT NOT NULL, settings_version INTEGER NOT NULL,
 selection TEXT NOT NULL DEFAULT '{}', result TEXT, error_code TEXT,
 created_at TEXT NOT NULL, checked_at TEXT
);
ALTER TABLE model_calls ADD COLUMN avatar_run_id INTEGER REFERENCES avatar_runs(id);
CREATE UNIQUE INDEX one_avatar_call ON model_calls(avatar_run_id) WHERE avatar_run_id IS NOT NULL;
CREATE INDEX avatar_scope ON avatar_runs(bot_id,chat_id,access_version,user_id,id);
