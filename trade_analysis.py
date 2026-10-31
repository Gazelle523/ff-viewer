"""Import accepted ESPN trades and compare acquired players' fantasy points after each deal."""
import json
from collections import defaultdict
from datetime import datetime, timezone

import pandas as pd


def sync_trade_history(con, league, season, last_period=None):
    """Fetch accepted trades for one league season and persist their player legs.

    ESPN's mTransactions2 response can omit both status and trade items. The package's player-card
    helper repairs missing item lists; the explicit status fallback handles ESPN's incomplete rows.
    """
    from espn_api.football.player import Player
    from espn_api.football.transaction import Transaction

    lid = int(league.league_id)
    last_period = int(last_period or getattr(league, "finalScoringPeriod", 17) or 17)
    grouped = {}
    warnings = []

    for period in range(1, last_period + 1):
        params = {"view": "mTransactions2", "scoringPeriodId": period}
        filters = {"transactions": {"filterType": {"value": ["TRADE_ACCEPT"]}}}
        try:
            data = league.espn_request.league_get(
                params=params, headers={"x-fantasy-filter": json.dumps(filters)}
            )
            rows = data.get("transactions", [])
            for raw in rows:
                if not raw.get("status"):
                    raw["status"] = "EXECUTED"
            txns = [Transaction(raw, league.player_map, league.get_team_data) for raw in rows]
            if txns:
                try:
                    league._fill_trade_accept_from_player_cards(
                        txns, period, {"TRADE_ACCEPT"}, True, None, "week", Player
                    )
                except Exception as exc:  # keep any legs ESPN returned even if enrichment fails
                    warnings.append(f"Week {period}: player detail enrichment failed ({type(exc).__name__})")
            for txn in txns:
                if txn.type != "TRADE_ACCEPT":
                    continue
                trade_id = txn.related_transaction_id or txn.id
                if not trade_id:
                    continue
                rec = grouped.setdefault(str(trade_id), {
                    "accepted_at": 0, "scoring_period": int(txn.scoring_period or period), "legs": set()
                })
                rec["accepted_at"] = max(rec["accepted_at"], int(txn.date or 0))
                rec["scoring_period"] = int(txn.scoring_period or period)
                for item in txn.items:
                    if (item.type == "TRADE" and item.playerId and item.from_team_id is not None
                            and item.to_team_id is not None):
                        rec["legs"].add((int(item.playerId), str(item.player),
                                         int(item.from_team_id), int(item.to_team_id)))
        except Exception as exc:
            warnings.append(f"Week {period}: transaction request failed ({type(exc).__name__}: {exc})")

    for trade_id, rec in grouped.items():
        con.execute(
            "INSERT OR REPLACE INTO trade_history VALUES (?,?,?,?,?)",
            (lid, int(season), trade_id, rec["accepted_at"], rec["scoring_period"]),
        )
        for player_id, player_name, from_team, to_team in rec["legs"]:
            con.execute(
                "INSERT OR REPLACE INTO trade_legs VALUES (?,?,?,?,?,?,?)",
                (lid, int(season), trade_id, player_id, player_name, from_team, to_team),
            )
    con.commit()
    return {"trades": len(grouped), "itemized": sum(bool(r["legs"]) for r in grouped.values()),
            "warnings": warnings}


def _season_filter(season):
    if season is None:
        return None
    return [int(season)] if isinstance(season, int) else [int(s) for s in season]


