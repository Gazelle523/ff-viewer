"""Fantasy Football Analyzer UI.  Run with:  streamlit run app.py"""
import hashlib
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

# Catch a half-updated project folder with a clear message instead of a stack trace.
_REQUIRED = {
    "insights": ["box_score", "find_games", "data_coverage", "records_book", "standings", "latest_results",
                 "add_box_score_links", "LINK_KEYS", "possible_duplicate_owners", "summarize_efficiency"],
    "db": ["repair_duplicate_weeks", "get_repair_log", "find_duplicate_weeks", "log_repair"],
    "metrics": ["position_average", "luck_by_owner", "schedule_strength", "bench_points", "luck"],
    "explore": ["QUESTIONS"],
}
_stale = [f"{mod}.py" for mod, names in _REQUIRED.items() if any(not hasattr(globals()[mod], n) for n in names)]
if _stale:
    st.error(f"Some project files are out of date: {', '.join(_stale)}. Copy ALL the code files from the newest download "
             "into this folder (app.py, db.py, explore.py, ingest.py, insights.py, metrics.py, recap.py, sqlrun.py), "
             "then stop the app (Ctrl+C) and start it again with: streamlit run app.py")
    st.stop()

con = db.connect()
# The hosted copy never writes to the database; it relies on the views already being in the file.
if not VIEWER or not con.execute("SELECT 1 FROM sqlite_master WHERE name = 'games'").fetchone():
    db.init_db(con)

# Tidy existing data on your own PC: drop weeks ESPN repeated after a season ended, merge duplicate owners.
if not VIEWER:
    try:
        from owner_names import normalize_owner_names
        removed_weeks = db.repair_duplicate_weeks(con)
        renamed_rows = normalize_owner_names(con)
        if removed_weeks or renamed_rows:
            st.sidebar.info(f"Cleaned up your data: removed {removed_weeks} duplicated week(s), "
                            f"merged {renamed_rows} owner name(s). Re-publish to update the hosted copy.")
    except Exception as e:  # never let tidying block the app
        st.sidebar.warning(f"Data cleanup skipped: {e}")

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

league_ids = dict(zip(leagues["name"], leagues["league_id"].astype(int)))
league_seasons = metrics.q(con, "SELECT league_id, season FROM leagues")
seasons = sorted({int(s) for s in league_seasons["season"]}, reverse=True)

# ---- pages ----
PAGE_HOME, PAGE_EXPLORE, PAGE_PROFILE = "🏠 Home", "🔎 Explore", "👤 Owner profile"
PAGE_POWER, PAGE_LUCK, PAGE_LINEUPS = "📈 Power rankings & charts", "🍀 Luck & schedule", "🧠 Lineup decisions"
PAGE_H2H, PAGE_CONSIST, PAGE_POS = "⚔️ Head-to-head", "📊 Consistency", "🏈 Positions & players"
PAGE_RECORDS, PAGE_RECAP = "📜 Records & streaks", "📰 Weekly recap"
PAGE_DATA, PAGE_SQL, PAGE_ASK = "🩺 Data check", "🧑‍💻 SQL", "💬 Ask (optional)"
PAGES = [PAGE_HOME, PAGE_EXPLORE, PAGE_PROFILE, PAGE_POWER, PAGE_LUCK, PAGE_LINEUPS, PAGE_H2H,
         PAGE_CONSIST, PAGE_POS, PAGE_RECORDS, PAGE_RECAP, PAGE_DATA, PAGE_SQL, PAGE_ASK]
SINGLE_SEASON_PAGES = {PAGE_HOME, PAGE_POWER, PAGE_RECAP}  # these show one season at a time
NO_FILTER_PAGES = {PAGE_SQL, PAGE_ASK}
if VIEWER:  # the Ask page spends the owner's API credits, so the shared copy doesn't offer it
    PAGES = [p for p in PAGES if p != PAGE_ASK]
page = st.sidebar.radio("Page", PAGES, key="page")

# ---- filters: the same two controls for every page ----
uses_filters = page not in NO_FILTER_PAGES
st.sidebar.markdown("**Filters**")
league_choice = st.sidebar.selectbox("League", ["All leagues"] + list(league_ids), key="f_league",
                                     disabled=not uses_filters)
