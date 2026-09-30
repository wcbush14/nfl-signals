"""
Polymarket US signal checker: NFL, college football, and MLB.

Every few hours (run automatically by GitHub Actions) this:
  1. Pulls live moneylines from many sportsbooks and live Polymarket US prices
     (the actual price to buy each team, plus Polymarket US's trading fee).
  2. Turns any game where a team is cheaper on Polymarket US than at the
     sportsbooks, after fees, into a plain-English signal: which team, the
     most to pay, and how much.
  3. Sends that signal to your phone.
  4. Grades past signals: did the price move your way, did the team win,
     and what the paper profit would have been.

Settings (leagues, practice vs. live mode, bankroll, edge needed) are in settings.json.
"""

import json
import os
import re
import statistics
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

HERE = Path(__file__).parent
DATA = HERE / "nfl_data"
SETTINGS = json.loads((HERE / "settings.json").read_text())

PM_US = "https://gateway.polymarket.us"
ODDS_BASE = "https://api.the-odds-api.com/v4/sports"
NFL_SCORES = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"
MLB_SCHEDULE = "https://statsapi.mlb.com/api/v1/schedule"
TAKER_FEE = 0.0695        # Polymarket US taker fee: 0.0695 x price x (1 - price) per contract
MIN_EDGE_AT_LIMIT = 0.01  # "buy at X or less" still leaves a 1-point edge after fees
DAYS_AHEAD = 8

LEAGUES = {
    "nfl": {"label": "NFL", "odds_key": "americanfootball_nfl", "pm_slugs": ["nfl"],
            "log": "live_log_us.csv", "signals": "signals_us.csv"},
    "cfb": {"label": "CFB", "odds_key": "americanfootball_ncaaf",
            "pm_slugs": ["cfb", "ncaaf", "college-football", "ncaa-football", "ncaafb"],
            "log": "live_log_cfb.csv", "signals": "signals_cfb.csv"},
    "mlb": {"label": "MLB", "odds_key": "baseball_mlb", "pm_slugs": ["mlb"],
            "log": "live_log_mlb.csv", "signals": "signals_mlb.csv"},
}
ACTIVE = [lg for lg in SETTINGS.get("leagues", ["nfl"]) if lg in LEAGUES]

try:
    from zoneinfo import ZoneInfo
    CENTRAL = ZoneInfo("America/Chicago")
except Exception:
    CENTRAL = timezone(timedelta(hours=-5))

NICKNAMES = {
    "cardinals": "ARI", "falcons": "ATL", "ravens": "BAL", "bills": "BUF",
    "panthers": "CAR", "bears": "CHI", "bengals": "CIN", "browns": "CLE",
    "cowboys": "DAL", "broncos": "DEN", "lions": "DET", "packers": "GB",
    "texans": "HOU", "colts": "IND", "jaguars": "JAX", "chiefs": "KC",
    "raiders": "LV", "chargers": "LAC", "rams": "LA", "dolphins": "MIA",
    "vikings": "MIN", "patriots": "NE", "saints": "NO", "giants": "NYG",
    "jets": "NYJ", "eagles": "PHI", "steelers": "PIT", "49ers": "SF",
    "seahawks": "SEA", "buccaneers": "TB", "titans": "TEN",
    "commanders": "WAS",
}
TEAM_NAMES = {code: nick.title() for nick, code in NICKNAMES.items()}
TEAM_NAMES["SF"] = "49ers"

# MLB: two-word nicknames first so "Red Sox" and "White Sox" don't get mixed up
MLB_NICKNAMES = {
    "red sox": "BOS", "white sox": "CWS", "blue jays": "TOR", "diamondbacks": "ARI",
    "d-backs": "ARI", "braves": "ATL", "orioles": "BAL", "cubs": "CHC", "reds": "CIN",
    "guardians": "CLE", "rockies": "COL", "tigers": "DET", "astros": "HOU",
    "royals": "KC", "angels": "LAA", "dodgers": "LAD", "marlins": "MIA",
    "brewers": "MIL", "twins": "MIN", "mets": "NYM", "yankees": "NYY",
    "athletics": "ATH", "phillies": "PHI", "pirates": "PIT", "padres": "SD",
    "giants": "SF", "mariners": "SEA", "cardinals": "STL", "rays": "TB",
    "rangers": "TEX", "nationals": "WSH",
}
MLB_NAMES = {code: nick.title() for nick, code in MLB_NICKNAMES.items()}
MLB_NAMES.update({"BOS": "Red Sox", "CWS": "White Sox", "TOR": "Blue Jays", "ARI": "Diamondbacks"})
CODED = {"nfl": (NICKNAMES, TEAM_NAMES), "mlb": (MLB_NICKNAMES, MLB_NAMES)}

