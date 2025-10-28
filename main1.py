#!/usr/bin/env python3
"""
Fetch latest NBA.com Power Rankings, list the top 4 teams,
and show each one's opponents over the next 7 days.

Dependencies:
  pip install requests beautifulsoup4

Notes:
- NBA sites can block non-browser requests. We send reasonable headers.
- The Power Rankings page format can change; the parser uses multiple heuristics.
- Schedule data comes from data.nba.com daily scoreboards (public JSON).
"""

import re
import sys
import json
import time
import math
import html
import datetime as dt
from collections import defaultdict
from typing import List, Dict, Tuple, Optional

import requests
from bs4 import BeautifulSoup

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

POWER_RANKINGS_INDEX = "https://www.nba.com/news/power-rankings"


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(BASE_HEADERS)
    s.timeout = 20  # type: ignore[attr-defined]
    return s


def get_latest_power_rankings_url(session: requests.Session) -> str:
    """
    Find the latest Power Rankings article URL from nba.com/news/power-rankings.
    Fallback: scan anchors that look like a Power Rankings story.
    """
    resp = session.get(POWER_RANKINGS_INDEX)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    # Heuristic 1: look for article cards under the listing page
    candidates = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = " ".join(a.get_text(" ").split()).strip().lower()
        if "/news/" in href and "power-rankings" in href:
            candidates.append(href)
        elif "power rankings" in text and "/news/" in href:
            candidates.append(href)

    # Make absolute and de-dupe while preserving order
    seen = set()
    abs_candidates = []
    for href in candidates:
        if href.startswith("/"):
            href = "https://www.nba.com" + href
        if href not in seen:
            seen.add(href)
            abs_candidates.append(href)

    if not abs_candidates:
        raise RuntimeError("Could not locate a Power Rankings article link on nba.com.")

    # The first one on the listing page is typically the latest
    return abs_candidates[0]


def parse_top_teams_from_article(session: requests.Session, url: str, top_n: int = 4) -> List[str]:
    """
    Parse the Power Rankings article and extract the top N team full names.
    Tries several patterns because formatting varies week-to-week.
    """
    resp = session.get(url)
    resp.raise_for_status()
    html_text = resp.text
    soup = BeautifulSoup(html_text, "html.parser")

    # Prefer content area
    article_root = soup.find("article") or soup

    # Pattern A: headings like "1. Boston Celtics"
    teams = []
    for tag_name in ["h1", "h2", "h3", "h4", "h5", "p", "li", "strong"]:
        for t in article_root.find_all(tag_name):
            txt = " ".join(t.get_text(" ").split())
            m = re.match(r"^\s*(\d{1,2})\.\s+([A-Za-z .&'-–—\-0-9]+)", txt)
            if m:
                rank = int(m.group(1))
                name = m.group(2).strip()
                # Clean trailing annotations like "— Last week: 1" or "(+2)"
                name = re.split(r"\s+[–—-]\s+| \(|  - ", name)[0].strip()
                # Common tidy-ups
                name = name.replace("LA ", "Los Angeles ").replace("L.A.", "Los Angeles")
                # Avoid accidental captures like "1. Notes"
                if any(word in name.lower() for word in ["notes", "takeaways", "rankings"]):
                    continue
                teams.append((rank, name))

    # If we didn't catch enough, try a looser regex over the whole text
    if len({r for r, _ in teams}) < top_n:
        text = " ".join(article_root.get_text(" ").split())
        for m in re.finditer(r"(\d{1,2})\.\s+([A-Za-z .&'-–—\-0-9]+?)(?:\s+[–—-]\s+|\s+\(|\s+Last week|\s+LW:|\s+Record|\s+\d{1,2}\.)", text):
            rank = int(m.group(1))
            name = m.group(2).strip()
            name = re.split(r"\s+[–—-]\s+| \(|  - ", name)[0].strip()
            name = name.replace("LA ", "Los Angeles ").replace("L.A.", "Los Angeles")
            teams.append((rank, name))

    # Deduplicate by rank, sort, and keep top_n
    by_rank = {}
    for rank, name in teams:
        if 1 <= rank <= 30 and rank not in by_rank:
            by_rank[rank] = name
    top = [by_rank[r] for r in sorted(by_rank.keys())[:top_n]]

    if len(top) < top_n:
        raise RuntimeError(f"Could not extract top {top_n} teams from the article.")
    return top


def fetch_teams_index(session: requests.Session) -> Dict[str, dict]:
    """
    Fetch teams metadata from data.nba.com for mapping fullName/nickname -> {teamId, tricode, fullName, nickname}
    Try current year +/- 1 to be safe in preseason/postseason.
    """
    year_candidates = [dt.date.today().year, dt.date.today().year - 1, dt.date.today().year + 1]
    data = None
    for year in year_candidates:
        url = f"https://data.nba.com/prod/v2/{year}/teams.json"
        try:
            r = session.get(url, headers=JSON_HEADERS)
            if r.status_code == 200:
                data = r.json()
                break
        except Exception:
            continue
    if not data:
        raise RuntimeError("Could not download teams index from data.nba.com.")

    teams = data.get("league", {}).get("standard", []) or data.get("league", {}).get("vegas", []) or []
    index: Dict[str, dict] = {}
    for t in teams:
        if not t.get("isNBAFranchise", True):
            continue
        full = t.get("fullName", "")
        nick = t.get("nickname", "")
        tri = t.get("tricode", "")
        tid = t.get("teamId", "")
        if not (full and tri and tid):
            continue
        # multiple keys for robust lookup
        for key in {
            full.lower(),
            nick.lower(),
            full.replace("LA ", "Los Angeles ").replace("L.A.", "Los Angeles").lower(),
            full.replace("Saint", "St.").lower(),
            re.sub(r"\s+", " ", full.lower()),
        }:
            index[key] = {"teamId": tid, "tricode": tri, "fullName": full, "nickname": nick}
    # Add a few common aliases
    def add_alias(alias: str, canonical_full: str):
        if canonical_full.lower() in index:
            index[alias.lower()] = index[canonical_full.lower()]

    add_alias("la clippers", "Los Angeles Clippers")
    add_alias("la lakers", "Los Angeles Lakers")
    add_alias("ny knicks", "New York Knicks")
    add_alias("portland blazers", "Portland Trail Blazers")
    add_alias("golden state", "Golden State Warriors")

    return index


