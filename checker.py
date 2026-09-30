"""
NFL Polymarket signal checker.

Every few hours (run automatically by GitHub Actions) this:
  1. Pulls live moneylines from ~9 sportsbooks and live Polymarket US prices
     (the actual price to buy each team, plus Polymarket US's trading fee).
  2. Turns any game where a team is cheaper on Polymarket US than at the
     sportsbooks, after fees, into a plain-English signal: which team, the
     most to pay, and how much.
  3. Sends that signal to your phone.
  4. Grades past signals: did the price move your way, did the team win,
     and what the paper profit would have been.

Settings (practice vs. live mode, bankroll, edge needed) are in settings.json.
"""

import json
import os
import statistics
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

HERE = Path(__file__).parent
DATA = HERE / "nfl_data"
LOG = DATA / "live_log_us.csv"
SIGNALS = DATA / "signals_us.csv"
SETTINGS = json.loads((HERE / "settings.json").read_text())

PM_US = "https://gateway.polymarket.us"
TAKER_FEE = 0.0695    # Polymarket US taker fee: 0.0695 x price x (1 - price) per contract
ODDS_API = "https://api.the-odds-api.com/v4/sports/americanfootball_nfl/odds"
SCORES = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"
MIN_EDGE_AT_LIMIT = 0.01  # "buy at X or less" still leaves a 1-point edge after fees
DAYS_AHEAD = 8

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

credits_left = "?"


class CheckError(Exception):
    """A problem to show in plain English."""


def team_code(name):
    name = str(name).lower()
    for nick, code in NICKNAMES.items():
        if nick in name:
            return code
    return None


def fetch(url, params=None):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "nfl-model/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read()), resp.headers


def as_list(value):
    return json.loads(value) if isinstance(value, str) else (value or [])


def implied(american):
    return -american / (-american + 100) if american < 0 else 100 / (american + 100)