credits_left = "?"
pm_links = {}   # league -> Polymarket US page that worked


class CheckError(Exception):
    """A problem to show in plain English."""


# ---------- Small helpers ----------
def path(lg, kind):
    return DATA / LEAGUES[lg][kind]


def team_code(lg, name):
    """Short team code for leagues with a fixed team list (NFL, MLB)."""
    name = str(name).lower()
    for nick, code in CODED[lg][0].items():
        if nick in name:
            return code
    return None


def nfl_code(name):
    return team_code("nfl", name)


def display(lg, team):
    return CODED[lg][1].get(team, team) if lg in CODED else team


def fetch(url, params=None):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "sports-signals/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read()), resp.headers


def implied(american):
    return -american / (-american + 100) if american < 0 else 100 / (american + 100)


def central(utc_text, fmt="%a %I:%M %p"):
    t = datetime.strptime(utc_text, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    return t.astimezone(CENTRAL).strftime(fmt).replace(" 0", " ")


def fee(price):
    """Polymarket US taker fee per contract, in dollars."""
    return TAKER_FEE * price * (1 - price)


def all_in(price):
    """What one contract really costs: price plus the trading fee."""
    return price + fee(price)


def _amount(obj):
    try:
        return float((obj or {}).get("value"))
    except (TypeError, ValueError):
        return None


def odds_api_key():
    key = os.environ.get("ODDS_API_KEY", "").strip()
    if not key and (HERE / "odds_api_key.txt").exists():
        key = (HERE / "odds_api_key.txt").read_text().strip()
    if not key:
        raise CheckError("The Odds API key is missing. Add the ODDS_API_KEY secret.")
    return key


def odds_api(url, params):
    global credits_left
    try:
        data, headers = fetch(url, {"apiKey": odds_api_key(), **params})
    except urllib.error.HTTPError as err:
        if err.code in (401, 403):
            raise CheckError("The Odds API rejected the key. Check the ODDS_API_KEY secret.")
        if err.code == 429:
            raise CheckError("Out of Odds API credits for this month.")
        raise
    credits_left = headers.get("x-requests-remaining", credits_left)
    return data


# ---------- College team-name matching ----------
def _tokens(text):
    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode().lower()
    text = text.replace("&", " and ").replace("'", "").replace("(", " ").replace(")", " ")
    return [t for t in re.findall(r"[a-z0-9]+", text) if t not in {"the", "of", "university"}]


def name_weights(names):
    """Rare words (mascots like 'buckeyes') count more than common ones ('state', 'tigers')."""
    counts = {}
    for n in names:
        for t in set(_tokens(n)):
            counts[t] = counts.get(t, 0) + 1
    return {t: 1 / c ** 0.5 for t, c in counts.items()}


def name_score(odds_name, pm_text, pm_abbr, weights):
    shared = set(_tokens(odds_name)) & set(_tokens(pm_text))
    score = sum(weights.get(t, 1.0) for t in shared)
    abbr = re.sub(r"[^a-z0-9]", "", str(pm_abbr or "").lower())
    if len(abbr) >= 3 and "".join(_tokens(odds_name)).startswith(abbr):
        score += 0.5
    return score


# ---------- Live prices ----------
def sportsbook_odds(lg):
    events = odds_api(f"{ODDS_BASE}/{LEAGUES[lg]['odds_key']}/odds",
                      {"regions": "us", "markets": "h2h", "oddsFormat": "american"})
    now = datetime.now(timezone.utc)
    rows = []
    for e in events:
        kickoff = pd.to_datetime(e["commence_time"], utc=True).to_pydatetime()
        if not (now < kickoff < now + timedelta(days=DAYS_AHEAD)):
            continue
        home_raw, away_raw = e["home_team"], e["away_team"]
        home = team_code(lg, home_raw) if lg in CODED else home_raw
        away = team_code(lg, away_raw) if lg in CODED else away_raw
        if not home or not away:
            continue
        probs = []
        for book in e.get("bookmakers", []):
            for market in book.get("markets", []):
                if market.get("key") != "h2h":
                    continue
                prices = {o["name"]: o["price"] for o in market["outcomes"]}
                if home_raw in prices and away_raw in prices:
                    h, a = implied(prices[home_raw]), implied(prices[away_raw])
                    probs.append(h / (h + a))  # remove the book's cut
        if probs:
            rows.append({"kickoff_utc": kickoff, "away_team": away, "home_team": home,
                         "books_home_prob": statistics.median(probs),
                         "books_low": min(probs), "books_high": max(probs),
                         "num_books": len(probs)})
    return pd.DataFrame(rows)


def league_events(lg):
    """All upcoming game events for a league on Polymarket US."""
    for slug in LEAGUES[lg]["pm_slugs"]:
        events, offset = [], 0
        try:
            while offset < 1000:
                data, _ = fetch(f"{PM_US}/v2/leagues/{slug}/events",
                                {"limit": 100, "offset": offset})
                batch = data.get("events", []) if isinstance(data, dict) else (data or [])
                events += batch
                if len(batch) < 100:
                    break
                offset += 100
        except urllib.error.HTTPError:
            continue
        if events:
            pm_links[lg] = (SETTINGS.get("polymarket_link") if lg == "nfl"
                            else f"https://polymarket.us/sports/{slug}")  # league page
            return events
    return []


def polymarket_prices(lg):
    """Buy prices for both teams in every upcoming game on Polymarket US."""
    rows = []
    for e in league_events(lg):
        if e.get("live") or e.get("ended") or e.get("closed"):
            continue
        start = None
        for field in ("startTime", "eventDate", "startDate"):
            if e.get(field):
                try:
                    start = pd.to_datetime(e[field], utc=True).to_pydatetime()
                    break
                except Exception:
                    pass
        teams_by_id = {t.get("id"): t for t in (e.get("teams") or [])}
        for m in e.get("markets") or []:
            kind = str(m.get("sportsMarketType") or m.get("marketType") or "").lower()
            label = str(m.get("title") or m.get("question") or "").lower()
            if "moneyline" not in kind or m.get("closed") or m.get("hidden"):
                continue
            if any(w in label for w in ("1h", "half", "quarter", "q1", "2h")):
                continue
            sides = m.get("marketSides") or []
            long_side = next((x for x in sides if x.get("long") is True), None)
            short_side = next((x for x in sides if x.get("long") is False), None)
            if not long_side or not short_side:
                continue

            def describe(side):
                t = side.get("team") or teams_by_id.get(side.get("teamId")) or {}
                text = " ".join(str(v) for v in (t.get("name"), t.get("alias"), t.get("safeName"),
                                                 side.get("description")) if v)
                abbr = t.get("abbreviation") or t.get("displayAbbreviation") or ""
                return text, abbr

            a_text, a_abbr = describe(long_side)
            b_text, b_abbr = describe(short_side)
            if not a_text or not b_text:
                continue
            # One instrument per game: "long team wins". Buying the long team
            # costs the ask; buying the other team (shorting) costs 1 - bid.
            bid, ask = _amount(m.get("bestBidQuote")), _amount(m.get("bestAskQuote"))
            if bid is None or ask is None:
                try:
                    bbo, _ = fetch(f"{PM_US}/v1/markets/{m.get('slug')}/bbo")
                    md = bbo.get("marketData", {})
                    bid, ask = _amount(md.get("bestBid")), _amount(md.get("bestAsk"))
                except Exception:
                    continue
            if bid is None or ask is None or not 0 < bid < ask < 1:
                continue
            rows.append({"a_text": a_text, "b_text": b_text, "a_abbr": a_abbr, "b_abbr": b_abbr,
                         "start": start, "a_buy": ask, "b_buy": 1 - bid, "a_mid": (bid + ask) / 2,
                         "pm_volume": float(m.get("volume") or 0)})
    return pd.DataFrame(rows)


def match_games(lg, books, pm):
    """Pair each sportsbook game with its Polymarket US market: (game row, market row, home_is_a)."""
    pairs = []
    if lg in CODED:
        pm = pm.assign(team_a=pm.a_text.map(lambda t: team_code(lg, t)),
                       team_b=pm.b_text.map(lambda t: team_code(lg, t)))
        used = set()
        for _, g in books.sort_values("kickoff_utc").iterrows():
            match = pm[(((pm.team_a == g.home_team) & (pm.team_b == g.away_team)) |
                        ((pm.team_a == g.away_team) & (pm.team_b == g.home_team))) &
                       ~pm.index.isin(used)]
            if match.empty:
                continue
            # Same two teams can meet twice (baseball doubleheaders, series):
            # pick the market whose start time is closest to this game.
            gap = match.start.map(lambda t: abs((t - g.kickoff_utc).total_seconds())
                                  if t is not None and not pd.isna(t) else 0)
            match = match.assign(time_gap=gap)
            m = match.sort_values(["time_gap", "pm_volume"], ascending=[True, False]).iloc[0]
            if lg == "mlb" and m.time_gap > 36 * 3600:
                continue  # that market is for a different series
            used.add(m.name)
            pairs.append((g, m, m.team_a == g.home_team))
        return pairs

    weights = name_weights(list(books.home_team) + list(books.away_team))
    options = []
    for gi, g in books.iterrows():
        for mi, m in pm.iterrows():
            if m.start is not None and not pd.isna(m.start):
                if abs((m.start - g.kickoff_utc).total_seconds()) > 36 * 3600:
                    continue  # different week
            for home_is_a in (True, False):
                h_text, h_abbr = (m.a_text, m.a_abbr) if home_is_a else (m.b_text, m.b_abbr)
                a_text, a_abbr = (m.b_text, m.b_abbr) if home_is_a else (m.a_text, m.a_abbr)
                hs = name_score(g.home_team, h_text, h_abbr, weights)
                aw = name_score(g.away_team, a_text, a_abbr, weights)
                if min(hs, aw) >= 0.4 and hs + aw >= 1.0:  # skip ambiguous matches
                    options.append((hs + aw, gi, mi, home_is_a))
    # If a game fits two markets (or a market fits two games) about equally well,
    # skip it rather than guess, e.g. two "Tigers vs Bulldogs" games the same day.
    ambiguous_g, ambiguous_m = set(), set()
    for idx, bad in ((1, ambiguous_g), (2, ambiguous_m)):
        best = {}
        for o in options:
            best[o[idx]] = max(best.get(o[idx], 0), o[0])
        for key, top in best.items():
            rivals = {o[3 - idx] for o in options if o[idx] == key and o[0] >= top - 0.05}
            if len(rivals) > 1:
                bad.add(key)
    options = [o for o in options if o[1] not in ambiguous_g and o[2] not in ambiguous_m]

    used_g, used_m = set(), set()
    for score, gi, mi, home_is_a in sorted(options, key=lambda o: -o[0]):
        if gi in used_g or mi in used_m:
            continue
        used_g.add(gi)
        used_m.add(mi)
        pairs.append((books.loc[gi], pm.loc[mi], home_is_a))
    return pairs


def compare_now(lg):
    """Live Polymarket US vs. sportsbooks, one row per game."""
    books = sportsbook_odds(lg)
    pm = polymarket_prices(lg)
    if books.empty or pm.empty:
        return pd.DataFrame(), len(books), len(pm)
    now = datetime.now(timezone.utc)
    rows = []
    for g, m, home_is_a in match_games(lg, books, pm):
        pm_home = m.a_mid if home_is_a else 1 - m.a_mid
        home_buy = m.a_buy if home_is_a else m.b_buy
        away_buy = m.b_buy if home_is_a else m.a_buy
        rows.append({
            "checked_at": now.strftime("%Y-%m-%d %H:%M"),
            "kickoff_utc": g.kickoff_utc.strftime("%Y-%m-%d %H:%M"),
            "hours_to_kickoff": round((g.kickoff_utc - now).total_seconds() / 3600, 1),
            "away_team": g.away_team, "home_team": g.home_team,
            "pm_home_price": round(pm_home, 4), "books_home_prob": round(g.books_home_prob, 4),
            "books_low": round(g.books_low, 4), "books_high": round(g.books_high, 4),
            "num_books": g.num_books, "gap": round(pm_home - g.books_home_prob, 4),
            "pm_home_buy": round(home_buy, 4), "pm_away_buy": round(away_buy, 4),
            "home_edge": round(g.books_home_prob - all_in(home_buy), 4),
            "away_edge": round((1 - g.books_home_prob) - all_in(away_buy), 4),
            "pm_volume": m.pm_volume, "pm_link": pm_links.get(lg, "https://polymarket.us"),
        })
    return pd.DataFrame(rows), len(books), len(pm)


# ---------- Signals ----------
def find_signals(snapshot, lg):
    """Teams that are cheaper on Polymarket US than at the books, after fees."""
    need = SETTINGS["edge_needed_pts"] / 100
    out = []
    for r in snapshot.itertuples():
        if r.hours_to_kickoff < 0.25:
            continue
        for team, buy, books, edge in (
                (r.home_team, r.pm_home_buy, r.books_home_prob, r.home_edge),
                (r.away_team, r.pm_away_buy, 1 - r.books_home_prob, r.away_edge)):
            if edge < need:
                continue
            max_price = max((c / 100 for c in range(1, 100)
                             if books - all_in(c / 100) >= MIN_EDGE_AT_LIMIT), default=buy)
            max_price = max(max_price, round(buy, 2))
            pay = all_in(buy)
            kelly = max(0.0, (books - pay) / (1 - pay)) / 4   # quarter-Kelly
            pct = min(kelly, SETTINGS["max_bet_pct"] / 100)
            out.append({
                "signal_id": (f"{r.kickoff_utc[:10]}_{r.away_team}_{r.home_team}_{team}"
                              if lg == "nfl" else
                              f"{lg}_{r.kickoff_utc.replace(' ', 'T')}_{r.away_team}_"
                              f"{r.home_team}_{team}"),
                "league": lg, "found_at": r.checked_at, "kickoff_utc": r.kickoff_utc,
                "away_team": r.away_team, "home_team": r.home_team, "team": team,
                "entry_price": round(buy, 3), "max_price": max_price,
                "books_prob": round(books, 3), "edge_pts": round(edge * 100, 1),
                "stake": round(SETTINGS["bankroll"] * pct), "mode": SETTINGS["mode"],
                "pm_link": r.pm_link,
            })
    return pd.DataFrame(out)


def signal_message(s):
    lg = s.get("league", "nfl")
    tag = LEAGUES[lg]["label"]
    name = display(lg, s["team"])
    game = f"{display(lg, s['away_team'])} @ {display(lg, s['home_team'])}"
    when = central(s["kickoff_utc"])
    if s["mode"] == "live":
        title = f"{tag} bet: {name}, ${s['stake']:.0f}"
        body = (f"Buy {name} on Polymarket US at {s['max_price'] * 100:.0f}c or less. "
                f"Stake ${s['stake']:.0f}. Books give them {s['books_prob'] * 100:.0f}%, "
                f"a {s['edge_pts']:.1f}-pt edge after fees. {game}, {when} CT.")
    else:
        title = f"{tag} practice signal: {name}"
        body = (f"{name} at {s['entry_price'] * 100:.0f}c on Polymarket US, books say "
                f"{s['books_prob'] * 100:.0f}% ({s['edge_pts']:.1f}-pt edge after fees). "
                f"Would buy at {s['max_price'] * 100:.0f}c or less (${s['stake']:.0f}). "
                f"{game}, {when} CT. Practice mode: no real bet.")
    return title, body


def notify(title, body, link=None, sport="football"):
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        return False
    headers = {"Title": title.encode("ascii", "ignore").decode(), "Tags": sport,
               "Priority": "high"}
    if link:
        headers["Click"] = link
    req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=body.encode("utf-8"),
                                 headers=headers, method="POST")
    urllib.request.urlopen(req, timeout=15)
    return True


