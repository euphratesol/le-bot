-- Per-user daily Wordle scores, parsed from the Wordle app's daily
-- recap message. Keyed on (guild, day, user) so re-importing the same
-- recap (live message or backfill) is a no-op.
CREATE TABLE wordle_scores (
    guild_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    day      TEXT NOT NULL,  -- ISO date the puzzle was played (recap date - 1)
    guesses  INTEGER,        -- 1-6; NULL means failed (X/6)
    crowned  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, day, user_id)
);

-- Where the Wordle app posts recaps in each guild, plus the newest recap
-- message processed. Startup catch-up scans history after this point to
-- import recaps posted while the bot was offline.
CREATE TABLE wordle_channels (
    guild_id        INTEGER PRIMARY KEY,
    channel_id      INTEGER NOT NULL,
    last_message_id INTEGER NOT NULL
);
