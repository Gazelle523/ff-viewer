"""Deeper analytics built on the SQL views.

Every function takes an open sqlite connection and returns pandas DataFrames (or a dict of them).
league_id / season = None means "all". Regular season only unless stated otherwise.
"""
import os

import numpy as np
import pandas as pd

PYTHAG_EXPONENT = 2.37  # commonly used for football; tweak if you like
TEAM_WEEK = ["league_id", "season", "week", "team_id"]


# ---------------------------------------------------------------- ESPN box score links
# Best-known format of ESPN's matchup page. If it ever opens the wrong page, set ESPN_BOXSCORE_URL in .env to a
# working link using the placeholders {league_id} {season} {week} {team_id} (see the README).
DEFAULT_BOX_SCORE_URL = ("https://fantasy.espn.com/football/boxscore?leagueId={league_id}&matchupPeriodId={week}"
                         "&scoringPeriodId={week}&seasonId={season}&teamId={team_id}&view=scoringperiod")
LINK_KEYS = ["league_id", "season", "week", "team_id"]


def box_score_url(league_id, season, week, team_id):
    """Link to ESPN's page for one team's matchup (either team of a game opens the same game)."""
    try:
        template = os.getenv("ESPN_BOXSCORE_URL") or DEFAULT_BOX_SCORE_URL
        return template.format(league_id=int(league_id), season=int(season), week=int(week), team_id=int(team_id))
    except (TypeError, ValueError):
        return None


def add_box_score_links(df):
    """If a table has league_id, season, week and team_id columns, replace the two id columns with a box_score link."""
    if df is None or df.empty or not all(c in df.columns for c in LINK_KEYS):
        return df
    out = df.copy()
    links = [box_score_url(*row) for row in zip(out["league_id"], out["season"], out["week"], out["team_id"])]
    out = out.drop(columns=["league_id", "team_id"])
    out.insert(0, "box_score", links)  # first column, so it's visible without scrolling sideways
    return out


# ---------------------------------------------------------------- loading helpers
def _q(con, sql, params=()):
    return pd.read_sql_query(sql, con, params=params)


def _where(league_id=None, season=None, week=None, extra=()):
    clauses, params = list(extra), []
    if league_id:
        clauses.append("league_id = ?")
        params.append(int(league_id))
    if season:  # one season, or several (a tuple/list), e.g. "last 3 seasons"
        picked = [season] if isinstance(season, (int, np.integer)) else list(season)
        clauses.append("season IN (%s)" % ", ".join("?" * len(picked)))
        params.extend(int(s) for s in picked)
    if week:
        clauses.append("week = ?")
        params.append(int(week))
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def games(con, league_id=None, season=None, regular_only=True):
    """One row per team per game, including all-play numbers (expected_win)."""
    where, params = _where(league_id, season, extra=["is_regular_season = 1"] if regular_only else [])
    return _q(con, f"SELECT * FROM weekly_results{where}", params)


def _player_rows(con, league_id=None, season=None, week=None, starters_only=False):
    where, params = _where(league_id, season, week, ["is_starter = 1"] if starters_only else [])
    df = _q(con, "SELECT league_id, league_name, season, week, team_id, team_name, owner, player_id, "
                 f"player_name, position, slot, is_starter, points FROM player_games{where}", params)
    df["points"] = df["points"].fillna(0.0)
    return df


# ---------------------------------------------------------------- lineup math
def _eligible(slot: str) -> frozenset:
    """Which player positions may fill a lineup slot."""
    if slot == "D/ST":
        return frozenset({"D/ST"})
    if slot == "OP":
        return frozenset({"QB", "RB", "WR", "TE"})
    if slot == "FLEX":
        return frozenset({"RB", "WR", "TE"})
    if "/" in slot:
        return frozenset(slot.split("/"))
    return frozenset({slot})


def _best_lineup(slots, pool) -> float:
    """Best possible total for the given starting slots. pool = [(position, points), ...].
    Fill the most restrictive slots first; with nested eligibility this greedy approach is optimal."""
    remaining = sorted(pool, key=lambda p: -p[1])
    total = 0.0
    for slot in sorted(slots, key=lambda s: len(_eligible(s))):
        elig = _eligible(slot)
        for i, (pos, pts) in enumerate(remaining):
            if pos in elig:
                total += pts
                remaining.pop(i)
                break
    return total


