"""
Phone-friendly dashboard: is there a bet right now, and where?
Hosted on Streamlit Community Cloud. Reads the log that GitHub Actions
updates every few hours, and can run a fresh check on demand.
"""

import os
from datetime import datetime, timezone

import pandas as pd
import streamlit as st

st.set_page_config(page_title="NFL Signals", page_icon="🏈", layout="centered")

# Secrets from Streamlit Cloud become environment variables for checker.py
try:
    for name in ("ODDS_API_KEY",):
        if name in st.secrets:
            os.environ[name] = st.secrets[name]
except Exception:
    pass

import checker  # noqa: E402

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@500;700&family=Barlow:wght@400;500;600&display=swap');
html, body, [class*="css"], .stMarkdown, p, li, button { font-family: 'Barlow', system-ui, sans-serif; }
.block-container { max-width: 560px; padding-top: 3.5rem; }
header[data-testid="stHeader"] { background: transparent; }
.stMarkdown p.status { font-family: 'Barlow Condensed', sans-serif; font-weight: 700;
          font-size: 2.6rem; line-height: 1.05; margin: 0 0 .5rem; letter-spacing: -.01em; }
.stMarkdown p.status.quiet { color: #5B6778; }
.stMarkdown p.sub { color: #5B6778; font-size: 1rem; line-height: 1.45; margin-bottom: 1.2rem; }
.practice { background: #FFF4DB; border-left: 4px solid #E0A21B; padding: .65rem .9rem;
            border-radius: 4px; font-size: .95rem; margin-bottom: 1.2rem; }
.ticket { background: #FFFFFF; border: 1px solid #D5DBD2; border-left: 6px solid #1F7A4D;
          border-radius: 6px; padding: 1rem 1.1rem .8rem; margin-bottom: .5rem; }
.ticket.practice-t { border-left-color: #E0A21B; }
.ticket.stale { border-left-color: #A9B2BC; opacity: .75; }
.team { font-family: 'Barlow Condensed', sans-serif; font-weight: 700; font-size: 2rem; line-height: 1; }
.action { font-size: 1.25rem; font-weight: 600; margin: .35rem 0 .15rem; }
.meta { color: #5B6778; font-size: .95rem; }
.record { font-size: 1rem; line-height: 1.5; }
</style>
""", unsafe_allow_html=True)

settings = checker.SETTINGS
practice = settings["mode"] != "live"


def cents(p):
    return f"{p * 100:.0f}¢"


# ---------- Load the latest data ----------
log = pd.read_csv(checker.LOG) if checker.LOG.exists() else pd.DataFrame()
latest = (log[log.checked_at == log.checked_at.max()]
          .drop_duplicates(["away_team", "home_team"], keep="last") if not log.empty else log)
fresh = st.session_state.get("fresh")
snapshot = fresh if fresh is not None else latest
now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")

saved = checker.load_signals()
found_now = checker.find_signals(snapshot) if not snapshot.empty else pd.DataFrame()
signals = pd.concat([saved, found_now], ignore_index=True) if not found_now.empty else saved
if not signals.empty:
    signals = signals[signals.kickoff_utc > now_utc].drop_duplicates("signal_id", keep="last")


def current_price(s):
    """Latest Polymarket price for the signal's team, from the newest snapshot."""
    row = snapshot[(snapshot.away_team == s.away_team) & (snapshot.home_team == s.home_team)]
    if row.empty:
        return None
    p = row.iloc[0].pm_home_price
    return p if s.team == s.home_team else 1 - p


active, stale = [], []
for s in (signals.itertuples() if not signals.empty else []):
    price = current_price(s)
    (active if price is not None and price <= s.max_price else stale).append((s, price))

# ---------- Headline ----------
if active:
    word = "practice signal" if practice else "bet"
    n = len(active)
    st.markdown(f'<p class="status">{n} {word}{"s" if n > 1 else ""} right now</p>',
                unsafe_allow_html=True)
else:
    st.markdown('<p class="status quiet">No bets right now</p>', unsafe_allow_html=True)

if not snapshot.empty:
    when = checker.central(snapshot.checked_at.max(), "%a %I:%M %p")
    st.markdown(f'<p class="sub">Last checked {when} CT across {len(snapshot)} games. '
                'Checks run every 3 hours; you get a phone alert when a signal appears.</p>',
                unsafe_allow_html=True)
else:
    st.markdown('<p class="sub">No checks yet. Tap "Check now" below.</p>', unsafe_allow_html=True)

if practice:
    st.markdown('<div class="practice"><b>Practice mode.</b> Signals are tracked to prove '
                'whether the edge is real. Don\'t place real bets yet.</div>',
                unsafe_allow_html=True)

# ---------- Bet tickets ----------
for s, price in active:
    name = checker.TEAM_NAMES.get(s.team, s.team)
    stake = f"${s.stake:.0f}" + (" (practice)" if practice else "")
    st.markdown(f"""
<div class="ticket {'practice-t' if practice else ''}">
  <div class="team">{name}</div>
  <div class="action">Buy at {cents(s.max_price)} or less, {stake}</div>
  <div class="meta">Now {cents(price)} on Polymarket; books give them {s.books_prob * 100:.0f}%.<br>
  {s.away_team} @ {s.home_team}, {checker.central(s.kickoff_utc)} CT</div>
</div>""", unsafe_allow_html=True)
    st.link_button(f"Open {s.away_team} @ {s.home_team} on Polymarket", s.pm_link,
                   width="stretch")

for s, price in stale:
    name = checker.TEAM_NAMES.get(s.team, s.team)
    now_txt = f"now {cents(price)}" if price is not None else "no current price"
    st.markdown(f"""
<div class="ticket stale">
  <div class="team">{name}</div>
  <div class="action">Skip: price moved past {cents(s.max_price)}</div>
  <div class="meta">Was {cents(s.entry_price)}, {now_txt}. {s.away_team} @ {s.home_team},
  {checker.central(s.kickoff_utc)} CT</div>
</div>""", unsafe_allow_html=True)

# ---------- Check now ----------
st.write("")
if st.button("Check now", type="primary", width="stretch"):
    with st.spinner("Checking Polymarket and 9 sportsbooks..."):
        try:
            st.session_state["fresh"] = checker.compare_now()
            st.session_state["credits"] = checker.credits_left
            st.rerun()
        except checker.CheckError as err:
            st.error(str(err))
if "credits" in st.session_state:
    st.caption(f"Each check uses 1 Odds API credit. {st.session_state['credits']} left this month.")

# ---------- Track record ----------
st.markdown("#### Track record")
rec = checker.track_record()
if rec["signals"] == 0:
    st.markdown('<p class="record">No signals yet. They\'ll be graded automatically '
                'after each game.</p>', unsafe_allow_html=True)
else:
    lines = [f"{rec['signals']} signals so far, {rec['graded']} graded."]
    if rec["graded"]:
        roi = rec["profit"] / rec["staked"] if rec["staked"] else 0
        lines.append(f"Record {rec['wins']}-{rec['losses']}, paper profit "
                     f"{'+' if rec['profit'] >= 0 else '-'}${abs(rec['profit']):,.0f} "
                     f"({roi:+.0%} per dollar bet).")
    if rec["price_checked"]:
        lines.append(f"Price moved in our favor before kickoff in "
                     f"{rec['price_moved_our_way']} of {rec['price_checked']}.")
    st.markdown('<p class="record">' + "<br>".join(lines) + "</p>", unsafe_allow_html=True)
    st.caption("A real edge usually shows up first as prices moving in our favor, "
               "then as profit over 50+ signals.")

# ---------- All games ----------
with st.expander("All games right now"):
    if snapshot.empty:
        st.write("No data yet.")
    else:
        view = snapshot.copy()
        view["Game"] = view.away_team + " @ " + view.home_team
        view["Kickoff (CT)"] = view.kickoff_utc.map(checker.central)
        view["Polymarket"] = (view.pm_home_price * 100).round(0).astype(int).astype(str) + "¢"
        view["Books"] = (view.books_home_prob * 100).round(0).astype(int).astype(str) + "%"
        view["Gap"] = (view.gap * 100).round(1)
        st.caption("Home team's win chance. Gap is in points; negative means the home "
                   "team is cheaper on Polymarket.")
        st.dataframe(view[["Game", "Kickoff (CT)", "Polymarket", "Books", "Gap"]]
                     .sort_values("Gap", key=abs, ascending=False),
                     hide_index=True, width="stretch")
