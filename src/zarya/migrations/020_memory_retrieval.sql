CREATE TABLE memory_relations (
 id INTEGER PRIMARY KEY, bot_id TEXT NOT NULL, chat_id TEXT NOT NULL,
 access_version INTEGER NOT NULL, kind TEXT NOT NULL CHECK(kind='contradicts'),
 from_id INTEGER NOT NULL REFERENCES memory_facts(id), from_version INTEGER NOT NULL,
 to_id INTEGER NOT NULL REFERENCES memory_facts(id), to_version INTEGER NOT NULL,
 batch_id INTEGER NOT NULL REFERENCES memory_batches(id), proof TEXT NOT NULL,
 created_at REAL NOT NULL, CHECK(from_id!=to_id)
);
CREATE INDEX memory_relation_scope ON memory_relations(bot_id,chat_id,access_version,id DESC);
CREATE INDEX memory_source_scope ON memory_sources(bot_id,chat_id,access_version,received_at DESC);
CREATE INDEX memory_episode_scope ON memory_batches(bot_id,chat_id,access_version,state,id DESC);