EFF_COLS = ["league_id", "league_name", "season", "week", "team_id", "team_name", "owner",
            "actual", "optimal", "points_lost", "efficiency"]


def lineup_efficiency(con, league_id=None, season=None, week=None) -> pd.DataFrame:
    """Per team-week: points actually started vs the best lineup that roster could have fielded."""
    pg = _player_rows(con, league_id, season, week)
    rows = []
    for _, g in pg.groupby(TEAM_WEEK, sort=False):
        started = g["is_starter"].to_numpy() == 1
        if not started.any():
            continue
        usable = g[g["slot"] != "IR"]  # IR players can't be started
        actual = float(g.loc[started, "points"].sum())
        optimal = max(actual, _best_lineup(g.loc[started, "slot"].tolist(),
                                           list(zip(usable["position"], usable["points"]))))
        first = g.iloc[0]
        rows.append({
            "league_id": first.league_id, "league_name": first.league_name, "season": first.season,
            "week": first.week, "team_id": first.team_id, "team_name": first.team_name, "owner": first.owner,
            "actual": round(actual, 1), "optimal": round(optimal, 1),
            "points_lost": round(optimal - actual, 1),
            "efficiency": round(actual / optimal * 100, 1) if optimal else 100.0,
        })
    return pd.DataFrame(rows, columns=EFF_COLS)


def summarize_efficiency(e: pd.DataFrame, by_season=False) -> pd.DataFrame:
    """Aggregate lineup_efficiency output per owner (optionally per season)."""
    if e.empty:
        return e
    keys = ["owner"] + (["season"] if by_season else [])
    s = e.groupby(keys).agg(weeks=("week", "count"), actual=("actual", "sum"),
                            optimal=("optimal", "sum"), points_lost=("points_lost", "sum")).reset_index()
    s["efficiency_pct"] = (s["actual"] / s["optimal"] * 100).round(1)
    for c in ("actual", "optimal", "points_lost"):
        s[c] = s[c].round(1)
    return s.sort_values("efficiency_pct", ascending=False).reset_index(drop=True)


REGRET_COLS = ["league_id", "team_id", "league_name", "season", "week", "owner", "benched", "benched_pos", "benched_points",
               "started", "started_slot", "started_points", "points_lost"]


def start_sit_regrets(con, league_id=None, season=None) -> pd.DataFrame:
    """For each starter, the best benched player who could have taken that slot and outscored him."""
    pg = _player_rows(con, league_id, season)
    rows = []
    for _, g in pg.groupby(TEAM_WEEK, sort=False):
        bench = g[(g["is_starter"] == 0) & (g["slot"] != "IR")]
        if bench.empty:
            continue
        for s in g[g["is_starter"] == 1].itertuples():
            cand = bench[bench["position"].isin(_eligible(s.slot)) & (bench["points"] > s.points)]
            if cand.empty:
                continue
            b = cand.loc[cand["points"].idxmax()]
            rows.append({
                "league_id": s.league_id, "team_id": s.team_id,
                "league_name": s.league_name, "season": s.season, "week": s.week, "owner": s.owner,
                "benched": b["player_name"], "benched_pos": b["position"], "benched_points": b["points"],
                "started": s.player_name, "started_slot": s.slot, "started_points": s.points,
                "points_lost": round(b["points"] - s.points, 1),
            })
    out = pd.DataFrame(rows, columns=REGRET_COLS)
    return out.sort_values("points_lost", ascending=False).reset_index(drop=True)


# ---------------------------------------------------------------- head to head
def h2h_long(con, league_id=None, season=None, regular_only=True) -> pd.DataFrame:
    g = games(con, league_id, season, regular_only)
    t = g.groupby(["owner", "opp_owner"]).agg(games=("win", "count"), wins=("win", "sum"),
                                              pf=("points_for", "sum"), pa=("points_against", "sum")).reset_index()
    t["losses"] = t["games"] - t["wins"]
    t["win_pct"] = t["wins"] / t["games"]
    t["avg_for"] = (t["pf"] / t["games"]).round(1)
    t["avg_against"] = (t["pa"] / t["games"]).round(1)
    return t


def h2h_matrix(h: pd.DataFrame) -> pd.DataFrame:
    """Rows = owner, columns = opponent, values = the row owner's win % against that opponent."""
    return h.pivot(index="owner", columns="opp_owner", values="win_pct")


