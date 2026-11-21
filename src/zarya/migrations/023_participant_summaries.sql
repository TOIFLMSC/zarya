CREATE TABLE participant_summary_blocks (
 id INTEGER PRIMARY KEY,
 bot_id TEXT NOT NULL, chat_id TEXT NOT NULL, access_version INTEGER NOT NULL,
 sender_id TEXT NOT NULL, manifest TEXT NOT NULL, media_refs TEXT NOT NULL DEFAULT '[]',
 state TEXT NOT NULL DEFAULT 'queued', result TEXT, error_code TEXT,
 settings_version INTEGER, instruction_hash TEXT,
 created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT
);
CREATE INDEX participant_summary_scope ON participant_summary_blocks
 (bot_id,chat_id,access_version,sender_id,id);
CREATE TABLE participant_summary_sources (
 block_id INTEGER NOT NULL REFERENCES participant_summary_blocks(id),
 message_id INTEGER NOT NULL, event_id INTEGER NOT NULL REFERENCES events(id),
 PRIMARY KEY(block_id,message_id)
);
CREATE INDEX participant_summary_revision ON participant_summary_sources(event_id,block_id);
ALTER TABLE model_calls ADD COLUMN participant_summary_id INTEGER
 REFERENCES participant_summary_blocks(id);
CREATE UNIQUE INDEX one_call_per_participant_summary ON model_calls(participant_summary_id);
