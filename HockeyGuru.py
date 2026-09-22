#!/usr/bin/env python3
"""
============================================================
  HOCKEY GURU - NHL Nightly Goal-Scorer Cheat Sheet
============================================================

  WHAT IT DOES:
  Answers one question: who is built to score TONIGHT?

  For every game on the slate it looks at:
    - How many shots does the skater generate, 5-on-5 and on the power play?
    - How often does the opponent get shorthanded, and how bad is its PK?
    - How many shots does the opponent give up, and of what quality?
    - Who is in the opposing net -- and is it the backup?
    - What does Vegas say the team will score?
    - Is the player healthy? Is the team on a back-to-back?

  Output: a ranked board of skaters by ANYTIME-GOAL probability with the
  SHOTS-ON-GOAL floor beside it, plus a goalie board. No salary cap. No
  roster. Just: here are the guys built to light the lamp.

  SETUP:
    pip install requests
    Optional: set ODDS_API_KEY for game totals + moneylines
              (free at https://the-odds-api.com/)
              set GITHUB_TOKEN to deploy the board to GitHub Pages

  RUNNING:
    python HockeyGuru.py                 # tonight's slate
    python HockeyGuru.py --date 2026-01-15 --backtest --no-deploy
                                         # rebuild a past slate and grade it
============================================================
"""

import os
import re
import io
import csv
import sys
import json
import math
import time
import requests
import webbrowser

from datetime import datetime, date, timedelta
from concurrent.futures import ThreadPoolExecutor

try:
    from zoneinfo import ZoneInfo
    EASTERN = ZoneInfo("America/New_York")   # handles the EDT/EST switch itself
except Exception:                            # pragma: no cover
    from datetime import timezone
    EASTERN = timezone(timedelta(hours=-5))


def to_eastern(iso_utc):
    """NHL publishes puck drops in UTC; a 10:00 PM ET game reads as 02:00 tomorrow otherwise."""
    try:
        return datetime.fromisoformat(iso_utc.replace("Z", "+00:00")).astimezone(EASTERN)
    except Exception:
        return None


def now_et():
    return datetime.now(EASTERN)


# ============================================================
#  CONFIG
# ============================================================

OUTPUT_FOLDER      = "reports"
# Every slate's calls are logged here, then graded against the box scores.
# Without this the grades are unfalsifiable -- no way to know if any of the
# weighting works.
PREDICTIONS_FOLDER = "predictions"
# Completed-season stats never change, so they are cached to disk. Delete the
# folder to force a re-pull.
CACHE_FOLDER       = "cache"

# GitHub Actions has no browser; the workflow sets GITHUB_ACTIONS=true.
AUTO_OPEN_BROWSER  = os.environ.get("GITHUB_ACTIONS", "") != "true"
DELAY              = 0.12
GAME_LOG_WORKERS   = 3         # parallel NHL API pulls for player game logs (6 drew 429s)

# --- Slate --------------------------------------------------------------------
MIN_GAME_START_HOUR_ET = 0     # drop games that start before this ET hour (0 = keep all)
# DraftKings' main NHL slate is the 7:00 PM ET wave. Games at/after 6:30 PM ET
# count as "main"; the board has a toggle to hide the rest.
MAIN_SLATE_START_ET    = 18.5
# Exhibition games are only used when there is NO regular-season slate that
# day. That makes the last week of camp a free end-to-end dry run.
INCLUDE_PRESEASON      = True

TOP_PER_POS = {"C": 8, "W": 12, "D": 8, "G": 8}

# DraftKings NHL Classic scoring. Verify against DK's rules page each season.
DK = {
    "goal": 8.5, "assist": 5.0, "sog": 1.5, "block": 1.3,
    "hat_trick": 3.0, "sog5_bonus": 3.0, "blk3_bonus": 3.0, "pts3_bonus": 3.0,
    "g_win": 6.0, "g_save": 0.7, "g_ga": -3.5, "g_shutout": 4.0, "g_otl": 2.0,
    "g_35saves": 3.0,
}

GRADE_COLORS = {"A+": "#00e676", "A": "#69f0ae", "B+": "#b9f6ca",
                "B": "#fff176", "C": "#ffb74d", "D": "#ff5252", "OUT": "#ff4444"}

# Grade anchors are ABSOLUTE, not slate-relative: a 40% chance to score is an
# A+ on a 2-game Tuesday and on a 15-game Saturday alike. The scorecard shows
# whether the probabilities hold; move these only if it says so.
SKATER_GRADES = [("A+", 40.0), ("A", 33.0), ("B+", 27.0), ("B", 21.0), ("C", 15.0)]
# Goalies are graded on projected DK points (win + saves - goals against).
# A good spot projects ~14-15 (favourite, busy net, sound goalie); 17+ is rare.
GOALIE_GRADES = [("A+", 17.0), ("A", 15.5), ("B+", 14.0), ("B", 12.5), ("C", 10.5)]

# --- Odds API budget ------------------------------------------------------------
# Free tier is 500 credits/month. One /odds call with h2h+totals for the WHOLE
# slate costs 2 credits, so a 3-hour cache keeps a full month of daily boards
# under 100 credits. The /sports list is free and reports the balance.
ODDS_API_KEY     = os.environ.get("ODDS_API_KEY", "")
ODDS_CACHE_HOURS = 3
ODDS_RESERVE     = 40

GITHUB_TOKEN  = os.environ.get("GITHUB_TOKEN", "")
GITHUB_USER   = "christopher2smithtrade-sketch"
GITHUB_REPO   = "HockeyGuru"
GITHUB_BRANCH = "main"
PAGES_URL     = f"https://{GITHUB_USER}.github.io/{GITHUB_REPO}/"
ACTIONS_URL   = f"https://github.com/{GITHUB_USER}/{GITHUB_REPO}/actions/workflows/hockey.yml"

# -- ntfy push notifications (same app as H-Bomb and Gridiron) ------------------
# Subscribe to this topic in the ntfy app. Every push carries a "Run workflow"
# button that opens the Actions page, so a stale run is one tap from a re-run.
NTFY_ENABLED        = True
NTFY_TOPIC          = "hockey-guru"
NTFY_NOTIFY_SUCCESS = True      # False = alert only when something breaks

NHL  = "https://api-web.nhle.com/v1"
REST = "https://api.nhle.com/stats/rest/en"
ESPN = "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl"
MP   = "https://moneypuck.com/moneypuck/playerData/seasonSummary"
UA   = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128 Safari/537.36"}

# abbrev -> (full name, primary colour)
TEAMS = {
    "ANA": ("Anaheim Ducks",         "#F47A38"), "BOS": ("Boston Bruins",         "#FFB81C"),
    "BUF": ("Buffalo Sabres",        "#003087"), "CAR": ("Carolina Hurricanes",   "#CC0000"),
    "CBJ": ("Columbus Blue Jackets", "#002654"), "CGY": ("Calgary Flames",        "#D2001C"),
    "CHI": ("Chicago Blackhawks",    "#CF0A2C"), "COL": ("Colorado Avalanche",    "#6F263D"),
    "DAL": ("Dallas Stars",          "#006847"), "DET": ("Detroit Red Wings",     "#CE1126"),
    "EDM": ("Edmonton Oilers",       "#FF4C00"), "FLA": ("Florida Panthers",      "#C8102E"),
    "LAK": ("Los Angeles Kings",     "#A2AAAD"), "MIN": ("Minnesota Wild",        "#154734"),
    "MTL": ("Montreal Canadiens",    "#AF1E2D"), "NJD": ("New Jersey Devils",     "#CE0E2D"),
    "NSH": ("Nashville Predators",   "#FFB81C"), "NYI": ("New York Islanders",    "#00539B"),
    "NYR": ("New York Rangers",      "#0038A8"), "OTT": ("Ottawa Senators",       "#DA1A32"),
    "PHI": ("Philadelphia Flyers",   "#F74902"), "PIT": ("Pittsburgh Penguins",   "#FCB514"),
    "SEA": ("Seattle Kraken",        "#99D9D9"), "SJS": ("San Jose Sharks",       "#006D75"),
    "STL": ("St. Louis Blues",       "#002F87"), "TBL": ("Tampa Bay Lightning",   "#002868"),
    "TOR": ("Toronto Maple Leafs",   "#00205B"), "UTA": ("Utah Mammoth",          "#71AFE5"),
    "VAN": ("Vancouver Canucks",     "#00843D"), "VGK": ("Vegas Golden Knights",  "#B4975A"),
    "WPG": ("Winnipeg Jets",         "#041E42"), "WSH": ("Washington Capitals",   "#C8102E"),
}
# Books and DailyFaceoff use full names; the nickname (last word) is unique
# league-wide. Extra aliases cover the Utah rename and accent-free spellings.
NICK_TO_ABBR = {v[0].split()[-1].lower(): k for k, v in TEAMS.items()}
NICK_TO_ABBR.update({"club": "UTA", "utah": "UTA", "hc": "UTA", "arizona": "UTA", "coyotes": "UTA"})
# MoneyPuck spells a few clubs its own way.
MP_TEAM = {"T.B": "TBL", "N.J": "NJD", "L.A": "LAK", "S.J": "SJS", "ARI": "UTA", "UTA": "UTA"}


def team_abbr_from_name(name):
    if not name:
        return None
    words = re.sub(r"[^A-Za-z ]", "", name).lower().split()
    for w in reversed(words):
        if w in NICK_TO_ABBR:
            return NICK_TO_ABBR[w]
    return None


def display_color(hex_color):
    """Navy jerseys vanish on the dark board; lift anything too dark toward white."""
    try:
        r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    except Exception:
        return "#8b949e"
    lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255
    if lum < 0.35:
        r, g, b = (int(c + (255 - c) * 0.5) for c in (r, g, b))
    return f"#{r:02x}{g:02x}{b:02x}"


def notify(title, message, tags="", priority="default", click=None, run_button=True):
    """Push via ntfy.sh. A notification failure must never break a run."""
    if not NTFY_ENABLED:
        return
    try:
        headers = {"Title": title.encode("utf-8"), "Priority": priority}
        if tags:
            headers["Tags"] = tags
        if click:
            headers["Click"] = click
        actions = []
        if click:
            actions.append(f"view, Open board, {click}")
        if run_button:
            actions.append(f"view, Run workflow, {ACTIONS_URL}")
        if actions:
            headers["Actions"] = "; ".join(actions)
        requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=message.encode("utf-8"),
                      headers=headers, timeout=15)
    except Exception as e:
        print(f"  [!] Notify error: {e}")


def verify_live(timestamp, tries=4):
    """
    Confirm the live Pages site actually shows this run. A successful API push
    does not guarantee Pages rebuilt -- H-Bomb hit exactly this, and a stale
    board with no warning is worse than a failed run.
    """
    for attempt in range(1, tries + 1):
        try:
            time.sleep(20 if attempt > 1 else 8)
            r = requests.get(PAGES_URL, params={"cb": int(time.time())},
                             headers={"Cache-Control": "no-cache"}, timeout=20)
            if r.status_code == 200 and timestamp in r.text:
                print(f"  [OK] Live site verified (attempt {attempt})")
                return True
        except Exception as e:
            print(f"  verify attempt {attempt}/{tries}: {e}")
    print("  [!] LIVE SITE NOT UPDATED -- push succeeded but Pages is stale")
    return False


# ============================================================
#  SEASON / HELPERS
# ============================================================

def season_ids(d):
    """(NHL seasonId, MoneyPuck year) for the season that date d belongs to."""
    y = d.year if d.month >= 9 else d.year - 1
    return y * 10000 + (y + 1), y


# The NHL API answers 429 once a few threads hammer it. One shared pause
# timestamp makes every worker back off together instead of each one
# rediscovering the limit on its own.
_PAUSE_UNTIL = 0.0
_PAUSE_LOCK = __import__("threading").Lock()


def get_json(url, params=None, tries=5, timeout=20, headers=None):
    """GET with retries. One transient timeout must not drop a whole team from the board."""
    global _PAUSE_UNTIL
    for attempt in range(tries):
        wait = _PAUSE_UNTIL - time.time()
        if wait > 0:
            time.sleep(wait)
        try:
            r = requests.get(url, params=params, timeout=timeout, headers=headers)
            if r.status_code == 429:
                delay = float(r.headers.get("Retry-After") or 0) or 6.0 * (attempt + 1)
                with _PAUSE_LOCK:
                    _PAUSE_UNTIL = max(_PAUSE_UNTIL, time.time() + delay)
                print(f"  ... rate limited, pausing {delay:.0f}s")
                continue
            r.raise_for_status()
            return r.json()
        except Exception:
            if attempt == tries - 1:
                raise
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"gave up after {tries} tries: {url}")


def fetch(url, params=None, label="", headers=None):
    try:
        data = get_json(url, params=params, headers=headers)
        time.sleep(DELAY)
        return data
    except Exception as e:
        print(f"  [!] {label or url}: {e}")
        return {}


def _norm_name(n):
    """Lowercase, strip accents/suffixes/punctuation, for cross-source name matching."""
    import unicodedata
    n = unicodedata.normalize("NFKD", n or "").encode("ascii", "ignore").decode()
    n = n.lower()
    n = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b\.?", "", n)
    n = re.sub(r"[^a-z ]", "", n)
    return " ".join(n.split())


# Two cache tiers. cache/season/ holds completed-season pulls that never change
# and is committed by the workflow so Actions runs start warm. cache/daily/ is
# everything that goes stale (odds, rosters, game logs, box scores) and stays
# out of git -- committing it would grow the repo by megabytes a day.
def _cache_path(key, permanent=False):
    return os.path.join(CACHE_FOLDER, "season" if permanent else "daily", f"{key}.json")


def cache_load(key, max_age_hours=None, permanent=False):
    """Cached JSON for key, or None if missing / unreadable / older than max_age_hours."""
    try:
        with open(_cache_path(key, permanent), "r", encoding="utf-8") as f:
            data = json.load(f)
        if max_age_hours is not None:
            age_h = (time.time() - data.get("_fetched_at", 0)) / 3600
            if age_h > max_age_hours:
                return None
        return data
    except Exception:
        return None


def cache_save(key, data, permanent=False):
    """Write data to the cache (stamped); failures are non-fatal."""
    try:
        path = _cache_path(key, permanent)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if isinstance(data, dict):
            data["_fetched_at"] = time.time()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception as e:
        print(f"  [!] Cache write {key}: {e}")


def mmss(s):
    """'17:23' -> 1043 seconds."""
    try:
        m, sec = str(s).split(":")
        return int(m) * 60 + int(sec)
    except Exception:
        return 0


def poisson_tail(lam, k):
    """P(X >= k) for X ~ Poisson(lam)."""
    if lam <= 0:
        return 0.0
    p, cdf = math.exp(-lam), 0.0
    for i in range(k):
        cdf += p
        p *= lam / (i + 1)
    return max(0.0, min(1.0, 1.0 - cdf))


def clip(x, lo, hi):
    return max(lo, min(hi, x))


def regress(rate, weight, prior, k):
    """Shrink an observed rate toward a prior; k is the sample size at which they split 50/50."""
    return (rate * weight + prior * k) / (weight + k) if weight + k > 0 else prior


# ============================================================
#  DATA FETCHING
# ============================================================

def get_slate(slate_date):
    """Tonight's games from the NHL schedule feed. Returns (games, preseason_flag)."""
    data = fetch(f"{NHL}/schedule/{slate_date.isoformat()}", label="schedule")
    partners   = {p.get("partnerId"): (p.get("name") or "") for p in data.get("oddsPartners", [])}
    dk_partner = next((pid for pid, n in partners.items() if "draftkings" in n.lower()), None)
    day    = next((d for d in data.get("gameWeek", []) if d.get("date") == slate_date.isoformat()), None)
    raw    = (day or {}).get("games", [])
    picked = [g for g in raw if g.get("gameType") == 2]
    preseason = False
    if not picked and INCLUDE_PRESEASON:
        picked = [g for g in raw if g.get("gameType") == 1]
        preseason = bool(picked)

    def moneyline(team):
        # The NHL feed carries partner prices close to puck drop; DraftKings is
        # the US partner. Absent until a day or so before the game.
        for o in team.get("odds") or []:
            if dk_partner is not None and o.get("providerId") == dk_partner:
                return o.get("value")
        return None

    games = []
    for g in picked:
        start = to_eastern(g.get("startTimeUTC", ""))
        hour  = start.hour + start.minute / 60 if start else 19.0
        if hour < MIN_GAME_START_HOUR_ET:
            continue
        home, away = g["homeTeam"], g["awayTeam"]
        state = g.get("gameState", "FUT")
        games.append({
            "game_id":   g["id"], "type": g.get("gameType", 2),
            "home":      home["abbrev"], "away": away["abbrev"],
            "home_name": TEAMS.get(home["abbrev"], ((home.get("commonName") or {}).get("default", home["abbrev"]),))[0],
            "away_name": TEAMS.get(away["abbrev"], ((away.get("commonName") or {}).get("default", away["abbrev"]),))[0],
            "start_et":  start,
            "start_str": start.strftime("%I:%M %p").lstrip("0") if start else "TBD",
            "state":     state,
            "started":   state not in ("FUT", "PRE"),
            "final":     state in ("OFF", "FINAL"),
            "main_slate": hour >= MAIN_SLATE_START_ET,
            "venue":     (g.get("venue") or {}).get("default", ""),
            "ml_home_nhl": moneyline(home), "ml_away_nhl": moneyline(away),
            "home_score": home.get("score"), "away_score": away.get("score"),
        })
    games.sort(key=lambda x: x["start_et"].timestamp() if x["start_et"] else 0)
    return games, preseason


