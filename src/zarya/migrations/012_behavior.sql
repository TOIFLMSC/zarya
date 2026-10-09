CREATE TABLE conversation_moods (
 bot_id TEXT NOT NULL, chat_id TEXT NOT NULL, thread_id INTEGER NOT NULL DEFAULT 0,
 tone TEXT NOT NULL DEFAULT 'neutral', intensity REAL NOT NULL DEFAULT 0,
 updated_at REAL NOT NULL, version INTEGER NOT NULL DEFAULT 1,
 epoch INTEGER NOT NULL DEFAULT 1,
 source_run_id INTEGER REFERENCES dialogue_runs(id),
 PRIMARY KEY(bot_id,chat_id,thread_id)
);
CREATE TABLE group_behavior (
 bot_id TEXT NOT NULL, chat_id TEXT NOT NULL, mode TEXT NOT NULL DEFAULT 'off',
 chance_percent REAL NOT NULL DEFAULT 5, version INTEGER NOT NULL DEFAULT 1,
 PRIMARY KEY(bot_id,chat_id)
);
CREATE TABLE behavior_decisions (
 run_id INTEGER PRIMARY KEY REFERENCES dialogue_runs(id),
 mode TEXT NOT NULL, action TEXT NOT NULL, plan_json TEXT NOT NULL,
 public_reason TEXT NOT NULL, applied INTEGER NOT NULL DEFAULT 0,
 created_at REAL NOT NULL
);
