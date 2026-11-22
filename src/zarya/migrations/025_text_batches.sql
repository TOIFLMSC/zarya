CREATE TABLE text_batches (
 job_id INTEGER PRIMARY KEY REFERENCES jobs(id),
 bot_id TEXT NOT NULL, chat_id TEXT NOT NULL, thread_id INTEGER NOT NULL,
 sender_id TEXT NOT NULL, access_version INTEGER NOT NULL,
 opened_at REAL NOT NULL, ready_at REAL NOT NULL, sealed INTEGER NOT NULL DEFAULT 0,
 first_message_at INTEGER NOT NULL, last_message_at INTEGER NOT NULL
);
CREATE INDEX text_batch_scope ON text_batches
 (bot_id,chat_id,thread_id,sender_id,access_version,sealed,ready_at);
CREATE TABLE text_batch_members (
 job_id INTEGER NOT NULL REFERENCES text_batches(job_id),
 message_id INTEGER NOT NULL, event_id INTEGER NOT NULL REFERENCES events(id),
 trigger TEXT NOT NULL,
 PRIMARY KEY(job_id,message_id), UNIQUE(event_id)
);
CREATE INDEX text_batch_message ON text_batch_members(message_id,job_id);