def central(utc_text, fmt="%a %-I:%M %p"):
    t = datetime.strptime(utc_text, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    return t.astimezone(CENTRAL).strftime(fmt.replace("%-I", "%I")).replace(" 0", " ")


# ---------- Live prices ----------
def sportsbook_odds():
    global credits_left
    key = os.environ.get("ODDS_API_KEY", "").strip()
    if not key and (HERE / "odds_api_key.txt").exists():
        key = (HERE / "odds_api_key.txt").read_text().strip()
    if not key:
        raise CheckError("The Odds API key is missing. Add the ODDS_API_KEY secret.")
    try:
        events, headers = fetch(ODDS_API, {"apiKey": key, "regions": "us",
                                           "markets": "h2h", "oddsFormat": "american"})
    except urllib.error.HTTPError as err:
        if err.code in (401, 403):
            raise CheckError("The Odds API rejected the key. Check the ODDS_API_KEY secret.")
        if err.code == 429:
            raise CheckError("Out of Odds API credits for this month.")
        raise
    credits_left = headers.get("x-requests-remaining", "?")

    now = datetime.now(timezone.utc)
    rows = []
    for e in events:
        kickoff = pd.to_datetime(e["commence_time"], utc=True).to_pydatetime()
        home, away = team_code(e["home_team"]), team_code(e["away_team"])
        if not (now < kickoff < now + timedelta(days=DAYS_AHEAD)) or not home or not away:
            continue
        probs = []
        for book in e.get("bookmakers", []):
            for market in book.get("markets", []):
                if market.get("key") != "h2h":
                    continue
                prices = {team_code(o["name"]): o["price"] for o in market["outcomes"]}
                if home in prices and away in prices:
                    h, a = implied(prices[home]), implied(prices[away])
                    probs.append(h / (h + a))
        if probs:
            rows.append({"kickoff_utc": kickoff, "away_team": away, "home_team": home,
                         "books_home_prob": statistics.median(probs),
                         "books_low": min(probs), "books_high": max(probs),
                         "num_books": len(probs)})
    return pd.DataFrame(rows)


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


def polymarket_prices():
    """Buy prices for both teams in every upcoming NFL game on Polymarket US."""
    events, offset = [], 0
    while offset < 500:
        data, _ = fetch(f"{PM_US}/v2/leagues/nfl/events", {"limit": 100, "offset": offset})
        batch = data.get("events", []) if isinstance(data, dict) else (data or [])
        events += batch
        if len(batch) < 100:
            break
        offset += 100

    rows = []
    for e in events:
        if e.get("live") or e.get("ended") or e.get("closed"):
            continue
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

            def code(side):
                t = side.get("team") or teams_by_id.get(side.get("teamId")) or {}
                return team_code(" ".join(str(v) for v in (t.get("name"), t.get("alias"),
                                                            side.get("description")) if v))

            long_code, short_code = code(long_side), code(short_side)
            if not long_code or not short_code or long_code == short_code:
                continue

            # One instrument per game: "long team wins". Buying the long team
            # costs the ask; buying the other team (shorting) costs 1 - bid.
            bid, ask = _amount(m.get("bestBidQuote")), _amount(m.get("bestAskQuote"))
            depth = None
            if bid is None or ask is None:
                try:
                    bbo, _ = fetch(f"{PM_US}/v1/markets/{m.get('slug')}/bbo")
                    md = bbo.get("marketData", {})
                    bid, ask = _amount(md.get("bestBid")), _amount(md.get("bestAsk"))
                    depth = md.get("askShares")
                except Exception:
                    continue
            if bid is None or ask is None or not 0 < bid < ask < 1:
                continue
            rows.append({"team_a": long_code, "team_b": short_code,
                         "a_buy": ask, "b_buy": 1 - bid, "a_mid": (bid + ask) / 2,
                         "pm_volume": float(m.get("volume") or 0), "depth": depth,
                         "pm_link": SETTINGS["polymarket_link"]})
    return pd.DataFrame(rows)


def compare_now():
    """Live Polymarket vs. sportsbooks, one row per game."""
    books, pm = sportsbook_odds(), polymarket_prices()
    if books.empty or pm.empty:
        return pd.DataFrame()
    now = datetime.now(timezone.utc)
    rows = []
    for g in books.itertuples():
        match = pm[((pm.team_a == g.home_team) & (pm.team_b == g.away_team)) |
                   ((pm.team_a == g.away_team) & (pm.team_b == g.home_team))]
        if match.empty:
            continue
        m = match.sort_values("pm_volume", ascending=False).iloc[0]
        home_is_a = m.team_a == g.home_team
        pm_home = m.a_mid if home_is_a else 1 - m.a_mid
        home_buy = m.a_buy if home_is_a else m.b_buy
        away_buy = m.b_buy if home_is_a else m.a_buy
        home_edge = g.books_home_prob - all_in(home_buy)
        away_edge = (1 - g.books_home_prob) - all_in(away_buy)
        rows.append({
            "checked_at": now.strftime("%Y-%m-%d %H:%M"),
            "kickoff_utc": g.kickoff_utc.strftime("%Y-%m-%d %H:%M"),
            "hours_to_kickoff": round((g.kickoff_utc - now).total_seconds() / 3600, 1),
            "away_team": g.away_team, "home_team": g.home_team,
            "pm_home_price": round(pm_home, 4), "books_home_prob": round(g.books_home_prob, 4),
            "books_low": round(g.books_low, 4), "books_high": round(g.books_high, 4),
            "num_books": g.num_books, "gap": round(pm_home - g.books_home_prob, 4),
            "pm_home_buy": round(home_buy, 4), "pm_away_buy": round(away_buy, 4),
            "home_edge": round(home_edge, 4), "away_edge": round(away_edge, 4),
            "pm_volume": m.pm_volume, "pm_link": m.pm_link,
        })
    return pd.DataFrame(rows)


# ---------- Turning gaps into signals ----------
def find_signals(snapshot):
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
            # Highest price (in cents) that still leaves a small edge after fees
            max_price = max((c / 100 for c in range(1, 100)
                             if books - all_in(c / 100) >= MIN_EDGE_AT_LIMIT), default=buy)
            max_price = max(max_price, round(buy, 2))
            # Bet size: quarter-Kelly on the all-in cost, capped at max_bet_pct
            pay = all_in(buy)
            kelly = max(0.0, (books - pay) / (1 - pay)) / 4
            pct = min(kelly, SETTINGS["max_bet_pct"] / 100)
            out.append({
                "signal_id": f"{r.kickoff_utc[:10]}_{r.away_team}_{r.home_team}_{team}",
                "found_at": r.checked_at, "kickoff_utc": r.kickoff_utc,
                "away_team": r.away_team, "home_team": r.home_team, "team": team,
                "entry_price": round(buy, 3), "max_price": max_price,
                "books_prob": round(books, 3), "edge_pts": round(edge * 100, 1),
                "stake": round(SETTINGS["bankroll"] * pct), "mode": SETTINGS["mode"],
                "pm_link": r.pm_link,
            })
    return pd.DataFrame(out)


def signal_message(s):
    name = TEAM_NAMES.get(s["team"], s["team"])
    when = central(s["kickoff_utc"])
    if s["mode"] == "live":
        title = f"Bet: {name}, ${s['stake']:.0f}"
        body = (f"Buy {name} on Polymarket US at {s['max_price'] * 100:.0f}c or less. "
                f"Stake ${s['stake']:.0f}. Books give them {s['books_prob'] * 100:.0f}%, "
                f"a {s['edge_pts']:.1f}-pt edge after fees. "
                f"{s['away_team']} @ {s['home_team']}, {when} CT.")
    else:
        title = f"Practice signal: {name}"
        body = (f"{name} at {s['entry_price'] * 100:.0f}c on Polymarket US, books say "
                f"{s['books_prob'] * 100:.0f}% ({s['edge_pts']:.1f}-pt edge after fees). "
                f"Would buy at {s['max_price'] * 100:.0f}c or less (${s['stake']:.0f}). "
                f"{s['away_team']} @ {s['home_team']}, {when} CT. Practice mode: no real bet.")
    return title, body


def notify(title, body, link=None):
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        return False
    headers = {"Title": title, "Tags": "football", "Priority": "high"}
    if link:
        headers["Click"] = link
    req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=body.encode("utf-8"),
                                 headers=headers, method="POST")
    urllib.request.urlopen(req, timeout=15)
    return True