def load_signals(lg):
    p = path(lg, "signals")
    return pd.read_csv(p) if p.exists() else pd.DataFrame()


def record_new_signals(found, lg):
    existing = load_signals(lg)
    seen = set(existing["signal_id"]) if not existing.empty else set()
    new = found[~found["signal_id"].isin(seen)] if not found.empty else found
    for s in new.to_dict("records"):
        notify(*signal_message(s), s["pm_link"], "baseball" if lg == "mlb" else "football")
    if not new.empty:
        pd.concat([existing, new], ignore_index=True).to_csv(path(lg, "signals"), index=False)
    return new


# ---------- Grading past signals ----------
def results_table(lg):
    """Final margins (home minus away) for recent games."""
    if lg == "nfl":
        s = pd.read_csv(NFL_SCORES, usecols=["gameday", "away_team", "home_team", "result"])
        return s.rename(columns={"result": "margin"})
    if lg == "mlb":
        today = datetime.now(timezone.utc).date()
        data, _ = fetch(MLB_SCHEDULE, {"sportId": 1, "startDate": str(today - timedelta(days=4)),
                                       "endDate": str(today)})
        rows = []
        for day in data.get("dates", []):
            for g in day.get("games", []):
                if g.get("status", {}).get("abstractGameState") != "Final":
                    continue
                home, away = g["teams"]["home"], g["teams"]["away"]
                if "score" not in home or "score" not in away:
                    continue
                rows.append({"gameday": g["gameDate"][:10], "start": g["gameDate"],
                             "away_team": team_code("mlb", away["team"]["name"]),
                             "home_team": team_code("mlb", home["team"]["name"]),
                             "margin": home["score"] - away["score"]})
        return pd.DataFrame(rows, columns=["gameday", "start", "away_team", "home_team", "margin"])
    games = odds_api(f"{ODDS_BASE}/{LEAGUES[lg]['odds_key']}/scores", {"daysFrom": 3})
    rows = []
    for g in games:
        if not g.get("completed") or not g.get("scores"):
            continue
        pts = {s["name"]: float(s["score"]) for s in g["scores"]}
        if g["home_team"] in pts and g["away_team"] in pts:
            rows.append({"gameday": g["commence_time"][:10], "away_team": g["away_team"],
                         "home_team": g["home_team"],
                         "margin": pts[g["home_team"]] - pts[g["away_team"]]})
    return pd.DataFrame(rows, columns=["gameday", "away_team", "home_team", "margin"])


