"""Deeper analytics built on the SQL views.

Every function takes an open sqlite connection and returns pandas DataFrames (or a dict of them).
league_id / season = None means "all". Regular season only unless stated otherwise.
"""
import numpy as np
import pandas as pd

PYTHAG_EXPONENT = 2.37  # commonly used for football; tweak if you like
TEAM_WEEK = ["league_id", "season", "week", "team_id"]


# ---------------------------------------------------------------- loading helpers
def _q(con, sql, params=()):
    return pd.read_sql_query(sql, con, params=params)


def _where(league_id=None, season=None, week=None, extra=()):
    clauses, params = list(extra), []
    if league_id:
        clauses.append("league_id = ?")
        params.append(int(league_id))
    if season:
        clauses.append("season = ?")
        params.append(int(season))
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


REGRET_COLS = ["league_name", "season", "week", "owner", "benched", "benched_pos", "benched_points",
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
    g = games(con, league_id, season)
    if g.empty:
        return {}
    cols = ["league_name", "season", "week", "owner", "team_name", "points_for", "points_against"]
    key = ["league_id", "season", "week"]
    top = g.loc[g.groupby(key)["points_for"].idxmax()]
    low = g.loc[g.groupby(key)["points_for"].idxmin()]
    wins = g[g["win"] == 1].copy()
    wins["margin"] = (wins["points_for"] - wins["points_against"]).round(1)
    margin_cols = ["league_name", "season", "week", "owner", "opp_owner", "points_for", "points_against", "margin"]
    return {
        "top_scorer_weeks": top.groupby("owner").size().rename("weeks_as_top_scorer")
                               .sort_values(ascending=False).reset_index(),
        "low_scorer_weeks": low.groupby("owner").size().rename("weeks_as_low_scorer")
                               .sort_values(ascending=False).reset_index(),
        "highest_scores": g.nlargest(limit, "points_for")[cols].reset_index(drop=True),
        "lowest_scores": g.nsmallest(limit, "points_for")[cols].reset_index(drop=True),
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


def owner_top_players(con, owner, limit=10) -> pd.DataFrame:
    return _q(con, """
        SELECT player_name, position, COUNT(*) AS weeks_started,
               ROUND(SUM(points), 1) AS total_points, ROUND(AVG(points), 2) AS avg_points
        FROM player_games WHERE owner = ? AND is_starter = 1
        GROUP BY league_id, player_id, player_name, position
        ORDER BY total_points DESC LIMIT ?""", (owner, int(limit)))


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
def career_summary(con, owner):
    g = _q(con, "SELECT * FROM weekly_results WHERE is_regular_season = 1 AND owner = ?", (owner,))
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