def load_signals():
    return pd.read_csv(SIGNALS) if SIGNALS.exists() else pd.DataFrame()


def record_new_signals(found):
    """Save and announce signals we haven't announced before."""
    existing = load_signals()
    seen = set(existing["signal_id"]) if not existing.empty else set()
    new = found[~found["signal_id"].isin(seen)] if not found.empty else found
    for s in new.to_dict("records"):
        notify(*signal_message(s), s["pm_link"])
    if not new.empty:
        pd.concat([existing, new], ignore_index=True).to_csv(SIGNALS, index=False)
    return new


# ---------- Grading past signals ----------
def grade_signals():
    """Fill in closing price and result for signals whose games have finished."""
    sig = load_signals()
    if sig.empty:
        return sig
    for col in ["close_price", "won", "profit"]:
        if col not in sig:
            sig[col] = None
    todo = sig[sig["won"].isna()]
    if todo.empty:
        return sig

    log = pd.read_csv(LOG) if LOG.exists() else pd.DataFrame()
    try:
        scores = pd.read_csv(SCORES, usecols=["gameday", "away_team", "home_team", "result"])
    except Exception:
        scores = pd.DataFrame(columns=["gameday", "away_team", "home_team", "result"])

    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    for i, s in todo.iterrows():
        if s.kickoff_utc > now_utc:
            continue  # not played yet
        # Closing price: last Polymarket price we logged before kickoff
        if not log.empty:
            before = log[(log.away_team == s.away_team) & (log.home_team == s.home_team) &
                         (log.kickoff_utc == s.kickoff_utc) & (log.checked_at < s.kickoff_utc)]
            if not before.empty:
                last = before.sort_values("checked_at").iloc[-1]
                close = last.pm_home_buy if s.team == s.home_team else last.pm_away_buy
                sig.at[i, "close_price"] = round(close, 3)
        # Result
        days_apart = (pd.to_datetime(scores.gameday) - pd.to_datetime(s.kickoff_utc[:10])).abs()
        game = scores[(scores.away_team == s.away_team) & (scores.home_team == s.home_team) &
                      (days_apart <= pd.Timedelta(days=1))]
        if game.empty or pd.isna(game.iloc[0].result):
            continue
        margin = game.iloc[0].result  # home points minus away points
        if margin == 0:
            sig.at[i, "won"], sig.at[i, "profit"] = 0.5, 0.0
            continue
        won = (margin > 0) == (s.team == s.home_team)
        pay = all_in(s.entry_price)
        stake = s.stake if s.stake > 0 else 10
        sig.at[i, "won"] = 1.0 if won else 0.0
        sig.at[i, "profit"] = round(stake * (1 - pay) / pay if won else -stake, 2)
    sig.to_csv(SIGNALS, index=False)
    return sig