league_id = league_ids.get(league_choice)
season_choice = st.sidebar.selectbox("Season", ["All seasons", "Last 3 seasons"] + seasons, key="f_season",
                                     disabled=not uses_filters)
if season_choice == "All seasons":
    season_filter = None
elif season_choice == "Last 3 seasons":
    season_filter = tuple(seasons[:3])
else:
    season_filter = (int(season_choice),)

if not uses_filters:
    st.sidebar.caption("This page doesn't use the filters.")
elif page in SINGLE_SEASON_PAGES:
    st.sidebar.caption("The filters apply to every page except SQL and Ask. This page shows one season at a time, "
                       "so with several seasons selected it uses the latest one.")
else:
    st.sidebar.caption("The filters apply to every page except SQL and Ask.")

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


def _dataframe(obj, **kwargs):
    """st.dataframe that works across Streamlit versions. Returns (selection event or None, worked)."""
    for width_kw in ({"use_container_width": True}, {"width": "stretch"}):
        try:
            return st.dataframe(obj, **kwargs, **width_kw), True
        except TypeError:
            continue
    return None, False


def _selected_rows(event):
    try:
        return list(event.selection.rows)
    except Exception:
        try:
            return list(event["selection"]["rows"])
        except Exception:
            return []


def render_box_score(league_id, season, week, team_id):
    """The full box score for one game, built from the stored data (works even where ESPN shows 0.0)."""
    b = insights.box_score(con, league_id, season, week, team_id)
    if b is None:
        st.warning("That game isn't stored in the database.")
        return
    try:
        box = st.container(border=True)
    except TypeError:  # older Streamlit has no bordered containers
        box = st.container()
    with box:
        margin = f"{b['winner']} won by {b['margin']:.1f}" if b["winner"] != "Tie" else "Tied"
        st.markdown(f"#### {b['league_name']}: {b['season']} week {b['week']} ({b['stage']})")
        st.caption(margin)
        for col, side in zip(st.columns(2), b["sides"]):
            with col:
                crown = " 🏆" if side["owner"] == b["winner"] else ""
                st.markdown(f"**{side['owner']}** ({side['team']}){crown}")
                st.metric("Score", f"{side['score']:.1f}")
                if side["lineup"].empty:
                    st.caption("No lineup data is stored for this game, so only the score is available.")
                    continue
                if side.get("points_missing"):
                    st.warning("ESPN returned 0.0 for every player in this lineup (its known issue with past seasons), "
                               "so only the team score above is reliable.")
                _dataframe(side["lineup"], hide_index=True)
                st.caption(f"Starters {side['starters_total']:.1f} · bench {side['bench_total']:.1f} · "
                           f"best possible lineup {side['best_possible']:.1f} ({side['efficiency']:.0f}% efficient)")
        st.caption(f"[Open on ESPN]({b['espn_url']}) (ESPN may show 0.0 for older seasons)")


def _game_label(row):
    who = next((row[c] for c in ("owner", "winner") if c in row.index), "?")
    vs = next((row[c] for c in ("opponent", "loser", "opp_owner") if c in row.index), None)
    return f"{int(row['season'])} week {int(row['week'])}: {who}" + (f" vs {vs}" if vs else "")


MAX_OPEN_BOX_SCORES = 3


def _editor(view, **kwargs):
    """st.data_editor that works across Streamlit versions. Returns (edited table or None, worked)."""
    for width_kw in ({"use_container_width": True}, {"width": "stretch"}):
        try:
            return st.data_editor(view, **kwargs, **width_kw), True
        except TypeError:
            continue
    return None, False


