CREATE TABLE task_states (
 id INTEGER PRIMARY KEY,
 bot_id TEXT NOT NULL, chat_id TEXT NOT NULL, thread_id INTEGER NOT NULL,
 access_version INTEGER NOT NULL, anchor_id INTEGER NOT NULL,
 revision INTEGER NOT NULL DEFAULT 0, attempted INTEGER NOT NULL DEFAULT 0,
 ready_at REAL NOT NULL DEFAULT 0, ready_revision INTEGER, result TEXT, proof TEXT,
 retired INTEGER NOT NULL DEFAULT 0,
 UNIQUE(bot_id,chat_id,access_version,anchor_id)
);
CREATE INDEX task_states_scope ON task_states(bot_id,chat_id,access_version,thread_id,retired);
CREATE INDEX task_states_queue ON task_states(bot_id,retired,ready_at,id);
CREATE TABLE task_members (
 task_id INTEGER NOT NULL REFERENCES task_states(id),
 message_id INTEGER NOT NULL, event_id INTEGER NOT NULL REFERENCES events(id),
 contributes INTEGER NOT NULL,
 PRIMARY KEY(task_id,message_id), UNIQUE(event_id)
);
CREATE INDEX task_members_message ON task_members(message_id,task_id);
ALTER TABLE model_calls ADD COLUMN task_state_id INTEGER REFERENCES task_states(id);
ALTER TABLE model_calls ADD COLUMN task_revision INTEGER;
CREATE UNIQUE INDEX one_call_per_task_revision ON model_calls(task_state_id,task_revision);