# ── Odds ─────────────────────────────────────────────────────

def american_to_prob(ml):
    ml = float(str(ml).replace("+", ""))
    return -ml / (-ml + 100) if ml < 0 else 100 / (ml + 100)


def _odds_value_to_prob(v):
    """The NHL feed quotes US partners in American odds ('-150') and European ones in decimals ('1.65')."""
    s = str(v).strip()
    try:
        if s.startswith(("+", "-")) or abs(float(s)) >= 100:
            return american_to_prob(s)
        d = float(s)
        return 1 / d if d > 1 else None
    except Exception:
        return None


def implied_goals(total, p_home):
    """
    Split a game total into team goals from the home win probability.
    A Poisson matchup on a ~6-goal total moves the win probability roughly
    0.16 per goal of expected margin, so margin = (p - 0.5) / 0.16.
    """
    d = clip((p_home - 0.5) / 0.16, -1.8, 1.8)
    return round((total + d) / 2, 2), round((total - d) / 2, 2)


def odds_api_slate(slate_date):
    """
    Totals + moneylines for every upcoming NHL game from The Odds API, one call
    for the whole board (2 credits), cached ODDS_CACHE_HOURS. Keyed
    "HOME|AWAY|YYYY-MM-DD" -- the date matters because home-and-home sets put
    the same pair on consecutive nights.
    """
    if not ODDS_API_KEY:
        print("  [!] ODDS_API_KEY not set -- using default totals (6.0) and home edge")
        return {}
    key = f"odds_{slate_date.isoformat()}"
    cached = cache_load(key, ODDS_CACHE_HOURS)
    if cached:
        age_h = (time.time() - cached.get("_fetched_at", 0)) / 3600
        print(f"  Using cached odds ({age_h:.1f}h old)")
        return cached["lines"]
    try:
        # The /sports list is free AND reports the balance, so the budget is
        # checked at zero cost before the paid call.
        r = requests.get("https://api.the-odds-api.com/v4/sports/",
                         params={"apiKey": ODDS_API_KEY}, timeout=15)
        remaining = int(r.headers.get("x-requests-remaining", 0) or 0)
        if remaining - 2 < ODDS_RESERVE:
            stale = cache_load(key)
            print(f"  [!] Odds API below reserve ({remaining} left) -- "
                  f"{'keeping stale odds' if stale else 'no odds this run'}")
            return stale["lines"] if stale else {}
        r = requests.get(
            "https://api.the-odds-api.com/v4/sports/icehockey_nhl/odds/",
            params={"apiKey": ODDS_API_KEY, "regions": "us", "markets": "h2h,totals",
                    "oddsFormat": "american", "bookmakers": "draftkings,fanduel,betmgm"},
            timeout=25,
        )
        r.raise_for_status()
        lines = {}
        for ev in r.json():
            h = team_abbr_from_name(ev.get("home_team"))
            a = team_abbr_from_name(ev.get("away_team"))
            start = to_eastern(ev.get("commence_time", ""))
            if not h or not a or not start:
                continue
            books = ev.get("bookmakers", [])
            # This is a DraftKings tool, so use DraftKings lines when offered
            book = next((b for b in books if b.get("key") == "draftkings"), None) or (books[0] if books else None)
            if not book:
                continue
            total = ph = pa = None
            for mkt in book.get("markets", []):
                if mkt["key"] == "totals":
                    for o in mkt.get("outcomes", []):
                        if o.get("name") == "Over" and o.get("point"):
                            total = float(o["point"])
                elif mkt["key"] == "h2h":
                    for o in mkt.get("outcomes", []):
                        if o.get("name") == ev.get("home_team"):
                            ph = american_to_prob(o["price"])
                        elif o.get("name") == ev.get("away_team"):
                            pa = american_to_prob(o["price"])
            lines[f"{h}|{a}|{start.date().isoformat()}"] = {
                "total": total, "p_home_raw": ph, "p_away_raw": pa, "book": book.get("key", "")}
        remaining = int(r.headers.get("x-requests-remaining", remaining) or remaining)
        print(f"  Odds API: priced {len(lines)} upcoming games -- {remaining} credits left this month")
        cache_save(key, {"lines": lines})
        return lines
    except Exception as e:
        print(f"  [!] Odds API: {e}")
        stale = cache_load(key)
        return stale["lines"] if stale else {}


def get_odds(games, slate_date):
    """Per game: total, devigged home win probability, implied goals per side."""
    # The book only prices upcoming games; a backtest date would burn credits for nothing
    api = odds_api_slate(slate_date) if slate_date >= now_et().date() else {}
    out = {}
    for g in games:
        total, ph, src = 6.0, None, "default"
        line = api.get(f"{g['home']}|{g['away']}|{slate_date.isoformat()}")
        if line:
            if line.get("total"):
                total, src = line["total"], line.get("book") or "odds-api"
            if line.get("p_home_raw") and line.get("p_away_raw"):
                ph = line["p_home_raw"] / (line["p_home_raw"] + line["p_away_raw"])
        if ph is None and g.get("ml_home_nhl") and g.get("ml_away_nhl"):
            a, b = _odds_value_to_prob(g["ml_home_nhl"]), _odds_value_to_prob(g["ml_away_nhl"])
            if a and b:
                ph = a / (a + b)
                if src == "default":
                    src = "nhl-dk (ML only)"
        if ph is None:
            ph = 0.54          # home ice alone is worth about four points of win probability
        ih, ia = implied_goals(total, ph)
        out[g["game_id"]] = {"total": total, "p_home": round(ph, 3), "p_away": round(1 - ph, 3),
                             "imp_home": ih, "imp_away": ia, "source": src}
    priced = sum(1 for v in out.values() if v["source"] != "default")
    print(f"  Lines for {priced}/{len(games)} games")
    return out


# ── Injuries ─────────────────────────────────────────────────

UNAVAILABLE = {"out", "injured reserve", "ir", "long term injured reserve", "ltir",
               "suspension", "suspended"}


def get_injuries():
    """ESPN's NHL injury feed -> {normalised name: {status, detail, return, comment}}."""
    data = fetch(f"{ESPN}/injuries", label="injuries")
    out = {}
    for team in data.get("injuries", []):
        for i in team.get("injuries", []):
            name   = (i.get("athlete") or {}).get("displayName", "")
            det    = i.get("details") or {}
            detail = " - ".join(x for x in (det.get("type", ""), det.get("detail", ""))
                                if x and x != "Not Specified")
            out[_norm_name(name)] = {
                "status":  i.get("status", "") or "",
                "detail":  detail,
                "return":  det.get("returnDate", "") or "",
                "comment": (i.get("shortComment") or "")[:140],
                "team":    team.get("displayName", ""),
            }
    return out


def is_unavailable(status):
    return (status or "").strip().lower() in UNAVAILABLE


# ── Starting goalies ─────────────────────────────────────────

def get_starting_goalies(slate_date):
    """
    DailyFaceoff's starting-goalie page, which embeds its data as JSON.
    {abbrev: {name, status: Confirmed|Projected, news, source, at}}.
    A third-party page, so any failure just means "no confirmations yet".
    """
    key = f"starters_{slate_date.isoformat()}"
    cached = cache_load(key, 0.5)
    if cached:
        return cached["starters"]
    out = {}
    try:
        r = requests.get(f"https://www.dailyfaceoff.com/starting-goalies/{slate_date.isoformat()}",
                         headers=UA, timeout=25)
        r.raise_for_status()
        m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', r.text, re.S)
        rows = json.loads(m.group(1))["props"]["pageProps"].get("data") or [] if m else []
        for row in rows:
            for side in ("home", "away"):
                abbr = team_abbr_from_name(row.get(f"{side}TeamName"))
                name = row.get(f"{side}GoalieName")
                if not abbr or not name:
                    continue
                strength = (row.get(f"{side}NewsStrengthName") or "").lower()
                out[abbr] = {
                    "name":   name,
                    "status": "Confirmed" if strength.startswith("confirm") else "Projected",
                    "news":   (row.get(f"{side}NewsDetails") or "").strip(),
                    "source": row.get(f"{side}NewsSourceName") or "",
                    "at":     row.get(f"{side}NewsCreatedAt") or "",
                }
        confirmed = sum(1 for v in out.values() if v["status"] == "Confirmed")
        print(f"  DailyFaceoff: {confirmed} confirmed / {len(out)} listed")
        cache_save(key, {"starters": out})
    except Exception as e:
        print(f"  [!] DailyFaceoff starters: {e}")
        stale = cache_load(key)
        if stale:
            return stale["starters"]
    return out


# ── Season stats (NHL stats API) ─────────────────────────────

def get_league_stats(season_id, current_season_id):
    """
    Every skater's and goalie's season line from the NHL stats API -- four
    calls for the whole league. Completed seasons cache for good; the live one
    refreshes every 6 h.
    """
    permanent = season_id < current_season_id
    key = f"rest_{season_id}"
    cached = cache_load(key, None if permanent else 6, permanent=permanent)
    if cached:
        return cached
    exp = f"seasonId={season_id} and gameTypeId=2"

    def pull(report):
        try:
            return get_json(f"{REST}/{report}", params={"limit": -1, "cayenneExp": exp}).get("data", [])
        except Exception as e:
            print(f"  [!] NHL stats {report} {season_id}: {e}")
            return []

    summary, toi = pull("skater/summary"), pull("skater/timeonice")
    realtime, goalies = pull("skater/realtime"), pull("goalie/summary")

    sk = {}
    for r in summary:
        pid = str(r["playerId"])
        sk[pid] = {"name": r.get("skaterFullName", ""),
                   "team": (r.get("teamAbbrevs") or "").split(",")[-1].strip(),
                   "pos": r.get("positionCode", ""),
                   "gp": r.get("gamesPlayed") or 0, "g": r.get("goals") or 0,
                   "a": r.get("assists") or 0, "pts": r.get("points") or 0,
                   "sog": r.get("shots") or 0, "ppg": r.get("ppGoals") or 0,
                   "ppp": r.get("ppPoints") or 0, "toi_pg": r.get("timeOnIcePerGame") or 0}
    for r in toi:
        s = sk.get(str(r["playerId"]))
        if s:
            s.update({"pp_toi_pg": r.get("ppTimeOnIcePerGame") or 0,
                      "ev_toi_pg": r.get("evTimeOnIcePerGame") or 0,
                      "sh_toi_pg": r.get("shTimeOnIcePerGame") or 0})
    for r in realtime:
        s = sk.get(str(r["playerId"]))
        if s:
            s.update({"blk": r.get("blockedShots") or 0, "hits": r.get("hits") or 0,
                      "att": r.get("totalShotAttempts") or 0})
    gl = {}
    for r in goalies:
        pid = str(r["playerId"])
        gl[pid] = {"name": r.get("goalieFullName", ""),
                   "team": (r.get("teamAbbrevs") or "").split(",")[-1].strip(),
                   "gp": r.get("gamesPlayed") or 0, "gs": r.get("gamesStarted") or 0,
                   "w": r.get("wins") or 0, "l": r.get("losses") or 0, "otl": r.get("otLosses") or 0,
                   "sv": r.get("saves") or 0, "sa": r.get("shotsAgainst") or 0,
                   "ga": r.get("goalsAgainst") or 0, "so": r.get("shutouts") or 0}
    data = {"season": season_id, "skaters": sk, "goalies": gl}
    print(f"  NHL stats {season_id}: {len(sk)} skaters, {len(gl)} goalies")
    if sk:
        cache_save(key, data, permanent=permanent)
    return data


# ── MoneyPuck (expected goals) ───────────────────────────────

def _mp_csv(url):
    r = requests.get(url, headers=UA, timeout=90)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return list(csv.DictReader(io.StringIO(r.text)))


def _f(row, col):
    try:
        return float(row.get(col) or 0)
    except Exception:
        return 0.0


def get_moneypuck(year, current_year):
    """
    MoneyPuck season summaries: per-situation shot quality (xG) for skaters,
    goalies and teams, plus 5v5 line combos. Completed seasons cache for good;
    the live one refreshes every 12 h and simply does not exist until a few
    games are in the books.
    """
    permanent = year < current_year
    key = f"mp_{year}"
    cached = cache_load(key, None if permanent else 12, permanent=permanent)
    if cached:
        return cached
    try:
        sk_rows = _mp_csv(f"{MP}/{year}/regular/skaters.csv")
        if sk_rows is None:
            print(f"  MoneyPuck {year}: not published yet")
            return None
        g_rows = _mp_csv(f"{MP}/{year}/regular/goalies.csv") or []
        t_rows = _mp_csv(f"{MP}/{year}/regular/teams.csv") or []
        l_rows = _mp_csv(f"{MP}/{year}/regular/lines.csv") or []
    except Exception as e:
        print(f"  [!] MoneyPuck {year}: {e}")
        return cache_load(key, permanent=permanent)

    skaters = {}
    for r in sk_rows:
        d = skaters.setdefault(r["playerId"], {"name": r["name"], "pos": r["position"],
                                               "team": MP_TEAM.get(r["team"], r["team"])})
        d[r["situation"]] = {
            "gp": _f(r, "games_played"), "toi": _f(r, "icetime"),
            "sog": _f(r, "I_F_shotsOnGoal"), "g": _f(r, "I_F_goals"), "xg": _f(r, "I_F_xGoals"),
            "att": _f(r, "I_F_shotAttempts"), "hd": _f(r, "I_F_highDangerShots"),
            "a1": _f(r, "I_F_primaryAssists"), "a2": _f(r, "I_F_secondaryAssists"),
            "blk": _f(r, "shotsBlockedByPlayer"),
        }
    goalies = {}
    for r in g_rows:
        d = goalies.setdefault(r["playerId"], {"name": r["name"],
                                               "team": MP_TEAM.get(r["team"], r["team"])})
        d[r["situation"]] = {
            "gp": _f(r, "games_played"), "toi": _f(r, "icetime"),
            "xga": _f(r, "xGoals"), "ga": _f(r, "goals"), "sa": _f(r, "ongoal"),
            "ua": _f(r, "unblocked_shot_attempts"),
            "hd": _f(r, "highDangerShots"), "hdg": _f(r, "highDangerGoals"),
        }
    teams = {}
    for r in t_rows:
        t = MP_TEAM.get(r["team"], r["team"])
        teams.setdefault(t, {})[r["situation"]] = {
            "gp": _f(r, "games_played"), "toi": _f(r, "iceTime"),
            "xgf": _f(r, "xGoalsFor"), "xga": _f(r, "xGoalsAgainst"),
            "sf": _f(r, "shotsOnGoalFor"), "sa": _f(r, "shotsOnGoalAgainst"),
            "gf": _f(r, "goalsFor"), "ga": _f(r, "goalsAgainst"),
            "attf": _f(r, "shotAttemptsFor"), "atta": _f(r, "shotAttemptsAgainst"),
            "hdf": _f(r, "highDangerShotsFor"), "hda": _f(r, "highDangerShotsAgainst"),
        }
    lines = {}
    for r in l_rows:
        if r.get("situation") != "5on5":
            continue
        t = MP_TEAM.get(r["team"], r["team"])
        lid = r["lineId"]
        # lineId is the member player ids concatenated; NHL ids are 7 digits
        lines.setdefault(t, []).append({
            "name": r["name"], "kind": r["position"],
            "ids": [lid[i:i + 7] for i in range(0, len(lid), 7)],
            "toi": _f(r, "icetime"), "gp": _f(r, "games_played"), "xgf": _f(r, "xGoalsFor"),
        })
    for t in lines:
        lines[t].sort(key=lambda x: -x["toi"])

    data = {"year": year, "skaters": skaters, "goalies": goalies, "teams": teams, "lines": lines}
    print(f"  MoneyPuck {year}: {len(skaters)} skaters, {len(goalies)} goalies, {len(teams)} teams")
    cache_save(key, data, permanent=permanent)
    return data


# ── Rosters / game logs / rest ───────────────────────────────

def get_rosters(teams):
    """Current roster for each team on the slate -> {abbrev: [player]}."""
    out = {}
    for t in teams:
        key = f"roster_{t}"
        cached = cache_load(key, 12)
        if cached:
            out[t] = cached["players"]
            continue
        data = fetch(f"{NHL}/roster/{t}/current", label=f"roster {t}")
        players = []
        for group, dk in (("forwards", None), ("defensemen", "D"), ("goalies", "G")):
            for p in data.get(group, []):
                pos  = p.get("positionCode", "")
                name = f"{(p.get('firstName') or {}).get('default', '')} {(p.get('lastName') or {}).get('default', '')}".strip()
                players.append({"pid": str(p["id"]), "name": name, "pos": pos,
                                "dk_pos": dk or ("C" if pos == "C" else "W"),
                                "num": p.get("sweaterNumber"), "team": t,
                                "headshot": p.get("headshot", "")})
        if players:
            cache_save(key, {"players": players})
        out[t] = players
    return out


