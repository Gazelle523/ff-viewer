"""Fantasy Football Analyzer UI.  Run with:  streamlit run app.py"""
import os
import subprocess
import sys
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

import db
import explore
import insights
import metrics
import recap
import sqlrun

load_dotenv()
# The hosted copy for friends has no ingest.py, so it automatically becomes a read-only viewer
# (no sync button, no Ask page). You can also force this on your own PC with VIEWER_MODE=1.
VIEWER = os.getenv("VIEWER_MODE") == "1" or not (Path(__file__).parent / "ingest.py").exists()
st.set_page_config(page_title="Fantasy Football Analyzer", page_icon="🏈", layout="wide")
st.title("🏈 Fantasy Football Analyzer")

con = db.connect()
# The hosted copy never writes to the database; it relies on the views already being in the file.
if not VIEWER or not con.execute("SELECT 1 FROM sqlite_master WHERE name = 'games'").fetchone():
    db.init_db(con)

# ---- sync controls ----
last_sync = metrics.q(con, "SELECT value FROM meta WHERE key = 'last_sync'")
st.sidebar.caption(f"Last synced: {last_sync['value'].iloc[0] if len(last_sync) else 'never'}")
if not VIEWER and st.sidebar.button("🔄 Sync current season now"):
    with st.spinner("Syncing from ESPN (can take a minute)..."):
        result = subprocess.run([sys.executable, "ingest.py", "--current"], capture_output=True,
                                text=True, cwd=Path(__file__).parent)
    if result.returncode == 0:
        st.sidebar.success("Sync complete.")
    else:
        st.sidebar.error("Sync had errors:")
    st.sidebar.code((result.stdout + result.stderr)[-1500:])
    con = db.connect()  # reopen so everything below sees the new data

leagues = metrics.q(con, "SELECT DISTINCT league_id, name FROM leagues ORDER BY name")
if leagues.empty:
    st.warning("No data yet. Run `python ingest.py` (or `python seed_demo.py` for fake demo data), then refresh.")
    st.stop()

# ---- sidebar: filters and page ----
league_ids = dict(zip(leagues["name"], leagues["league_id"].astype(int)))
league_choice = st.sidebar.selectbox("League", ["All leagues"] + list(league_ids))
league_id = league_ids.get(league_choice)

seasons = [int(s) for s in metrics.q(con, "SELECT DISTINCT season FROM leagues ORDER BY season DESC")["season"]]
season_choice = st.sidebar.selectbox("Season", ["All seasons"] + seasons)
season = None if season_choice == "All seasons" else int(season_choice)
st.sidebar.caption("Most pages respect these two filters. Luck and schedule stats use regular-season games only.")

PAGES = ["🔎 Explore", "👤 Owner profile", "📈 Power rankings & charts", "🍀 Luck & schedule",
         "🧠 Lineup decisions", "⚔️ Head-to-head", "📊 Consistency", "🏈 Positions & players",
         "📜 Records & streaks", "📰 Weekly recap", "🧑‍💻 SQL", "💬 Ask (optional)"]
if VIEWER:  # the Ask page spends the owner's API credits, so the shared copy doesn't offer it
    PAGES = PAGES[:-1]
page = st.sidebar.radio("Page", PAGES)

owners = metrics.q(con, "SELECT DISTINCT owner FROM teams ORDER BY owner")["owner"].tolist()
POSITIONS = ["QB", "RB", "WR", "TE", "K", "D/ST"]
DATA_VERSION = db.db_path().stat().st_mtime  # changes after every sync, which refreshes cached results


# ---- helpers ----
@st.cache_data(show_spinner="Crunching numbers...")
def _cached(fn_name, lid, sn, version, kwargs):
    c = db.connect(read_only=True)
    try:
        return getattr(insights, fn_name)(c, league_id=lid, season=sn, **dict(kwargs))
    finally:
        c.close()


def run(fn_name, lid, sn, **kwargs):
    """Run a function from insights.py with caching (cache resets whenever the data changes)."""
    return _cached(fn_name, lid, sn, DATA_VERSION, tuple(sorted(kwargs.items())))