def nemesis_table(h: pd.DataFrame, min_games=3) -> pd.DataFrame:
    rows = []
    for owner, grp in h[h["games"] >= min_games].groupby("owner"):
        worst = grp.sort_values(["win_pct", "games"], ascending=[True, False]).iloc[0]
        best = grp.sort_values(["win_pct", "games"], ascending=[False, False]).iloc[0]
        rows.append({"owner": owner,
                     "nemesis": worst["opp_owner"], "record_vs_nemesis": f"{worst['wins']:g}-{worst['losses']:g}",
                     "favorite_victim": best["opp_owner"], "record_vs_victim": f"{best['wins']:g}-{best['losses']:g}"})
    return pd.DataFrame(rows, columns=["owner", "nemesis", "record_vs_nemesis", "favorite_victim", "record_vs_victim"])


def rivalries(h: pd.DataFrame, min_games=4) -> pd.DataFrame:
    """Pairs with the most games and the closest series."""
    d = h[(h["owner"] < h["opp_owner"]) & (h["games"] >= min_games)].copy()
    d["lead"] = (d["wins"] - d["losses"]).abs()
    d = d.sort_values(["lead", "games"], ascending=[True, False])
    d = d.rename(columns={"wins": "owner_wins", "losses": "opp_wins"})
    return d[["owner", "opp_owner", "games", "owner_wins", "opp_wins", "lead"]].reset_index(drop=True)


# ---------------------------------------------------------------- consistency, luck variants
def consistency(con, league_id=None, season=None) -> pd.DataFrame:
    g = games(con, league_id, season)
    if g.empty:
        return pd.DataFrame()
    key = ["league_id", "season", "week"]
    g["vs_avg"] = g["points_for"] - g.groupby(key)["points_for"].transform("mean")
    last_week = g.groupby(["league_id", "season"])["week"].transform("max")
    g["half"] = np.where(g["week"] <= last_week / 2, "first", "second")
    out = g.groupby("owner").agg(
        games=("week", "count"), avg_score=("points_for", "mean"), std_dev=("points_for", "std"),
        best=("points_for", "max"), worst=("points_for", "min"),
        boom_pct=("expected_win", lambda s: (s >= 0.75).mean() * 100),
        bust_pct=("expected_win", lambda s: (s <= 0.25).mean() * 100)).reset_index()
    out["consistency_cv"] = (out["std_dev"] / out["avg_score"] * 100).round(1)  # lower = steadier
    halves = g.groupby(["owner", "half"])["vs_avg"].mean().unstack()
    for h in ("first", "second"):
        if h not in halves:
            halves[h] = np.nan
    out["first_half_vs_avg"] = out["owner"].map(halves["first"]).round(1)
    out["second_half_vs_avg"] = out["owner"].map(halves["second"]).round(1)
    out["finishing_kick"] = (out["second_half_vs_avg"] - out["first_half_vs_avg"]).round(1)
    for c in ("avg_score", "std_dev", "boom_pct", "bust_pct"):
        out[c] = out[c].round(1)
    return out.sort_values("consistency_cv").reset_index(drop=True)


def pythagorean(con, league_id=None, season=None, exponent=PYTHAG_EXPONENT) -> pd.DataFrame:
    g = games(con, league_id, season)
    if g.empty:
        return pd.DataFrame()
    t = g.groupby(["league_name", "season", "owner", "team_name"]).agg(
        games=("win", "count"), wins=("win", "sum"), pf=("points_for", "sum"), pa=("points_against", "sum"),
        all_play_wins=("expected_win", "sum")).reset_index()
    pct = t["pf"] ** exponent / (t["pf"] ** exponent + t["pa"] ** exponent)
    t["pythag_wins"] = (pct * t["games"]).round(2)
    t["luck_vs_pythag"] = (t["wins"] - t["pythag_wins"]).round(2)
    t["luck_vs_all_play"] = (t["wins"] - t["all_play_wins"]).round(2)
    t["all_play_wins"] = t["all_play_wins"].round(2)
    return t.drop(columns=["pf", "pa"]).sort_values("luck_vs_pythag", ascending=False).reset_index(drop=True)