def _norm_log(g):
    """One NHL game-log row (skater or goalie) -> compact dict."""
    row = {"date": g.get("gameDate", ""), "gid": g.get("gameId"),
           "opp": g.get("opponentAbbrev", ""), "home": g.get("homeRoadFlag") == "H",
           "toi": mmss(g.get("toi", "0:00"))}
    if "shotsAgainst" in g:
        row.update({"started": int(g.get("gamesStarted") or 0), "dec": g.get("decision") or "",
                    "sa": g.get("shotsAgainst") or 0, "ga": g.get("goalsAgainst") or 0})
    else:
        row.update({"g": g.get("goals") or 0, "a": g.get("assists") or 0,
                    "pts": g.get("points") or 0, "sog": g.get("shots") or 0,
                    "ppp": g.get("powerPlayPoints") or 0})
    return row


def get_game_logs(players, season_id, prior_id, gtype, slate_date, fetch_cur=True):
    """
    Per player: this season's games (refreshed every 4 h, one shared file) and
    last season's (cached for good). Newest first, and cut off before
    slate_date so a backtest never peeks at the future. fetch_cur=False skips
    the current season (exhibition logs are empty and cost a call each).
    """
    store_cur = cache_load(f"gamelogs_{season_id}", 4) or {"logs": {}}
    store_pri = cache_load(f"gamelogs_{prior_id}", permanent=True) or {"logs": {}}
    need = ([(p["pid"], season_id, gtype) for p in players if p["pid"] not in store_cur["logs"]]
            if fetch_cur else [])
    need += [(p["pid"], prior_id, 2) for p in players if p["pid"] not in store_pri["logs"]]

    def one(item):
        pid, sid, gt = item
        try:
            rows = get_json(f"{NHL}/player/{pid}/game-log/{sid}/{gt}").get("gameLog", [])
            time.sleep(0.15)             # ~6 calls/s across the pool keeps the NHL API happy
            return pid, sid, [_norm_log(g) for g in rows]
        except Exception as e:
            print(f"  [!] game log {pid} {sid}: {e}")
            return pid, sid, None

    if need:
        print(f"  Pulling {len(need)} game logs ({GAME_LOG_WORKERS} at a time)...")
        with ThreadPoolExecutor(max_workers=GAME_LOG_WORKERS) as ex:
            for pid, sid, games in ex.map(one, need):
                if games is None:
                    continue
                (store_cur if sid == season_id else store_pri)["logs"][pid] = games
        cache_save(f"gamelogs_{season_id}", store_cur)
        cache_save(f"gamelogs_{prior_id}", store_pri, permanent=True)

    cutoff = slate_date.isoformat()
    logs = {}
    for p in players:
        cur = [g for g in store_cur["logs"].get(p["pid"], []) if g["date"] < cutoff]
        pri = [g for g in store_pri["logs"].get(p["pid"], []) if g["date"] < cutoff]
        cur.sort(key=lambda g: g["date"], reverse=True)
        pri.sort(key=lambda g: g["date"], reverse=True)
        logs[p["pid"]] = {"cur": cur, "prior": pri}
    return logs


def get_team_context(teams, season_id, gtype, slate_date):
    """
    Rest situation per team from its season schedule: back-to-back, three games
    in four nights, days since the last game, and the previous game's id (to see
    who was in net).
    """
    out = {}
    for t in teams:
        key = f"sched_{t}_{season_id}"
        cached = cache_load(key, 12)
        if not cached:
            data  = fetch(f"{NHL}/club-schedule-season/{t}/{season_id}", label=f"schedule {t}")
            games = []
            for g in data.get("games", []):
                if g.get("gameType") not in (1, 2):
                    continue
                is_home = g["homeTeam"]["abbrev"] == t
                games.append({"date": g.get("gameDate", ""), "gid": g.get("id"),
                              "type": g.get("gameType"), "home": is_home,
                              "opp": g["awayTeam"]["abbrev"] if is_home else g["homeTeam"]["abbrev"]})
            cached = {"games": games}
            if games:
                cache_save(key, cached)
        games = [g for g in cached["games"] if g["type"] == gtype]
        d = slate_date.isoformat()
        y = (slate_date - timedelta(days=1)).isoformat()
        w = (slate_date - timedelta(days=4)).isoformat()
        prev = max((g for g in games if g["date"] < d), key=lambda g: g["date"], default=None)
        out[t] = {
            "b2b": any(g["date"] == y for g in games),
            "three_in_four": sum(1 for g in games if w < g["date"] < d) >= 2,
            "rest_days": (slate_date - date.fromisoformat(prev["date"])).days if prev else None,
            "prev_gid": prev["gid"] if prev else None,
        }
    return out


def rosters_from_boxscores(games, name_lookup):
    """
    Backtest only: the players who actually dressed that night, from the box
    scores. Today's roster feed would put every offseason mover on the wrong
    team and grade the model on lineups that never existed.
    """
    out = {}
    for g in games:
        box = get_boxscore(g["game_id"])
        for side in ("homeTeam", "awayTeam"):
            abbr = (box.get(side) or {}).get("abbrev", "")
            grp  = (box.get("playerByGameStats") or {}).get(side, {})
            for key, dk in (("forwards", None), ("defense", "D"), ("goalies", "G")):
                for p in grp.get(key, []):
                    pid = str(p["playerId"])
                    pos = p.get("position", "")
                    name = name_lookup.get(pid) or (p.get("name") or {}).get("default", pid)
                    out.setdefault(abbr, []).append({
                        "pid": pid, "name": name, "pos": pos,
                        "dk_pos": dk or ("C" if pos == "C" else "W"),
                        "num": p.get("sweaterNumber"), "team": abbr, "headshot": ""})
    return out


def get_boxscore(game_id):
    """Box score; kept once final so scoring never re-pulls a finished game."""
    key = f"box_{game_id}"
    cached = cache_load(key)
    if cached and cached.get("gameState") in ("OFF", "FINAL"):
        return cached
    data = fetch(f"{NHL}/gamecenter/{game_id}/boxscore", label=f"boxscore {game_id}")
    if data.get("gameState") in ("OFF", "FINAL"):
        cache_save(key, data)
    return data


# ============================================================
#  MATCHUP SCORING ENGINE
# ============================================================

# Where a skater with no track record starts, and what thin samples shrink
# toward. Shot rates are per 60 minutes at 5v5 / 5v4; shooting % is on goal.
PRIORS = {
    "F": {"ev_sog60": 6.2, "pp_sog60": 10.7, "ev_sh": 0.110, "pp_sh": 0.157,
          "ast_pg": 0.38, "blk_pg": 0.55, "toi": 14 * 60, "pp_toi": 60},
    "D": {"ev_sog60": 4.0, "pp_sog60":  8.2, "ev_sh": 0.050, "pp_sh": 0.085,
          "ast_pg": 0.28, "blk_pg": 1.35, "toi": 19 * 60, "pp_toi": 45},
}
# League rates used until MoneyPuck team data is loaded (2025-26 levels).
# other_sog / other_goals: the model builds 5v5 and 5v4 explicitly; 4v4, 3v3
# overtime, 5v3, shorthanded and empty-net play add ~10% of shots and ~19% of
# goals on top (measured from MoneyPuck team files), applied as multipliers.
LG_DEFAULT = {
    "sog60_5v5": 26.0, "xga60_5v5": 2.05, "xg_per_shot_5v5": 0.095, "attf60_5v5": 50.0,
    "pp_time_pg": 270.0, "pk_time_pg": 270.0, "pk_xga60": 6.6, "pp_xgf60": 6.6,
    "goals_pg": 3.08, "sog_pg": 27.5, "xg_per_shot": 0.112, "sv": 0.900, "goals_per_xg": 1.0,
    "other_sog": 1.10, "other_goals": 1.19,
    "xsh": {"F": {"5on5": 0.115, "5on4": 0.159}, "D": {"5on5": 0.047, "5on4": 0.081}},
    "fin": {"F": {"5on5": 0.98, "5on4": 0.99}, "D": {"5on5": 1.11, "5on4": 1.08}},
}


def grade_for(score, table):
    for g, cut in table:
        if score >= cut:
            return g
    return "D"


def league_baselines(mp):
    """League-average rates from a MoneyPuck team file (a completed season, ideally)."""
    lg = dict(LG_DEFAULT)
    if not mp:
        return lg
    tot = {}
    for sits in mp["teams"].values():
        for sit, v in sits.items():
            a = tot.setdefault(sit, {k: 0.0 for k in ("gp", "toi", "xgf", "xga", "sf", "sa", "gf", "ga", "attf", "atta")})
            for k in a:
                a[k] += v.get(k, 0)
    s5, pp, pk, al = tot.get("5on5"), tot.get("5on4"), tot.get("4on5"), tot.get("all")
    if s5 and s5["toi"]:
        lg["sog60_5v5"]       = s5["sf"] / s5["toi"] * 3600
        lg["xga60_5v5"]       = s5["xga"] / s5["toi"] * 3600
        lg["xg_per_shot_5v5"] = s5["xgf"] / max(1, s5["sf"])
        lg["attf60_5v5"]      = s5["attf"] / s5["toi"] * 3600
    if pp and pp["gp"]:
        lg["pp_time_pg"] = pp["toi"] / pp["gp"]
        lg["pp_xgf60"]   = pp["xgf"] / max(1, pp["toi"]) * 3600
    if pk and pk["gp"]:
        lg["pk_time_pg"] = pk["toi"] / pk["gp"]
        lg["pk_xga60"]   = pk["xga"] / max(1, pk["toi"]) * 3600
    if al and al["gp"]:
        lg["goals_pg"]     = al["gf"] / al["gp"]
        lg["sog_pg"]       = al["sf"] / al["gp"]
        lg["xg_per_shot"]  = al["xgf"] / max(1, al["sf"])
        lg["sv"]           = 1 - al["ga"] / max(1, al["sa"])
        lg["goals_per_xg"] = al["gf"] / max(1, al["xgf"])
        if s5 and pp:
            lg["other_sog"]   = al["sf"] / max(1, s5["sf"] + pp["sf"])
            lg["other_goals"] = al["gf"] / max(1, s5["gf"] + pp["gf"])
    # Position-level finishing: what a shot is worth (xG per SOG) and how
    # actual goals run against it. Forwards convert their xG almost exactly;
    # defensemen beat theirs by ~10% (xG models under-rate point shots
    # through traffic), so each position gets its own calibration.
    xsh = {"F": dict(lg["xsh"]["F"]), "D": dict(lg["xsh"]["D"])}
    fin = {"F": dict(lg["fin"]["F"]), "D": dict(lg["fin"]["D"])}
    for grp, poss in (("F", ("C", "L", "R")), ("D", ("D",))):
        for sit in ("5on5", "5on4"):
            g = s = xg = 0.0
            for v in mp.get("skaters", {}).values():
                if v.get("pos") in poss and sit in v:
                    g, s, xg = g + v[sit]["g"], s + v[sit]["sog"], xg + v[sit]["xg"]
            if s > 1000 and xg > 0:
                xsh[grp][sit] = xg / s
                fin[grp][sit] = g / xg
    lg["xsh"], lg["fin"] = xsh, fin
    return lg


def team_profiles(mp_cur, mp_prior, lg):
    """
    Per-team rates, this season blended into last as games accumulate (fully
    trusted after 20). Ranks are 1 = most generous to opposing shooters.
    """
    prof = {}
    for t in TEAMS:
        def sit(mp, s):
            return ((mp or {}).get("teams", {}).get(t) or {}).get(s) if mp else None
        cur_all = sit(mp_cur, "all")
        gp_cur  = cur_all["gp"] if cur_all else 0
        w = min(1.0, gp_cur / 20.0)

        def rate(s, num, den, per=3600, fallback=None):
            vals = []
            for mp, wt in ((mp_cur, w), (mp_prior, 1 - w)):
                v = sit(mp, s)
                if v and v.get(den) and wt > 0:
                    vals.append((v[num] / v[den] * per, wt))
            if not vals:
                return fallback
            tw = sum(wt for _, wt in vals)
            return sum(v * wt for v, wt in vals) / tw

        prof[t] = {
            "gp_cur":     gp_cur,
            "sa60_5v5":   rate("5on5", "sa", "toi", fallback=lg["sog60_5v5"]),
            "sf60_5v5":   rate("5on5", "sf", "toi", fallback=lg["sog60_5v5"]),
            "xga60_5v5":  rate("5on5", "xga", "toi", fallback=lg["xga60_5v5"]),
            "xgf60_5v5":  rate("5on5", "xgf", "toi", fallback=lg["xga60_5v5"]),
            "attf60_5v5": rate("5on5", "attf", "toi", fallback=lg["attf60_5v5"]),
            "sa_pg":      rate("all", "sa", "gp", per=1, fallback=lg["sog_pg"]),
            "sf_pg":      rate("all", "sf", "gp", per=1, fallback=lg["sog_pg"]),
            "gf_pg":      rate("all", "gf", "gp", per=1, fallback=lg["goals_pg"]),
            "ga_pg":      rate("all", "ga", "gp", per=1, fallback=lg["goals_pg"]),
            "xg_per_shot_for":     rate("all", "xgf", "sf", per=1, fallback=lg["xg_per_shot"]),
            "xg_per_shot_against": rate("all", "xga", "sa", per=1, fallback=lg["xg_per_shot"]),
            "pp_time_pg": rate("5on4", "toi", "gp", per=1, fallback=lg["pp_time_pg"]),
            "pk_time_pg": rate("4on5", "toi", "gp", per=1, fallback=lg["pk_time_pg"]),
            "pk_xga60":   rate("4on5", "xga", "toi", fallback=lg["pk_xga60"]),
            "pp_xgf60":   rate("5on4", "xgf", "toi", fallback=lg["pp_xgf60"]),
        }

    def rank(key):
        for i, t in enumerate(sorted(prof, key=lambda t: -prof[t][key]), 1):
            prof[t][f"{key}_rank"] = i
    for key in ("sa_pg", "sa60_5v5", "xga60_5v5", "pk_xga60", "pk_time_pg", "attf60_5v5", "ga_pg", "sf_pg"):
        rank(key)
    return prof


def _mean(xs):
    xs = list(xs)
    return sum(xs) / len(xs) if xs else 0.0