def table(df, name, styler=None):
    """Show a table with a CSV download button."""
    obj = styler if styler is not None else df
    try:
        st.dataframe(obj, use_container_width=True, hide_index=True)
    except TypeError:  # newer Streamlit versions replaced this argument
        st.dataframe(obj, width="stretch", hide_index=True)
    if len(df):
        st.download_button("⬇ Download CSV", df.to_csv(index=False).encode("utf-8"),
                           file_name=f"{name}.csv", mime="text/csv", key=f"dl_{name}")


def _shade(delta, vmax):
    if delta is None or pd.isna(delta) or not vmax:
        return ""
    x = max(-1.0, min(1.0, delta / vmax))
    rgb = "46,160,67" if x >= 0 else "220,53,69"
    return f"background-color: rgba({rgb},{0.12 + 0.5 * abs(x):.2f})"


def heat(df, cols, center=0.0, vmax=None, fmt=None):
    """Green/red cell shading around a center value."""
    if vmax is None:
        m = df[cols].sub(center).abs().max().max()
        vmax = float(m) if pd.notna(m) and m else 1.0
    f = lambda v: _shade(None if pd.isna(v) else v - center, vmax)  # noqa: E731
    styler = df.style
    styler = styler.map(f, subset=cols) if hasattr(styler, "map") else styler.applymap(f, subset=cols)
    if fmt:
        styler = styler.format(fmt, subset=cols, na_rep="")
    return styler


def pick_league():
    """The sidebar league, or the first league if 'All leagues' is selected."""
    if league_id:
        return league_id, league_choice
    return int(leagues["league_id"].iloc[0]), str(leagues["name"].iloc[0])


def pick_league_season():
    """One league and one season, for pages that only make sense for a single league-season."""
    lid, lname = pick_league()
    sn = season or max(seasons)
    if league_id is None or season is None:
        st.caption(f"Showing {lname}, {sn}. Pick a league and season in the sidebar to change this.")
    return lid, lname, sn


def regular_weeks(lid, sn):
    df = metrics.q(con, "SELECT DISTINCT week FROM matchups WHERE league_id = ? AND season = ? "
                        "AND is_regular_season = 1 ORDER BY week", (lid, sn))
    return [int(w) for w in df["week"]]


# =====================================================================================
if page == PAGES[0]:  # ---- Explore: guided questions, no AI, free
    st.caption("Pick a question, fill in the options, get the answer. League and Season come from the sidebar.")
    titles = {q["title"]: q for q in explore.QUESTIONS}
    q = titles[st.selectbox("Question", list(titles))]
    st.caption(q["help"])

    p = {"league_id": league_id, "season": season}
    cols = st.columns(max(len(q["params"]), 1))
    for col, spec in zip(cols, q["params"]):
        key, kind, label = spec["key"], spec["kind"], spec["label"]
        wkey = f"{q['id']}_{key}"
        with col:
            if kind == "owner":
                p[key] = st.selectbox(label, owners, key=wkey)
            elif kind == "position":
                p[key] = st.selectbox(label, POSITIONS, index=3, key=wkey)
            elif kind == "choice":
                p[key] = st.selectbox(label, spec["options"], key=wkey)
            elif kind == "bool":
                p[key] = st.checkbox(label, value=spec["default"], key=wkey)
            elif kind == "number":
                default = spec["default"]
                if key == "since":
                    default = max(min(seasons), max(seasons) - 2)
                p[key] = int(st.number_input(label, min_value=spec["min"], max_value=spec["max"],
                                             value=int(default), key=wkey))
    try:
        sql, params = q["build"](p)
        result = sqlrun.run_query(sql, params)
        table(result, f"explore_{q['id']}")
        if len(result) and "luck" in result.columns:
            st.bar_chart(result.set_index("owner")["luck"])
        with st.expander("SQL used (copy it into the SQL page to tweak)"):
            st.code(sql, language="sql")
            st.caption(f"Parameters, in order of the ? marks: {params}")
    except Exception as e:
        st.error(f"Query failed: {e}")

