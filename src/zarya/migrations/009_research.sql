CREATE TABLE research_messages (
 bot_id TEXT NOT NULL, chat_id TEXT NOT NULL, message_id INTEGER NOT NULL,
 event_id INTEGER NOT NULL REFERENCES events(id), access_version INTEGER NOT NULL,
 PRIMARY KEY(bot_id,chat_id,message_id)
);
CREATE TABLE research_runs (
 id INTEGER PRIMARY KEY, job_id INTEGER UNIQUE REFERENCES jobs(id),
 bot_id TEXT NOT NULL, chat_id TEXT NOT NULL, scope TEXT NOT NULL,
 access_version INTEGER NOT NULL, state TEXT NOT NULL, mode TEXT NOT NULL,
 question TEXT NOT NULL, material TEXT NOT NULL, manifest TEXT NOT NULL,
 sources TEXT NOT NULL DEFAULT '[]', result TEXT, actions TEXT NOT NULL DEFAULT '[]',
 settings_version INTEGER NOT NULL, model TEXT NOT NULL, reasoning TEXT NOT NULL,
 created_at TEXT NOT NULL, finished_at TEXT, error_code TEXT
);
ALTER TABLE model_calls ADD COLUMN research_run_id INTEGER REFERENCES research_runs(id);
ALTER TABLE model_calls ADD COLUMN search_cost_usd REAL;
CREATE UNIQUE INDEX one_call_per_research ON model_calls(research_run_id);
CREATE INDEX research_scope ON research_runs(bot_id,chat_id,id);
