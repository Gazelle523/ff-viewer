"""Weekly recap for each league.

    python recap.py                  # print the latest recap for every league in leagues.json
    python recap.py --league 123456  # one league
    python recap.py --week 7         # a specific regular-season week of the latest season
    python recap.py --post           # also post to Discord (once per league-week) if a webhook is configured

Webhook: set DISCORD_WEBHOOK_URL in .env, or add "discord_webhook": "https://..." to a league in leagues.json.
The sync job runs `recap.py --post` after each import, so a recap lands in your channel automatically.
"""
import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

import db
import insights


def latest_week(con, league_id):
    """(season, week) of the most recent regular-season week stored for this league, or None."""
    row = con.execute("SELECT MAX(season) FROM matchups WHERE league_id = ?", (league_id,)).fetchone()
    if not row or row[0] is None:
        return None
    season = row[0]
    row = con.execute("SELECT MAX(week) FROM matchups WHERE league_id = ? AND season = ? AND is_regular_season = 1",
                      (league_id, season)).fetchone()
    return (season, row[0]) if row and row[0] else None


def _link(row) -> str:
    url = insights.box_score_url(row["league_id"], row["season"], row["week"], row["team_id"])
    return f" ([box score]({url}))" if url else ""


def build_recap(con, league_id, season, week) -> str:
    g = insights.games(con, league_id, season)
    w = g[g["week"] == week]
    if w.empty:
        return ""
    lines = [f"🏈 **{w['league_name'].iloc[0]}: {season} Week {week} recap**", ""]

    top, low = w.loc[w["points_for"].idxmax()], w.loc[w["points_for"].idxmin()]
    lines.append(f"🔥 **Top score:** {top['owner']} ({top['team_name']}) with {top['points_for']:.1f}" + _link(top))
    lines.append(f"🥶 **Low score:** {low['owner']} ({low['team_name']}) with {low['points_for']:.1f}" + _link(low))

    winners = w[w["win"] == 1].copy()
    if not winners.empty:
        winners["margin"] = winners["points_for"] - winners["points_against"]
        big, close = winners.loc[winners["margin"].idxmax()], winners.loc[winners["margin"].idxmin()]
        lines.append(f"💥 **Blowout:** {big['owner']} beat {big['opp_owner']} by {big['margin']:.1f}" + _link(big))
        lines.append(f"😬 **Nail-biter:** {close['owner']} edged {close['opp_owner']} by {close['margin']:.1f}" + _link(close))
        lucky = winners.loc[winners["expected_win"].idxmin()]
        lines.append(f"🍀 **Luckiest win:** {lucky['owner']} won with a score that would have beaten only "
                     f"{lucky['expected_win'] * 100:.0f}% of the league" + _link(lucky))
    losers = w[w["win"] == 0]
    if not losers.empty:
        unlucky = losers.loc[losers["expected_win"].idxmax()]
        lines.append(f"💔 **Unluckiest loss:** {unlucky['owner']} scored {unlucky['points_for']:.1f} (beats "
                     f"{unlucky['expected_win'] * 100:.0f}% of the league) and still lost to {unlucky['opp_owner']}" + _link(unlucky))

    eff = insights.lineup_efficiency(con, league_id, season, week)
    if not eff.empty:
        best, worst = eff.loc[eff["efficiency"].idxmax()], eff.loc[eff["points_lost"].idxmax()]
        lines.append(f"🧠 **Best manager:** {best['owner']} ({best['efficiency']:.0f}% of the best possible lineup)")
        lines.append(f"🤦 **Biggest lineup miss:** {worst['owner']} left {worst['points_lost']:.1f} points on the bench" + _link(worst))

    pr = insights.power_rankings(con, league_id, season, through_week=week)
    if not pr.empty:
        lines += ["", "📈 **Power rankings**"]
        for r in pr.head(5).itertuples():
            arrow = f"▲{r.move}" if r.move > 0 else (f"▼{-r.move}" if r.move < 0 else "–")
            lines.append(f"{r.rank}. {r.owner} ({r.record}) {arrow}")
    return "\n".join(lines)


def _chunks(text, size=1900):
    chunk = ""
    for line in text.split("\n"):
        if len(chunk) + len(line) + 1 > size:
            yield chunk
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        yield chunk


def post_discord(url, text):
    for chunk in _chunks(text):
        req = urllib.request.Request(
            url, data=json.dumps({"content": chunk}).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "ff-analyzer/1.0"})
        urllib.request.urlopen(req, timeout=15).read()


def main():
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--league", type=int)
    parser.add_argument("--week", type=int)
    parser.add_argument("--post", action="store_true", help="post to Discord (once per league-week)")
    args = parser.parse_args()

    config = json.loads((Path(__file__).parent / "leagues.json").read_text())
    con = db.connect()
    db.init_db(con)
    failures = 0
    for cfg in config["leagues"]:
        lid = cfg["league_id"]
        if args.league and lid != args.league:
            continue
        found = latest_week(con, lid)
        if not found:
            print(f"{cfg.get('name', lid)}: no data yet")
            continue
        season, week = found
        if args.week:
            week = args.week
        text = build_recap(con, lid, season, week)
        if not text:
            print(f"{cfg.get('name', lid)}: nothing for {season} week {week}")
            continue
        print(text + "\n")
        if not args.post:
            continue
        webhook = cfg.get("discord_webhook") or os.getenv("DISCORD_WEBHOOK_URL")
        if not webhook:
            print("(no Discord webhook configured, not posting)")
            continue
        key = f"recap_posted_{lid}_{season}_{week}"
        if con.execute("SELECT 1 FROM meta WHERE key = ?", (key,)).fetchone():
            print(f"(already posted {season} week {week}, skipping)")
            continue
        try:
            post_discord(webhook, text)
            con.execute("INSERT OR REPLACE INTO meta VALUES (?, 'posted')", (key,))
            con.commit()
            print("Posted to Discord.")
        except Exception as e:
            failures += 1
            print(f"Discord post FAILED: {type(e).__name__}: {e}")
    con.close()
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
