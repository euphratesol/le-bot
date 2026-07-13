CREATE TABLE lobby_games (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    name       TEXT    NOT NULL COLLATE NOCASE,
    party_size INTEGER NOT NULL,
    emoji      TEXT,
    UNIQUE (guild_id, name)
);

CREATE TABLE game_ping_list (
    game_id INTEGER NOT NULL REFERENCES lobby_games(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL,
    PRIMARY KEY (game_id, user_id)
);

-- At most one active lobby per game (unique game_id).
CREATE TABLE lobbies (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id      INTEGER NOT NULL UNIQUE REFERENCES lobby_games(id) ON DELETE CASCADE,
    guild_id     INTEGER NOT NULL,
    channel_id   INTEGER NOT NULL,
    message_id   INTEGER NOT NULL UNIQUE,
    created_by   INTEGER NOT NULL,
    announced    INTEGER NOT NULL DEFAULT 0,
    last_ping_at TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE lobby_members (
    lobby_id  INTEGER NOT NULL REFERENCES lobbies(id) ON DELETE CASCADE,
    user_id   INTEGER NOT NULL,
    added_by  INTEGER NOT NULL,
    joined_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (lobby_id, user_id)
);