def skater_profile(p, rest_cur, rest_pri, mp_cur, mp_pri, logs, lg, injuries):
    """
    Everything the model needs about one skater, blended across this season
    and last. Volume (ice time, shot rate) trusts the current season quickly;
    finishing pools both seasons because shooting % needs hundreds of shots.
    """
    pos = "D" if p["pos"] == "D" else "F"
    pr  = PRIORS[pos]
    cur = rest_cur["skaters"].get(p["pid"])
    pri = rest_pri["skaters"].get(p["pid"])
    gp_cur = cur["gp"] if cur else 0
    gp_pri = pri["gp"] if pri else 0
    w = min(1.0, gp_cur / 20.0)

    def blend(fn, fallback):
        vals = []
        if cur and gp_cur:
            vals.append((fn(cur), w))
        if pri and gp_pri:
            vals.append((fn(pri), (1 - w) * min(1.0, gp_pri / 20.0)))
        tw = sum(x for _, x in vals)
        return sum(v * x for v, x in vals) / tw if tw > 0 else fallback

    toi_pg    = blend(lambda d: d.get("toi_pg", 0), pr["toi"])
    pp_toi_pg = blend(lambda d: d.get("pp_toi_pg", 0), pr["pp_toi"])
    ev_toi_pg = blend(lambda d: d.get("ev_toi_pg", 0), max(0, toi_pg - pp_toi_pg))
    sog_pg    = blend(lambda d: d["sog"] / d["gp"], 0.0)
    ast_pg    = blend(lambda d: d["a"] / d["gp"], pr["ast_pg"])
    blk_pg    = blend(lambda d: d.get("blk", 0) / d["gp"], pr["blk_pg"])
    ppp_pg    = blend(lambda d: d.get("ppp", 0) / d["gp"], 0.0)

    # MoneyPuck situational splits, pooled over both seasons
    def mp_sit(mp, s):
        return ((mp or {}).get("skaters", {}).get(p["pid"]) or {}).get(s) if mp else None

    def pooled(s, key):
        return sum(v[key] for v in (mp_sit(mp_cur, s), mp_sit(mp_pri, s)) if v)

    ev_toi_t, ev_sog_t = pooled("5on5", "toi"), pooled("5on5", "sog")
    ev_g, ev_xg        = pooled("5on5", "g"), pooled("5on5", "xg")
    pp_toi_t, pp_sog_t = pooled("5on4", "toi"), pooled("5on4", "sog")
    pp_g, pp_xg        = pooled("5on4", "g"), pooled("5on4", "xg")
    has_mp = ev_toi_t > 0

    if has_mp:
        # 300 min of 5v5 / 60 min of PP is where the prior loses its vote:
        # shot volume is the most stable skill in hockey, a full season stands
        ev_sog60 = regress(ev_sog_t / ev_toi_t * 3600, ev_toi_t / 60, pr["ev_sog60"], 300)
        pp_sog60 = regress(pp_sog_t / pp_toi_t * 3600 if pp_toi_t else pr["pp_sog60"],
                           pp_toi_t / 60, pr["pp_sog60"], 60)
        # Finishing = actual goals per shot, shrunk toward what HIS shot
        # quality says he should convert (xG per shot, itself shrunk toward
        # the position mean). A sniper keeps most of his edge; a hot streak
        # on 40 shots does not. 150 shots of 5v5 / 60 of PP is the pivot.
        xsh_p, fin_p = lg["xsh"][pos], lg["fin"][pos]
        ev_xsh = regress(ev_xg / ev_sog_t if ev_sog_t else xsh_p["5on5"], ev_sog_t, xsh_p["5on5"], 100)
        ev_sh  = regress(ev_g / ev_sog_t if ev_sog_t else ev_xsh * fin_p["5on5"], ev_sog_t,
                         ev_xsh * fin_p["5on5"], 150)
        pp_xsh = regress(pp_xg / pp_sog_t if pp_sog_t else xsh_p["5on4"], pp_sog_t, xsh_p["5on4"], 60)
        pp_sh  = regress(pp_g / pp_sog_t if pp_sog_t else pp_xsh * fin_p["5on4"], pp_sog_t,
                         pp_xsh * fin_p["5on4"], 60)
    else:
        # No xG history (rookie / call-up): all-situation NHL totals stand in
        tot_sog = (cur["sog"] if cur else 0) + (pri["sog"] if pri else 0)
        tot_g   = (cur["g"] if cur else 0) + (pri["g"] if pri else 0)
        minutes = (toi_pg * (gp_cur + gp_pri)) / 60
        ev_sog60 = regress(tot_sog / minutes * 60 if minutes else pr["ev_sog60"], minutes, pr["ev_sog60"], 300)
        pp_sog60 = pr["pp_sog60"]
        ev_sh    = regress(tot_g / tot_sog if tot_sog else pr["ev_sh"], tot_sog, pr["ev_sh"], 150)
        ev_xsh   = lg["xsh"][pos]["5on5"]
        pp_sh    = pr["pp_sh"]

    # Recent form: last 10 games (this season first, last season fills in).
    # Ice time moves first when a role changes, so tonight's TOI leans on the
    # last five games; shot volume vs. the season rate becomes a form factor.
    lg_ = logs.get(p["pid"], {"cur": [], "prior": []})
    recent = (lg_["cur"] + lg_["prior"])[:10]
    r_sog = _mean(g["sog"] for g in recent)
    r_toi = _mean(g["toi"] for g in recent[:5])
    toi_now = 0.5 * toi_pg + 0.5 * r_toi if len(recent) >= 3 and r_toi > 0 else toi_pg
    toi_now = max(toi_now, 6 * 60)
    scale   = toi_now / toi_pg if toi_pg else 1.0
    form = 1.0
    if len(recent) >= 5 and sog_pg > 0.3:
        form = clip(1 + 0.5 * (r_sog / sog_pg - 1), 0.8, 1.25)

    inj = injuries.get(_norm_name(p["name"]), {})
    return {
        **p, "pos_group": pos,
        "gp_cur": gp_cur, "gp_pri": gp_pri, "has_mp": has_mp,
        "toi_pg": toi_pg, "toi_now": toi_now, "ev_toi_now": ev_toi_pg * scale,
        "pp_toi_pg": pp_toi_pg, "sog_pg": sog_pg, "ast_pg": ast_pg, "blk_pg": blk_pg, "ppp_pg": ppp_pg,
        "ev_sog60": ev_sog60, "pp_sog60": pp_sog60, "ev_sh": ev_sh, "ev_xsh": ev_xsh, "pp_sh": pp_sh,
        "sh_raw": (ev_g / ev_sog_t) if ev_sog_t else None,
        "goals_cur": cur["g"] if cur else 0, "goals_pri": pri["g"] if pri else 0,
        "recent": recent[:10], "r_sog": r_sog, "r_toi": r_toi, "form": form,
        "toi_trend": (r_toi / toi_pg) if (toi_pg and r_toi) else 1.0,
        "r_goals": sum(g["g"] for g in recent), "r_pts": sum(g["pts"] for g in recent),
        "inj_status": inj.get("status", ""), "inj_detail": inj.get("detail", ""),
        "inj_return": inj.get("return", ""),
        "n_seasons": int(bool(gp_cur)) + int(bool(gp_pri)),
    }


def attach_lines(profiles, mp_cur, mp_pri):
    """Label each skater's 5v5 line (L1-L4 / P1-P3) from the freshest MoneyPuck line data."""
    by_team = {}
    for p in profiles:
        by_team.setdefault(p["team"], []).append(p)
    for team, plist in by_team.items():
        src = None
        for mp in (mp_cur, mp_pri):
            if mp and mp.get("lines", {}).get(team):
                src = mp["lines"][team]
                break
        if not src:
            continue
        ranks, seen = {}, {"line": 0, "pairing": 0}
        for ln in src:
            kind = ln["kind"]
            if kind not in seen:
                continue
            ids = tuple(ln["ids"])
            if any(pid in ranks for pid in ids) or ln["gp"] < 3:
                continue
            seen[kind] += 1
            label = f"{'L' if kind == 'line' else 'P'}{seen[kind]}"
            for pid in ids:
                ranks[pid] = (label, ln["name"])
        for p in plist:
            label, mates = ranks.get(p["pid"], ("", ""))
            p["line_label"], p["line_mates"] = label, mates


def goalie_profile(g, rest_cur, rest_pri, mp_cur, mp_pri, logs, lg):
    """Save quality (regressed sv%, GSAx-based factor) and workload for one goalie."""
    cur = rest_cur["goalies"].get(g["pid"])
    pri = rest_pri["goalies"].get(g["pid"])
    sa = (cur["sa"] if cur else 0) + (pri["sa"] if pri else 0)
    sv = (cur["sv"] if cur else 0) + (pri["sv"] if pri else 0)
    sv_pct = regress(sv / sa if sa else lg["sv"], sa, lg["sv"], 800)

    def mp_all(mp):
        return ((mp or {}).get("goalies", {}).get(g["pid"]) or {}).get("all") if mp else None
    xga = sum(v["xga"] for v in (mp_all(mp_cur), mp_all(mp_pri)) if v)
    ga  = sum(v["ga"] for v in (mp_all(mp_cur), mp_all(mp_pri)) if v)
    # GA vs. expected, shrunk toward even with 25 goals of prior: a .930 hot
    # streak on 300 shots barely moves it, a full season does.
    if xga > 0:
        factor = clip((ga + 25) / (xga + 25), 0.82, 1.18)
    else:
        factor = clip((1 - sv_pct) / (1 - lg["sv"]), 0.82, 1.18)

    lg_ = logs.get(g["pid"], {"cur": [], "prior": []})
    starts = [x for x in (lg_["cur"] + lg_["prior"]) if x.get("started")]
    last5  = starts[:5]
    r_sa = sum(x["sa"] for x in last5)
    r_sv = (r_sa - sum(x["ga"] for x in last5)) / r_sa if r_sa else None
    return {
        **g,
        "gs_cur": cur["gs"] if cur else 0, "gs_pri": pri["gs"] if pri else 0,
        "gp_cur": cur["gp"] if cur else 0,
        "record": f"{cur['w']}-{cur['l']}-{cur['otl']}" if cur and cur["gp"] else (f"{pri['w']}-{pri['l']}-{pri['otl']} (last yr)" if pri else ""),
        "sv_pct": sv_pct, "sv_raw": (sv / sa) if sa else None, "shots_seen": sa,
        "factor": factor, "gsax": (xga - ga) if xga else None,
        "last_start": starts[0]["date"] if starts else None,
        "r_sv": r_sv, "r_starts": len(last5),
        "shots_per_start": _mean(x["sa"] for x in starts[:10]) if starts else None,
    }


def expected_starters(team, goalies, dfo, rest, slate_date, team_gp, lg):
    """
    Who is in net tonight, and how sure we are.
      Confirmed  -- DailyFaceoff has a beat-writer confirmation
      Projected  -- DailyFaceoff's projection, or our own: the #1 by starts,
                    unless he played yesterday on a back-to-back (then the #2)
    """
    goalies = sorted(goalies, key=lambda x: -(x["gs_cur"] if team_gp >= 5 else x["gs_pri"]))

    def result(starter, status, reason, others):
        return {"starter": starter, "status": status, "reason": reason[:160], "others": others,
                "is_backup": bool(starter and goalies and starter is not goalies[0])}

    entry = dfo.get(team)
    if entry:
        match = next((g for g in goalies if _norm_name(g["name"]) == _norm_name(entry["name"])), None)
        reason = entry.get("news") or f"DailyFaceoff {entry['status'].lower()}"
        if match:
            return result(match, entry["status"], reason, [g for g in goalies if g is not match])
        # Listed by DailyFaceoff but not on the roster feed yet (a morning
        # call-up): a league-average stand-in, flagged as a backup for skaters.
        stub = {"pid": "dfo_" + _norm_name(entry["name"]).replace(" ", "_"), "name": entry["name"],
                "pos": "G", "dk_pos": "G", "team": team, "num": None, "headshot": "",
                "gs_cur": 0, "gs_pri": 0, "gp_cur": 0, "record": "", "sv_pct": lg["sv"], "sv_raw": None,
                "shots_seen": 0, "factor": 1.0, "gsax": None, "last_start": None, "r_sv": None,
                "r_starts": 0, "shots_per_start": None}
        return {"starter": stub, "status": entry["status"], "reason": reason[:160],
                "others": goalies, "is_backup": True}
    if not goalies:
        return result(None, "Unknown", "no goalies on roster", [])
    yesterday = (slate_date - timedelta(days=1)).isoformat()
    top = goalies[0]
    if rest.get("b2b") and top.get("last_start") == yesterday and len(goalies) > 1:
        return result(goalies[1], "Projected",
                      f"{top['name'].split()[-1]} started last night (back-to-back)", [top] + goalies[2:])
    return result(top, "Projected", "team's #1 by starts (no confirmation yet)", goalies[1:])


def score_skater(p, game, odds, ctx):
    """Anytime-goal probability and SOG floor for one skater tonight."""
    lg = ctx["lg"]
    is_home = p["team"] == game["home"]
    opp = game["away"] if is_home else game["home"]
    T, O = ctx["teams"][p["team"]], ctx["teams"][opp]
    line = odds[game["game_id"]]
    imp  = line["imp_home"] if is_home else line["imp_away"]
    rest = ctx["rest"].get(p["team"], {})
    goalie = ctx["opp_goalie"].get(opp)

    # -- power-play minutes tonight: the skater's share of his team's PP time,
    #    applied to how much PP time this matchup should produce
    exp_team_pp = lg["pp_time_pg"] * (0.5 * T["pp_time_pg"] / lg["pp_time_pg"]
                                      + 0.5 * O["pk_time_pg"] / lg["pk_time_pg"])
    pp_share = clip(p["pp_toi_pg"] / max(60.0, T["pp_time_pg"]), 0.0, 1.0)
    pp_toi   = pp_share * exp_team_pp
    ev_toi   = p["ev_toi_now"]

    # -- shot volume
    opp_shots = clip(O["sa60_5v5"] / lg["sog60_5v5"], 0.85, 1.15)
    env   = clip((imp / lg["goals_pg"]) ** 0.5, 0.85, 1.20)
    home  = 1.03 if is_home else 0.97
    tired = 0.97 if rest.get("b2b") else 1.0
    e_sog_ev = ev_toi / 3600 * p["ev_sog60"] * opp_shots * env * home * tired * p["form"]
    e_sog_pp = pp_toi / 3600 * p["pp_sog60"] * home * p["form"]
    # 4v4, 3v3, 5v3, shorthanded and empty-net shots ride on top of the two
    # modelled states (~10% of shots, ~19% of goals -- see LG_DEFAULT)
    e_sog    = (e_sog_ev + e_sog_pp) * lg["other_sog"]

    # -- finishing: his regressed conversion, then the opponent's chance
    #    quality and the goalie in the way
    sh_ev  = p["ev_sh"]
    opp_q  = clip(O["xga60_5v5"] / lg["xga60_5v5"], 0.85, 1.15) ** 0.5
    opp_pk = clip(O["pk_xga60"] / lg["pk_xga60"], 0.80, 1.25) ** 0.5
    gf     = goalie["factor"] if goalie else 1.0
    lam    = gf * (e_sog_ev * sh_ev * opp_q + e_sog_pp * p["pp_sh"] * opp_pk) * lg["other_goals"]
    p_goal = 1 - math.exp(-lam)

    # -- the rest of the DK line
    e_ast = p["ast_pg"] * env * tired
    e_blk = p["blk_pg"] * clip(O["attf60_5v5"] / lg["attf60_5v5"], 0.85, 1.15)
    p3, p5 = poisson_tail(e_sog, 3), poisson_tail(e_sog, 5)
    pb3, ppts3, phat = poisson_tail(e_blk, 3), poisson_tail(lam + e_ast, 3), poisson_tail(lam, 3)
    dk = (DK["goal"] * lam + DK["assist"] * e_ast + DK["sog"] * e_sog + DK["block"] * e_blk
          + DK["sog5_bonus"] * p5 + DK["blk3_bonus"] * pb3 + DK["pts3_bonus"] * ppts3
          + DK["hat_trick"] * phat)

    # Headline is the goal; the SOG floor nudges it so a 4-shot D-man edges a
    # 2-shot forward with the same goal odds.
    composite = 100 * p_goal + 3.0 * (e_sog - 2.5)
    out_flag  = is_unavailable(p["inj_status"])
    grade     = "OUT" if out_flag else grade_for(composite, SKATER_GRADES)

    r = {
        **p, "game_id": game["game_id"], "opp": opp, "is_home": is_home,
        "start_str": game["start_str"], "main_slate": game["main_slate"],
        "p_goal": p_goal, "lam": lam, "e_sog": e_sog,
        "e_sog_ev": e_sog_ev * lg["other_sog"], "e_sog_pp": e_sog_pp * lg["other_sog"],
        "p_sog3": p3, "p_sog5": p5, "e_ast": e_ast, "e_blk": e_blk, "dk": dk,
        "composite": composite, "grade": grade, "grade_color": GRADE_COLORS[grade],
        "pp_share": pp_share, "pp_toi": pp_toi, "pp_unit": "PP1" if pp_share >= 0.45 else ("PP2" if pp_share >= 0.15 else ""),
        "imp": imp, "total": line["total"], "p_win": line["p_home"] if is_home else line["p_away"],
        "opp_sa_pg": O["sa_pg"], "opp_sa_rank": O["sa_pg_rank"],
        "opp_xga60": O["xga60_5v5"], "opp_xga_rank": O["xga60_5v5_rank"],
        "opp_pk_xga60": O["pk_xga60"], "opp_pk_rank": O["pk_xga60_rank"],
        "opp_pk_time": O["pk_time_pg"], "opp_pen_rank": O["pk_time_pg_rank"],
        "goalie": goalie, "b2b": bool(rest.get("b2b")), "rest_days": rest.get("rest_days"),
        "sh_use": sh_ev, "opp_q": opp_q, "opp_pk_f": opp_pk, "env": env, "opp_shots": opp_shots,
    }
    r["scout"] = scout_note(r)
    return r


def scout_note(r):
    bits = []
    opp = r["opp"]
    if r["pp_unit"] == "PP1":
        bits.append(f"PP1 ({r['pp_toi'] / 60:.1f} PP min projected)")
    elif r["pp_unit"] == "PP2":
        bits.append("PP2")
    if r["opp_pk_rank"] <= 8:
        bits.append(f"{opp} PK bleeds chances (#{r['opp_pk_rank']} worst)")
    if r["opp_pen_rank"] <= 8:
        bits.append(f"{opp} takes penalties (#{r['opp_pen_rank']} most SH time)")
    if r["opp_sa_rank"] <= 8:
        bits.append(f"{opp} allows {r['opp_sa_pg']:.1f} SOG/gm (#{r['opp_sa_rank']} most)")
    elif r["opp_sa_rank"] >= 25:
        bits.append(f"{opp} suppresses shots ({r['opp_sa_pg']:.1f}/gm, #{33 - r['opp_sa_rank']} fewest)")
    g = r.get("goalie")
    if g:
        last = g["name"].split()[-1]
        if g.get("backup"):
            bits.append(f"backup {last} expected in net ({g['status'].lower()})")
        elif g["factor"] >= 1.06:
            bits.append(f"{last} has been leaky ({g['gsax']:+.1f} GSAx)" if g.get("gsax") is not None else f"{last} below average in net")
        elif g["factor"] <= 0.94:
            bits.append(f"{last} is a wall ({g['gsax']:+.1f} GSAx)" if g.get("gsax") is not None else f"{last} is a tough draw")
    if r["form"] >= 1.10:
        bits.append(f"volume up: {r['r_sog']:.1f} SOG/gm last 10")
    elif r["form"] <= 0.90:
        bits.append(f"volume down: {r['r_sog']:.1f} SOG/gm last 10")
    if r["toi_trend"] >= 1.08:
        bits.append("ice time trending up")
    if r["imp"] >= 3.4:
        bits.append(f"Vegas likes {r['team']}: {r['imp']:.1f} implied goals")
    elif r["imp"] <= 2.6:
        bits.append(f"low implied total ({r['imp']:.1f} goals)")
    if r["b2b"]:
        bits.append("2nd of a back-to-back")
    if not r["has_mp"] and r["n_seasons"] == 0:
        bits.append("no NHL track record -- position averages only")
    return " · ".join(bits) or "Nothing special in the matchup -- this is his baseline."


