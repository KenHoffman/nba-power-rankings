#!/usr/bin/env python3
"""
NBA.com Power Rankings -> Top 4 teams -> Opponents in the next 7 days.

Fixes vs prior version:
- Never treats the category page as an article.
- Chooses the freshest real article by validating candidates and reading publish time.
- Extra parsing fallbacks for different article formats.
- Still avoids data.nba.com; uses cdn.nba.com schedule.

Usage:
  pip install requests beautifulsoup4
  python power_rankings_next_week.py
"""

import re
import datetime as dt
from typing import List, Dict, Tuple, Optional
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
JSON_HEADERS = {**BASE_HEADERS, "Accept": "application/json, text/plain, */*"}

INDEX_CANDIDATES = [
    "https://www.nba.com/news/category/power-rankings",
    "https://www.nba.com/news/power-rankings",
]
SCHEDULE_URL = "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json"

# ---------- Team names / normalization ----------
CANON_TEAMS = [
    "Atlanta Hawks","Boston Celtics","Brooklyn Nets","Charlotte Hornets","Chicago Bulls",
    "Cleveland Cavaliers","Dallas Mavericks","Denver Nuggets","Detroit Pistons","Golden State Warriors",
    "Houston Rockets","Indiana Pacers","Los Angeles Clippers","Los Angeles Lakers","Memphis Grizzlies",
    "Miami Heat","Milwaukee Bucks","Minnesota Timberwolves","New Orleans Pelicans","New York Knicks",
    "Oklahoma City Thunder","Orlando Magic","Philadelphia 76ers","Phoenix Suns",
    "Portland Trail Blazers","Sacramento Kings","San Antonio Spurs","Toronto Raptors","Utah Jazz","Washington Wizards",
]
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

def _clean(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\bla\b", "los angeles", s)
    s = s.strip()
    return ALIASES.get(s, s)

CANON_SET = {_clean(t) for t in CANON_TEAMS}

def is_team_name(text: str) -> bool:
    return _clean(text) in CANON_SET

def canonicalize(text: str) -> str:
    key = _clean(text)
    if key in CANON_SET:
        for t in CANON_TEAMS:
            if _clean(t) == key:
                return t
    return text.strip()

# ---------- HTTP session ----------
def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(BASE_HEADERS)
    return s

# ---------- Article discovery ----------
def _absolutize(href: str) -> str:
    return href if href.startswith("http") else ("https://www.nba.com" + href if href.startswith("/") else href)

def _is_valid_article_href(href: str) -> bool:
    """
    Accept only real article slugs like /news/xxx-power-rankings-yyy
    Reject category/index slugs such as /news/category/power-rankings or /news/power-rankings
    """
    href_l = href.lower()
    if not href_l.startswith("/news/"):
        return False
    if "/category/" in href_l:
        return False
    if href_l.rstrip("/") == "/news/power-rankings":
        return False
    if "power-rankings" not in href_l:
        return False
    # Likely an article if there's at least one more segment after /news/
    return True

def _extract_publish_time(soup: BeautifulSoup) -> Optional[dt.datetime]:
    # Common OpenGraph/JSON-LD patterns
    meta = soup.find("meta", {"property": "article:published_time"}) or soup.find("meta", {"name": "publishDate"})
    if meta and meta.get("content"):
        for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
            try:
                return dt.datetime.strptime(meta["content"], fmt)
            except Exception:
                pass
    # Sometimes a <time datetime="...">
    t = soup.find("time")
    if t and t.get("datetime"):
        try:
            return dt.datetime.fromisoformat(t["datetime"].replace("Z", "+00:00"))
        except Exception:
            pass
    return None

def _looks_like_power_rankings_article(soup: BeautifulSoup) -> bool:
    art = soup.find("article") or soup
    text = " ".join((art.get_text(" ") or "").split())
    # Heuristics: ranking markers + several team names
    markers = sum(1 for m in re.finditer(r"(?:^|\s)#\d{1,2}(?:\s|$)", text))
    team_hits = sum(1 for t in CANON_TEAMS if t in text)
    return markers >= 2 or team_hits >= 10

def get_latest_power_rankings_article(session: requests.Session) -> str:
    candidates = []
    for url in INDEX_CANDIDATES:
        r = session.get(url, timeout=20)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.select("a[href]"):
            href = a.get("href", "")
            if _is_valid_article_href(href):
                candidates.append(_absolutize(href))

    # dedupe preserving order
    seen, ordered = set(), []
    for u in candidates:
        if u not in seen:
            seen.add(u)
            ordered.append(u)

    if not ordered:
        raise RuntimeError("Could not find any Power Rankings article links on index pages.")

    # Score/validate candidates and pick the freshest plausible article
    scored = []
    for u in ordered[:12]:  # check a handful; enough for freshness
        try:
            rr = session.get(u, timeout=20)
            if rr.status_code != 200:
                continue
            soup = BeautifulSoup(rr.text, "html.parser")
            if not _looks_like_power_rankings_article(soup):
                continue
            ts = _extract_publish_time(soup) or dt.datetime.min.replace(tzinfo=None)
            scored.append((ts, u))
        except Exception:
            continue

    if not scored:
        # last resort: take first candidate
        return ordered[0]

    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][1]

