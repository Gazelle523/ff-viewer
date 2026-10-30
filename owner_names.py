"""Merge duplicate owner profiles (same person, different ESPN display names).

Edit owner_aliases.json to add more. Format:  { "Name To Show": ["other name", "another name"], ... }
Matching ignores capitalization, spaces and punctuation, so "gatchell 71" also matches "Gatchell71".
Names that differ only by capitalization/spacing are merged automatically even without an alias entry.
"""
import json
import re
import unicodedata
from collections import Counter
from pathlib import Path

ALIASES_FILE = Path(__file__).parent / "owner_aliases.json"


def name_key(name) -> str:
    s = unicodedata.normalize("NFKC", str(name)).casefold()
    return re.sub(r"[^a-z0-9]", "", s) or s.strip()


def load_aliases() -> dict:
    """key -> canonical display name."""
    if not ALIASES_FILE.exists():
        return {}
    data = json.loads(ALIASES_FILE.read_text(encoding="utf-8"))
    out = {}
    for canonical, others in data.items():
        if canonical.startswith("_") or not isinstance(others, list):
            continue  # "_note" style comment entries
        out[name_key(canonical)] = canonical
        for other in others:
            out[name_key(other)] = canonical
    return out


def hidden_profiles() -> set[str]:
    """Owner names omitted from the Owner profile selector while retained in league records."""
    if not ALIASES_FILE.exists():
        return set()
    data = json.loads(ALIASES_FILE.read_text(encoding="utf-8"))
    return {name_key(name) for name in data.get("_hidden_profiles", [])}


def normalize_owner_names(con) -> int:
    """Rewrite teams.owner to canonical names. Safe to run repeatedly. Returns rows changed."""
    aliases = load_aliases()
    counts = Counter({o: n for o, n in con.execute("SELECT owner, COUNT(*) FROM teams GROUP BY owner") if o})
    best = {}  # key -> most common raw spelling (used when there's no alias entry)
    for owner, n in counts.items():
        k = name_key(owner)
        mixed_case = owner != owner.upper() and owner != owner.lower()  # prefer "Zoe Smith" over ZOE SMITH / zoe smith
        rank = (n, mixed_case, len(owner), owner)
        if k not in best or rank > best[k][0]:
            best[k] = (rank, owner)
    changed = 0
    for owner in counts:
        k = name_key(owner)
        target = aliases.get(k) or best[k][1]
        if target != owner:
            changed += con.execute("UPDATE teams SET owner = ? WHERE owner = ?", (target, owner)).rowcount
    data = json.loads(ALIASES_FILE.read_text(encoding="utf-8")) if ALIASES_FILE.exists() else {}
    for rule in data.get("_team_owner_overrides", []):
        rows = con.execute(
            "SELECT team_id, team_name FROM teams WHERE league_id = ? AND season = ?",
            (int(rule["league_id"]), int(rule["season"])),
        ).fetchall()
        team_ids = [team_id for team_id, team_name in rows if name_key(team_name) == name_key(rule["team_name"])]
        for team_id in team_ids:
            changed += con.execute(
                "UPDATE teams SET owner = ? WHERE league_id = ? AND season = ? AND team_id = ? AND owner != ?",
                (rule["owner"], int(rule["league_id"]), int(rule["season"]), team_id, rule["owner"]),
            ).rowcount
    if changed:
        con.commit()
    return changed