def score_goalie(g, status, reason, game, odds, ctx):
    """Projected DraftKings points for a goalie, assuming he starts."""
    lg = ctx["lg"]
    is_home = g["team"] == game["home"]
    opp  = game["away"] if is_home else game["home"]
    T, O = ctx["teams"][g["team"]], ctx["teams"][opp]
    line = odds[game["game_id"]]
    p_win   = line["p_home"] if is_home else line["p_away"]
    imp_opp = line["imp_away"] if is_home else line["imp_home"]
    rest    = ctx["rest"].get(g["team"], {})

    # Shots faced: half the opponent's volume, half what this team allows,
    # scaled by the game total. Home teams outshoot visitors by a few percent.
    e_shots = (0.5 * O["sf_pg"] + 0.5 * T["sa_pg"]) * (line["total"] / 6.0) ** 0.5 * (0.97 if is_home else 1.03)
    # Save % against THIS opponent's shot quality
    q = clip(O["xg_per_shot_for"] / lg["xg_per_shot"], 0.85, 1.15)
    sv_adj = 1 - (1 - g["sv_pct"]) * q
    model_ga = e_shots * (1 - sv_adj)
    # Vegas-anchored: the implied goals against already know the matchup
    e_ga = 0.5 * model_ga + 0.5 * imp_opp
    yesterday = (ctx["slate_date"] - timedelta(days=1)).isoformat()
    if rest.get("b2b") and g.get("last_start") == yesterday:
        e_ga *= 1.05                     # goalies on zero rest give up a little more
    e_saves = max(0.0, e_shots - e_ga)
    p_so  = math.exp(-e_ga)
    p_35  = poisson_tail(e_saves, 35)
    p_otl = clip(0.115 * (1 - p_win) / 0.5, 0.05, 0.20)
    dk = (DK["g_win"] * p_win + DK["g_save"] * e_saves + DK["g_ga"] * e_ga
          + DK["g_shutout"] * p_so + DK["g_otl"] * p_otl + DK["g_35saves"] * p_35)
    grade = grade_for(dk, GOALIE_GRADES)
    backup = status == "Backup"
    r = {
        **g, "game_id": game["game_id"], "opp": opp, "is_home": is_home,
        "start_str": game["start_str"], "main_slate": game["main_slate"],
        "status": status, "reason": reason, "backup": backup,
        "p_win": p_win, "e_shots": e_shots, "e_ga": e_ga, "e_saves": e_saves,
        "p_so": p_so, "p_35": p_35, "dk": dk, "composite": dk,
        "grade": grade, "grade_color": GRADE_COLORS[grade],
        "opp_sf_pg": O["sf_pg"], "opp_sf_rank": O["sf_pg_rank"], "opp_gf_pg": O["gf_pg"],
        "imp_opp": imp_opp, "total": line["total"], "b2b": bool(rest.get("b2b")),
    }
    bits = []
    if p_win >= 0.6:
        bits.append(f"{r['p_win'] * 100:.0f}% to win")
    elif p_win <= 0.42:
        bits.append(f"only {r['p_win'] * 100:.0f}% to win")
    if O["sf_pg_rank"] <= 8:
        bits.append(f"{opp} fires {O['sf_pg']:.1f} SOG/gm (#{O['sf_pg_rank']} most) -- save volume")
    elif O["sf_pg_rank"] >= 25:
        bits.append(f"{opp} generates little ({O['sf_pg']:.1f} SOG/gm)")
    if g.get("gsax") is not None and abs(g["gsax"]) >= 8:
        bits.append(f"{g['gsax']:+.1f} GSAx over the last two seasons")
    if r["b2b"] and g.get("last_start") == yesterday:
        bits.append("started last night")
    if backup:
        bits.append("NOT the expected starter")
    r["scout"] = " · ".join(bits) or "Even matchup."
    return r


# ============================================================
#  RESULTS TRACKING
# ============================================================

def _pred_path(slate_date, backtest=False):
    return os.path.join(PREDICTIONS_FOLDER, f"{'backtest' if backtest else 'pred'}_{slate_date.isoformat()}.json")


def _gh_headers():
    return {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}


def _repo_get(path):
    """Fetch a file from the GitHub repo. Returns (bytes, sha) or (None, None)."""
    import base64
    try:
        r = requests.get(f"https://api.github.com/repos/{GITHUB_USER}/{GITHUB_REPO}/contents/{path}",
                         headers=_gh_headers(), timeout=15)
        if r.status_code != 200:
            return None, None
        j = r.json()
        return base64.b64decode(j["content"]), j["sha"]
    except Exception:
        return None, None


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def sync_predictions_down(slate_date):
    """
    Pull tonight's log from the repo before doing anything. The tool runs from
    two places -- the PC and GitHub Actions -- and if each kept its own log
    they would drift and the scorecard would be unreliable. The repo copy is
    the single record.
    """
    if not GITHUB_TOKEN:
        return
    fname = os.path.basename(_pred_path(slate_date))
    data, _ = _repo_get(f"{PREDICTIONS_FOLDER}/{fname}")
    if data is None:
        return
    os.makedirs(PREDICTIONS_FOLDER, exist_ok=True)
    local = os.path.join(PREDICTIONS_FOLDER, fname)
    try:
        remote = json.loads(data)
        mine = _read_json(local)
        if mine and mine.get("logged_at", "") >= remote.get("logged_at", ""):
            return                       # local copy is already the newer one
        with open(local, "wb") as f:
            f.write(data)
        print(f"  Synced {fname} from repo (logged {remote.get('logged_at', '?')[:16]})")
    except Exception as e:
        print(f"  [!] Prediction sync: {e}")


def sync_predictions_up(path):
    """Push a prediction log back to the repo so every machine sees it."""
    if not GITHUB_TOKEN or not os.path.exists(path):
        return
    with open(path, "rb") as f:
        content = f.read()
    rel = path.replace("\\", "/")
    if not deploy_file(content, rel, _gh_headers(), f"Predictions {os.path.basename(path)}"):
        print(f"  [!] Could not push {rel} to repo")


def logged_starters(slate_date):
    """Goalies logged as tonight's starters before this run: {team: name}. None if no file."""
    data = _read_json(_pred_path(slate_date))
    if not data:
        return None
    return {r["team"]: r["name"] for r in data.get("predictions", [])
            if r.get("pos") == "G" and r.get("status") in ("Confirmed", "Projected")}


def logged_out(slate_date):
    data = _read_json(_pred_path(slate_date))
    if not data:
        return None
    return {r["name"] for r in data.get("predictions", []) if r.get("grade") == "OUT"}


def log_predictions(slate_date, skaters, goalies, games, preseason=False, backtest=False):
    """
    Save tonight's calls so they can be graded tomorrow.

    Rewritten on every run so the stored file is the LAST read before puck
    drop -- except per game: once a game has started, its rows are frozen.
    Logging a "prediction" after the result is known would quietly make the
    accuracy numbers meaningless.
    """
    os.makedirs(PREDICTIONS_FOLDER, exist_ok=True)
    path = _pred_path(slate_date, backtest)
    done_teams = {t for g in games if g["started"] or g["final"] for t in (g["home"], g["away"])}
    if backtest:
        done_teams = set()               # a backtest is hindsight by definition; score it anyway

    prior = {}
    old = _read_json(path)
    if old:
        prior = {r["pid"]: r for r in old.get("predictions", [])}

    rows, kept = [], 0
    for p in skaters:
        if p["team"] in done_teams and p["pid"] in prior:
            rows.append(prior[p["pid"]])
            kept += 1
            continue
        row = {"pid": p["pid"], "name": p["name"], "pos": p["dk_pos"], "team": p["team"],
               "opp": p["opp"], "game_id": p["game_id"], "grade": p["grade"],
               "composite": round(p["composite"], 1), "p_goal": round(p["p_goal"], 4),
               "e_sog": round(p["e_sog"], 2), "dk": round(p["dk"], 2),
               "pp_unit": p["pp_unit"], "main": p["main_slate"]}
        if p["team"] in done_teams:
            row["late"] = True           # game already under way and never logged: not a real call
        rows.append(row)
    for g in goalies:
        if g["team"] in done_teams and g["pid"] in prior:
            rows.append(prior[g["pid"]])
            kept += 1
            continue
        row = {"pid": g["pid"], "name": g["name"], "pos": "G", "team": g["team"], "opp": g["opp"],
               "game_id": g["game_id"], "grade": g["grade"], "status": g["status"],
               "dk": round(g["dk"], 2), "p_win": round(g["p_win"], 3),
               "e_saves": round(g["e_saves"], 1), "e_ga": round(g["e_ga"], 2), "main": g["main_slate"]}
        if g["team"] in done_teams:
            row["late"] = True
        rows.append(row)

    out = {"date": slate_date.isoformat(), "preseason": preseason, "backtest": backtest,
           "logged_at": datetime.now().isoformat(timespec="seconds"),
           "games": [{"game_id": g["game_id"], "home": g["home"], "away": g["away"],
                      "start_str": g["start_str"]} for g in games],
           "predictions": rows}
    if old and old.get("scored"):
        out["scored"] = old["scored"]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)
    msg = f"  Logged {len(rows)} calls -> {path}"
    if kept:
        msg += f" ({kept} locked -- games under way or final)"
    print(msg)
    return path


def _box_actuals(game_ids):
    """
    Box-score lines for FINISHED games. Returns ({pid: actual}, finished_teams).
    Goalie decisions are not in the box score, so the goalie with the most ice
    time on each side gets it: W, L, or OTL when the game went past regulation.
    """
    actual, finished = {}, set()
    for gid in game_ids:
        box = get_boxscore(gid)
        if box.get("gameState") not in ("OFF", "FINAL"):
            continue
        home, away = box.get("homeTeam", {}), box.get("awayTeam", {})
        hs, as_ = home.get("score", 0) or 0, away.get("score", 0) or 0
        last = (box.get("gameOutcome") or {}).get("lastPeriodType", "REG")
        stats = box.get("playerByGameStats", {})
        for side, team, won, opp_score in (("homeTeam", home, hs > as_, as_), ("awayTeam", away, as_ > hs, hs)):
            abbr = team.get("abbrev", "")
            finished.add(abbr)
            grp = stats.get(side, {})
            for key in ("forwards", "defense"):
                for p in grp.get(key, []):
                    g, a = p.get("goals", 0) or 0, p.get("assists", 0) or 0
                    sog, blk = p.get("sog", 0) or 0, p.get("blockedShots", 0) or 0
                    pts = g + a
                    dk = (DK["goal"] * g + DK["assist"] * a + DK["sog"] * sog + DK["block"] * blk
                          + (DK["hat_trick"] if g >= 3 else 0) + (DK["sog5_bonus"] if sog >= 5 else 0)
                          + (DK["blk3_bonus"] if blk >= 3 else 0) + (DK["pts3_bonus"] if pts >= 3 else 0))
                    actual[str(p["playerId"])] = {"g": g, "a": a, "sog": sog, "blk": blk, "pts": pts,
                                                  "dk": dk, "toi": mmss(p.get("toi", "0:00")), "team": abbr}
            goalies = grp.get("goalies", [])
            decider = max(goalies, key=lambda x: mmss(x.get("toi", "0:00")), default=None)
            for p in goalies:
                toi = mmss(p.get("toi", "0:00"))
                if toi <= 0 and not p.get("starter"):
                    continue
                saves, ga = p.get("saves", 0) or 0, p.get("goalsAgainst", 0) or 0
                dec = ""
                if p is decider:
                    dec = "W" if won else ("OTL" if last in ("OT", "SO") else "L")
                so = dec == "W" and opp_score == 0 and toi >= 55 * 60
                dk = (DK["g_save"] * saves + DK["g_ga"] * ga + (DK["g_win"] if dec == "W" else 0)
                      + (DK["g_otl"] if dec == "OTL" else 0) + (DK["g_shutout"] if so else 0)
                      + (DK["g_35saves"] if saves >= 35 else 0))
                actual[str(p["playerId"])] = {"saves": saves, "ga": ga, "dec": dec, "so": so,
                                              "starter": bool(p.get("starter")), "toi": toi,
                                              "dk": dk, "team": abbr}
    return actual, finished


def score_date(slate_date, backtest=False, force=False):
    """
    Grade one logged slate against the box scores.

    Two things matter and are easy to misread separately:
      * Do the higher grades actually score more often? (does the ranking work)
      * Is the probability honest? (a Brier score: 0 = perfect, 0.25 = coin flip)
    Once every game is final the result is written into the log file so it is
    never re-pulled.
    """
    path = _pred_path(slate_date, backtest)
    data = _read_json(path)
    if not data:
        return None
    if data.get("scored") and not force:
        return data["scored"]
    gids = [g["game_id"] for g in data.get("games", [])]
    actual, finished = _box_actuals(gids)
    if not finished:
        return None

    tiers, brier, sog_err, dk_err = {}, [], [], []
    scratched = matched = 0
    hits = []
    for p in data["predictions"]:
        if p.get("late") or p["grade"] == "OUT":
            continue
        if p["team"] not in finished:
            continue
        a = actual.get(p["pid"])
        if p["pos"] == "G":
            continue
        if a is None or "g" not in a:
            # Final game, no line: healthy scratch or a late injury. A real 0
            # for DFS purposes; dropping it would flatter the grades.
            a = {"g": 0, "sog": 0, "dk": 0.0}
            scratched += 1
        matched += 1
        hit = 1 if a["g"] > 0 else 0
        t = tiers.setdefault(p["grade"], {"n": 0, "p_sum": 0.0, "hits": 0, "sog": 0.0, "e_sog": 0.0, "dk": 0.0, "e_dk": 0.0})
        t["n"] += 1
        t["p_sum"] += p["p_goal"]
        t["hits"] += hit
        t["sog"] += a["sog"]
        t["e_sog"] += p["e_sog"]
        t["dk"] += a["dk"]
        t["e_dk"] += p["dk"]
        brier.append((p["p_goal"] - hit) ** 2)
        sog_err.append(abs(a["sog"] - p["e_sog"]))
        dk_err.append(abs(a["dk"] - p["dk"]))
        if p["grade"] in ("A+", "A"):
            hits.append({"name": p["name"], "team": p["team"], "grade": p["grade"],
                         "p_goal": p["p_goal"], "g": a["g"], "sog": a["sog"], "dk": a["dk"]})

    g_n = g_started = g_scored = 0
    g_err = []
    for p in data["predictions"]:
        if p.get("pos") != "G" or p.get("late") or p["team"] not in finished:
            continue
        if p.get("status") not in ("Confirmed", "Projected"):
            continue
        a = actual.get(p["pid"])
        g_n += 1
        if a and a.get("starter"):
            g_started += 1
            g_scored += 1
            g_err.append(abs(a["dk"] - p["dk"]))

    if not matched:
        return None
    for t in tiers.values():
        t["hit_rate"] = round(t["hits"] / t["n"], 3)
        t["avg_p"] = round(t["p_sum"] / t["n"], 3)
        t["avg_sog"] = round(t["sog"] / t["n"], 2)
        t["avg_e_sog"] = round(t["e_sog"] / t["n"], 2)
        t["avg_dk"] = round(t["dk"] / t["n"], 1)
        t["avg_e_dk"] = round(t["e_dk"] / t["n"], 1)

    all_teams = {t for g in data.get("games", []) for t in (g["home"], g["away"])}
    games_left = len(all_teams - finished) // 2
    result = {
        "date": slate_date.isoformat(), "matched": matched, "scratched": scratched,
        "brier": round(sum(brier) / len(brier), 4),
        "sog_mae": round(sum(sog_err) / len(sog_err), 2),
        "dk_mae": round(sum(dk_err) / len(dk_err), 1),
        "hit_rate": round(sum(t["hits"] for t in tiers.values()) / matched, 3),
        "avg_p": round(sum(t["p_sum"] for t in tiers.values()) / matched, 3),
        "tiers": tiers, "partial": games_left > 0, "games_left": games_left,
        "goalie": {"n": g_n, "started": g_started,
                   "dk_mae": round(sum(g_err) / len(g_err), 1) if g_err else None},
        "top_calls": sorted(hits, key=lambda x: -x["p_goal"])[:8],
        "preseason": data.get("preseason", False),
    }
    if not result["partial"]:
        data["scored"] = result
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=1)
            if not backtest:
                sync_predictions_up(path)
        except Exception as e:
            print(f"  [!] Could not store scorecard: {e}")
    return result


