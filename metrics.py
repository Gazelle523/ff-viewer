"""Ready-made analytics. Each function returns a pandas DataFrame."""
import pandas as pd

from insights import _where


def q(con, sql: str, params=()) -> pd.DataFrame:
    return pd.read_sql_query(sql, con, params=params)


def _filters(season=None, league_id=None, extra=None):
    """WHERE clause for league/season filters. season may be None, one season, or several."""
    return _where(league_id, season, extra=list(extra or []))


def luck(con, season=None, league_id=None) -> pd.DataFrame:
    """Actual wins minus all-play expected wins, per team-season. Positive = lucky."""
    where, params = _filters(season, league_id, ["is_regular_season = 1"])
    return q(con, f"""
        SELECT league_name, season, owner, team_name,
               COUNT(*) AS games, SUM(win) AS wins,
               ROUND(SUM(expected_win), 2) AS expected_wins,
               ROUND(SUM(win) - SUM(expected_win), 2) AS luck
        FROM weekly_results {where}
        GROUP BY league_name, season, owner, team_name
        ORDER BY luck DESC""", params)


def luck_by_owner(con, league_id=None, season=None) -> pd.DataFrame:
    """Luck per owner across the chosen leagues and seasons."""
    where, params = _filters(season, league_id, ["is_regular_season = 1"])
    return q(con, f"""
        SELECT owner, COUNT(*) AS games, SUM(win) AS wins,
               ROUND(SUM(expected_win), 2) AS expected_wins,
               ROUND(SUM(win) - SUM(expected_win), 2) AS luck
        FROM weekly_results {where}
        GROUP BY owner ORDER BY luck DESC""", params)


def schedule_strength(con, season=None, league_id=None) -> pd.DataFrame:
    """Hardest schedules first. opp_avg_ppg = average of your opponents' season scoring averages."""
    where, params = _filters(season, league_id)
    return q(con, f"""
        SELECT league_name, season, owner, team_name, games,
               ROUND(opp_avg_ppg, 2) AS opp_avg_ppg,
               ROUND(opp_avg_win_pct, 3) AS opp_avg_win_pct,
               ROUND(avg_points_against, 2) AS avg_points_against
        FROM schedule_strength {where}
        ORDER BY opp_avg_ppg DESC""", params)


def position_average(con, owner, position, season=None, league_id=None, starters_only=True) -> pd.DataFrame:
    """Average points per player-week for an owner's players at a position, by season."""
    extra = ["owner = ?", "position = ?"] + (["is_starter = 1"] if starters_only else [])
    where, params = _filters(season, league_id, extra)
    return q(con, f"""
        SELECT season, owner, position, COUNT(*) AS player_weeks,
               ROUND(AVG(points), 2) AS avg_points, ROUND(SUM(points), 1) AS total_points
        FROM player_games{where}
        GROUP BY season, owner, position ORDER BY season""", [owner, position.upper()] + params)


def bench_points(con, season=None, league_id=None) -> pd.DataFrame:
    """Points sitting on the bench per team-season (excludes IR)."""
    where, params = _filters(season, league_id, ["slot = 'BE'"])
    return q(con, f"""
        SELECT league_name, season, owner, team_name, ROUND(SUM(points), 1) AS bench_points
        FROM player_games {where}
        GROUP BY league_name, season, owner, team_name
        ORDER BY bench_points DESC""", params)
