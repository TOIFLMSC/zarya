ALTER TABLE memory_facts ADD COLUMN observation_kind TEXT NOT NULL DEFAULT 'none';
ALTER TABLE memory_evidence ADD COLUMN provenance TEXT NOT NULL DEFAULT 'legacy';
ALTER TABLE memory_evidence ADD COLUMN batch_id INTEGER;
ALTER TABLE memory_evidence ADD COLUMN context_dependencies TEXT;
ALTER TABLE memory_evidence ADD COLUMN supporting INTEGER NOT NULL DEFAULT 0;