def accuracy_history(slate_date, days=14):
    """Every scored slate in the window, newest first, plus an all-time roll-up."""
    recent = []
    for i in range(1, days + 1):
        r = score_date(slate_date - timedelta(days=i))
        if r:
            recent.append(r)
    # All-time: stored scorecards are free to read; anything older than the
    # window that was never finalised is left alone.
    total = {"n": 0, "hits": 0, "p_sum": 0.0, "brier_w": 0.0, "sog_w": 0.0, "days": 0, "tiers": {}}
    seen = {r["date"] for r in recent}
    files = []
    if os.path.isdir(PREDICTIONS_FOLDER):
        files = sorted(f for f in os.listdir(PREDICTIONS_FOLDER) if f.startswith("pred_") and f.endswith(".json"))
    for f in files:
        d = f[5:15]
        r = next((x for x in recent if x["date"] == d), None)
        if r is None and d not in seen:
            stored = _read_json(os.path.join(PREDICTIONS_FOLDER, f)) or {}
            r = stored.get("scored")
        if not r or r.get("preseason"):
            continue
        total["n"] += r["matched"]
        total["hits"] += sum(t["hits"] for t in r["tiers"].values())
        total["p_sum"] += sum(t["p_sum"] for t in r["tiers"].values())
        total["brier_w"] += r["brier"] * r["matched"]
        total["sog_w"] += r["sog_mae"] * r["matched"]
        total["days"] += 1
        for g, t in r["tiers"].items():
            a = total["tiers"].setdefault(g, {"n": 0, "hits": 0, "p_sum": 0.0})
            a["n"] += t["n"]
            a["hits"] += t["hits"]
            a["p_sum"] += t["p_sum"]
    if total["n"]:
        total["hit_rate"] = round(total["hits"] / total["n"], 3)
        total["avg_p"] = round(total["p_sum"] / total["n"], 3)
        total["brier"] = round(total["brier_w"] / total["n"], 4)
        total["sog_mae"] = round(total["sog_w"] / total["n"], 2)
        for t in total["tiers"].values():
            t["hit_rate"] = round(t["hits"] / t["n"], 3)
            t["avg_p"] = round(t["p_sum"] / t["n"], 3)
    return recent, total


# ============================================================
#  HTML DASHBOARD
# ============================================================

CSS = """
* { box-sizing:border-box; margin:0; padding:0; }
body { background:#0d1117; color:#e6edf3; font-family:'Segoe UI',system-ui,sans-serif; line-height:1.5; }
a { color:#79c0ff; }

#splash { position:fixed; inset:0; z-index:9999; display:flex; flex-direction:column; align-items:center; justify-content:center;
  background:radial-gradient(ellipse at center,#0b1d33 0%,#07101c 60%,#000 100%); transition:opacity .8s ease; }
#splash.fade-out { opacity:0; pointer-events:none; }
.splash-title { font-size:2.8rem; font-weight:900; letter-spacing:6px; color:#79c0ff; text-shadow:0 0 40px #79c0ff88; animation:glow 2s ease-in-out infinite alternate; }
.splash-sub { font-size:1rem; color:#8b949e; letter-spacing:3px; margin-top:8px; text-transform:uppercase; }
.puck { width:70px; height:26px; border-radius:50%; background:#111; border:3px solid #333; box-shadow:0 8px 20px #000a; margin-bottom:26px; animation:slide 1.6s ease-in-out infinite alternate; }
@keyframes glow { from { text-shadow:0 0 20px #79c0ff66; } to { text-shadow:0 0 60px #79c0ffcc,0 0 100px #79c0ff44; } }
@keyframes slide { from { transform:translateX(-90px) rotate(-8deg); } to { transform:translateX(90px) rotate(8deg); } }

.header { background:linear-gradient(135deg,#0b1d33,#0d1117 70%); padding:34px 20px; text-align:center; border-bottom:2px solid #1f6feb; }
.header h1 { font-size:2.5rem; color:#79c0ff; letter-spacing:3px; font-weight:800; }
.header p { color:#8b949e; margin-top:6px; font-size:14px; }
.header .lock { color:#e6edf3; font-weight:600; }
.banner { background:#3d2e00; border:1px solid #9e6a03; color:#f2cc60; padding:10px 16px; border-radius:8px; margin:18px auto 0; max-width:1100px; font-size:13px; }

.container { max-width:1100px; margin:0 auto; padding:24px 14px; }
h2.section-title { color:#79c0ff; font-size:1.15rem; margin:34px 0 14px; border-bottom:1px solid #21262d; padding-bottom:8px; display:flex; justify-content:space-between; align-items:baseline; flex-wrap:wrap; gap:8px; }
h2.section-title small { color:#8b949e; font-size:12px; font-weight:400; }

.toolbar { display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-bottom:14px; }
.toggle { background:#161b22; border:1px solid #30363d; color:#c9d1d9; padding:8px 14px; border-radius:8px; cursor:pointer; font-size:13px; font-family:inherit; }
.toggle.on { border-color:#58a6ff; color:#58a6ff; background:#0f1f33; }
.games-row { display:flex; flex-wrap:wrap; gap:10px; margin-bottom:22px; }
.game-chip { background:#161b22; border:1px solid #30363d; border-radius:10px; padding:11px 14px; min-width:180px; cursor:pointer; user-select:none; transition:border-color .15s,background .15s; }
.game-chip:hover { border-color:#1f6feb; background:#1c2128; }
.game-chip.active { border-color:#58a6ff; background:#0f1f33; box-shadow:0 0 0 1px #58a6ff55; }
.game-chip .teams { font-size:1rem; font-weight:700; display:flex; align-items:center; gap:6px; }
.game-chip img { width:22px; height:22px; }
.game-chip .time { font-size:12px; color:#8b949e; margin-top:2px; }
.game-chip .meta { font-size:11px; color:#6e7681; margin-top:2px; }
.game-chip .g { color:#c9d1d9; }
.game-chip .conf { color:#3fb950; } .game-chip .proj { color:#d29922; }
.filter-banner { display:none; background:#0f1f33; border:1px solid #58a6ff55; border-radius:8px; padding:9px 14px; margin-bottom:18px; color:#58a6ff; font-size:13px; font-weight:600; }

.about { margin:0 0 22px; }
.about-bar { width:100%; display:flex; justify-content:space-between; align-items:center; gap:12px; background:#161b22; border:1px solid #30363d; border-radius:12px; padding:12px 16px; color:#c9d1d9; font-size:13px; text-align:left; cursor:pointer; font-family:inherit; }
.about-bar:hover { border-color:#58a6ff; }
.about-body { display:none; background:#0f141b; border:1px solid #30363d; border-top:none; border-radius:0 0 12px 12px; padding:14px 18px; font-size:13px; color:#c9d1d9; }
.about.open .about-body { display:block; } .about.open .about-bar { border-radius:12px 12px 0 0; }
.about-body p { margin:6px 0; } .about-body b { color:#79c0ff; }

.player-card { display:flex; gap:12px; background:#161b22; border:1px solid #30363d; border-radius:12px; padding:14px; margin-bottom:12px; align-items:stretch; }
.player-card[data-hidden="1"] { display:none; }
.player-card.out { opacity:.55; }
.player-card.backup { opacity:.6; }
.card-rank { min-width:64px; display:flex; flex-direction:column; align-items:center; justify-content:center; border-radius:10px; font-size:13px; font-weight:700; padding:6px; }
.card-body { flex:1; min-width:0; }
.card-name { display:flex; flex-wrap:wrap; align-items:baseline; gap:6px 10px; }
.card-name .nm { font-size:1.1rem; font-weight:700; }
.card-name .ctx { color:#8b949e; font-size:13px; }
.tag { font-size:11px; font-weight:700; padding:1px 7px; border-radius:6px; border:1px solid; }
.tag.pp1 { color:#f2cc60; border-color:#f2cc6066; background:#f2cc6011; }
.tag.pp2 { color:#c9d1d9; border-color:#30363d; }
.tag.line { color:#79c0ff; border-color:#79c0ff55; }
.tag.dtd { color:#f2cc60; border-color:#f2cc60; } .tag.out { color:#ff7b72; border-color:#ff7b72; }
.tag.conf { color:#3fb950; border-color:#3fb950; } .tag.proj { color:#d29922; border-color:#d29922; } .tag.bkp { color:#8b949e; border-color:#8b949e; }
.card-label { font-size:14px; font-weight:600; margin:4px 0 8px; }
.card-stats { display:grid; grid-template-columns:repeat(auto-fill,minmax(230px,1fr)); gap:8px 14px; }
.stat-label { font-size:10px; text-transform:uppercase; letter-spacing:1px; color:#6e7681; }
.stat-val { font-size:13px; color:#e6edf3; display:flex; align-items:center; gap:6px; }
.stat-sub { font-size:11px; color:#8b949e; }
.bar { display:inline-block; width:70px; height:7px; background:#21262d; border-radius:4px; overflow:hidden; vertical-align:middle; }
.bar i { display:block; height:100%; border-radius:4px; }
.card-scout { margin-top:9px; font-size:12px; color:#c9d1d9; border-top:1px dashed #30363d; padding-top:7px; }
.card-score { min-width:58px; display:flex; align-items:center; justify-content:center; font-size:1.4rem; font-weight:800; }
.show-all { background:#161b22; border:1px dashed #30363d; color:#8b949e; width:100%; padding:10px; border-radius:10px; cursor:pointer; font-family:inherit; font-size:13px; margin:2px 0 8px; }
.show-all:hover { color:#e6edf3; border-color:#58a6ff; }
.empty { color:#8b949e; font-size:13px; padding:12px; background:#161b22; border-radius:10px; border:1px solid #30363d; }

table.score { width:100%; border-collapse:collapse; font-size:13px; margin-bottom:14px; }
table.score th, table.score td { padding:7px 8px; border-bottom:1px solid #21262d; text-align:right; white-space:nowrap; }
table.score th:first-child, table.score td:first-child { text-align:left; }
table.score th { color:#8b949e; font-weight:600; font-size:11px; text-transform:uppercase; letter-spacing:1px; }
.good { color:#3fb950; } .bad { color:#ff7b72; } .muted { color:#8b949e; }
.footer { text-align:center; color:#6e7681; font-size:12px; padding:30px 0 40px; }
@media (max-width:600px) { .card-score { display:none; } .card-rank { min-width:54px; } .header h1 { font-size:1.9rem; } }
"""

JS = """
window.addEventListener('load', function () {
  setTimeout(function () { var s = document.getElementById('splash'); if (s) s.classList.add('fade-out'); }, 900);
  try {
    var about = document.getElementById('about');
    if (!localStorage.getItem('hg_about_seen')) { about.classList.add('open'); localStorage.setItem('hg_about_seen', '1'); }
  } catch (e) {}
});
function toggleAbout() { document.getElementById('about').classList.toggle('open'); }
var activeGame = null, mainOnly = false;
function applyFilters() {
  var cards = document.querySelectorAll('.player-card');
  cards.forEach(function (c) {
    var teamOk = !activeGame || activeGame.indexOf(c.dataset.team) >= 0;
    var mainOk = !mainOnly || c.dataset.main === '1';
    c.style.display = (teamOk && mainOk && (c.dataset.hidden !== '1' || c.dataset.shown === '1')) ? '' : 'none';
  });
  document.querySelectorAll('.game-chip[data-teams]').forEach(function (g) {
    g.style.display = (!mainOnly || g.dataset.main === '1') ? '' : 'none';
  });
  var b = document.getElementById('filter-banner');
  if (activeGame) { b.style.display = 'block'; b.textContent = 'Showing ' + activeGame.join(' vs ') + ' only -- tap the game again or "All games" to clear.'; }
  else { b.style.display = 'none'; }
}
function pickGame(el) {
  var teams = el.dataset.teams ? el.dataset.teams.split(',') : null;
  document.querySelectorAll('.game-chip').forEach(function (g) { g.classList.remove('active'); });
  if (teams && activeGame && teams[0] === activeGame[0]) { activeGame = null; }
  else { activeGame = teams; el.classList.add('active'); }
  if (!activeGame) { var all = document.getElementById('all-chip'); if (all) all.classList.add('active'); }
  applyFilters();
}
function toggleMain(el) { mainOnly = !mainOnly; el.classList.toggle('on', mainOnly); applyFilters(); }
function showAll(sec, btn) {
  document.querySelectorAll('#' + sec + ' .player-card[data-hidden="1"]').forEach(function (c) { c.dataset.shown = '1'; });
  btn.style.display = 'none'; applyFilters();
}
"""


def _bar(value, max_value, color):
    pct = int(clip(value / max_value if max_value else 0, 0, 1) * 100)
    return f'<span class="bar"><i style="width:{pct}%;background:{color}"></i></span>'