def grade_signals(lg):
    sig = load_signals(lg)
    if sig.empty:
        return sig
    for col in ["close_price", "won", "profit"]:
        if col not in sig:
            sig[col] = None
    now = datetime.now(timezone.utc)
    ready_before = (now - timedelta(hours=4)).strftime("%Y-%m-%d %H:%M")
    todo = sig[sig["won"].isna() & (sig["kickoff_utc"] < ready_before)]
    if todo.empty:
        return sig

    log_path = path(lg, "log")
    log = pd.read_csv(log_path) if log_path.exists() else pd.DataFrame()
    try:
        scores = results_table(lg)
    except Exception as err:
        print(f"  ({LEAGUES[lg]['label']} results not available yet: {err})")
        return sig

    for i, s in todo.iterrows():
        if not log.empty and pd.isna(s.close_price):
            before = log[(log.away_team == s.away_team) & (log.home_team == s.home_team) &
                         (log.kickoff_utc == s.kickoff_utc) & (log.checked_at < s.kickoff_utc)]
            if not before.empty:
                last = before.sort_values("checked_at").iloc[-1]
                close = last.pm_home_buy if s.team == s.home_team else last.pm_away_buy
                sig.at[i, "close_price"] = round(close, 3)
        days_apart = (pd.to_datetime(scores.gameday) - pd.to_datetime(s.kickoff_utc[:10])).abs()
        game = scores[(scores.away_team == s.away_team) & (scores.home_team == s.home_team) &
                      (days_apart <= pd.Timedelta(days=1))]
        if "start" in game and len(game) > 1:
            kick = pd.to_datetime(s.kickoff_utc, utc=True)
            game = game.assign(t=(pd.to_datetime(game.start, utc=True) - kick).abs()).sort_values("t")
        if game.empty or pd.isna(game.iloc[0].margin):
            continue
        margin = game.iloc[0].margin
        if margin == 0:
            sig.at[i, "won"], sig.at[i, "profit"] = 0.5, 0.0
            continue
        won = (margin > 0) == (s.team == s.home_team)
        pay = all_in(s.entry_price)
        stake = s.stake if s.stake > 0 else 10
        sig.at[i, "won"] = 1.0 if won else 0.0
        sig.at[i, "profit"] = round(stake * (1 - pay) / pay if won else -stake, 2)
    sig.to_csv(path(lg, "signals"), index=False)
    return sig


