"""A catalog of ready-made questions. Each one turns dropdown choices into SQL. No AI involved.

To add your own: append a dict to QUESTIONS with an id, title, help text, a list of params,
and a build(p) function returning (sql, query_params). p always contains league_id and season
from the sidebar filters (None = all), plus whatever params you declare.

Param kinds: owner, position, number, bool, choice.
"""
from insights import _where


def _flt(p, extra=()):
    """WHERE clause using the sidebar League and Season filters (season may be one or several)."""
    return _where(p.get("league_id"), p.get("season"), extra=list(extra))


LIMIT = {"key": "limit", "kind": "number", "label": "How many rows", "default": 10, "min": 1, "max": 200}
OWNER = {"key": "owner", "kind": "owner", "label": "Owner"}
STAGE = "CASE WHEN is_regular_season = 1 THEN 'Regular season' ELSE 'Playoffs' END AS stage"
POSITION = {"key": "position", "kind": "position", "label": "Position"}


def luck(p):
    where, params = _flt(p, ["is_regular_season = 1"])
    direction = "DESC" if p["order"] == "Luckiest first" else "ASC"
    return (f"""SELECT owner, COUNT(*) AS games, SUM(win) AS wins,
       ROUND(SUM(expected_win), 2) AS expected_wins,
       ROUND(SUM(win) - SUM(expected_win), 2) AS luck
FROM weekly_results{where}
GROUP BY owner ORDER BY luck {direction} LIMIT ?""", params + [p["limit"]])


def schedule(p):
    where, params = _flt(p)
    direction = "DESC" if p["order"] == "Hardest first" else "ASC"
    return (f"""SELECT league_name, season, owner, team_name,
       ROUND(opp_avg_ppg, 2) AS opp_avg_ppg, ROUND(opp_avg_win_pct, 3) AS opp_avg_win_pct,
       ROUND(avg_points_against, 2) AS avg_points_against
FROM schedule_strength{where}
ORDER BY opp_avg_ppg {direction} LIMIT ?""", params + [p["limit"]])


def position_avg(p):
    extra = ["owner = ?", "position = ?"] + (["is_starter = 1"] if p["starters_only"] else [])
    where, params = _flt(p, extra)
    return (f"""SELECT season, owner, position, COUNT(*) AS player_weeks,
       ROUND(AVG(points), 2) AS avg_points, ROUND(SUM(points), 1) AS total_points
FROM player_games{where}
GROUP BY season, owner, position ORDER BY season""", [p["owner"], p["position"]] + params)


def top_player_weeks(p):
    extra = ["is_starter = 1"] if p["starters_only"] else []
    params = []
    if p["position"] != "Any":
        extra.append("position = ?")
        params.append(p["position"])
    where, fparams = _flt(p, extra)
    order = "DESC" if p["order"] == "Best" else "ASC"
    return (f"""SELECT league_id, team_id, season, week, league_name, owner, player_name, position, points
FROM player_games{where}
ORDER BY points {order} LIMIT ?""", params + fparams + [p["limit"]])


def team_scores(p):
    where, params = _flt(p)
    order = "DESC" if p["order"] == "Highest" else "ASC"
    return (f"""SELECT league_id, team_id, league_name, season, week, {STAGE}, owner, team_name, points_for,
       opp_owner AS opponent, opp_team_name AS opponent_team, points_against, win
FROM games{where}
ORDER BY points_for {order} LIMIT ?""", params + [p["limit"]])


def unlucky_games(p):
    unlucky = p["order"] == "Unluckiest losses (highest score, still lost)"
    where, params = _flt(p, ["win = 0" if unlucky else "win = 1"])
    order = "DESC" if unlucky else "ASC"
    return (f"""SELECT league_id, team_id, league_name, season, week, {STAGE}, owner, opp_owner AS opponent,
       points_for, points_against
FROM games{where}
ORDER BY points_for {order} LIMIT ?""", params + [p["limit"]])


def margins(p):
    where, params = _flt(p, ["win = 1"])
    order = "DESC" if p["order"] == "Biggest blowouts" else "ASC"
    return (f"""SELECT league_id, team_id, league_name, season, week, {STAGE}, owner AS winner, opp_owner AS loser,
       points_for, points_against, ROUND(points_for - points_against, 1) AS margin
FROM games{where}
ORDER BY margin {order} LIMIT ?""", params + [p["limit"]])


def head_to_head(p):
    where, params = _flt(p, ["owner = ?", "opp_owner = ?"])
    return (f"""SELECT owner, opp_owner, COUNT(*) AS games, SUM(win) AS wins,
       COUNT(*) - SUM(win) AS losses,
       ROUND(AVG(points_for), 1) AS avg_points_for, ROUND(AVG(points_against), 1) AS avg_points_against
FROM games{where}
GROUP BY owner, opp_owner""", [p["owner"], p["owner2"]] + params)


