-- Per-user counters, one row per (guild, user, counter name).
CREATE TABLE counters (
    guild_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    name     TEXT    NOT NULL,
    value    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id, name)
);

-- Append-only log of interactions; derive new stats from this later.
CREATE TABLE events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    event_type TEXT    NOT NULL,
    detail     TEXT,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_events_guild_user ON events (guild_id, user_id);
