#!/usr/bin/env python3
"""
Fetch latest NBA.com Power Rankings, list the top 4 teams,
and show each one's opponents over the next 7 days.

Dependencies:
  pip install requests beautifulsoup4

What changed vs. previous version:
- Power Rankings parser now supports the current "#1" / team-link pattern.
- Index page lookup also checks /news/category/power-rankings.
- Fixed date window to exactly 7 days (today + next 6).
- More robust team-name mapping and scoreboard parsing.
"""

import re
import json
import datetime as dt
from collections import defaultdict
from typing import List, Dict, Tuple

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/127.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nba.com/",
    "Origin": "https://www.nba.com",
    "Connection": "keep-alive",
}

JSON_HEADERS = {
    **BASE_HEADERS,
    "Accept": "application/json, text/plain, */*",
}

INDEX_CANDIDATES = [
    "https://www.nba.com/news/category/power-rankings",
    "https://www.nba.com/news/power-rankings",
]

def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(BASE_HEADERS)
    # requests.Session doesn't really store a default timeout; pass per-call.
    return s

def get_latest_power_rankings_url(session: requests.Session) -> str:
    """
    Find the latest Power Rankings article URL from NBA.com.
    Tries both the category page and the legacy index.
    """
    candidates = []
    for url in INDEX_CANDIDATES:
        r = session.get(url, timeout=20)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            text = (a.get_text(" ") or "").strip().lower()
            if "/news/" in href and "power-rankings" in href:
                candidates.append(href)
            elif "power rankings" in text and "/news/" in href:
                candidates.append(href)

    if not candidates:
        raise RuntimeError("Could not locate a Power Rankings link on nba.com.")

    # Dedup + absolutize while preserving order
    seen = set()
    abs_candidates = []
    for href in candidates:
        if href.startswith("/"):
            href = "https://www.nba.com" + href
        if href not in seen:
            seen.add(href)
            abs_candidates.append(href)

    return abs_candidates[0]

def fetch_teams_index(session: requests.Session) -> Dict[str, dict]:
    """
    Fetch teams metadata from data.nba.com for mapping names -> ids.
    Tries current year +/- 1 to straddle preseason/season boundaries.
    """
    year_today = dt.date.today().year
    data = None  # <-- ensure it's defined even if no request returns 200

    for year in (year_today, year_today - 1, year_today + 1):
        try:
            r = session.get(
                f"https://data.nba.com/prod/v2/{year}/teams.json",
                headers=JSON_HEADERS,
                timeout=20,
            )
            if r.status_code == 200:
                tmp = r.json()
                # make sure the payload actually has the structure we expect
                if isinstance(tmp, dict) and tmp.get("league"):
                    data = tmp
                    break
        except Exception:
            # try the next year candidate
            continue

    if data is None:
        raise RuntimeError("Could not download teams index from data.nba.com.")

    teams = (
            data.get("league", {}).get("standard", [])
            or data.get("league", {}).get("vegas", [])
            or []
    )

    index: Dict[str, dict] = {}
    for t in teams:
        if not t.get("isNBAFranchise", True):
            continue
        full = t.get("fullName") or ""
        nick = t.get("nickname") or ""
        tri  = t.get("tricode") or ""
        tid  = t.get("teamId") or ""
        if not (full and tri and tid):
            continue

        keys = {
            full.lower(),
            nick.lower(),
            full.replace("LA ", "Los Angeles ").replace("L.A.", "Los Angeles").lower(),
            re.sub(r"\s+", " ", full.lower()),
        }
        for k in keys:
            index[k] = {
                "teamId": tid,
                "tricode": tri,
                "fullName": full,
                "nickname": nick,
            }

    # helpful aliases
    def alias(a, canonical):
        if canonical.lower() in index:
            index[a.lower()] = index[canonical.lower()]

    alias("la clippers", "Los Angeles Clippers")
    alias("la lakers", "Los Angeles Lakers")
    alias("ny knicks", "New York Knicks")
    alias("golden state", "Golden State Warriors")
    alias("portland blazers", "Portland Trail Blazers")

    return index

def team_name_candidates(name: str) -> List[str]:
    name = name.strip()
    variants = {
        name,
        name.replace("L.A.", "Los Angeles"),
        name.replace("LA ", "Los Angeles "),
        re.sub(r"\s+", " ", name),
    }
    toks = name.split()
    for k in range(1, min(3, len(toks)) + 1):
        variants.add(" ".join(toks[-k:]))
    return [v.lower() for v in sorted(variants)]