def all_time(p):
    where, params = _flt(p, ["is_regular_season = 1"])
    return (f"""SELECT owner, COUNT(DISTINCT season || '-' || league_id) AS seasons, COUNT(*) AS games,
       SUM(win) AS wins, ROUND(AVG(win), 3) AS win_pct, ROUND(AVG(points_for), 2) AS ppg
FROM games{where}
GROUP BY owner ORDER BY win_pct DESC LIMIT ?""", params + [p["limit"]])


def owner_best_players(p):
    where, params = _flt(p, ["owner = ?", "is_starter = 1"])
    return (f"""SELECT player_name, position, COUNT(*) AS weeks_started,
       ROUND(SUM(points), 1) AS total_points, ROUND(AVG(points), 2) AS avg_points
FROM player_games{where}
GROUP BY player_id, player_name, position ORDER BY total_points DESC LIMIT ?""", [p["owner"]] + params + [p["limit"]])


def bench(p):
    where, params = _flt(p, ["slot = 'BE'"])
    return (f"""SELECT league_name, season, owner, team_name, ROUND(SUM(points), 1) AS bench_points
FROM player_games{where}
GROUP BY league_name, season, owner, team_name
ORDER BY bench_points DESC LIMIT ?""", params + [p["limit"]])


QUESTIONS = [
    {"id": "luck", "title": "Who is the luckiest / unluckiest owner?",
     "help": "Actual wins minus all-play expected wins. Regular season. Uses the sidebar filters.",
     "params": [{"key": "order", "kind": "choice", "label": "Sort", "options": ["Luckiest first", "Unluckiest first"]}, LIMIT],
     "build": luck},
    {"id": "sos", "title": "Who has the hardest / easiest schedule?",
     "help": "Average of opponents' season points per game. Uses the sidebar filters.",
     "params": [{"key": "order", "kind": "choice", "label": "Sort", "options": ["Hardest first", "Easiest first"]}, LIMIT],
     "build": schedule},
    {"id": "posavg", "title": "Average points for an owner's players at a position",
     "help": "E.g. an owner's tight ends over the last 3 seasons (pick \"Last 3 seasons\" in the Season filter). Uses the sidebar filters.",
     "params": [OWNER, POSITION, {"key": "starters_only", "kind": "bool", "label": "Starters only", "default": True}],
     "build": position_avg},
    {"id": "topweeks", "title": "Best / worst individual player performances",
     "help": "Single player-games, including playoff weeks. Uses the sidebar filters.",
     "params": [{"key": "order", "kind": "choice", "label": "Show", "options": ["Best", "Worst"]},
                {"key": "position", "kind": "choice", "label": "Position", "options": ["Any", "QB", "RB", "WR", "TE", "K", "D/ST"]},
                {"key": "starters_only", "kind": "bool", "label": "Starters only", "default": True}, LIMIT],
     "build": top_player_weeks},
    {"id": "teamscores", "title": "Highest / lowest team scores",
     "help": "Single-team scores, with the opponent and the week. Includes playoff games (see the stage column). Uses the sidebar filters.",
     "params": [{"key": "order", "kind": "choice", "label": "Show", "options": ["Highest", "Lowest"]}, LIMIT],
     "build": team_scores},
    {"id": "unlucky", "title": "Unluckiest losses / luckiest wins",
     "help": "Big scores that still lost, or tiny scores that still won. Uses the sidebar filters.",
     "params": [{"key": "order", "kind": "choice", "label": "Show",
                 "options": ["Unluckiest losses (highest score, still lost)", "Luckiest wins (lowest score, still won)"]}, LIMIT],
     "build": unlucky_games},
    {"id": "margins", "title": "Biggest blowouts / closest games",
     "help": "By winning margin. Uses the sidebar filters.",
     "params": [{"key": "order", "kind": "choice", "label": "Show", "options": ["Biggest blowouts", "Closest games"]}, LIMIT],
     "build": margins},
    {"id": "h2h", "title": "Head-to-head record between two owners",
     "help": "All games where the first owner faced the second. Uses the sidebar filters.",
     "params": [OWNER, {"key": "owner2", "kind": "owner", "label": "Opponent"}],
     "build": head_to_head},
    {"id": "alltime", "title": "All-time owner records",
     "help": "Regular season win % and points per game. Uses the sidebar filters.",
     "params": [LIMIT],
     "build": all_time},
    {"id": "bestplayers", "title": "An owner's highest-scoring players",
     "help": "Total points while in the starting lineup. Uses the sidebar filters.",
     "params": [OWNER, LIMIT],
     "build": owner_best_players},
    {"id": "bench", "title": "Most points left on the bench",
     "help": "Excludes IR. Uses the sidebar filters.",
     "params": [LIMIT],
     "build": bench},
]