def track_record(sig=None):
    """Plain-English summary of how the signals have done."""
    sig = load_signals() if sig is None else sig
    if sig.empty:
        return {"signals": 0}
    for col in ["close_price", "won", "profit"]:
        if col not in sig:
            sig[col] = None
    done = sig[sig["won"].notna()]
    closed = sig[sig["close_price"].notna()]
    return {
        "signals": len(sig),
        "graded": len(done),
        "wins": int((done["won"] == 1).sum()) if len(done) else 0,
        "losses": int((done["won"] == 0).sum()) if len(done) else 0,
        "profit": float(done["profit"].sum()) if len(done) else 0.0,
        "staked": float(done["stake"].clip(lower=10).sum()) if len(done) else 0.0,
        "price_moved_our_way": int((closed["close_price"] > closed["entry_price"]).sum()),
        "price_checked": len(closed),
    }


# ---------- Daily summary ----------
SUMMARY_SENT = DATA / "summary_sent.txt"


def daily_summary(snapshot, force=False):
    """One morning notification: open bets, biggest gap, and the track record."""
    now_ct = datetime.now(timezone.utc).astimezone(CENTRAL)
    today = now_ct.strftime("%Y-%m-%d")
    already = SUMMARY_SENT.exists() and SUMMARY_SENT.read_text().strip() == today
    if not force and (already or not 6 <= now_ct.hour <= 8):
        return False

    lines = []
    open_now = find_signals(snapshot) if not snapshot.empty else pd.DataFrame()
    if open_now.empty:
        lines.append("No bets right now.")
    else:
        for s in open_now.to_dict("records"):
            name = TEAM_NAMES.get(s["team"], s["team"])
            lines.append(f"Open: {name} at {s['max_price'] * 100:.0f}c or less "
                         f"({s['away_team']} @ {s['home_team']}, {central(s['kickoff_utc'])} CT).")
    if not snapshot.empty:
        best = snapshot[["home_edge", "away_edge"]].max(axis=1)
        top = snapshot.loc[best.idxmax()]
        team = top.home_team if top.home_edge >= top.away_edge else top.away_team
        lines.append(f"Watching {len(snapshot)} games. Closest to a signal: "
                     f"{TEAM_NAMES.get(team, team)} at "
                     f"{best.max() * 100:+.1f} pts after fees (signal at "
                     f"+{SETTINGS['edge_needed_pts']:.0f}).")
    rec = track_record()
    if rec["signals"]:
        rec_line = f"Track record: {rec['signals']} signal" + ("s" if rec["signals"] != 1 else "")
        if rec["graded"]:
            sign = "+" if rec["profit"] >= 0 else "-"
            rec_line += (f", {rec['wins']}-{rec['losses']}, "
                         f"{sign}${abs(rec['profit']):,.0f} paper")
        if rec["price_checked"]:
            rec_line += (f", price moved our way {rec['price_moved_our_way']}"
                         f"/{rec['price_checked']}")
        lines.append(rec_line + ".")
    else:
        lines.append("No signals yet this season.")
    lines.append(f"Odds API credits left: {credits_left}.")

    mode = "Practice mode" if SETTINGS["mode"] != "live" else "Live mode"
    if notify(f"NFL daily check ({mode})", " ".join(lines)):
        DATA.mkdir(exist_ok=True)
        SUMMARY_SENT.write_text(today)
        return True
    return False


# ---------- Command line / GitHub Actions ----------
if __name__ == "__main__":
    testing = os.environ.get("TEST_ALERT", "").lower() == "true"
    if testing:
        sent = notify("NFL alerts are working", "Test notification from your NFL signal checker.")
        print("Test alert sent." if sent else "No NTFY_TOPIC set, so no test alert was sent.")

    DATA.mkdir(exist_ok=True)
    try:
        snap = compare_now()
    except CheckError as err:
        raise SystemExit(f"Problem: {err}")

    grade_signals()
    if snap.empty:
        print("No upcoming games to compare right now.")
    else:
        old = pd.read_csv(LOG) if LOG.exists() else pd.DataFrame()
        pd.concat([old, snap], ignore_index=True).to_csv(LOG, index=False)
        new = record_new_signals(find_signals(snap))
        print(f"Checked {len(snap)} games. New signals: {len(new)}.")
        for s in new.to_dict("records"):
            print("  " + " | ".join(signal_message(s)))
    if daily_summary(snap, force=testing):
        print("Daily summary sent.")
    print("Track record:", track_record())
    print("Odds API credits left:", credits_left)