def track_record(lg):
    sig = load_signals(lg)
    if sig.empty:
        return {"signals": 0}
    for col in ["close_price", "won", "profit"]:
        if col not in sig:
            sig[col] = None
    done = sig[sig["won"].notna()]
    closed = sig[sig["close_price"].notna()]
    return {
        "signals": len(sig), "graded": len(done),
        "wins": int((done["won"] == 1).sum()), "losses": int((done["won"] == 0).sum()),
        "profit": float(done["profit"].sum()) if len(done) else 0.0,
        "price_moved_our_way": int((closed["close_price"] > closed["entry_price"]).sum()),
        "price_checked": len(closed),
    }


# ---------- Daily summary ----------
SUMMARY_SENT = DATA / "summary_sent.txt"


def record_line(lg):
    rec = track_record(lg)
    if not rec["signals"]:
        return "No signals yet."
    line = f"Record: {rec['signals']} signal" + ("s" if rec["signals"] != 1 else "")
    if rec["graded"]:
        sign = "+" if rec["profit"] >= 0 else "-"
        line += f", {rec['wins']}-{rec['losses']}, {sign}${abs(rec['profit']):,.0f} paper"
    if rec["price_checked"]:
        line += f", price moved our way {rec['price_moved_our_way']}/{rec['price_checked']}"
    return line + "."