# ---------- Parsing top teams from article ----------
def parse_top_teams_from_article(session: requests.Session, url: str, top_n: int = 4) -> List[str]:
    r = session.get(url, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    article = soup.find("article") or soup

    # Strategy A: '#1' markers then nearest team link (/team/) or team name
    results: Dict[int, str] = {}
    nodes = list(article.descendants)

    def is_rank_marker(node, rank: int) -> bool:
        if isinstance(node, NavigableString):
            return node.strip() == f"#{rank}"
        if isinstance(node, Tag):
            txt = (node.get_text("", strip=True) or "")
            return txt == f"#{rank}"
        return False

    for rank in range(1, top_n + 1):
        found = None
        for i, node in enumerate(nodes):
            if is_rank_marker(node, rank):
                for j in range(i + 1, min(i + 600, len(nodes))):
                    nxt = nodes[j]
                    if is_rank_marker(nxt, rank + 1):
                        break
                    if isinstance(nxt, Tag):
                        # Prefer a team page link
                        if nxt.name == "a" and nxt.get("href") and "/team/" in nxt["href"]:
                            txt = (nxt.get_text(" ", strip=True) or "")
                            if is_team_name(txt):
                                found = canonicalize(txt)
                                break
                        # Otherwise, scan text content in this node for a team name
                        txt = (nxt.get_text(" ", strip=True) or "")
                        if txt:
                            for t in CANON_TEAMS:
                                if t in txt:
                                    found = t
                                    break
                        if found:
                            break
                if found:
                    results[rank] = found
                    break

    if len(results) >= top_n:
        return [results[r] for r in range(1, top_n + 1)]

    # Strategy B: headings/paragraphs "1. Team Name"
    candidates = []
    for tag in article.find_all(["h1","h2","h3","h4","h5","p","li","strong","div","span"]):
        txt = " ".join((tag.get_text(" ") or "").split())
        m = re.match(r"^\s*(\d{1,2})\s*[\.\)\-–—:]\s+(.+?)\s*(?:[–—-]\s+.*|\(.*|$)", txt)
        if m:
            rnk = int(m.group(1))
            name = m.group(2).strip()
            # if there is an <a> to /team/ inside, prefer its text
            a = tag.find("a", href=True)
            if a and "/team/" in a["href"]:
                name = a.get_text(" ", strip=True) or name
            if is_team_name(name):
                candidates.append((rnk, canonicalize(name)))

    by_rank: Dict[int, str] = {}
    for rnk, nm in candidates:
        if 1 <= rnk <= 30 and rnk not in by_rank:
            by_rank[rnk] = nm
    if by_rank and len(by_rank) >= top_n:
        return [by_rank[r] for r in sorted(by_rank)[:top_n]]

    # Strategy C: line-based scan — look for lines that start with a rank and contain a team within 2 lines
    lines = [l.strip() for l in (article.get_text("\n") or "").splitlines()]
    for rnk in range(1, top_n + 1):
        pat = re.compile(rf"^(?:#|No\.\s*)?{rnk}(?:[\.\)\-–—: ]|$)")
        for i, line in enumerate(lines):
            if pat.match(line):
                window = " ".join(lines[i:i+3])
                for t in CANON_TEAMS:
                    if t in window:
                        by_rank[rnk] = t
                        break
                if rnk in by_rank:
                    break
    if len([k for k in by_rank if 1 <= k <= top_n]) >= top_n:
        return [by_rank[r] for r in range(1, top_n + 1)]

    raise RuntimeError(f"Could not extract top {top_n} teams from the article at {url}")

# ---------- Schedule (cdn.nba.com) ----------
def load_season_schedule(session: requests.Session) -> List[dict]:
    r = session.get(SCHEDULE_URL, headers=JSON_HEADERS, timeout=30)
    r.raise_for_status()
    data = r.json()
    game_dates = (data.get("leagueSchedule") or {}).get("gameDates") or []
    out = []
    for gd in game_dates:
        dstr = gd.get("gameDate")
        try:
            d = dt.datetime.strptime(dstr, "%Y-%m-%d").date()
        except Exception:
            continue
        for g in gd.get("games", []):
            h = g.get("homeTeam", {}) or {}
            a = g.get("awayTeam", {}) or {}
            home_full = canonicalize(f"{h.get('teamCity','').strip()} {h.get('teamName','').strip()}".strip())
            away_full = canonicalize(f"{a.get('teamCity','').strip()} {a.get('teamName','').strip()}".strip())
            out.append({"date": d, "home": home_full, "away": away_full})
    return out

def upcoming_opponents_next_week(
        schedule: List[dict],
        teams: List[str],
        days: int = 7,
) -> Dict[str, List[Tuple[dt.date, str, str]]]:
    today = dt.date.today()
    end = today + dt.timedelta(days=days - 1)
    want = {_clean(t) for t in teams}

    by_team: Dict[str, List[Tuple[dt.date, str, str]]] = defaultdict(list)
    for game in schedule:
        d = game["date"]
        if not (today <= d <= end):
            continue
        h, a = game["home"], game["away"]
        if _clean(h) in want:
            by_team[h].append((d, a, "HOME"))
        if _clean(a) in want:
            by_team[a].append((d, h, "AWAY"))
    for k in list(by_team.keys()):
        by_team[k].sort(key=lambda x: x[0])
    return by_team

# ---------- Main ----------
def main():
    session = make_session()

    # 1) Find latest Power Rankings article (validated)
    pr_url = get_latest_power_rankings_article(session)

    # 2) Extract top 4 teams
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