def trade_leaderboard(con, league_id=None, season=None):
    """One row per accepted trade, ranked by acquired-player points above replacement per week.

    Only weeks strictly after the transaction scoring period count, avoiding partial-week credit.
    Player scores are looked up wherever that player appeared on a league roster in that week. A
    player's rate is adjusted by the best rostered player just beyond that position's weekly
    starter cutoff; the side's value is the average of those player-level surpluses.
    """
    params, clauses = [], []
    if league_id:
        clauses.append("h.league_id = ?")
        params.append(int(league_id))
    seasons = _season_filter(season)
    if seasons:
        clauses.append("h.season IN (" + ",".join("?" for _ in seasons) + ")")
        params.extend(seasons)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    history = con.execute(
        "SELECT h.league_id,h.season,h.trade_id,h.accepted_at,h.scoring_period,l.name AS league_name "
        "FROM trade_history h LEFT JOIN leagues l ON l.league_id=h.league_id AND l.season=h.season" + where,
        params,
    ).fetchall()
    if not history:
        return pd.DataFrame()

    grouped = defaultdict(list)
    for row in history:
        grouped[(int(row[0]), int(row[1]))].append(row)
    output = []
    for (lid, year), trades in grouped.items():
        score_rows = con.execute(
            "SELECT player_id,week,MAX(COALESCE(points,0)),MAX(position) FROM lineup_entries "
            "WHERE league_id=? AND season=? GROUP BY player_id,week", (lid, year)
        ).fetchall()
        scores = defaultdict(dict)
        positions = {}
        for player_id, week, points, position in score_rows:
            scores[int(player_id)][int(week)] = float(points or 0)
            positions[(int(player_id), int(week))] = position
        starter_rows = con.execute(
            "SELECT week,position,COUNT(DISTINCT player_id) FROM lineup_entries "
            "WHERE league_id=? AND season=? AND is_starter=1 GROUP BY week,position", (lid, year)
        ).fetchall()
        starter_counts = {(int(week), str(position)): int(count)
                          for week, position, count in starter_rows if position}
        rostered_rows = con.execute(
            "SELECT player_id,week,MAX(position),MAX(COALESCE(points,0)) FROM lineup_entries "
            "WHERE league_id=? AND season=? AND COALESCE(slot,'')!='IR' GROUP BY player_id,week", (lid, year)
        ).fetchall()
        rostered_points = defaultdict(list)
        for _player_id, week, position, points in rostered_rows:
            if position:
                rostered_points[(int(week), str(position))].append(float(points or 0))
        replacement = {}
        for key, values in rostered_points.items():
            cutoff = starter_counts.get(key)
            ranked_scores = sorted(values, reverse=True)
            if cutoff is not None and 0 <= cutoff < len(ranked_scores):
                replacement[key] = ranked_scores[cutoff]
        team_names = {int(tid): (owner or "Unknown owner", name or "Unknown team")
                      for tid, name, owner in con.execute(
                          "SELECT team_id,team_name,owner FROM teams WHERE league_id=? AND season=?",
                          (lid, year))}
        for h in trades:
            _, _, trade_id, accepted_at, period, league_name = h
            period = int(period or 0)
            legs = con.execute(
                "SELECT player_id,player_name,from_team_id,to_team_id FROM trade_legs "
                "WHERE league_id=? AND season=? AND trade_id=? ORDER BY player_name",
                (lid, year, trade_id),
            ).fetchall()
            participants = sorted({int(team_id) for _, _, sender, receiver in legs
                                   for team_id in (sender, receiver) if team_id is not None})
            if len(participants) < 2:
                continue
            assets = {tid: [] for tid in participants}
            for player_id, player_name, sender, receiver in legs:
                receiver = int(receiver)
                above_replacement = []
                raw_points = []
                for week, points in scores.get(int(player_id), {}).items():
                    if week <= period:
                        continue
                    raw_points.append(points)
                    position = positions.get((int(player_id), week))
                    replacement_points = replacement.get((week, str(position))) if position else None
                    if replacement_points is not None:
                        above_replacement.append(points - replacement_points)
                raw_ppg = sum(raw_points) / len(raw_points) if raw_points else None
                assets[receiver].append((str(player_name), sum(above_replacement), len(above_replacement), raw_ppg))

            a, b = participants[:2]
            has_assets = bool(assets[a]) and bool(assets[b])
            def team_value_ppg(team_id):
                rates = [surplus / weeks for _, surplus, weeks, _ in assets[team_id] if weeks]
                complete = bool(assets[team_id]) and len(rates) == len(assets[team_id])
                return (sum(rates) / len(rates), complete) if complete else (None, False)

            a_ppg, a_complete = team_value_ppg(a)
            b_ppg, b_complete = team_value_ppg(b)
            rankable = has_assets and a_complete and b_complete
            edge = round(abs(a_ppg - b_ppg), 2) if rankable else None
            winner_id = a if rankable and a_ppg > b_ppg else b if rankable and b_ppg > a_ppg else None
            if not has_assets:
                verdict = "Incomplete player return"
            elif not rankable:
                verdict = "No post-trade scoring data"
            elif winner_id is None:
                verdict = "Even"
            else:
                verdict = team_names.get(winner_id, ("Unknown owner", "Unknown team"))[0]
            timestamp = int(accepted_at or 0)
            date = datetime.fromtimestamp(timestamp / 1000, timezone.utc).strftime("%Y-%m-%d") if timestamp else ""

            owner_label = lambda team_id: team_names.get(team_id, ("Unknown owner", "Unknown team"))[0]
            side_a, side_b = owner_label(a), owner_label(b)
            output.append({
                "league_id": lid, "league": league_name or str(lid), "season": year,
                "date": date, "trade_week": period,
                "side_a_players": [(name, ppg) for name, _, _, ppg in assets[a]],
                "side_b_players": [(name, ppg) for name, _, _, ppg in assets[b]],
                "side_a": side_a, "side_a_ppg": round(a_ppg, 2) if a_ppg is not None else None,
                "side_b": side_b, "side_b_ppg": round(b_ppg, 2) if b_ppg is not None else None,
                "winner": verdict, "winner_edge": edge,
                "itemized": bool(legs), "rankable": rankable,
                "trade_id": str(trade_id),
            })
    result = pd.DataFrame(output)
    if result.empty:
        return result
    return result.sort_values(["rankable", "winner_edge", "date"], ascending=[False, False, False],
                              na_position="last").reset_index(drop=True)


def owner_trade_rankings(board):
    """Rank owners by average signed above-replacement edge across their graded trades."""
    stats = defaultdict(lambda: {"trades": 0, "graded": 0, "wins": 0, "losses": 0, "ties": 0, "net": 0.0})
    if board is None or board.empty:
        return pd.DataFrame(columns=["rank", "trader", "trades", "graded", "record", "avg_net_edge"])

    for row in board.itertuples(index=False):
        a, b = row.side_a, row.side_b
        stats[a]["trades"] += 1
        stats[b]["trades"] += 1
        if not row.rankable:
            continue
        edge_a = float(row.side_a_ppg) - float(row.side_b_ppg)
        for owner, edge in ((a, edge_a), (b, -edge_a)):
            s = stats[owner]
            s["graded"] += 1
            s["net"] += edge
            if edge > 0:
                s["wins"] += 1
            elif edge < 0:
                s["losses"] += 1
            else:
                s["ties"] += 1

    rows = []
    for owner, s in stats.items():
        rows.append({
            "trader": owner, "trades": s["trades"], "graded": s["graded"],
            "record": f"{s['wins']}/{s['losses']}",
            "avg_net_edge": round(s["net"] / s["graded"], 2) if s["graded"] else None,
        })
    result = pd.DataFrame(rows).sort_values(
        ["avg_net_edge", "graded", "trades", "trader"],
        ascending=[False, False, False, True], na_position="last"
    ).reset_index(drop=True)
    result.insert(0, "rank", range(1, len(result) + 1))
    return result