def median_record(con, league_id=None, season=None) -> pd.DataFrame:
    """What if every team also played the league median each week?"""
    g = games(con, league_id, season)
    if g.empty:
        return pd.DataFrame()
    med = g.groupby(["league_id", "season", "week"])["points_for"].transform("median")
    g["median_win"] = np.where(g["points_for"] > med, 1.0, np.where(g["points_for"] == med, 0.5, 0.0))
    t = g.groupby(["league_name", "season", "owner", "team_name"]).agg(
        games=("win", "count"), wins=("win", "sum"), median_wins=("median_win", "sum")).reset_index()
    t["combined_wins"] = t["wins"] + t["median_wins"]
    t["combined_win_pct"] = (t["combined_wins"] / (2 * t["games"])).round(3)
    return t.sort_values("combined_win_pct", ascending=False).reset_index(drop=True)


# ---------------------------------------------------------------- records and streaks
def streaks(con, league_id=None, season=None) -> pd.DataFrame:
    """Longest win/loss streaks per owner per league (streaks carry across seasons unless a season is chosen)."""
    g = games(con, league_id, season).sort_values(["league_id", "owner", "season", "week"])
    rows = []
    for (lname, owner), grp in g.groupby(["league_name", "owner"]):
        w = l = best_w = best_l = 0
        for r in grp["win"]:
            if r == 1:
                w, l = w + 1, 0
            elif r == 0:
                w, l = 0, l + 1
            else:
                w = l = 0
            best_w, best_l = max(best_w, w), max(best_l, l)
        rows.append({"league_name": lname, "owner": owner, "longest_win_streak": best_w,
                     "longest_loss_streak": best_l,
                     "current_streak": f"W{w}" if w else (f"L{l}" if l else "-")})
    return pd.DataFrame(rows, columns=["league_name", "owner", "longest_win_streak",
                                       "longest_loss_streak", "current_streak"])


def records_book(con, league_id=None, season=None, limit=10) -> dict:
    """Single-game records across every game: regular season, playoffs and consolation (the stage and week columns
    say which). The weeks-as-top/lowest-scorer counts use the regular season only (playoff weeks have fewer teams)."""
    g = games(con, league_id, season, regular_only=False)
    if g.empty:
        return {}
    key = ["league_id", "season", "week"]
    g = g.rename(columns={"opp_owner": "opponent", "opp_team_name": "opponent_team"})
    g["stage"] = np.where(g["is_regular_season"] == 1, "Regular season", "Playoffs")
    score_cols = ["league_id", "team_id", "league_name", "season", "week", "stage", "owner", "team_name",
                  "points_for", "opponent", "opponent_team", "points_against"]
    reg = g[g["is_regular_season"] == 1]
    top = reg.loc[reg.groupby(key)["points_for"].idxmax()] if not reg.empty else reg
    low = reg.loc[reg.groupby(key)["points_for"].idxmin()] if not reg.empty else reg
    owners = sorted(g["owner"].dropna().unique().tolist(), key=str.casefold)

    def scorer_counts(scored, count_name, never_emoji):
        counts = scored.groupby("owner").size().reindex(owners, fill_value=0)
        out = counts.rename(count_name).rename_axis("owner").reset_index()
        never = out[count_name] == 0
        out.loc[never, "owner"] = out.loc[never, "owner"] + f" {never_emoji}"
        return out.sort_values([count_name, "owner"], ascending=[False, True], kind="stable").reset_index(drop=True)

    wins = g[g["win"] == 1].copy()
    wins["margin"] = (wins["points_for"] - wins["points_against"]).round(1)
    wins = wins.rename(columns={"owner": "winner", "team_name": "winner_team", "opponent": "loser",
                                "opponent_team": "loser_team", "points_for": "winner_score",
                                "points_against": "loser_score"})
    margin_cols = ["league_id", "team_id", "league_name", "season", "week", "stage", "winner", "winner_team",
                   "winner_score", "loser", "loser_team", "loser_score", "margin"]
    return {
        "top_scorer_weeks": scorer_counts(top, "weeks_as_top_scorer", "💀"),
        "low_scorer_weeks": scorer_counts(low, "weeks_as_low_scorer", "👑"),
        "highest_scores": g.nlargest(limit, "points_for")[score_cols].reset_index(drop=True),
        "lowest_scores": g.nsmallest(limit, "points_for")[score_cols].reset_index(drop=True),
        "blowouts": wins.nlargest(limit, "margin")[margin_cols].reset_index(drop=True),
        "closest": wins[wins["margin"] > 0].nsmallest(limit, "margin")[margin_cols].reset_index(drop=True),
    }