elif page == PAGES[1]:  # ---- Owner profile (works across all leagues)
    owner = st.selectbox("Owner", owners, key="prof_owner")
    st.caption("All leagues and seasons combined (regular season). Ignores the sidebar filters.")
    cs = insights.career_summary(con, owner)
    if cs is None:
        st.info("No regular-season games for this owner.")
    else:
        o = cs["overall"]
        c = st.columns(5)
        c[0].metric("League-seasons", o["seasons"])
        c[1].metric("Record", o["record"])
        c[2].metric("Win %", f"{o['win_pct']:.1%}")
        c[3].metric("Points / game", f"{o['ppg']:.1f}")
        c[4].metric("Luck (wins)", f"{o['luck']:+.1f}")
        c = st.columns(2)
        c[0].metric("Best week", f"{o['best_week']:.1f}")
        c[1].metric("Worst week", f"{o['worst_week']:.1f}")
        st.subheader("Season by season")
        table(cs["by_season"], "profile_seasons")
        st.subheader("Record against each opponent")
        h = insights.h2h_long(con)
        mine = h[h["owner"] == owner].sort_values("win_pct", ascending=False)
        table(mine[["opp_owner", "games", "wins", "losses", "avg_for", "avg_against"]], "profile_h2h")
        st.subheader("Streaks")
        sk = insights.streaks(con)
        table(sk[sk["owner"] == owner].drop(columns=["owner"]), "profile_streaks")
        st.subheader("Highest-scoring starters")
        table(insights.owner_top_players(con, owner), "profile_players")

elif page == PAGES[2]:  # ---- Power rankings & charts
    lid, lname, sn = pick_league_season()
    weeks = regular_weeks(lid, sn)
    if not weeks:
        st.info("No regular-season games stored for this league and season.")
    else:
        wmax = weeks[-1]
        through = st.slider("Through week", weeks[0], wmax, wmax) if wmax > weeks[0] else wmax
        st.subheader(f"Power rankings: {lname} {sn}, through week {through}")
        st.caption("Power score = 60% season-long all-play win % + 40% last-3-weeks all-play win %. "
                   "`move` = places gained (+) or lost (-) since the previous week.")
        table(run("power_rankings", lid, sn, through_week=int(through)), "power_rankings")
        st.subheader("Luck over time")
        st.caption("Cumulative (actual wins - expected wins). Rising = running lucky.")
        st.line_chart(run("luck_over_time", lid, sn))
        st.subheader("Weekly scores")
        st.line_chart(run("weekly_scores", lid, sn))

elif page == PAGES[3]:  # ---- Luck & schedule
    st.subheader("Luck: actual wins minus all-play expected wins")
    st.caption("Expected wins = wins you'd have if you played every team every week. Positive = lucky.")
    table(metrics.luck(con, season, league_id), "luck_by_team_season")
    career = metrics.luck_by_owner(con, league_id)
    st.subheader("Career luck by owner")
    st.bar_chart(career.set_index("owner")["luck"])
    table(career, "luck_by_owner")

    st.subheader("A second opinion: Pythagorean luck")
    st.caption("Expected wins from points scored vs allowed. If both luck columns agree, the verdict is solid.")
    table(run("pythagorean", league_id, season), "pythagorean")

    st.subheader("What if you also played the league median each week?")
    st.caption("A common fix for schedule luck: one extra win each week for beating the median score.")
    table(run("median_record", league_id, season), "median_record")

    st.subheader("Strength of schedule (hardest first)")
    st.caption("opp_avg_ppg = average of your opponents' season scoring averages.")
    table(metrics.schedule_strength(con, season, league_id), "schedule_strength")

elif page == PAGES[4]:  # ---- Lineup decisions
    st.caption("Based on lineups, so seasons without lineup data won't appear. Includes playoff weeks.")
    eff = run("lineup_efficiency", league_id, season)
    if eff.empty:
        st.info("No lineup data for this selection.")
    else:
        st.subheader("Manager efficiency")
        st.caption("Points you started vs the best lineup your roster could have fielded (respecting slots, flex "
                   "spots and IR). 100% = perfect.")
        by_season = st.checkbox("Split by season", value=False)
        table(insights.summarize_efficiency(eff, by_season), "manager_efficiency")

        st.subheader("Worst single-week lineups")
        worst = eff.sort_values("points_lost", ascending=False).head(15)
        table(worst[["league_name", "season", "week", "owner", "actual", "optimal", "points_lost", "efficiency"]],
              "worst_lineups")

        st.subheader("Start/sit regrets")
        st.caption("A benched player who outscored the starter he could have replaced.")
        reg = run("start_sit_regrets", league_id, season)
        table(reg.head(25), "start_sit_regrets")
        if len(reg):
            leaders = reg.groupby("owner").agg(regrets=("points_lost", "count"),
                                               total_points_lost=("points_lost", "sum")).round(1)
            st.subheader("Regret leaderboard")
            st.caption("Counts each starter once, so a few swaps can overlap.")
            table(leaders.sort_values("total_points_lost", ascending=False).reset_index(), "regret_leaders")

    st.subheader("Points sitting on the bench")
    table(metrics.bench_points(con, season, league_id), "bench_points")