def parse_top_teams_from_article(session: requests.Session, url: str,
                                 teams_index: Dict[str, dict],
                                 top_n: int = 4) -> List[str]:
    """
    Parse the Power Rankings article and extract the top N teams.
    Supports current format: '#1' line followed by a linked team name,
    and falls back to older '1. Team' text patterns.
    """
    r = session.get(url, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    article = soup.find("article") or soup

    # Build a set of canonical team full names for fast checking.
    valid_full_names = {v["fullName"] for v in teams_index.values()}

    # --- Primary strategy: '#1' markers followed by a linked team name ---
    results: Dict[int, str] = {}
    texts = list(article.descendants)

    def is_rank_marker(node, rank: int) -> bool:
        if isinstance(node, NavigableString):
            return node.strip() == f"#{rank}"
        if isinstance(node, Tag):
            txt = (node.get_text("", strip=True) or "")
            # some tags may contain exactly '#1' etc.
            return txt == f"#{rank}"
        return False

    for rank in range(1, top_n + 1):
        found_name = None
        # scan for the marker
        for i, node in enumerate(texts):
            if is_rank_marker(node, rank):
                # walk forward through "next elements" after the marker,
                # stop if we hit the next marker.
                for j in range(i + 1, min(i + 400, len(texts))):
                    nxt = texts[j]
                    # stop if we reach the next rank marker
                    if is_rank_marker(nxt, rank + 1):
                        break
                    if isinstance(nxt, Tag) and nxt.name == "a":
                        nm = nxt.get_text(" ", strip=True)
                        if nm in valid_full_names:
                            found_name = nm
                            break
                break
        if found_name:
            results[rank] = found_name

    # If we got enough via the new format, return in order.
    if len(results) >= top_n:
        return [results[r] for r in range(1, top_n + 1)]

    # --- Fallback: older "1. Boston Celtics" style in headings/paragraphs ---
    teams = []
    for tag_name in ["h1", "h2", "h3", "h4", "h5", "p", "li", "strong", "div", "span"]:
        for t in article.find_all(tag_name):
            txt = " ".join((t.get_text(" ") or "").split())
            m = re.match(r"^\s*(\d{1,2})\.\s+([A-Za-z .&'–—\-0-9]+)", txt)
            if m:
                rank = int(m.group(1))
                name = m.group(2).strip()
                name = re.split(r"\s+[–—-]\s+| \(|  - ", name)[0].strip()
                name = name.replace("LA ", "Los Angeles ").replace("L.A.", "Los Angeles")
                teams.append((rank, name))
    by_rank = {}
    for rnk, name in teams:
        if 1 <= rnk <= 30 and rnk not in by_rank:
            by_rank[rnk] = name
    top = [by_rank[r] for r in sorted(by_rank)[:top_n]]

    if len(top) < top_n:
        raise RuntimeError(f"Could not extract top {top_n} teams from the article.")
    return top

def date_range_days(start: dt.date, days: int) -> List[dt.date]:
    # Exactly `days` days starting at `start` (exclusive end)
    return [start + dt.timedelta(days=i) for i in range(days)]

def fetch_scoreboard_for_date(session: requests.Session, d: dt.date) -> dict:
    url = f"https://data.nba.com/prod/v2/{d.strftime('%Y%m%d')}/scoreboard.json"
    r = session.get(url, headers=JSON_HEADERS, timeout=20)
    if r.status_code == 404:
        return {"games": []}
    r.raise_for_status()
    data = r.json()
    # Normalize common shapes
    if "games" in data:
        return data
    if "g" in data:
        return {"games": data["g"]}
    return {"games": []}

def upcoming_opponents_next_week(
        session: requests.Session,
        team_ids: List[str],
        teams_index: Dict[str, dict],
        days: int = 7,
) -> Dict[str, List[Tuple[dt.date, str, str]]]:
    """
    For each teamId, collect (date, opponent_full_name, 'HOME'/'AWAY') for the next `days` days.
    """
    by_team: Dict[str, List[Tuple[dt.date, str, str]]] = defaultdict(list)
    today = dt.date.today()
    for d in date_range_days(today, days):
        sb = fetch_scoreboard_for_date(session, d)
        games = sb.get("games", [])
        for g in games:
            # accommodate multiple JSON variants
            h = g.get("hTeam") or g.get("homeTeam") or g.get("h") or {}
            v = g.get("vTeam") or g.get("awayTeam") or g.get("v") or {}
            hid = (h.get("teamId") or h.get("tid") or "").strip()
            vid = (v.get("teamId") or v.get("tid") or "").strip()
            h_tri = h.get("triCode") or h.get("tri") or ""
            v_tri = v.get("triCode") or v.get("tri") or ""

            def full_by_tid_or_tri(tid: str, tri: str) -> str:
                for entry in teams_index.values():
                    if entry["teamId"] == tid:
                        return entry["fullName"]
                for entry in teams_index.values():
                    if entry["tricode"] == tri:
                        return entry["fullName"]
                return tri or tid or "Unknown"

            if hid in team_ids:
                opp_full = full_by_tid_or_tri(vid, v_tri)
                by_team[hid].append((d, opp_full, "HOME"))
            if vid in team_ids:
                opp_full = full_by_tid_or_tri(hid, h_tri)
                by_team[vid].append((d, opp_full, "AWAY"))

    for tid in by_team:
        by_team[tid].sort(key=lambda x: x[0])
    return by_team

def main():
    session = make_session()

    # 1) Find latest Power Rankings article
    pr_url = get_latest_power_rankings_url(session)

    # 2) Build teams index first (used by the parser)
    teams_index = fetch_teams_index(session)

    # 3) Parse top 4 teams from the article
    top4 = parse_top_teams_from_article(session, pr_url, teams_index, top_n=4)

    # 4) Map article names -> teamIds
    resolved, unresolved = [], []
    for name in top4:
        match = None
        for key in team_name_candidates(name):
            if key in teams_index:
                match = teams_index[key]
                break
        (resolved if match else unresolved).append(match or {"fullName": name})

    if unresolved:
        print("WARNING: Could not map these team names to NBA IDs; they will be skipped in schedule lookup:")
        for u in unresolved:
            print(f"  - {u['fullName']}")
        print()

    team_ids = [r["teamId"] for r in resolved if "teamId" in r]

    # 5) Pull next-week opponents (today + next 6 days = 7 total)
    schedule = upcoming_opponents_next_week(session, team_ids, teams_index, days=7)

    # 6) Print results
    print(f"Latest NBA.com Power Rankings article:\n  {pr_url}\n")
    print("Top 4 teams and opponents in the next 7 days:\n")

    for r in resolved:
        team_name = r["fullName"]
        tid = r.get("teamId")
        print(f"{team_name}:")
        if not tid or tid not in schedule or not schedule[tid]:
            print("  (No games in the next 7 days)")
        else:
            for d, opp, ha in schedule[tid]:
                print(f"  {d.isoformat()} — {'vs' if ha=='HOME' else '@'} {opp}")
        print()

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}")