# ---------------------------------------------------------------- positions and players
def position_strength(con, league_id=None, season=None) -> pd.DataFrame:
    """Owner x position grid: average weekly starter points above/below the league average at that position."""
    pg = _player_rows(con, league_id, season, starters_only=True)
    if pg.empty:
        return pd.DataFrame()
    tw = pg.groupby(TEAM_WEEK + ["owner", "position"])["points"].sum().reset_index()
    tw["vs_avg"] = tw["points"] - tw.groupby(["league_id", "season", "week", "position"])["points"].transform("mean")
    return tw.groupby(["owner", "position"])["vs_avg"].mean().unstack().round(2)


def carried_by(con, league_id=None, season=None) -> pd.DataFrame:
    """For each team-season, the starter who supplied the biggest share of the team's starter points."""
    pg = _player_rows(con, league_id, season, starters_only=True)
    if pg.empty:
        return pd.DataFrame()
    keys = ["league_name", "season", "owner", "team_name"]
    p = pg.groupby(keys + ["player_id", "player_name", "position"])["points"].sum().reset_index()
    p["team_total"] = p.groupby(keys)["points"].transform("sum")
    p["share_pct"] = (p["points"] / p["team_total"] * 100).round(1)
    top = p.loc[p.groupby(keys)["points"].idxmax()].copy()
    top["points"] = top["points"].round(1)
    top["team_total"] = top["team_total"].round(1)
    return top.drop(columns=["player_id"]).sort_values("share_pct", ascending=False).reset_index(drop=True)


def loyal_players(con, league_id=None, season=None, min_seasons=2) -> pd.DataFrame:
    """Players an owner keeps starting across multiple seasons."""
    pg = _player_rows(con, league_id, season, starters_only=True)
    if pg.empty:
        return pd.DataFrame()
    t = pg.groupby(["league_name", "owner", "player_id", "player_name", "position"]).agg(
        weeks_started=("week", "count"), seasons=("season", "nunique"), total_points=("points", "sum"),
        avg_points=("points", "mean")).reset_index()
    t = t[t["seasons"] >= min_seasons].drop(columns=["player_id"])
    t["total_points"] = t["total_points"].round(1)
    t["avg_points"] = t["avg_points"].round(2)
    return t.sort_values("weeks_started", ascending=False).reset_index(drop=True)


def owner_top_players(con, owner, limit=10, league_id=None, season=None) -> pd.DataFrame:
    where, params = _where(league_id, season, extra=["owner = ?", "is_starter = 1"])
    return _q(con, f"""
        SELECT player_name, position, COUNT(*) AS weeks_started,
               ROUND(SUM(points), 1) AS total_points, ROUND(AVG(points), 2) AS avg_points
        FROM player_games{where}
        GROUP BY league_id, player_id, player_name, position
        ORDER BY total_points DESC LIMIT ?""", [owner] + params + [int(limit)])


# ---------------------------------------------------------------- power rankings and time series
def power_rankings(con, league_id, season, through_week=None) -> pd.DataFrame:
    """Power score = 60% season all-play win % + 40% last-3-weeks all-play win %."""
    g = games(con, league_id, season).sort_values("week")
    if through_week:
        g = g[g["week"] <= int(through_week)]
    if g.empty:
        return pd.DataFrame()
    last = int(g["week"].max())

    def snapshot(upto):
        d = g[g["week"] <= upto]
        recent = d[d["week"] > upto - 3]
        t = d.groupby("team_id").agg(team_name=("team_name", "last"), owner=("owner", "last"),
                                     games=("win", "count"), wins=("win", "sum"),
                                     ppg=("points_for", "mean"), all_play=("expected_win", "mean"))
        t["last3_all_play"] = recent.groupby("team_id")["expected_win"].mean().reindex(t.index).fillna(t["all_play"])
        t["last3_ppg"] = recent.groupby("team_id")["points_for"].mean().reindex(t.index)
        t["power"] = 0.6 * t["all_play"] + 0.4 * t["last3_all_play"]
        t["rank"] = t["power"].rank(ascending=False, method="min").astype(int)
        return t

    cur = snapshot(last)
    cur["move"] = (snapshot(last - 1)["rank"].reindex(cur.index) - cur["rank"]).fillna(0).astype(int) if last > 1 else 0
    cur["record"] = cur.apply(lambda r: f"{r['wins']:g}-{r['games'] - r['wins']:g}", axis=1)
    out = pd.DataFrame({
        "rank": cur["rank"], "move": cur["move"], "owner": cur["owner"], "team": cur["team_name"],
        "record": cur["record"], "all_play_pct": (cur["all_play"] * 100).round(1),
        "last3_all_play_pct": (cur["last3_all_play"] * 100).round(1),
        "ppg": cur["ppg"].round(1), "last3_ppg": cur["last3_ppg"].round(1),
        "power_score": (cur["power"] * 100).round(1)})
    return out.sort_values("rank").reset_index(drop=True)