elif page == PAGES[5]:  # ---- Head-to-head
    lid, lname = pick_league()
    st.caption(f"{lname}, regular season{'s' if season is None else f' {season}'}. Pick a league in the sidebar; "
               "owners only face each other within a league.")
    h = run("h2h_long", lid, season)
    if h.empty:
        st.info("No games for this selection.")
    else:
        st.subheader("Win % matrix")
        st.caption("Row owner's win % against the column owner. Green = owns them, red = owned.")
        m = insights.h2h_matrix(h).reset_index()
        other = [c for c in m.columns if c != "owner"]
        table(m, "h2h_matrix", heat(m, other, center=0.5, vmax=0.5, fmt="{:.0%}"))

        min_games = st.slider("Minimum games for nemesis / rivalry", 1, 10, 3)
        st.subheader("Nemesis and favorite victim")
        table(insights.nemesis_table(h, min_games), "nemesis")
        st.subheader("Closest rivalries")
        st.caption("Pairs with the most evenly matched series.")
        table(insights.rivalries(h, max(min_games, 2)), "rivalries")

elif page == PAGES[6]:  # ---- Consistency
    st.caption("Regular season. consistency_cv = std dev as % of average (lower = steadier). boom/bust = share of "
               "weeks scoring in the top/bottom quarter of the league. finishing_kick = second-half score vs "
               "league average minus first-half score vs league average.")
    c = run("consistency", league_id, season)
    if c.empty:
        st.info("No games for this selection.")
    else:
        table(c, "consistency")
        st.subheader("Consistency (lower = steadier)")
        st.bar_chart(c.set_index("owner")["consistency_cv"])

elif page == PAGES[7]:  # ---- Positions & players
    st.subheader("Average points by position for an owner")
    c1, c2, c3, c4 = st.columns(4)
    owner = c1.selectbox("Owner", owners, key="pos_owner")
    position = c2.selectbox("Position", POSITIONS, index=3, key="pos_position")
    min_season, max_season = min(seasons), max(seasons)
    since = c3.number_input("Since season", min_value=min_season, max_value=max_season,
                            value=max(min_season, max_season - 2), key="pos_since")
    starters_only = c4.checkbox("Starters only", value=True, key="pos_starters")
    pos_df = metrics.position_average(con, owner, position, int(since), starters_only)
    table(pos_df, "position_average")
    if not pos_df.empty:
        total = (pos_df["avg_points"] * pos_df["player_weeks"]).sum() / pos_df["player_weeks"].sum()
        st.metric(f"Overall average ({position}, since {int(since)})", f"{total:.2f} pts per player-week")

    st.subheader("Where each owner wins and loses by position")
    st.caption("Average weekly starter points above (+) or below (-) the league average at that position.")
    ps = run("position_strength", league_id, season)
    if ps.empty:
        st.info("No lineup data for this selection.")
    else:
        pdf = ps.reset_index()
        table(pdf, "position_strength", heat(pdf, [c for c in pdf.columns if c != "owner"], fmt="{:+.1f}"))

    st.subheader("Carried by one player?")
    st.caption("Each team-season's top starter and his share of the team's starter points.")
    table(run("carried_by", league_id, season), "carried_by")

    st.subheader("Loyalty: players an owner keeps starting")
    min_seasons = st.slider("Minimum seasons", 1, 6, 2)
    table(run("loyal_players", league_id, season, min_seasons=int(min_seasons)), "loyal_players")

