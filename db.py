"""SQLite storage: tables for raw ESPN data plus views that make analysis (and LLM-written SQL) easy."""
import os
import sqlite3
from pathlib import Path

DEFAULT_DB = Path(__file__).parent / "data" / "fantasy.db"


def db_path() -> Path:
    # Set FF_DB_PATH to point the whole app at a different database (e.g. the demo one).
    return Path(os.getenv("FF_DB_PATH", DEFAULT_DB))


TABLES = """
CREATE TABLE IF NOT EXISTS leagues (
    league_id INTEGER, season INTEGER, name TEXT, reg_season_weeks INTEGER,
    PRIMARY KEY (league_id, season)
);
CREATE TABLE IF NOT EXISTS teams (
    league_id INTEGER, season INTEGER, team_id INTEGER, team_name TEXT, owner TEXT,
    PRIMARY KEY (league_id, season, team_id)
);
CREATE TABLE IF NOT EXISTS matchups (
    league_id INTEGER, season INTEGER, week INTEGER, is_regular_season INTEGER,
    home_team_id INTEGER, away_team_id INTEGER, home_score REAL, away_score REAL,
    PRIMARY KEY (league_id, season, week, home_team_id, away_team_id)
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY, value TEXT
);
CREATE TABLE IF NOT EXISTS lineup_entries (
    league_id INTEGER, season INTEGER, week INTEGER, team_id INTEGER,
    player_id INTEGER, player_name TEXT, position TEXT, nfl_team TEXT,
    slot TEXT, is_starter INTEGER, points REAL,
    PRIMARY KEY (league_id, season, week, team_id, player_id)
);
"""

# Views are dropped and recreated every time so definition changes always take effect.
VIEWS = """
DROP VIEW IF EXISTS games;
CREATE VIEW games AS
WITH both_sides AS (
    SELECT league_id, season, week, is_regular_season,
           home_team_id AS team_id, away_team_id AS opp_team_id,
           home_score AS points_for, away_score AS points_against
    FROM matchups
    UNION ALL
    SELECT league_id, season, week, is_regular_season,
           away_team_id, home_team_id, away_score, home_score
    FROM matchups
)
SELECT b.league_id, l.name AS league_name, b.season, b.week, b.is_regular_season,
       b.team_id, t.team_name, t.owner,
       b.opp_team_id, o.team_name AS opp_team_name, o.owner AS opp_owner,
       b.points_for, b.points_against,
       CASE WHEN b.points_for > b.points_against THEN 1.0
            WHEN b.points_for = b.points_against THEN 0.5 ELSE 0.0 END AS win
FROM both_sides b
JOIN leagues l ON l.league_id = b.league_id AND l.season = b.season
JOIN teams t   ON t.league_id = b.league_id AND t.season = b.season AND t.team_id = b.team_id
JOIN teams o   ON o.league_id = b.league_id AND o.season = b.season AND o.team_id = b.opp_team_id;

DROP VIEW IF EXISTS weekly_results;
CREATE VIEW weekly_results AS
-- One row per team per week, with "all-play" numbers: how many teams you would have
-- beaten that week. expected_win is your all-play win % (0..1). Luck = win - expected_win.
SELECT g.*,
       (RANK() OVER w - 1)
         + 0.5 * (COUNT(*) OVER (PARTITION BY league_id, season, week, points_for) - 1) AS all_play_wins,
       ((RANK() OVER w - 1)
         + 0.5 * (COUNT(*) OVER (PARTITION BY league_id, season, week, points_for) - 1))
         / (COUNT(*) OVER (PARTITION BY league_id, season, week) - 1.0) AS expected_win
FROM games g
WINDOW w AS (PARTITION BY league_id, season, week ORDER BY points_for);

DROP VIEW IF EXISTS team_season_stats;
CREATE VIEW team_season_stats AS
-- Regular season only.
SELECT league_id, league_name, season, team_id, team_name, owner,
       COUNT(*) AS games, SUM(win) AS wins, AVG(win) AS win_pct,
       AVG(points_for) AS ppg, AVG(points_against) AS pa_pg
FROM games WHERE is_regular_season = 1
GROUP BY league_id, league_name, season, team_id, team_name, owner;

DROP VIEW IF EXISTS schedule_strength;
CREATE VIEW schedule_strength AS
-- Regular season only. Higher opp_avg_ppg = harder schedule (opponents who score more).
SELECT g.league_id, g.league_name, g.season, g.team_id, g.team_name, g.owner,
       COUNT(*) AS games,
       AVG(s.ppg) AS opp_avg_ppg,
       AVG(s.win_pct) AS opp_avg_win_pct,
       AVG(g.points_against) AS avg_points_against
FROM games g
JOIN team_season_stats s
  ON s.league_id = g.league_id AND s.season = g.season AND s.team_id = g.opp_team_id
WHERE g.is_regular_season = 1
GROUP BY g.league_id, g.league_name, g.season, g.team_id, g.team_name, g.owner;

DROP VIEW IF EXISTS player_games;
CREATE VIEW player_games AS
-- One row per player per week on a roster. is_starter = 1 if in the starting lineup.
SELECT le.league_id, l.name AS league_name, le.season, le.week, le.team_id, t.team_name, t.owner,
       le.player_id, le.player_name, le.position, le.nfl_team, le.slot, le.is_starter, le.points
FROM lineup_entries le
JOIN leagues l ON l.league_id = le.league_id AND l.season = le.season
JOIN teams t   ON t.league_id = le.league_id AND t.season = le.season AND t.team_id = le.team_id;
"""


def connect(read_only: bool = False) -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if read_only:
        con = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, check_same_thread=False)
    else:
        con = sqlite3.connect(path, check_same_thread=False)
    return con


def init_db(con: sqlite3.Connection) -> None:
    con.executescript(TABLES)
    con.executescript(VIEWS)
    con.commit()


def schema_text(con: sqlite3.Connection) -> str:
    """Human/LLM readable schema (tables + views) pulled straight from SQLite."""
    rows = con.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE type IN ('table','view') "
        "AND name NOT LIKE 'sqlite_%' ORDER BY type DESC, name"
    ).fetchall()
    return "\n\n".join(f"-- {t}: {n}\n{sql};" for t, n, sql in rows)