def _esc(s):
    return (str(s) if s is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _inj_tag(p):
    s = p.get("inj_status", "")
    if not s:
        return ""
    title = _esc(" ".join(x for x in (p.get("inj_detail", ""), p.get("inj_return", "") and f"return {p['inj_return']}") if x))
    if is_unavailable(s):
        return f'<span class="tag out" title="{title}">OUT: {_esc(s)}</span>'
    return f'<span class="tag dtd" title="{title}">{_esc(s)}</span>'


def _skater_card(p, rank, hidden):
    team_color = display_color(TEAMS.get(p["team"], ("", "#8b949e"))[1])
    home_away  = "vs" if p["is_home"] else "@"
    tags = ""
    if p["pp_unit"]:
        tags += f'<span class="tag {p["pp_unit"].lower()}">{p["pp_unit"]}</span>'
    if p.get("line_label"):
        tags += f'<span class="tag line" title="{_esc(p.get("line_mates", ""))}">{p["line_label"]}</span>'
    tags += _inj_tag(p)
    g = p.get("goalie") or {}
    g_status = g.get("status", "Unknown")
    g_cls = "conf" if g_status == "Confirmed" else ("bkp" if g.get("backup") else "proj")
    g_line = (f'{_esc(g.get("name", "?"))} <span class="tag {g_cls}">{_esc(g_status)}</span>' if g else "unknown")
    g_sub = ""
    if g:
        parts = [f"sv .{int(round(g['sv_pct'] * 1000)):03d}"]
        if g.get("gsax") is not None:
            parts.append(f"{g['gsax']:+.1f} GSAx")
        parts.append(f"factor {g['factor']:.2f}")
        g_sub = " &bull; ".join(parts)
    recent = p.get("recent", [])[:10]
    last_str = " ".join(f"{x['sog']}{'*' * x['g']}" for x in recent[:6]) or "no games"
    trend = p["toi_trend"]
    trend_txt = ("&#9650;" if trend >= 1.08 else ("&#9660;" if trend <= 0.92 else "&#8212;"))
    trend_clr = "#3fb950" if trend >= 1.08 else ("#ff7b72" if trend <= 0.92 else "#8b949e")
    sh_raw = f"{p['sh_raw'] * 100:.1f}%" if p.get("sh_raw") is not None else "n/a"
    rest = f"{p['rest_days']}d rest" if p.get("rest_days") is not None else "rest n/a"
    if p["b2b"]:
        rest = "back-to-back"
    style = f' style="border-left:4px solid {team_color}"'
    cls = "player-card" + (" out" if p["grade"] == "OUT" else "")
    label = (f"{p['p_goal'] * 100:.0f}% to score &bull; {p['e_sog']:.1f} SOG projected"
             if p["grade"] != "OUT" else f"Ruled out ({_esc(p['inj_status'])})")
    return f"""
    <div class="{cls}"{style} data-hidden="{1 if hidden else 0}" data-team="{p['team']}" data-main="{1 if p['main_slate'] else 0}">
      <div class="card-rank" style="background:{p['grade_color']}22;color:{p['grade_color']};border:1px solid {p['grade_color']}44">
        #{rank}<strong style="font-size:1.2rem">{p['grade']}</strong>
      </div>
      <div class="card-body">
        <div class="card-name">
          <span class="nm" style="color:{team_color}">{_esc(p['name'])}</span>
          <span class="ctx">{p['pos']} &bull; {p['team']} {home_away} {p['opp']} &bull; {p['start_str']} ET</span>
          {tags}
        </div>
        <div class="card-label" style="color:{p['grade_color']}">{label}</div>
        <div class="card-stats">
          <div><div class="stat-label">Goal odds</div>
            <div class="stat-val">{_bar(p['p_goal'], 0.5, '#3fb950')} {p['p_goal'] * 100:.0f}% (&lambda; {p['lam']:.2f})</div>
            <div class="stat-sub">5v5 finishing {p['sh_use'] * 100:.1f}% (raw {sh_raw}, shot quality {p['ev_xsh'] * 100:.1f}%) &bull; PP {p['pp_sh'] * 100:.1f}%</div></div>
          <div><div class="stat-label">SOG floor</div>
            <div class="stat-val">{_bar(p['e_sog'], 5.0, '#58a6ff')} {p['e_sog']:.1f} SOG</div>
            <div class="stat-sub">3+ SOG {p['p_sog3'] * 100:.0f}% &bull; 5+ SOG {p['p_sog5'] * 100:.0f}% &bull; 5v5 {p['e_sog_ev']:.1f} + PP {p['e_sog_pp']:.1f}</div></div>
          <div><div class="stat-label">Power play</div>
            <div class="stat-val">{p['pp_unit'] or 'no PP role'} &bull; {p['pp_toi'] / 60:.1f} min proj</div>
            <div class="stat-sub">{p['opp']} PK {p['opp_pk_xga60']:.1f} xGA/60 (#{p['opp_pk_rank']} worst) &bull; SH time #{p['opp_pen_rank']} most</div></div>
          <div><div class="stat-label">Matchup ({p['opp']} defense)</div>
            <div class="stat-val">{_bar(p['opp_sa_pg'], 36, '#f2cc60')} {p['opp_sa_pg']:.1f} SOG allowed/gm</div>
            <div class="stat-sub">#{p['opp_sa_rank']} most shots allowed &bull; 5v5 xGA/60 {p['opp_xga60']:.2f} (#{p['opp_xga_rank']})</div></div>
          <div><div class="stat-label">Opposing goalie</div>
            <div class="stat-val">{g_line}</div>
            <div class="stat-sub">{g_sub}</div></div>
          <div><div class="stat-label">Environment</div>
            <div class="stat-val">total {p['total']:.1f} &bull; {p['team']} implied {p['imp']:.1f}</div>
            <div class="stat-sub">{'home' if p['is_home'] else 'road'} &bull; {rest} &bull; win prob {p['p_win'] * 100:.0f}%</div></div>
          <div><div class="stat-label">Form (last 10)</div>
            <div class="stat-val">{p['r_sog']:.1f} SOG/gm &bull; {p['r_goals']} G &bull; {p['r_pts']} pts</div>
            <div class="stat-sub">SOG by game (* = goal): {last_str} &bull; TOI {p['r_toi'] / 60:.1f} vs {p['toi_pg'] / 60:.1f} <span style="color:{trend_clr}">{trend_txt}</span></div></div>
          <div><div class="stat-label">DraftKings projection</div>
            <div class="stat-val">{p['dk']:.1f} pts</div>
            <div class="stat-sub">{p['e_ast']:.2f} A &bull; {p['e_blk']:.1f} BLK &bull; season {p['sog_pg']:.1f} SOG/gm, {p['toi_pg'] / 60:.1f} min</div></div>
        </div>
        <div class="card-scout">{_esc(p['scout'])}</div>
      </div>
      <div class="card-score" style="color:{p['grade_color']}">{p['composite']:.0f}</div>
    </div>"""


def _goalie_card(g, rank, hidden):
    team_color = display_color(TEAMS.get(g["team"], ("", "#8b949e"))[1])
    home_away  = "vs" if g["is_home"] else "@"
    st = g["status"]
    cls = "conf" if st == "Confirmed" else ("bkp" if st == "Backup" else "proj")
    last = g.get("last_start") or "n/a"
    r_sv = f".{int(round(g['r_sv'] * 1000)):03d}" if g.get("r_sv") is not None else "n/a"
    gsax = f"{g['gsax']:+.1f} GSAx" if g.get("gsax") is not None else "no xG history"
    style = f' style="border-left:4px solid {team_color}"'
    return f"""
    <div class="player-card{' backup' if st == 'Backup' else ''}"{style} data-hidden="{1 if hidden else 0}" data-team="{g['team']}" data-main="{1 if g['main_slate'] else 0}">
      <div class="card-rank" style="background:{g['grade_color']}22;color:{g['grade_color']};border:1px solid {g['grade_color']}44">
        #{rank}<strong style="font-size:1.2rem">{g['grade']}</strong>
      </div>
      <div class="card-body">
        <div class="card-name">
          <span class="nm" style="color:{team_color}">{_esc(g['name'])}</span>
          <span class="ctx">G &bull; {g['team']} {home_away} {g['opp']} &bull; {g['start_str']} ET</span>
          <span class="tag {cls}">{_esc(st)}</span>
        </div>
        <div class="card-label" style="color:{g['grade_color']}">{g['dk']:.1f} DK projected &bull; {g['p_win'] * 100:.0f}% to win</div>
        <div class="card-stats">
          <div><div class="stat-label">Win odds</div>
            <div class="stat-val">{_bar(g['p_win'], 0.8, '#3fb950')} {g['p_win'] * 100:.0f}%</div>
            <div class="stat-sub">total {g['total']:.1f} &bull; {g['opp']} implied {g['imp_opp']:.1f} goals</div></div>
          <div><div class="stat-label">Workload</div>
            <div class="stat-val">{g['e_shots']:.1f} shots faced &bull; {g['e_saves']:.1f} saves</div>
            <div class="stat-sub">{g['opp']} fires {g['opp_sf_pg']:.1f} SOG/gm (#{g['opp_sf_rank']}) &bull; 35+ saves {g['p_35'] * 100:.0f}%</div></div>
          <div><div class="stat-label">Goals against</div>
            <div class="stat-val">{g['e_ga']:.2f} projected &bull; shutout {g['p_so'] * 100:.0f}%</div>
            <div class="stat-sub">{g['opp']} scores {g['opp_gf_pg']:.2f}/gm</div></div>
          <div><div class="stat-label">Save quality</div>
            <div class="stat-val">sv .{int(round(g['sv_pct'] * 1000)):03d} (regressed)</div>
            <div class="stat-sub">{gsax} &bull; last {g.get('r_starts', 0)} starts {r_sv} &bull; {g['shots_seen']} shots seen</div></div>
          <div><div class="stat-label">Status</div>
            <div class="stat-val">{_esc(st)} &bull; {g.get('record', '')}</div>
            <div class="stat-sub">{_esc(g['reason'])}</div></div>
          <div><div class="stat-label">Rest</div>
            <div class="stat-val">last start {last}</div>
            <div class="stat-sub">{'team on a back-to-back' if g['b2b'] else 'team rested'} &bull; {g['gs_cur']} starts this season</div></div>
        </div>
        <div class="card-scout">{_esc(g['scout'])}</div>
      </div>
      <div class="card-score" style="color:{g['grade_color']}">{g['dk']:.0f}</div>
    </div>"""


def _scorecard_html(history, total):
    if not history and not total.get("n"):
        return '<div class="empty">No graded slates yet. Every board is logged at puck drop and scored the next morning; the table fills in from day two.</div>'
    order = ["A+", "A", "B+", "B", "C", "D"]
    html = ""
    if total.get("n"):
        rows = ""
        for g in order:
            t = total["tiers"].get(g)
            if not t:
                continue
            gap = t["hit_rate"] - t["avg_p"]
            cls = "good" if abs(gap) < 0.04 else "bad"
            rows += (f'<tr><td><span style="color:{GRADE_COLORS[g]};font-weight:700">{g}</span></td>'
                     f'<td>{t["n"]}</td><td>{t["avg_p"] * 100:.0f}%</td><td class="{cls}">{t["hit_rate"] * 100:.0f}%</td>'
                     f'<td class="{cls}">{gap * 100:+.0f}</td></tr>')
        html += f"""
        <p class="muted" style="font-size:13px;margin-bottom:8px">All-time: {total['days']} slates, {total['n']} skater calls &bull;
          scored {total['hit_rate'] * 100:.0f}% of the time vs {total['avg_p'] * 100:.0f}% projected &bull;
          Brier {total['brier']:.3f} (0.25 = coin flip) &bull; SOG error {total['sog_mae']:.2f}/gm</p>
        <table class="score"><tr><th>Grade</th><th>Calls</th><th>Projected</th><th>Actually scored</th><th>Gap (pts)</th></tr>{rows}</table>"""
    if history:
        rows = ""
        for r in history:
            tiers = r["tiers"]
            top = [tiers[g] for g in ("A+", "A") if g in tiers]
            n_top = sum(t["n"] for t in top)
            top_txt = "-"
            if n_top:
                hr = sum(t["hits"] for t in top) / n_top
                pp = sum(t["p_sum"] for t in top) / n_top
                top_txt = f"{hr * 100:.0f}% / {pp * 100:.0f}% ({n_top})"
            gk = r.get("goalie", {})
            gk_txt = f"{gk.get('started', 0)}/{gk.get('n', 0)}" if gk.get("n") else "-"
            flag = ' <span class="muted">(partial)</span>' if r.get("partial") else ""
            rows += (f'<tr><td>{r["date"]}{flag}</td><td>{r["matched"]}</td>'
                     f'<td>{r["hit_rate"] * 100:.0f}% / {r["avg_p"] * 100:.0f}%</td><td>{top_txt}</td>'
                     f'<td>{r["brier"]:.3f}</td><td>{r["sog_mae"]:.2f}</td><td>{gk_txt}</td></tr>')
        html += f"""
        <table class="score"><tr><th>Slate</th><th>Calls</th><th>Scored / proj (all)</th><th>Scored / proj (A+ &amp; A)</th><th>Brier</th><th>SOG err</th><th>G starters right</th></tr>{rows}</table>"""
    return html


def render_html(slate_date, games, odds, by_pos, goalies, timestamp, history, total,
                preseason=False, backtest=False, lock_str=""):
    date_str = slate_date.strftime("%A, %B %d, %Y")
    banner = ""
    if backtest:
        banner = f'<div class="banner">BACKTEST -- {date_str} rebuilt with today\'s season data (rates leak the future; form and starters do not). Grades below are scored against the real box scores.</div>'
    elif preseason:
        banner = '<div class="banner">PRESEASON DRY RUN -- exhibition slate. Lines and goalies are guesses, half the rosters are prospects. This is the pipeline warming up; the real board starts on opening night.</div>'
    elif not games:
        banner = '<div class="banner">No NHL games today. Yesterday\'s scorecard is below.</div>'

    # Game chips
    chips = '<div class="game-chip active all-chip" id="all-chip" onclick="pickGame(this)"><div class="teams">All games</div><div class="time">tap a game to filter</div></div>'
    starter_of = {}
    for g in goalies:
        if g["status"] in ("Confirmed", "Projected"):
            starter_of[g["team"]] = g
    for g in games:
        o = odds.get(g["game_id"], {})
        fav = g["home"] if o.get("p_home", 0.5) >= 0.5 else g["away"]
        fav_p = max(o.get("p_home", 0.5), o.get("p_away", 0.5))

        def gtxt(team):
            s = starter_of.get(team)
            if not s:
                return f'<span class="g">{team}: ?</span>'
            c = "conf" if s["status"] == "Confirmed" else "proj"
            return f'<span class="g">{team}: <span class="{c}">{_esc(s["name"].split()[-1])}</span></span>'
        logo = lambda t: f'<img src="https://assets.nhle.com/logos/nhl/svg/{t}_dark.svg" alt="{t}" loading="lazy">'
        state = " &bull; FINAL" if g["final"] else (" &bull; LIVE" if g["started"] else "")
        chips += f"""<div class="game-chip" data-teams="{g['away']},{g['home']}" data-main="{1 if g['main_slate'] else 0}" onclick="pickGame(this)">
          <div class="teams">{logo(g['away'])}{g['away']} @ {logo(g['home'])}{g['home']}</div>
          <div class="time">{g['start_str']} ET{state} &bull; O/U {o.get('total', 6.0):.1f} &bull; {fav} {fav_p * 100:.0f}%</div>
          <div class="meta">{gtxt(g['away'])} &nbsp; {gtxt(g['home'])}</div></div>"""

    # Position sections
    sections = ""
    for pos, title in (("C", "Centers"), ("W", "Wingers"), ("D", "Defensemen")):
        plist = by_pos.get(pos, [])
        top_n = TOP_PER_POS.get(pos, 8)
        cards = "".join(_skater_card(p, i + 1, i >= top_n) for i, p in enumerate(plist))
        more = len(plist) - top_n
        btn = (f'<button class="show-all" onclick="showAll(\'sec-{pos}\', this)">Show all {len(plist)} {title.lower()} ({more} more)</button>'
               if more > 0 else "")
        body = cards + btn if plist else '<div class="empty">No skaters on the slate.</div>'
        sections += f'<h2 class="section-title" id="h-{pos}">{title} <small>ranked by anytime-goal probability, SOG floor as tiebreak</small></h2><div id="sec-{pos}">{body}</div>'

    gl = goalies
    top_g = TOP_PER_POS.get("G", 8)
    gcards = "".join(_goalie_card(g, i + 1, i >= top_g) for i, g in enumerate(gl))
    gmore = len(gl) - top_g
    gbtn = (f'<button class="show-all" onclick="showAll(\'sec-G\', this)">Show all {len(gl)} goalies incl. backups ({gmore} more)</button>'
            if gmore > 0 else "")
    gsec = (f'<h2 class="section-title" id="h-G">Goalies <small>projected DraftKings points; Confirmed beats Projected beats hope</small></h2>'
            f'<div id="sec-G">{gcards + gbtn if gl else "<div class=empty>No goalies on the slate.</div>"}</div>')

    scorecard = _scorecard_html(history, total)
    n_games = len(games)
    priced = sum(1 for v in odds.values() if v.get("source") != "default")
    conf = sum(1 for g in goalies if g["status"] == "Confirmed")

    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hockey Guru - NHL {slate_date.isoformat()}</title>
<link rel="manifest" href="manifest.json"><link rel="apple-touch-icon" href="icon.png"><link rel="icon" href="icon.png">
<meta name="theme-color" content="#0d1117">
<style>{CSS}</style>
</head><body>
<div id="splash"><div class="puck"></div><div class="splash-title">HOCKEY GURU</div><div class="splash-sub">who scores tonight</div></div>
<div class="header">
  <h1>HOCKEY GURU</h1>
  <p>NHL anytime-goal board &bull; {date_str} &bull; {n_games} games</p>
  <p>Generated {timestamp}{(' &bull; <span class="lock">first puck drop ' + lock_str + ' ET</span>') if lock_str else ''}</p>
  <p style="font-size:12px;color:#6e7681">lines priced {priced}/{n_games} &bull; {conf} confirmed goalies &bull; grades: {' '.join(f'<span style="color:{GRADE_COLORS[g]}">{g} &ge;{c:.0f}</span>' for g, c in SKATER_GRADES)}</p>
  {banner}
</div>
<div class="container">
  <div class="about" id="about">
    <button class="about-bar" onclick="toggleAbout()"><span><b style="color:#79c0ff">How this works</b> -- what the number means and where it comes from</span><span>&#9662;</span></button>
    <div class="about-body">
      <p><b>The headline is the chance a skater scores at least one goal tonight.</b> It is built bottom-up: projected 5v5 and power-play minutes &times; his shot rate in each &times; a finishing rate (what he has actually buried over two seasons, shrunk toward what his shot quality says he should convert), then adjusted for how many shots and chances the opponent gives up, how bad its penalty kill is, how often it takes penalties, who is in the opposing net, what Vegas expects the team to score, home ice and back-to-backs. Overtime, 4-on-4, 5-on-3 and empty-net goals ride on top as a league-average share.</p>
      <p><b>The SOG floor</b> is the same volume projection on its own -- the most predictable stat in hockey and worth 1.5 DK points a shot plus a bonus at five. The composite number on the right is goal% &times; 100 nudged by the floor; the letter grades are fixed cut-offs, not "top 5% of tonight".</p>
      <p><b>Goalies</b> are graded on projected DraftKings points: win probability from the moneyline, saves from the opponent's shot volume, goals against anchored half to the implied total. Status comes from DailyFaceoff beat-writer confirmations; without one we project the #1 by starts unless he played last night.</p>
      <p><b>The scorecard</b> at the bottom is the honesty check: every board is logged at puck drop and graded the next morning against the box scores. If the A+ rows do not score more often than the B rows, the model is wrong and the anchors move.</p>
      <p style="color:#8b949e">Data: NHL API (schedule, rosters, game logs, box scores), MoneyPuck (expected goals, line combos), ESPN (injuries), DailyFaceoff (starting goalies), The Odds API (DraftKings totals and moneylines).</p>
    </div>
  </div>
  <div class="toolbar"><button class="toggle" onclick="toggleMain(this)">Main slate only (7 PM+ ET)</button>
    <span class="muted" style="font-size:12px">Tap a game to see only its players.</span></div>
  <div class="games-row">{chips}</div>
  <div class="filter-banner" id="filter-banner"></div>
  {sections}
  {gsec}
  <h2 class="section-title">Scorecard <small>logged at puck drop, graded the next morning</small></h2>
  {scorecard}
</div>
<div class="footer">Hockey Guru &bull; paper picks only &bull; DK scoring: G {DK['goal']} / A {DK['assist']} / SOG {DK['sog']} / BLK {DK['block']} &bull; goalie W {DK['g_win']} / SV {DK['g_save']} / GA {DK['g_ga']}</div>
<script>{JS}</script>
</body></html>"""


# ============================================================
#  ICON + MANIFEST GENERATION
# ============================================================

def create_puck_png(size=180):
    """180x180 PNG of a puck on the board's navy -- no external libs needed."""
    import struct, zlib
    w = h = size
    cx, cy = w / 2, h * 0.50
    rx, ry = w * 0.40, h * 0.16          # puck seen from a low angle
    thick = h * 0.15
    BG, FACE, SIDE, EDGE, HL = (11, 29, 51, 255), (16, 16, 16, 255), (34, 34, 34, 255), (78, 78, 78, 255), (48, 48, 48, 255)
    rows = []
    for y in range(h):
        row = bytearray(b'\x00')         # PNG filter byte
        for x in range(w):
            dx = x - cx
            face = (dx / rx) ** 2 + ((y - cy) / ry) ** 2 <= 1.0
            side = (not face) and y > cy and (dx / rx) ** 2 + ((y - cy - thick) / ry) ** 2 <= 1.0
            band = face and (dx / rx) ** 2 + ((y - cy) / ry) ** 2 >= 0.84
            gleam = face and ((dx + rx * 0.25) / (rx * 0.5)) ** 2 + ((y - cy + ry * 0.2) / (ry * 0.4)) ** 2 <= 1.0
            if band:
                c = EDGE
            elif gleam:
                c = HL
            elif face:
                c = FACE
            elif side:
                c = SIDE
            else:
                c = BG
            row += bytes(c)
        rows.append(bytes(row))
    raw = b''.join(rows)

    def chunk(tag, data):
        body = tag + data
        return struct.pack('>I', len(data)) + body + struct.pack('>I', zlib.crc32(body) & 0xFFFFFFFF)

    png  = b'\x89PNG\r\n\x1a\n'
    png += chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 6, 0, 0, 0))
    png += chunk(b'IDAT', zlib.compress(raw, 9))
    png += chunk(b'IEND', b'')
    return png