elif page == PAGES[8]:  # ---- Records & streaks
    rb = run("records_book", league_id, season)
    if not rb:
        st.info("No games for this selection.")
    else:
        c1, c2 = st.columns(2)
        with c1:
            st.subheader("Most weeks as top scorer")
            table(rb["top_scorer_weeks"], "top_scorer_weeks")
        with c2:
            st.subheader("Most weeks as lowest scorer")
            table(rb["low_scorer_weeks"], "low_scorer_weeks")
        st.subheader("Highest single-game scores")
        table(rb["highest_scores"], "highest_scores")
        st.subheader("Lowest single-game scores")
        table(rb["lowest_scores"], "lowest_scores")
        st.subheader("Biggest blowouts")
        table(rb["blowouts"], "blowouts")
        st.subheader("Closest finishes")
        table(rb["closest"], "closest")
    st.subheader("Win and loss streaks")
    st.caption("Per owner per league, regular season. Streaks run across seasons unless you pick one.")
    table(run("streaks", league_id, season), "streaks")

elif page == PAGES[9]:  # ---- Weekly recap
    lid, lname, sn = pick_league_season()
    weeks = regular_weeks(lid, sn)
    if not weeks:
        st.info("No regular-season games stored for this league and season.")
    else:
        week = st.selectbox("Week", weeks, index=len(weeks) - 1)
        text = recap.build_recap(con, lid, sn, int(week))
        st.markdown(text.replace("\n", "  \n"))
        st.caption("Copy-friendly version:")
        st.code(text, language=None)
        st.caption("To post recaps to Discord automatically after each sync, see the README.")

elif page == PAGES[10]:  # ---- SQL: write your own
    st.caption("Write any read-only SELECT query. The app blocks anything that would change your data.")
    examples = {
        "(pick an example)": "",
        "Luck by owner, all time": (
            "SELECT owner, ROUND(SUM(win) - SUM(expected_win), 2) AS luck\n"
            "FROM weekly_results WHERE is_regular_season = 1\nGROUP BY owner ORDER BY luck DESC"),
        "Hardest schedules": (
            "SELECT league_name, season, owner, ROUND(opp_avg_ppg, 2) AS opp_avg_ppg\n"
            "FROM schedule_strength ORDER BY opp_avg_ppg DESC LIMIT 20"),
        "An owner's TE average by season": (
            "SELECT season, ROUND(AVG(points), 2) AS avg_te_points\n"
            "FROM player_games\nWHERE owner LIKE '%Alex%' AND position = 'TE' AND is_starter = 1\n"
            "GROUP BY season ORDER BY season"),
    }
    pick = st.selectbox("Start from an example", list(examples))
    sql_text = st.text_area("SQL", value=examples[pick] or "SELECT * FROM games LIMIT 20", height=180,
                            key=f"sql_{pick}")
    if st.button("Run query"):
        try:
            table(sqlrun.run_query(sql_text), "sql_result")
        except Exception as e:
            st.error(f"Query failed: {e}")
    with st.expander("Tables and views you can query"):
        st.code(db.schema_text(con), language="sql")

else:  # ---- Ask (optional, needs a paid API key)
    if not os.getenv("ANTHROPIC_API_KEY"):
        st.info(
            "This page is optional and needs an Anthropic API key (paid, usage-based). "
            "Everything else in the app works without it. Use Explore and SQL for free."
        )
    else:
        st.caption("Ask in plain English, e.g. “who has the hardest schedule so far this season?”")
        if "chat" not in st.session_state:
            st.session_state.chat = []
        for m in st.session_state.chat:
            with st.chat_message(m["role"]):
                st.markdown(m["content"])
                if m.get("sql"):
                    with st.expander("SQL used"):
                        st.code("\n\n".join(m["sql"]), language="sql")
        prompt = st.chat_input("Ask a question about your leagues")
        if prompt:
            with st.chat_message("user"):
                st.markdown(prompt)
            with st.chat_message("assistant"):
                with st.spinner("Thinking..."):
                    try:
                        from ask import ask
                        history = [(m["role"], m["content"]) for m in st.session_state.chat]
                        answer, sqls = ask(prompt, history)
                    except Exception as e:
                        answer, sqls = f"Something went wrong: {e}", []
                st.markdown(answer)
                if sqls:
                    with st.expander("SQL used"):
                        st.code("\n\n".join(sqls), language="sql")
            st.session_state.chat.append({"role": "user", "content": prompt})
            st.session_state.chat.append({"role": "assistant", "content": answer, "sql": sqls})
