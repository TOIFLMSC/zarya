CREATE TABLE photo_batches (
 id INTEGER PRIMARY KEY, bot_id TEXT NOT NULL, chat_id TEXT NOT NULL, scope TEXT NOT NULL,
 access_version INTEGER NOT NULL, album_key TEXT NOT NULL, generation INTEGER NOT NULL,
 state TEXT NOT NULL DEFAULT 'queued', model TEXT NOT NULL, processing_version TEXT NOT NULL,
 created_at TEXT NOT NULL, collect_until REAL NOT NULL, collect_deadline REAL NOT NULL,
 leader_job_id INTEGER REFERENCES jobs(id), leader_message_id INTEGER NOT NULL,
 fingerprint TEXT, result TEXT, error_code TEXT, cache_source INTEGER REFERENCES photo_batches(id),
 settings_version INTEGER, manifest TEXT, finished_at TEXT,
 UNIQUE(bot_id,chat_id,access_version,album_key,generation)
);
CREATE TABLE photo_items (
 id INTEGER PRIMARY KEY, batch_id INTEGER NOT NULL REFERENCES photo_batches(id),
 bot_id TEXT NOT NULL, chat_id TEXT NOT NULL, message_id INTEGER NOT NULL,
 event_id INTEGER NOT NULL REFERENCES events(id), file_id TEXT NOT NULL,
 file_unique_id TEXT NOT NULL, caption TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
 raw_path TEXT, image_path TEXT, content_hash TEXT, mime TEXT,
 width INTEGER, height INTEGER, byte_count INTEGER, error_code TEXT,
    UNIQUE(batch_id,message_id,event_id)
);
CREATE INDEX photos_scope ON photo_batches(bot_id,chat_id,id);
CREATE INDEX photos_message ON photo_items(bot_id,chat_id,message_id);
ALTER TABLE model_calls ADD COLUMN photo_batch_id INTEGER REFERENCES photo_batches(id);
CREATE UNIQUE INDEX photo_call_once ON model_calls(photo_batch_id) WHERE photo_batch_id IS NOT NULL;
