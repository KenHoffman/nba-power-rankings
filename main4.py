#!/usr/bin/env python3
"""
Power Rankings (nba.com) -> Top 4 teams -> Opponents in the next 7 days.

Dependencies:
  pip install requests beautifulsoup4

Why this version is more reliable:
- No calls to data.nba.com (which often blocks scripts).
- Uses cdn.nba.com/static JSON for the full season schedule.
- Team-name parsing works with current "#1" markers and older "1. Team" text.
"""

import re
import datetime as dt
from typing import List, Dict, Tuple
from collections import defaultdict

import requests
from bs4 import BeautifulSoup, Tag, NavigableString

# ---------- HTTP ----------
BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/127.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nba.com/",
}
JSON_HEADERS = {
    **BASE_HEADERS,
    "Accept": "application/json, text/plain, */*",
}

INDEX_CANDIDATES = [
    "https://www.nba.com/news/category/power-rankings",
    "https://www.nba.com/news/power-rankings",
]
SCHEDULE_URL = "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json"

# Canonical full team names (stable; used for parsing & name matching)
CANON_TEAMS = [
    "Atlanta Hawks","Boston Celtics","Brooklyn Nets","Charlotte Hornets","Chicago Bulls",
    "Cleveland Cavaliers","Dallas Mavericks","Denver Nuggets","Detroit Pistons","Golden State Warriors",
    "Houston Rockets","Indiana Pacers","Los Angeles Clippers","Los Angeles Lakers","Memphis Grizzlies",
    "Miami Heat","Milwaukee Bucks","Minnesota Timberwolves","New Orleans Pelicans","New York Knicks",
    "Oklahoma City Thunder","Orlando Magic","Philadelphia 76ers","Phoenix Suns",
    "Portland Trail Blazers","Sacramento Kings","San Antonio Spurs","Toronto Raptors","Utah Jazz","Washington Wizards",
]
# Helpful aliases -> canonical
ALIASES = {
    "la clippers": "los angeles clippers",
    "la lakers": "los angeles lakers",
    "ny knicks": "new york knicks",
    "portland blazers": "portland trail blazers",
    "gs warriors": "golden state warriors",
    "okc thunder": "oklahoma city thunder",
    "phx suns": "phoenix suns",
    "76ers": "philadelphia 76ers",
}

def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(BASE_HEADERS)
    return s

# ---------- Name normalization ----------
def _clean(s: str) -> str:
    s = s.lower().strip()
    # normalize punctuation and whitespace
    s = re.sub(r"[^\w\s]", " ", s)   # drop punctuation
    s = re.sub(r"\s+", " ", s)
    # common expansions
    s = re.sub(r"\bla\b", "los angeles", s)  # "LA Clippers" -> "los angeles clippers"
    s = s.strip()
    # alias map
    if s in ALIASES:
        s = ALIASES[s]
    return s

CANON_SET = {_clean(t) for t in CANON_TEAMS}

def is_team_name(text: str) -> bool:
    return _clean(text) in CANON_SET

def canonicalize(text: str) -> str:
    """Return the canonical full team name if we can; else return the original text."""
    key = _clean(text)
    if key in CANON_SET:
        # return with proper capitalization from CANON_TEAMS
        for t in CANON_TEAMS:
            if _clean(t) == key:
                return t
    return text.strip()

# ---------- Power Rankings scraping ----------
def get_latest_power_rankings_url(session: requests.Session) -> str:
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

    # absolutize & dedupe
    seen, out = set(), []
    for href in candidates:
        if href.startswith("/"):
            href = "https://www.nba.com" + href
        if href not in seen:
            seen.add(href)
            out.append(href)
    return out[0]