def create_manifest():
    """Web app manifest so Android Chrome installs it as a standalone app."""
    return json.dumps({
        "name": "Hockey Guru", "short_name": "HockeyGuru",
        "description": "NHL Nightly Goal-Scorer Cheat Sheet",
        "start_url": "./", "display": "standalone",
        "background_color": "#0d1117", "theme_color": "#58a6ff",
        "orientation": "any",
        "icons": [{"src": "icon.png", "sizes": "180x180", "type": "image/png", "purpose": "any maskable"}],
    }, indent=2)


# ============================================================
#  GITHUB DEPLOY
# ============================================================

def deploy_file(content_bytes, filename, headers, commit_msg):
    """Push a single file (bytes) to the repo through the Contents API."""
    import base64
    url = f"https://api.github.com/repos/{GITHUB_USER}/{GITHUB_REPO}/contents/{filename}"
    try:
        sha = requests.get(url, headers=headers, timeout=20).json().get("sha")
    except Exception:
        sha = None
    body = {"message": commit_msg, "content": base64.b64encode(content_bytes).decode(), "branch": GITHUB_BRANCH}
    if sha:
        body["sha"] = sha
    r = requests.put(url, headers=headers, json=body, timeout=30)
    return r.status_code in (200, 201)


def deploy(html):
    if not GITHUB_TOKEN:
        print("  [!] No GITHUB_TOKEN -- skipping deploy")
        return False
    headers = _gh_headers()
    msg = f"Hockey Guru update {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    deploy_file(create_puck_png(), "icon.png", headers, msg)
    deploy_file(create_manifest().encode(), "manifest.json", headers, msg)
    ok = deploy_file(html.encode(), "index.html", headers, msg)
    print(f"  [OK] Deployed --> {PAGES_URL}" if ok else "  [!] Deploy failed")
    return ok


# ============================================================
#  MAIN
# ============================================================

# The moments the board NEEDS to be fresh, Eastern time. These are NOT the
# cron times -- GitHub's free cron runs 1-3 h late, so the yml fires several
# crons ahead of each window and the skip/quiet logic sorts out the pile-up.
#   7:00 AM  score last night, warm the caches
#  11:00 AM  morning skates are done, goalie reports are out
#   4:30 PM  final pre-lock board (main slate locks at the 7 PM game)
#   6:15 PM  last look: late goalie confirmations, scratches
SCHEDULE_ET = [(7, 0), (11, 0), (16, 30), (18, 15)]
SKIP_IF_FRESHER_THAN_MIN = 20    # a scheduled run exits if the board is this fresh
# A quiet refresh (nothing newly OUT, no net change, board recently pushed)
# earns no phone ping -- otherwise dense crons mean six identical pushes.
NOTIFY_QUIET_REFRESH_MIN = 120
RUN_CHECK_GRACE_MIN      = 55    # GitHub cron runs 30-60 min late; check after that
NTFY_RUN_CHECKS          = True
RUN_CHECK_PRIORITY       = "default"


def is_scheduled_run():
    return os.environ.get("GITHUB_EVENT_NAME", "") == "schedule"


def live_board_age_minutes():
    """Minutes since index.html was last committed, or None if unknown."""
    if not GITHUB_TOKEN:
        return None
    try:
        from datetime import timezone
        c = requests.get(f"https://api.github.com/repos/{GITHUB_USER}/{GITHUB_REPO}/commits",
                         params={"path": "index.html", "per_page": 1}, headers=_gh_headers(),
                         timeout=15).json()
        last = datetime.strptime(c[0]["commit"]["committer"]["date"],
                                 "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - last).total_seconds() / 60
    except Exception as e:
        print(f"  board-age check failed ({e}) -- running anyway")
        return None


def next_scheduled_slot(now):
    best = None
    for hh, mm in SCHEDULE_ET:
        cand = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if cand <= now:
            cand += timedelta(days=1)
        if best is None or cand < best:
            best = cand
    return best


def book_run_check(now):
    """
    Schedule a delayed ntfy message for shortly after the next expected run.
    If GitHub's cron never fires, nothing runs, so nothing can alert you --
    this is the only piece that works from outside GitHub. Worded as a check:
    if an "updated" push already came, ignore it.
    """
    if not (NTFY_ENABLED and NTFY_RUN_CHECKS):
        return
    slot = next_scheduled_slot(now)
    fire = slot + timedelta(minutes=RUN_CHECK_GRACE_MIN)
    state = cache_load("run_check_state", permanent=True) or {}
    if state.get("booked") == slot.isoformat():
        print(f"  Run-check for {slot.strftime('%a %I:%M %p')} already booked")
        return
    try:
        headers = {
            "Title": f"Fresh after {slot.strftime('%a %I:%M %p')}?".encode("utf-8"),
            "Priority": RUN_CHECK_PRIORITY, "Tags": "hourglass",
            "Delay": str(int(fire.timestamp())),
            "Actions": f"view, Run workflow, {ACTIONS_URL}; view, Open board, {PAGES_URL}",
        }
        body = (f"The board should have refreshed after {slot.strftime('%a %I:%M %p ET')}. "
                f"If you got an 'updated' push since then, ignore this. "
                f"If not, GitHub is behind -- tap Run workflow.")
        requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=body.encode("utf-8"), headers=headers, timeout=15)
        print(f"  Booked run-check for {fire.strftime('%a %I:%M %p ET')}")
        cache_save("run_check_state", {"booked": slot.isoformat()}, permanent=True)
    except Exception as e:
        print(f"  [!] Could not book run-check: {e}")


def parse_args():
    import argparse
    ap = argparse.ArgumentParser(description="Hockey Guru -- NHL nightly goal-scorer board")
    ap.add_argument("--date", help="slate date YYYY-MM-DD (default: today, Eastern)")
    ap.add_argument("--backtest", action="store_true",
                    help="grade the rebuilt slate against its box scores immediately (past dates)")
    ap.add_argument("--no-deploy", action="store_true", help="build locally only")
    ap.add_argument("--no-notify", action="store_true", help="no ntfy pushes")
    return ap.parse_args()


def run():
    global NTFY_ENABLED
    try:
        sys.stdout.reconfigure(line_buffering=True)   # Actions logs show progress live
    except Exception:
        pass
    args = parse_args()
    if args.no_notify:
        NTFY_ENABLED = False
    os.makedirs(OUTPUT_FOLDER, exist_ok=True)

    # Backup crons fire 30 min apart. If the primary already deployed, exit
    # quietly -- no double work, no double push. Manual runs always run.
    if is_scheduled_run():
        age = live_board_age_minutes()
        if age is not None and age < SKIP_IF_FRESHER_THAN_MIN:
            print(f"Scheduled run: board deployed {age:.0f} min ago -- skipping (backup trigger).")
            return
        if age is not None:
            print(f"Scheduled run: board is {age:.0f} min old -- running.")

    now = now_et()
    timestamp  = now.strftime("%Y-%m-%d %I:%M %p %Z")
    slate_date = date.fromisoformat(args.date) if args.date else now.date()
    backtest   = bool(args.backtest)
    do_deploy  = not (args.no_deploy or backtest)
    season_id, mp_year = season_ids(slate_date)
    prior_id, prior_mp = season_id - 10001, mp_year - 1
    cur_season_id, cur_mp = season_ids(now.date())

    print(f"\n[HOCKEY GURU] NHL {slate_date}  --  {timestamp}{'  (BACKTEST)' if backtest else ''}\n")

    print("  Fetching the slate...")
    games, preseason = get_slate(slate_date)
    gtype = 1 if preseason else 2
    print(f"  {len(games)} games{' (PRESEASON dry run)' if preseason else ''}")
    for g in games:
        print(f"    {g['away']} @ {g['home']}  {g['start_str']} ET  [{g['state']}]")
    print()

    print("  Fetching lines...")
    odds = get_odds(games, slate_date)
    print("  Fetching injuries...")
    injuries = get_injuries()
    print(f"  {sum(1 for v in injuries.values() if is_unavailable(v['status']))} players out league-wide")
    print("  Fetching starting goalies...")
    dfo = get_starting_goalies(slate_date)
    print()

    print("  Loading season stats...")
    rest_cur = get_league_stats(season_id, cur_season_id)
    rest_pri = get_league_stats(prior_id, cur_season_id)
    mp_cur   = get_moneypuck(mp_year, cur_mp)
    mp_pri   = get_moneypuck(prior_mp, cur_mp)
    lg       = league_baselines(mp_pri or mp_cur)
    teams_p  = team_profiles(mp_cur, mp_pri, lg)
    print()

    slate_teams = sorted({t for g in games for t in (g["home"], g["away"])})
    print(f"  Building the pool for {len(slate_teams)} teams...")
    if backtest:
        names = {pid: v["name"] for src in (rest_cur, rest_pri) for grp in ("skaters", "goalies")
                 for pid, v in src[grp].items()}
        rosters = rosters_from_boxscores(games, names)
    else:
        rosters = get_rosters(slate_teams)
    players = [p for t in slate_teams for p in rosters.get(t, [])]
    # Only players with an NHL line in either season get game logs pulled --
    # a September roster carries 15 prospects with nothing to fetch.
    known = {pid for src in (rest_cur, rest_pri) for grp in ("skaters", "goalies") for pid in src[grp]}
    rest = get_team_context(slate_teams, season_id, gtype, slate_date)
    logs = get_game_logs([p for p in players if p["pid"] in known], season_id, prior_id, gtype, slate_date,
                         fetch_cur=not preseason)
    skaters = [skater_profile(p, rest_cur, rest_pri, mp_cur, mp_pri, logs, lg, injuries)
               for p in players if p["pos"] != "G"]
    attach_lines(skaters, mp_cur, mp_pri)
    goalie_profiles = [goalie_profile(p, rest_cur, rest_pri, mp_cur, mp_pri, logs, lg)
                       for p in players if p["pos"] == "G"]
    print(f"  {len(skaters)} skaters, {len(goalie_profiles)} goalies\n")

    print("  Scoring matchups...")
    ctx = {"lg": lg, "teams": teams_p, "rest": rest, "opp_goalie": {}, "slate_date": slate_date}
    goalie_rows = []
    for g in games:
        for team in (g["home"], g["away"]):
            tg = [x for x in goalie_profiles if x["team"] == team]
            pick = expected_starters(team, tg, dfo, rest.get(team, {}), slate_date, teams_p[team]["gp_cur"], lg)
            if pick["starter"]:
                s = pick["starter"]
                goalie_rows.append(score_goalie(s, pick["status"], pick["reason"], g, odds, ctx))
                ctx["opp_goalie"][team] = {"name": s["name"], "factor": s["factor"], "status": pick["status"],
                                           "sv_pct": s["sv_pct"], "gsax": s.get("gsax"), "backup": pick["is_backup"]}
            for o in pick["others"]:
                goalie_rows.append(score_goalie(o, "Backup", "not expected to start", g, odds, ctx))
    skater_rows = []
    for g in games:
        for p in skaters:
            if p["team"] in (g["home"], g["away"]):
                skater_rows.append(score_skater(p, g, odds, ctx))
    by_pos = {}
    for r in skater_rows:
        by_pos.setdefault(r["dk_pos"], []).append(r)
    for pos in by_pos:
        by_pos[pos].sort(key=lambda x: (x["grade"] == "OUT", -x["composite"]))
    goalie_rows.sort(key=lambda x: (x["status"] == "Backup", -x["dk"]))
    ranked = sorted((r for r in skater_rows if r["grade"] != "OUT"), key=lambda x: -x["composite"])
    for r in ranked[:10]:
        print(f"    {r['grade']:<3} {r['name']:<24} {r['team']} {'vs' if r['is_home'] else '@ '} {r['opp']:<3} "
              f"{r['p_goal'] * 100:4.0f}% goal  {r['e_sog']:.1f} SOG  {r['dk']:.1f} DK")
    print()

    print("  Logging predictions for later scoring...")
    if not backtest:
        sync_predictions_down(slate_date)
    prev_out      = logged_out(slate_date) if not backtest else None
    prev_starters = logged_starters(slate_date) if not backtest else None
    board_age     = live_board_age_minutes() if do_deploy else None
    pred_path     = log_predictions(slate_date, skater_rows, goalie_rows, games, preseason, backtest)
    if not backtest:
        sync_predictions_up(pred_path)
    now_out   = {r["name"] for r in skater_rows if r["grade"] == "OUT"}
    newly_out = sorted(now_out - prev_out) if prev_out is not None else []
    now_starters = {r["team"]: r["name"] for r in goalie_rows if r["status"] in ("Confirmed", "Projected")}
    net_changes  = ([(t, prev_starters[t], n) for t, n in now_starters.items()
                     if t in prev_starters and prev_starters[t] != n] if prev_starters else [])

    if backtest:
        result = score_date(slate_date, backtest=True, force=True)
        history, total = ([result] if result else []), {}
        if result:
            print(f"  Backtest: {result['matched']} calls, scored {result['hit_rate'] * 100:.0f}% vs "
                  f"{result['avg_p'] * 100:.0f}% projected, Brier {result['brier']}, SOG err {result['sog_mae']}")
            for g in ("A+", "A", "B+", "B", "C", "D"):
                t = result["tiers"].get(g)
                if t:
                    print(f"    {g:<3} n={t['n']:<4} proj {t['avg_p'] * 100:4.0f}%  actual {t['hit_rate'] * 100:4.0f}%  "
                          f"SOG {t['avg_e_sog']:.2f} proj / {t['avg_sog']:.2f} actual")
    else:
        history, total = accuracy_history(slate_date)
        if history:
            print(f"  Scored {len(history)} prior slate(s) against box scores")

    print("  Rendering dashboard...")
    lock_str = games[0]["start_str"] if games else ""
    html = render_html(slate_date, games, odds, by_pos, goalie_rows, timestamp, history, total,
                       preseason=preseason, backtest=backtest, lock_str=lock_str)
    fname = f"nhl_{slate_date.isoformat()}{'_backtest' if backtest else ''}.html"
    fpath = os.path.join(OUTPUT_FOLDER, fname)
    with open(fpath, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  [OK] Saved: {fpath}")
    if AUTO_OPEN_BROWSER:
        try:
            webbrowser.open("file://" + os.path.abspath(fpath))
        except Exception:
            pass

    if not do_deploy:
        print("  Deploy skipped.")
        return

    deployed = deploy(html)
    if deployed and verify_live(timestamp):
        quiet = (board_age is not None and board_age < NOTIFY_QUIET_REFRESH_MIN
                 and not newly_out and not net_changes and is_scheduled_run())
        if NTFY_NOTIFY_SUCCESS and not quiet:
            top = ranked[0] if ranked else None
            lines = []
            if top:
                lines.append(f"Top play: {top['name']} ({top['team']} {'vs' if top['is_home'] else '@'} {top['opp']}) "
                             f"{top['grade']} -- {top['p_goal'] * 100:.0f}% to score, {top['e_sog']:.1f} SOG")
            best_g = next((g for g in goalie_rows if g["status"] != "Backup"), None)
            if best_g:
                lines.append(f"Net: {best_g['name']} ({best_g['team']}, {best_g['status'].lower()}) {best_g['dk']:.0f} DK proj")
            if newly_out:
                lines.append("Newly OUT: " + ", ".join(newly_out[:6]))
            if net_changes:
                lines.append("Goalie change: " + "; ".join(f"{t} {a.split()[-1]} -> {b.split()[-1]}" for t, a, b in net_changes[:4]))
            if history and history[0]["date"] == (slate_date - timedelta(days=1)).isoformat():
                y = history[0]
                lines.append(f"Yesterday: scored {y['hit_rate'] * 100:.0f}% vs {y['avg_p'] * 100:.0f}% projected "
                             f"({y['matched']} calls, Brier {y['brier']:.3f})")
            title = f"Hockey Guru: {len(games)} games{' (preseason)' if preseason else ''}" if games else "Hockey Guru: no games today"
            notify(title, "\n".join(lines) or "Board updated.", tags="ice_hockey", click=PAGES_URL)
        elif quiet:
            print("  Quiet refresh -- no push")
    elif deployed:
        notify("Hockey Guru: Pages stale", "Push succeeded but the live site did not update. Tap Run workflow.",
               tags="warning", priority="high", click=PAGES_URL)
    if is_scheduled_run() or os.environ.get("GITHUB_ACTIONS") == "true":
        book_run_check(now_et())


if __name__ == "__main__":
    try:
        run()
    except SystemExit:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        notify("Hockey Guru FAILED", f"{type(e).__name__}: {e}", tags="rotating_light", priority="high")
        sys.exit(1)