def daily_summary(snapshots, force=False):
    now_ct = datetime.now(timezone.utc).astimezone(CENTRAL)
    today = now_ct.strftime("%Y-%m-%d")
    already = SUMMARY_SENT.exists() and SUMMARY_SENT.read_text().strip() == today
    if not force and (already or not 6 <= now_ct.hour <= 8):
        return False

    parts = []
    for lg in ACTIVE:
        tag = LEAGUES[lg]["label"]
        snap = snapshots.get(lg, pd.DataFrame())
        open_now = find_signals(snap, lg) if not snap.empty else pd.DataFrame()
        lines = []
        if open_now.empty:
            lines.append("No bets right now.")
        else:
            for s in open_now.to_dict("records"):
                lines.append(f"Open: {display(lg, s['team'])} at {s['max_price'] * 100:.0f}c "
                             f"or less ({central(s['kickoff_utc'])} CT).")
        if not snap.empty:
            best = snap[["home_edge", "away_edge"]].max(axis=1)
            top = snap.loc[best.idxmax()]
            team = top.home_team if top.home_edge >= top.away_edge else top.away_team
            lines.append(f"Watching {len(snap)} games; closest: {display(lg, team)} "
                         f"{best.max() * 100:+.1f} pts after fees.")
        else:
            lines.append("No games matched right now.")
        lines.append(record_line(lg))
        parts.append(f"{tag}: " + " ".join(lines))
    parts.append(f"Signals fire at +{SETTINGS['edge_needed_pts']:.0f} pts. "
                 f"Odds API credits left: {credits_left}.")

    mode = "Practice mode" if SETTINGS["mode"] != "live" else "Live mode"
    if notify(f"Daily check ({mode})", "\n".join(parts)):
        DATA.mkdir(exist_ok=True)
        SUMMARY_SENT.write_text(today)
        return True
    return False