def luck_over_time(con, league_id, season) -> pd.DataFrame:
    """Cumulative (wins - expected wins) by week, one column per owner."""
    g = games(con, league_id, season).sort_values("week")
    if g.empty:
        return pd.DataFrame()
    g["luck"] = g["win"] - g["expected_win"]
    g["cum_luck"] = g.groupby("team_id")["luck"].cumsum()
    return g.pivot_table(index="week", columns="owner", values="cum_luck", aggfunc="sum").round(2)


def weekly_scores(con, league_id, season) -> pd.DataFrame:
    g = games(con, league_id, season)
    if g.empty:
        return pd.DataFrame()
    return g.pivot_table(index="week", columns="owner", values="points_for", aggfunc="sum")


# ---------------------------------------------------------------- owner profile
def career_summary(con, owner, league_id=None, season=None):
    where, params = _where(league_id, season, extra=["is_regular_season = 1", "owner = ?"])
    g = _q(con, f"SELECT * FROM weekly_results{where}", [owner] + params)
    if g.empty:
        return None
    g["luck"] = g["win"] - g["expected_win"]
    by = g.groupby(["league_name", "season", "team_name"]).agg(
        games=("win", "count"), wins=("win", "sum"), ppg=("points_for", "mean"),
        best_week=("points_for", "max"), worst_week=("points_for", "min"), luck=("luck", "sum")).reset_index()
    by["win_pct"] = (by["wins"] / by["games"]).round(3)
    by[["ppg", "luck"]] = by[["ppg", "luck"]].round(2)
    by = by.sort_values(["season", "league_name"], ascending=[False, True]).reset_index(drop=True)
    wins, games_n = float(g["win"].sum()), int(len(g))
    overall = {"seasons": len(by), "games": games_n, "record": f"{wins:g}-{games_n - wins:g}",
               "win_pct": wins / games_n, "ppg": float(g["points_for"].mean()), "luck": float(g["luck"].sum()),
               "best_week": float(g["points_for"].max()), "worst_week": float(g["points_for"].min())}
    return {"overall": overall, "by_season": by}


# ---------------------------------------------------------------- home page helpers
def standings(con, league_id, season) -> pd.DataFrame:
    """Regular-season standings: wins, then points for."""
    g = games(con, league_id, season)
    if g.empty:
        return pd.DataFrame()
    g["luck"] = g["win"] - g["expected_win"]
    t = g.groupby("team_id").agg(owner=("owner", "last"), team=("team_name", "last"), games=("win", "count"),
                                 wins=("win", "sum"), pf=("points_for", "sum"), pa=("points_against", "sum"),
                                 luck=("luck", "sum")).reset_index(drop=True)
    t = t.sort_values(["wins", "pf"], ascending=False).reset_index(drop=True)
    t["record"] = t.apply(lambda r: f"{r['wins']:g}-{r['games'] - r['wins']:g}", axis=1)
    t["ppg"] = (t["pf"] / t["games"]).round(1)
    t["pf"], t["pa"], t["luck"] = t["pf"].round(1), t["pa"].round(1), t["luck"].round(2)
    t.insert(0, "rank", range(1, len(t) + 1))
    return t[["rank", "owner", "team", "record", "pf", "pa", "ppg", "luck"]]


