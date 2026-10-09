CREATE TABLE media_runs (
 id INTEGER PRIMARY KEY, bot_id TEXT NOT NULL, chat_id TEXT NOT NULL, scope TEXT NOT NULL,
 access_version INTEGER NOT NULL, message_id INTEGER NOT NULL, event_id INTEGER NOT NULL,
 kind TEXT NOT NULL, file_id TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
 processing_version TEXT NOT NULL, asr_model TEXT NOT NULL, model TEXT NOT NULL,
 settings_version INTEGER NOT NULL, limits TEXT NOT NULL, duration REAL,
 frames TEXT NOT NULL DEFAULT '[]', coverage TEXT NOT NULL DEFAULT '{}',
 transcript TEXT, result TEXT, error_code TEXT, created_at TEXT NOT NULL, finished_at TEXT,
 UNIQUE(bot_id,chat_id,access_version,event_id,processing_version)
);
CREATE TABLE media_dependencies (
 job_id INTEGER NOT NULL REFERENCES jobs(id), media_run_id INTEGER NOT NULL REFERENCES media_runs(id),
 PRIMARY KEY(job_id,media_run_id)
);
ALTER TABLE model_calls ADD COLUMN media_run_id INTEGER REFERENCES media_runs(id);
ALTER TABLE model_calls ADD COLUMN operation TEXT;
CREATE UNIQUE INDEX media_call_once ON model_calls(media_run_id,operation) WHERE media_run_id IS NOT NULL;
CREATE INDEX media_scope ON media_runs(bot_id,chat_id,id);
CREATE TABLE media_file_cleanup (path TEXT PRIMARY KEY);