def parse_top_teams_from_article(session: requests.Session, url: str, top_n: int = 4) -> List[str]:
    r = session.get(url, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    article = soup.find("article") or soup

    # Strategy A: look for '#1', '#2', ... markers and take the next anchor linking to a team
    results: Dict[int, str] = {}

    def is_rank_marker(node, rank: int) -> bool:
        if isinstance(node, NavigableString):
            return node.strip() == f"#{rank}"
        if isinstance(node, Tag):
            txt = (node.get_text("", strip=True) or "")
            return txt == f"#{rank}"
        return False

    nodes = list(article.descendants)
    for rank in range(1, top_n + 1):
        found = None
        for i, node in enumerate(nodes):
            if is_rank_marker(node, rank):
                # scan forward until next marker
                for j in range(i + 1, min(i + 400, len(nodes))):
                    nxt = nodes[j]
                    if is_rank_marker(nxt, rank + 1):
                        break
                    if isinstance(nxt, Tag) and nxt.name == "a" and nxt.has_attr("href"):
                        href = nxt["href"]
                        text = (nxt.get_text(" ", strip=True) or "").strip()
                        # prefer links to /team/
                        if "/team/" in href and is_team_name(text):
                            found = canonicalize(text)
                            break
                break
        if found:
            results[rank] = found

    if len(results) >= top_n:
        return [results[r] for r in range(1, top_n + 1)]

    # Strategy B: older "1. Team Name" style
    candidates = []
    for tag in article.find_all(["h1","h2","h3","h4","h5","p","li","strong","div","span"]):
        txt = " ".join((tag.get_text(" ") or "").split())
        m = re.match(r"^\s*(\d{1,2})\.\s+(.+?)\s*(?:[–—-]\s+.*|\(.*|$)", txt)
        if m:
            rank = int(m.group(1))
            name = m.group(2).strip()
            if is_team_name(name):
                candidates.append((rank, canonicalize(name)))

    by_rank: Dict[int, str] = {}
    for rnk, nm in candidates:
        if 1 <= rnk <= 30 and rnk not in by_rank:
            by_rank[rnk] = nm
    top = [by_rank[r] for r in sorted(by_rank)[:top_n]]
    if len(top) < top_n:
        raise RuntimeError(f"Could not extract top {top_n} teams from the article at {url}")
    return top

# ---------- Schedule lookup (cdn.nba.com) ----------
def load_season_schedule(session: requests.Session) -> List[dict]:
    """
    Returns a list of dicts:
      {date: datetime.date, home_full: 'City Name', away_full: 'City Name'}
    using cdn.nba.com/static JSON.
    """
    r = session.get(SCHEDULE_URL, headers=JSON_HEADERS, timeout=30)
    r.raise_for_status()
    data = r.json()
    game_dates = (data.get("leagueSchedule") or {}).get("gameDates") or []
    out = []
    for gd in game_dates:
        dstr = gd.get("gameDate")  # e.g., '2025-10-28'
        try:
            d = dt.datetime.strptime(dstr, "%Y-%m-%d").date()
        except Exception:
            continue
        for g in gd.get("games", []):
            h = g.get("homeTeam", {}) or {}
            a = g.get("awayTeam", {}) or {}
            # teamCity may be "LA" for the LA teams, so full names can be "LA Clippers" / "LA Lakers"
            home_full = f"{h.get('teamCity','').strip()} {h.get('teamName','').strip()}".strip()
            away_full = f"{a.get('teamCity','').strip()} {a.get('teamName','').strip()}".strip()
            # canonicalize common variants (e.g., LA -> Los Angeles)
            home_full = canonicalize(home_full)
            away_full = canonicalize(away_full)
            out.append({"date": d, "home": home_full, "away": away_full})
    return out

def upcoming_opponents_next_week(
        schedule: List[dict],
        teams: List[str],
        days: int = 7,
) -> Dict[str, List[Tuple[dt.date, str, str]]]:
    """
    For each team (canonical full name), list (date, opponent, HOME/AWAY) for the next `days` days (today inclusive).
    """
    today = dt.date.today()
    end = today + dt.timedelta(days=days - 1)
    want = {_clean(t) for t in teams}

    by_team: Dict[str, List[Tuple[dt.date, str, str]]] = defaultdict(list)
    for game in schedule:
        d = game["date"]
        if d < today or d > end:
            continue
        h, a = game["home"], game["away"]
        hc, ac = _clean(h), _clean(a)
        if hc in want:
            by_team[h].append((d, a, "HOME"))
        if ac in want:
            by_team[a].append((d, h, "AWAY"))

    # sort results
    for k in list(by_team.keys()):
        by_team[k].sort(key=lambda x: x[0])
    return by_team

# ---------- Main ----------
def main():
    session = make_session()

    # 1) Find latest Power Rankings article
    pr_url = get_latest_power_rankings_url(session)

    # 2) Extract top 4 teams (canonical names)
    top4 = parse_top_teams_from_article(session, pr_url, top_n=4)

    # 3) Load full season schedule once; filter to next 7 days
    schedule = load_season_schedule(session)

    # 4) Build per-team opponents
    opponents = upcoming_opponents_next_week(schedule, top4, days=7)

    # 5) Output
    print(f"Latest NBA.com Power Rankings article:\n  {pr_url}\n")
    print("Top 4 teams and opponents in the next 7 days:\n")
    for team in top4:
        print(team + ":")
        games = opponents.get(team, [])
        if not games:
            print("  (No games in the next 7 days)")
        else:
            for d, opp, ha in games:
                print(f"  {d.isoformat()} — {'vs' if ha=='HOME' else '@'} {opp}")
        print()

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}")