def table(df, name, styler=None):
    """Show a table with a CSV download button. Tables of games get a 'Box score' checkbox column: tick a row and
    that game's box score opens above the table."""
    game_cols = all(c in df.columns for c in insights.LINK_KEYS)
    games_table = game_cols and styler is None and not df.empty
    panel = st.container() if games_table else None
    shown = [c for c in df.columns if c not in ("league_id", "team_id")] if game_cols else list(df.columns)
    chosen = []

    if games_table:
        view = df[shown].copy()
        view.insert(0, "Box score", False)
        # a new key whenever the table's contents change, so ticks never carry over to different rows
        sig = hashlib.md5(df[shown].to_csv(index=False).encode("utf-8")).hexdigest()[:10]
        try:
            cfg = {"Box score": st.column_config.CheckboxColumn("Box score", help="Tick to open this game's box score",
                                                                default=False)}
            edited, ok = _editor(view, hide_index=True, key=f"tbl_{name}_{sig}", disabled=shown, column_config=cfg,
                                 num_rows="fixed")
        except AttributeError:
            edited, ok = None, False
        if ok:
            ticked = [i for i, v in enumerate(edited["Box score"].tolist()) if v]
            chosen = [df.iloc[i] for i in ticked[:MAX_OPEN_BOX_SCORES]]
            if len(ticked) > MAX_OPEN_BOX_SCORES:
                st.caption(f"Showing the first {MAX_OPEN_BOX_SCORES} ticked games. Untick some to see others.")
        else:  # older Streamlit without a checkbox column: pick the game from a list instead
            _dataframe(df[shown], hide_index=True)
            labels = [_game_label(r) for _, r in df.iterrows()]
            pick = st.selectbox("Show a box score", ["(none)"] + labels, key=f"pick_{name}")
            if pick != "(none)":
                chosen = [df.iloc[labels.index(pick)]]
    else:
        _dataframe(styler if styler is not None else df[shown], hide_index=True)

    for row in chosen:
        with panel:
            render_box_score(row["league_id"], row["season"], row["week"], row["team_id"])
    if len(df):
        csv = insights.add_box_score_links(df).rename(columns={"box_score": "espn_link"})
        st.download_button("⬇ Download CSV", csv.to_csv(index=False).encode("utf-8"),
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


def season_for(lid):
    """The latest season of this league that matches the Season filter (for single-season pages)."""
    avail = sorted((int(s) for s in league_seasons.loc[league_seasons["league_id"] == lid, "season"]), reverse=True)
    for s in avail:
        if season_filter is None or s in season_filter:
            return s
    return None


def per_league(render):
    """Run a page section for the chosen league, or in one tab per league when 'All leagues' is chosen."""
    if league_id:
        render(league_id, league_choice)
    else:
        for tab, (name, lid) in zip(st.tabs(list(league_ids)), league_ids.items()):
            with tab:
                render(lid, name)


def regular_weeks(lid, sn):
    df = metrics.q(con, "SELECT DISTINCT week FROM matchups WHERE league_id = ? AND season = ? "
                        "AND is_regular_season = 1 ORDER BY week", (lid, sn))
    return [int(w) for w in df["week"]]


# ---- page sections that run once per league ----
def render_home(lid, lname):
    sn = season_for(lid)
    if sn is None:
        st.info(f"{lname} has no data for the selected season(s).")
        return
    weeks = regular_weeks(lid, sn)
    st.subheader(f"{lname}: {sn}" + (f" (through week {weeks[-1]})" if weeks else ""))

    # headline numbers
    rb = run("records_book", lid, (sn,))
    luck_df = metrics.luck(con, (sn,), lid)
    eff = insights.summarize_efficiency(run("lineup_efficiency", lid, (sn,)))
    c = st.columns(4)
    if rb:
        top = rb["highest_scores"].iloc[0]
        c[0].metric("Highest score", f"{top['points_for']:.1f}", f"{top['owner']}, week {int(top['week'])}",
                    delta_color="off")
    if len(luck_df):
        lucky, unlucky = luck_df.iloc[0], luck_df.iloc[-1]
        c[1].metric("Luckiest owner", lucky["owner"], f"{lucky['luck']:+.1f} wins vs expected")
        c[2].metric("Unluckiest owner", unlucky["owner"], f"{unlucky['luck']:+.1f} wins vs expected")
    if len(eff):
        c[3].metric("Best lineup manager", eff.iloc[0]["owner"], f"{eff.iloc[0]['efficiency_pct']:.1f}% of best possible",
                    delta_color="off")

    left, right = st.columns(2)
    with left:
        st.markdown("**Standings** (regular season)")
        table(insights.standings(con, lid, sn), f"home_standings_{lid}")
    with right:
        if weeks:
            st.markdown("**Power rankings** (top 5)")
            pr = run("power_rankings", lid, sn, through_week=weeks[-1])
            table(pr[["rank", "move", "owner", "record", "power_score"]].head(5), f"home_power_{lid}")
        week, is_reg, res = insights.latest_results(con, lid, sn)
        if week:
            st.markdown(f"**Latest results: week {week}**" + ("" if is_reg else " (playoffs)"))
            table(res, f"home_results_{lid}")
    st.caption("Use the Page list in the sidebar to dig into luck, lineups, head-to-head, records and more.")


def render_power(lid, lname):
    sn = season_for(lid)
    weeks = regular_weeks(lid, sn) if sn else []
    if not weeks:
        st.info(f"{lname} has no regular-season games for the selected season(s).")
        return
    wmax = weeks[-1]
    through = st.slider("Through week", weeks[0], wmax, wmax, key=f"power_week_{lid}") if wmax > weeks[0] else wmax
    st.subheader(f"Power rankings: {lname} {sn}, through week {through}")
    st.caption("Power score = 60% season-long all-play win % + 40% last-3-weeks all-play win %. "
               "`move` = places gained (+) or lost (-) since the previous week.")
    table(run("power_rankings", lid, sn, through_week=int(through)), f"power_rankings_{lid}")
    st.subheader("Luck over time")
    st.caption("Cumulative (actual wins - expected wins). Rising = running lucky.")
    st.line_chart(run("luck_over_time", lid, sn))
    st.subheader("Weekly scores")
    st.line_chart(run("weekly_scores", lid, sn))


def render_h2h(lid, lname):
    h = run("h2h_long", lid, season_filter)
    if h.empty:
        st.info(f"{lname} has no games for the selected season(s).")
        return
    st.subheader("Win % matrix")
    st.caption("Row owner's win % against the column owner. Green = owns them, red = owned. Regular season.")
    m = insights.h2h_matrix(h).reset_index()
    other = [c for c in m.columns if c != "owner"]
    table(m, f"h2h_matrix_{lid}", heat(m, other, center=0.5, vmax=0.5, fmt="{:.0%}"))
    min_games = st.slider("Minimum games for nemesis / rivalry", 1, 10, 3, key=f"h2h_min_{lid}")
    st.subheader("Nemesis and favorite victim")
    table(insights.nemesis_table(h, min_games), f"nemesis_{lid}")
    st.subheader("Closest rivalries")
    st.caption("Pairs with the most evenly matched series.")
    table(insights.rivalries(h, max(min_games, 2)), f"rivalries_{lid}")


def render_recap(lid, lname):
    sn = season_for(lid)
    weeks = regular_weeks(lid, sn) if sn else []
    if not weeks:
        st.info(f"{lname} has no regular-season games for the selected season(s).")
        return
    week = st.selectbox("Week", weeks, index=len(weeks) - 1, key=f"recap_week_{lid}")
    text = recap.build_recap(con, lid, sn, int(week))
    st.markdown(text.replace("\n", "  \n"))
    st.caption("Copy-friendly version:")
    st.code(text, language=None)
    st.caption("To post recaps to Discord automatically after each sync, see the README.")


# =====================================================================================
if page == PAGE_HOME:  # ---- landing page
    st.caption("A snapshot of where each league stands. Pick a league and season in the sidebar.")
    per_league(render_home)

elif page == PAGE_EXPLORE:  # ---- guided questions, no AI, free
    st.caption("Pick a question, fill in the options, get the answer. League and Season come from the sidebar.")
    titles = {q["title"]: q for q in explore.QUESTIONS}
    q = titles[st.selectbox("Question", list(titles))]
    st.caption(q["help"])

    p = {"league_id": league_id, "season": season_filter}
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
                p[key] = int(st.number_input(label, min_value=spec["min"], max_value=spec["max"],
                                             value=int(spec["default"]), key=wkey))
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

elif page == PAGE_PROFILE:  # ---- Owner profile
    owner = st.selectbox("Owner", owners, key="prof_owner")
    st.caption("Regular season, for the leagues and seasons selected in the sidebar. "
               "Choose All leagues and All seasons for a full career.")
    cs = insights.career_summary(con, owner, league_id, season_filter)
    if cs is None:
        st.info("No regular-season games for this owner in the selected leagues and seasons.")
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
        h = insights.h2h_long(con, league_id, season_filter)
        mine = h[h["owner"] == owner].sort_values("win_pct", ascending=False)
        table(mine[["opp_owner", "games", "wins", "losses", "avg_for", "avg_against"]], "profile_h2h")
        st.subheader("Streaks")
        sk = insights.streaks(con, league_id, season_filter)
        table(sk[sk["owner"] == owner].drop(columns=["owner"]), "profile_streaks")
        st.subheader("Highest-scoring starters")
        table(insights.owner_top_players(con, owner, 10, league_id, season_filter), "profile_players")

    with st.expander("Spot duplicate owners (same person under two names)"):
        dupes = insights.possible_duplicate_owners(con)
        if dupes.empty:
            st.caption("No suspected duplicates found.")
        else:
            st.caption("These names look like the same person. To merge a pair, add it to owner_aliases.json "
                       "(see the README), then restart the app.")
            table(dupes, "possible_duplicates")

elif page == PAGE_POWER:  # ---- Power rankings & charts
    per_league(render_power)

elif page == PAGE_LUCK:  # ---- Luck & schedule
    st.subheader("Luck: actual wins minus all-play expected wins")
    st.caption("Expected wins = wins you'd have if you played every team every week. Positive = lucky.")
    table(metrics.luck(con, season_filter, league_id), "luck_by_team_season")
    career = metrics.luck_by_owner(con, league_id, season_filter)
    st.subheader("Luck by owner")
    if len(career):
        st.bar_chart(career.set_index("owner")["luck"])
    table(career, "luck_by_owner")

    st.subheader("A second opinion: Pythagorean luck")
    st.caption("Expected wins from points scored vs allowed. If both luck columns agree, the verdict is solid.")
    table(run("pythagorean", league_id, season_filter), "pythagorean")

    st.subheader("What if you also played the league median each week?")
    st.caption("A common fix for schedule luck: one extra win each week for beating the median score.")
    table(run("median_record", league_id, season_filter), "median_record")

    st.subheader("Strength of schedule (hardest first)")
    st.caption("opp_avg_ppg = average of your opponents' season scoring averages.")
    table(metrics.schedule_strength(con, season_filter, league_id), "schedule_strength")

elif page == PAGE_LINEUPS:  # ---- Lineup decisions
    st.caption("Based on lineups, so seasons without lineup data won't appear. Includes playoff weeks.")
    eff = run("lineup_efficiency", league_id, season_filter)
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
        table(worst[["league_id", "team_id", "league_name", "season", "week", "owner", "actual", "optimal", "points_lost",
                     "efficiency"]],
              "worst_lineups")

        st.subheader("Start/sit regrets")
        st.caption("A benched player who outscored the starter he could have replaced.")
        reg = run("start_sit_regrets", league_id, season_filter)
        table(reg.head(25), "start_sit_regrets")
        if len(reg):
            leaders = reg.groupby("owner").agg(regrets=("points_lost", "count"),
                                               total_points_lost=("points_lost", "sum")).round(1)
            st.subheader("Regret leaderboard")
            st.caption("Counts each starter once, so a few swaps can overlap.")
            table(leaders.sort_values("total_points_lost", ascending=False).reset_index(), "regret_leaders")

    st.subheader("Points sitting on the bench")
    table(metrics.bench_points(con, season_filter, league_id), "bench_points")

elif page == PAGE_H2H:  # ---- Head-to-head
    st.caption("Owners only face each other within a league, so each league gets its own tab when 'All leagues' "
               "is selected.")
    per_league(render_h2h)

elif page == PAGE_CONSIST:  # ---- Consistency
    st.caption("Regular season. consistency_cv = std dev as % of average (lower = steadier). boom/bust = share of "
               "weeks scoring in the top/bottom quarter of the league. finishing_kick = second-half score vs "
               "league average minus first-half score vs league average.")
    c = run("consistency", league_id, season_filter)
    if c.empty:
        st.info("No games for this selection.")
    else:
        table(c, "consistency")
        st.subheader("Consistency (lower = steadier)")
        st.bar_chart(c.set_index("owner")["consistency_cv"])

elif page == PAGE_POS:  # ---- Positions & players
    st.subheader("Average points by position for an owner")
    st.caption("Uses the sidebar League and Season filters. For 'the past 3 years', choose Last 3 seasons.")
    c1, c2, c3 = st.columns(3)
    owner = c1.selectbox("Owner", owners, key="pos_owner")
    position = c2.selectbox("Position", POSITIONS, index=3, key="pos_position")
    starters_only = c3.checkbox("Starters only", value=True, key="pos_starters")
    pos_df = metrics.position_average(con, owner, position, season_filter, league_id, starters_only)
    table(pos_df, "position_average")
    if not pos_df.empty:
        total = (pos_df["avg_points"] * pos_df["player_weeks"]).sum() / pos_df["player_weeks"].sum()
        st.metric(f"Overall average ({position}, selected seasons)", f"{total:.2f} pts per player-week")

    st.subheader("Where each owner wins and loses by position")
    st.caption("Average weekly starter points above (+) or below (-) the league average at that position.")
    ps = run("position_strength", league_id, season_filter)
    if ps.empty:
        st.info("No lineup data for this selection.")
    else:
        pdf = ps.reset_index()
        table(pdf, "position_strength", heat(pdf, [c for c in pdf.columns if c != "owner"], fmt="{:+.1f}"))

    st.subheader("Carried by one player?")
    st.caption("Each team-season's top starter and his share of the team's starter points.")
    table(run("carried_by", league_id, season_filter), "carried_by")

    st.subheader("Loyalty: players an owner keeps starting")
    min_seasons = st.slider("Minimum seasons", 1, 6, 2)
    table(run("loyal_players", league_id, season_filter, min_seasons=int(min_seasons)), "loyal_players")

elif page == PAGE_RECORDS:  # ---- Records & streaks
    limit = int(st.number_input("Rows to show in each record table", min_value=5, max_value=500, value=10,
                                step=5, key="records_limit"))
    st.caption("Single-game records include every game: regular season, playoffs and consolation. The week and stage "
               "columns say when each happened. Weeks as top or lowest scorer and streaks use the regular season only.")
    rb = run("records_book", league_id, season_filter, limit=limit)
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
        st.subheader(f"Highest single-game scores (top {limit})")
        table(rb["highest_scores"], "highest_scores")
        st.subheader(f"Lowest single-game scores (bottom {limit})")
        table(rb["lowest_scores"], "lowest_scores")
        st.subheader(f"Biggest blowouts (top {limit})")
        table(rb["blowouts"], "blowouts")
        st.subheader(f"Closest finishes (top {limit})")
        table(rb["closest"], "closest")
    st.subheader("Win and loss streaks")
    st.caption("Per owner per league, regular season. Streaks run across the seasons you've selected.")
    table(run("streaks", league_id, season_filter), "streaks")

elif page == PAGE_RECAP:  # ---- Weekly recap
    per_league(render_recap)

elif page == PAGE_DATA:  # ---- Data check
    st.caption("Check what is actually stored, find a specific game, and see anything the cleanup removed.")
    st.subheader("What's stored for each league and season")
    st.caption("regular_weeks + playoff_weeks = weeks_stored. If last_week is lower than you expect, or weeks_with_lineups "
               "is lower than weeks_stored, that data wasn't imported. top_score is the highest single-team score stored.")
    cov = insights.data_coverage(con)
    if league_id:
        cov = cov[cov["league"] == league_choice]
    if season_filter:
        cov = cov[cov["season"].isin(season_filter)]
    table(cov, "data_coverage")

    st.subheader("Find a game")
    st.caption("Every stored game for an owner, regular season and playoffs. Uses the sidebar League and Season filters.")
    c1, c2 = st.columns(2)
    who = c1.selectbox("Owner", owners, key="find_owner")
    opp = c2.selectbox("Opponent", ["Anyone"] + owners, key="find_opponent")
    table(insights.find_games(con, who, None if opp == "Anyone" else opp, league_id, season_filter), "find_game")

    st.subheader("Cleanup log")
    st.caption("Anything the automatic cleanup removed (weeks ESPN repeated after a season ended).")
    log = db.get_repair_log(con)
    if log:
        st.code("\n".join(log), language=None)
    else:
        st.caption("Nothing has been removed.")

elif page == PAGE_SQL:  # ---- SQL: write your own
    st.caption("Write any read-only SELECT query. The app blocks anything that would change your data. "
               "The sidebar filters don't apply here.")
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