def normalize_team_name_for_lookup(name: str) -> List[str]:
    """
    Produce several candidate keys for matching article team names to NBA index.
    """
    name = name.strip()
    variants = {
        name,
        name.replace("L.A.", "Los Angeles"),
        name.replace("LA ", "Los Angeles "),
        re.sub(r"\s+", " ", name),
    }

    # Try to derive nickname by dropping leading city words (keep last 1-3 tokens)
    tokens = name.split()
    for k in range(1, min(3, len(tokens)) + 1):
        nick = " ".join(tokens[-k:])
        variants.add(nick)

    return list({v.lower() for v in variants})


def date_range_days(start: dt.date, days: int) -> List[dt.date]:
    return [start + dt.timedelta(days=i) for i in range(days + 1)]  # inclusive


def fetch_scoreboard_for_date(session: requests.Session, d: dt.date) -> dict:
    url = f"https://data.nba.com/prod/v2/{d.strftime('%Y%m%d')}/scoreboard.json"
    r = session.get(url, headers=JSON_HEADERS)
    if r.status_code == 404:
        # No games that day, return empty
        return {"games": []}
    r.raise_for_status()
    data = r.json()
    # Some versions use "g" for games; normalize
    if "games" in data:
        return data
    elif "g" in data:
        return {"games": data["g"]}
    return {"games": []}


def upcoming_opponents_next_week(
        session: requests.Session,
        team_ids: List[str],
        teams_index: Dict[str, dict],
        days: int = 7,
) -> Dict[str, List[Tuple[dt.date, str, str]]]:
    """
    For each teamId, collect (date, opponent_full_name, 'HOME'/'AWAY') for the next `days` days (inclusive).
    """
    by_team: Dict[str, List[Tuple[dt.date, str, str]]] = defaultdict(list)
    today = dt.date.today()
    for d in date_range_days(today, days):
        sb = fetch_scoreboard_for_date(session, d)
        games = sb.get("games", [])
        for g in games:
            # Different shapes exist; defend with .get()
            h = g.get("hTeam") or g.get("h", {})
            v = g.get("vTeam") or g.get("v", {})
            hid = (h.get("teamId") or h.get("tid") or "").strip()
            vid = (v.get("teamId") or v.get("tid") or "").strip()
            h_tri = h.get("triCode") or h.get("tri") or ""
            v_tri = v.get("triCode") or v.get("tri") or ""

            # Get canonical full names via tricode or teamId
            def full_by_tid(tid: str, fallback_tri: str) -> str:
                for entry in teams_index.values():
                    if entry["teamId"] == tid:
                        return entry["fullName"]
                # fallback by triCode
                for entry in teams_index.values():
                    if entry.get("tricode") == fallback_tri:
                        return entry["fullName"]
                return fallback_tri or tid

            if hid in team_ids:
                opp_full = full_by_tid(vid, v_tri)
                by_team[hid].append((d, opp_full, "HOME"))
            if vid in team_ids:
                opp_full = full_by_tid(hid, h_tri)
                by_team[vid].append((d, opp_full, "AWAY"))
    # Sort each team's games by date
    for tid in by_team:
        by_team[tid].sort(key=lambda x: x[0])
    return by_team


def main():
    session = make_session()

    # 1) Find the latest Power Rankings article
    pr_url = get_latest_power_rankings_url(session)

    # 2) Parse top 4 teams
    top4 = parse_top_teams_from_article(session, pr_url, top_n=4)

    # 3) Build teams index and map article names -> teamIds
    teams_index = fetch_teams_index(session)

    resolved = []
    unresolved = []
    for name in top4:
        matched = None
        for key in normalize_team_name_for_lookup(name):
            if key in teams_index:
                matched = teams_index[key]
                break
        if matched is None:
            unresolved.append(name)
        else:
            resolved.append(matched)

    if unresolved:
        print("WARNING: Could not confidently map these teams to NBA ids; they will be skipped:")
        for u in unresolved:
            print(f"  - {u}")
        print()

    if not resolved:
        raise SystemExit("No teams could be mapped. Exiting.")

    team_ids = [r["teamId"] for r in resolved]

    # 4) Pull next-week opponents
    schedule = upcoming_opponents_next_week(session, team_ids, teams_index, days=7)

    # 5) Pretty-print results
    print(f"Latest NBA.com Power Rankings article:\n  {pr_url}\n")
    print("Top 4 teams and opponents in the next 7 days:\n")

    full_by_tid = {r["teamId"]: r["fullName"] for r in resolved}
    for tid in team_ids:
        team_name = full_by_tid[tid]
        print(f"{team_name}:")
        games = schedule.get(tid, [])
        if not games:
            print("  (No games in the next 7 days)")
        else:
            for d, opp, ha in games:
                print(f"  {d.isoformat()} — vs {opp}" if ha == "HOME" else f"  {d.isoformat()} — @ {opp}")
        print()

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # Surface a readable error; optionally re-raise for debugging
        print(f"ERROR: {e}")
        # raise