# ---------- Command line / GitHub Actions ----------
if __name__ == "__main__":
    testing = os.environ.get("TEST_ALERT", "").lower() == "true"
    if testing:
        sent = notify("Alerts are working", "Test notification from your signal checker.")
        print("Test alert sent." if sent else "No NTFY_TOPIC set, so no test alert was sent.")

    DATA.mkdir(exist_ok=True)
    snapshots, problems = {}, []
    for lg in ACTIVE:
        tag = LEAGUES[lg]["label"]
        try:
            snap, n_books, n_pm = compare_now(lg)
        except CheckError as err:
            problems.append(f"{tag}: {err}")
            continue
        except Exception as err:
            problems.append(f"{tag}: unexpected problem ({err})")
            continue
        print(f"{tag}: {n_books} games at the books, {n_pm} on Polymarket US, "
              f"{len(snap)} matched.")
        snapshots[lg] = snap
        if not snap.empty:
            log_path = path(lg, "log")
            old = pd.read_csv(log_path) if log_path.exists() else pd.DataFrame()
            pd.concat([old, snap], ignore_index=True).to_csv(log_path, index=False)
            new = record_new_signals(find_signals(snap, lg), lg)
            for s in new.to_dict("records"):
                print("  NEW: " + " | ".join(signal_message(s)))
        try:
            grade_signals(lg)
        except CheckError as err:
            problems.append(f"{tag} grading: {err}")
        print(f"  {record_line(lg)}")

    if daily_summary(snapshots, force=testing):
        print("Daily summary sent.")
    print("Odds API credits left:", credits_left)
    if problems:
        raise SystemExit("Problems:\n  " + "\n  ".join(problems))
