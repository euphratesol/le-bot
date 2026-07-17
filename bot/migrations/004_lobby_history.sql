-- Per-lobby membership history, shown as a card on the lobby message.
-- Rows die with the lobby (close / game removal) via the cascade.
CREATE TABLE lobby_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    lobby_id   INTEGER NOT NULL REFERENCES lobbies(id) ON DELETE CASCADE,
    actor_id   INTEGER NOT NULL,
    target_id  INTEGER,
    action     TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_lobby_history_lobby ON lobby_history(lobby_id, id);
