CREATE TABLE configuration_releases (
 version INTEGER PRIMARY KEY, settings_json TEXT NOT NULL, prompts_json TEXT NOT NULL,
 hash TEXT NOT NULL, prompt_hash TEXT NOT NULL, origin TEXT NOT NULL,
 parent_version INTEGER, created_at TEXT NOT NULL
);
CREATE TABLE configuration_drafts (
 id TEXT PRIMARY KEY, base_version INTEGER NOT NULL, document TEXT NOT NULL,
 hash TEXT NOT NULL, files_json TEXT NOT NULL, origin TEXT NOT NULL,
 state TEXT NOT NULL, created_at TEXT NOT NULL, applied_version INTEGER
);
CREATE TABLE configuration_bridge (
 id INTEGER PRIMARY KEY CHECK(id=1), root TEXT NOT NULL, epoch TEXT NOT NULL,
 seen_hash TEXT
);
CREATE TABLE configuration_files (
 path TEXT PRIMARY KEY, version INTEGER NOT NULL, hash TEXT NOT NULL,
 prefix TEXT NOT NULL, suffix TEXT NOT NULL, body TEXT NOT NULL, state TEXT NOT NULL,
 previous_hash TEXT
);
