CREATE TABLE obsidian_binding (
 id INTEGER PRIMARY KEY CHECK(id=1), root TEXT NOT NULL, epoch TEXT NOT NULL
);
CREATE TABLE obsidian_files (
 path TEXT PRIMARY KEY, bot_id TEXT NOT NULL, chat_id TEXT NOT NULL,
 kind TEXT NOT NULL, node_key TEXT NOT NULL, epoch TEXT NOT NULL,
 version INTEGER NOT NULL, access_version INTEGER NOT NULL,
 hash TEXT NOT NULL, prefix TEXT NOT NULL, suffix TEXT NOT NULL,
 baseline TEXT, seen_hash TEXT, state TEXT NOT NULL DEFAULT 'ready'
);
CREATE TABLE obsidian_changes (
 id TEXT PRIMARY KEY, path TEXT NOT NULL, bot_id TEXT NOT NULL, chat_id TEXT NOT NULL,
 fact_id INTEGER, epoch TEXT NOT NULL, expected_version INTEGER NOT NULL,
 access_version INTEGER NOT NULL, file_hash TEXT NOT NULL,
 before_text TEXT, proposed_text TEXT, state TEXT NOT NULL, reason TEXT,
 created_at TEXT NOT NULL, UNIQUE(path,file_hash,epoch)
);
CREATE INDEX obsidian_change_queue ON obsidian_changes(bot_id,state,created_at);
CREATE TABLE memory_commands (
 id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, bot_id TEXT NOT NULL,
 chat_id TEXT NOT NULL, fact_id INTEGER NOT NULL, origin TEXT NOT NULL,
 action TEXT NOT NULL, before_version INTEGER NOT NULL, after_version INTEGER NOT NULL,
 before_text TEXT, after_text TEXT, created_at TEXT NOT NULL
);
CREATE INDEX memory_command_history ON memory_commands(bot_id,created_at);