def latest_results(con, league_id, season):
    """(week, is_regular_season, DataFrame) for the most recent week stored for this league-season."""
    row = con.execute("SELECT MAX(week) FROM matchups WHERE league_id = ? AND season = ?",
                      (int(league_id), int(season))).fetchone()
    if not row or row[0] is None:
        return None, None, pd.DataFrame()
    week = int(row[0])
    raw = _q(con, """
        SELECT m.is_regular_season AS reg, m.league_id, m.season, m.week, m.home_team_id AS team_id,
               th.owner AS home, m.home_score, ta.owner AS away, m.away_score
        FROM matchups m
        JOIN teams th ON th.league_id = m.league_id AND th.season = m.season AND th.team_id = m.home_team_id
        JOIN teams ta ON ta.league_id = m.league_id AND ta.season = m.season AND ta.team_id = m.away_team_id
        WHERE m.league_id = ? AND m.season = ? AND m.week = ?""", (int(league_id), int(season), week))
    if raw.empty:
        return week, None, raw
    home_won = raw["home_score"] >= raw["away_score"]
    out = pd.DataFrame({
        "winner": raw["home"].where(home_won, raw["away"]),
        "winner_score": raw["home_score"].where(home_won, raw["away_score"]),
        "loser": raw["away"].where(home_won, raw["home"]),
        "loser_score": raw["away_score"].where(home_won, raw["home_score"])})
    out["margin"] = (out["winner_score"] - out["loser_score"]).round(1)
    for c in LINK_KEYS:
        out[c] = raw[c]
    return week, bool(raw["reg"].iloc[0]), out.sort_values("margin", ascending=False).reset_index(drop=True)


def possible_duplicate_owners(con) -> pd.DataFrame:
    """Owner names that may be the same person: near-identical spelling, or the same last name with
    first names that look like nicknames (Doug/Douglas, Rick/Ricky)."""
    from difflib import SequenceMatcher
    names = [r[0] for r in con.execute("SELECT DISTINCT owner FROM teams WHERE owner IS NOT NULL ORDER BY owner")]
    rows = []
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            ta, tb = a.casefold().split(), b.casefold().split()
            reason = None
            if SequenceMatcher(None, "".join(ta), "".join(tb)).ratio() >= 0.88:
                reason = "very similar spelling"
            elif (len(ta) >= 2 and len(tb) >= 2 and ta[-1] == tb[-1] and len(ta[-1]) >= 3
                  and (ta[0].startswith(tb[0]) or tb[0].startswith(ta[0]))):
                reason = "same last name, first names look like nicknames"
            if reason:
                rows.append({"owner_a": a, "owner_b": b, "why": reason})
    return pd.DataFrame(rows, columns=["owner_a", "owner_b", "why"])


# ---------------------------------------------------------------- data check
def data_coverage(con) -> pd.DataFrame:
    """What is actually stored for each league-season: weeks, playoff weeks, lineups, top score."""
    m = _q(con, """
        SELECT l.name AS league, m.league_id, m.season, l.reg_season_weeks AS regular_season_length,
               COUNT(DISTINCT m.week) AS weeks_stored, MIN(m.week) AS first_week, MAX(m.week) AS last_week,
               COUNT(DISTINCT CASE WHEN m.is_regular_season = 1 THEN m.week END) AS regular_weeks,
               COUNT(DISTINCT CASE WHEN m.is_regular_season = 0 THEN m.week END) AS playoff_weeks,
               COUNT(*) AS games, ROUND(MAX(MAX(m.home_score, m.away_score)), 1) AS top_score
        FROM matchups m JOIN leagues l ON l.league_id = m.league_id AND l.season = m.season
        GROUP BY l.name, m.league_id, m.season, l.reg_season_weeks""")
    lw = _q(con, "SELECT league_id, season, COUNT(DISTINCT week) AS weeks_with_lineups "
                 "FROM lineup_entries GROUP BY league_id, season")
    out = m.merge(lw, on=["league_id", "season"], how="left")
    out["weeks_with_lineups"] = out["weeks_with_lineups"].fillna(0).astype(int)
    return (out.drop(columns=["league_id"]).sort_values(["league", "season"], ascending=[True, False])
               .reset_index(drop=True))


