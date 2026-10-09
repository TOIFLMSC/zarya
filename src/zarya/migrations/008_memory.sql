CREATE TABLE memory_epochs (bot_id TEXT PRIMARY KEY, version INTEGER NOT NULL DEFAULT 1);
CREATE TABLE memory_sources (
 id INTEGER PRIMARY KEY, bot_id TEXT NOT NULL, chat_id TEXT NOT NULL, scope TEXT NOT NULL,
 message_id INTEGER NOT NULL, event_id INTEGER NOT NULL, sender_id TEXT NOT NULL,
 name TEXT NOT NULL, thread_id INTEGER, text TEXT NOT NULL, received_at REAL NOT NULL,
 access_version INTEGER NOT NULL, valid INTEGER NOT NULL DEFAULT 1, processed INTEGER NOT NULL DEFAULT 0,
 UNIQUE(bot_id,chat_id,message_id)
);
CREATE INDEX memory_source_queue ON memory_sources(bot_id,processed,valid,received_at);
CREATE TABLE memory_batches (
 id INTEGER PRIMARY KEY, bot_id TEXT NOT NULL, chat_id TEXT NOT NULL, scope TEXT NOT NULL,
 access_version INTEGER NOT NULL, epoch INTEGER NOT NULL, manifest TEXT NOT NULL,
 state TEXT NOT NULL, created_at TEXT NOT NULL, finished_at TEXT, summary TEXT,
 error_code TEXT, settings_version INTEGER NOT NULL
);
ALTER TABLE model_calls ADD COLUMN memory_batch_id INTEGER REFERENCES memory_batches(id);
CREATE UNIQUE INDEX one_call_per_memory_batch ON model_calls(memory_batch_id);
CREATE TABLE memory_facts (
 id INTEGER PRIMARY KEY, bot_id TEXT NOT NULL, chat_id TEXT NOT NULL, scope TEXT NOT NULL,
 access_version INTEGER NOT NULL, sender_id TEXT NOT NULL, category TEXT NOT NULL,
 fact_key TEXT NOT NULL, text TEXT NOT NULL, provenance TEXT NOT NULL, state TEXT NOT NULL,
 version INTEGER NOT NULL DEFAULT 1, curated INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, reviewed_at REAL NOT NULL
);
CREATE INDEX memory_fact_profile ON memory_facts(bot_id,chat_id,sender_id,state);
CREATE TABLE memory_evidence (
 fact_id INTEGER NOT NULL REFERENCES memory_facts(id), source_id INTEGER NOT NULL REFERENCES memory_sources(id),
 event_id INTEGER NOT NULL, PRIMARY KEY(fact_id,source_id)
);
CREATE TABLE memory_shares (
 fact_id INTEGER PRIMARY KEY REFERENCES memory_facts(id), fact_version INTEGER NOT NULL,
 state TEXT NOT NULL, authority TEXT NOT NULL, actor_id TEXT NOT NULL,
 consent_event_id INTEGER, updated_at TEXT NOT NULL
);
CREATE TABLE memory_tombstones (
 bot_id TEXT NOT NULL, chat_id TEXT NOT NULL, sender_id TEXT NOT NULL,
 category TEXT NOT NULL, fact_key TEXT NOT NULL, through_event INTEGER NOT NULL,
 PRIMARY KEY(bot_id,chat_id,sender_id,category,fact_key)
);
CREATE TABLE memory_consent_requests (
 token TEXT PRIMARY KEY, bot_id TEXT NOT NULL, sender_id TEXT NOT NULL,
 fact_id INTEGER NOT NULL REFERENCES memory_facts(id), fact_version INTEGER NOT NULL,
 expires_at REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE memory_file_cleanup (
 bot_id TEXT NOT NULL, path TEXT NOT NULL, PRIMARY KEY(bot_id,path)
);
