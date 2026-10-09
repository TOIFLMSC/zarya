CREATE INDEX model_calls_created ON model_calls(created_at);
CREATE TABLE pilot_reviews (case_id TEXT PRIMARY KEY,status TEXT NOT NULL,note TEXT NOT NULL,version INTEGER NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE pilot_preferences (id INTEGER PRIMARY KEY CHECK(id=1),version INTEGER NOT NULL,warning_usd REAL);
INSERT INTO pilot_preferences VALUES (1,1,NULL);