def find_games(con, owner, opponent=None, league_id=None, season=None, limit=300) -> pd.DataFrame:
    """Every stored game for an owner (optionally against one opponent), regular season and playoffs."""
    extra, params = ["owner = ?"], [owner]
    if opponent:
        extra.append("opp_owner = ?")
        params.append(opponent)
    where, more = _where(league_id, season, extra=extra)
    return _q(con, f"""
        SELECT league_id, team_id, league_name, season, week,
               CASE WHEN is_regular_season = 1 THEN 'Regular season' ELSE 'Playoffs' END AS stage,
               owner, points_for, opp_owner AS opponent, points_against
        FROM games{where} ORDER BY season DESC, week DESC LIMIT ?""", params + more + [int(limit)])


# ---------------------------------------------------------------- in-app box score
SLOT_ORDER = ["QB", "RB", "WR", "TE", "RB/WR/TE", "FLEX", "WR/TE", "RB/WR", "OP", "D/ST", "K",
              "DT", "DE", "LB", "DL", "CB", "S", "DB", "DP"]
SLOT_LABEL = {"RB/WR/TE": "FLEX", "WR/TE": "W/T", "RB/WR": "R/W", "BE": "Bench"}


def _slot_rank(slot):
    if slot == "BE":
        return 90
    if slot == "IR":
        return 99
    return SLOT_ORDER.index(slot) if slot in SLOT_ORDER else 50


def box_score(con, league_id, season, week, team_id):
    """Everything needed to show one game: both teams, scores and full lineups from the stored data.
    Returns None if the game isn't stored."""
    key = (int(league_id), int(season), int(week))
    m = con.execute("SELECT home_team_id, away_team_id, home_score, away_score, is_regular_season FROM matchups "
                    "WHERE league_id = ? AND season = ? AND week = ? AND (home_team_id = ? OR away_team_id = ?)",
                    key + (int(team_id), int(team_id))).fetchone()
    if not m:
        return None
    home, away, home_score, away_score, regular = m
    info = {r[0]: (r[1], r[2]) for r in con.execute(
        "SELECT team_id, team_name, owner FROM teams WHERE league_id = ? AND season = ?", key[:2])}
    league = con.execute("SELECT name FROM leagues WHERE league_id = ? AND season = ?", key[:2]).fetchone()

    sides = []
    for tid, score in ((home, home_score), (away, away_score)):
        lu = _q(con, "SELECT slot, is_starter, player_name AS player, position AS pos, nfl_team AS nfl, points "
                     "FROM lineup_entries WHERE league_id = ? AND season = ? AND week = ? AND team_id = ?",
                key + (int(tid),))
        side = {"team_id": tid, "team": info.get(tid, ("?", "?"))[0], "owner": info.get(tid, ("?", "?"))[1],
                "score": float(score), "lineup": pd.DataFrame(), "starters_total": None, "bench_total": None,
                "best_possible": None, "efficiency": None, "points_missing": False}
        if not lu.empty:
            lu["points"] = lu["points"].fillna(0.0)
            started = lu["is_starter"] == 1
            usable = lu[lu["slot"] != "IR"]
            side["starters_total"] = round(float(lu.loc[started, "points"].sum()), 1)
            side["bench_total"] = round(float(lu.loc[lu["slot"] == "BE", "points"].sum()), 1)
            best = max(side["starters_total"], _best_lineup(lu.loc[started, "slot"].tolist(),
                                                            list(zip(usable["pos"], usable["points"]))))
            side["best_possible"] = round(best, 1)
            side["efficiency"] = round(side["starters_total"] / best * 100, 1) if best else 100.0
            side["points_missing"] = bool(side["starters_total"] == 0 and float(score) > 0)  # ESPN's 0.0 issue
            lu["_rank"] = lu["slot"].map(_slot_rank)
            lu = lu.sort_values("_rank", kind="stable")
            lu["slot"] = lu["slot"].map(lambda x: SLOT_LABEL.get(x, x))
            side["lineup"] = lu[["slot", "player", "pos", "nfl", "points"]].reset_index(drop=True)
        sides.append(side)

    if home_score == away_score:
        winner = "Tie"
    else:
        winner = sides[0]["owner"] if home_score > away_score else sides[1]["owner"]
    return {"league_name": league[0] if league else "", "season": key[1], "week": key[2],
            "stage": "Regular season" if regular else "Playoffs", "sides": sides, "winner": winner,
            "margin": round(abs(float(home_score) - float(away_score)), 1),
            "espn_url": box_score_url(key[0], key[1], key[2], team_id)}
