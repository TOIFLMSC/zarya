ALTER TABLE telegram_access
ADD COLUMN message_batches_enabled INTEGER NOT NULL DEFAULT 1
CHECK (message_batches_enabled IN (0,1));
