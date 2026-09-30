# NFL Polymarket signals

- `checker.py` runs every 3 hours on GitHub Actions: compares Polymarket prices with ~9 sportsbooks,
  sends a phone alert (ntfy) when Polymarket is cheaper, and grades past signals.
- `app.py` is the phone dashboard (Streamlit Community Cloud).
- `settings.json` controls practice vs. live mode, bankroll, and how big a gap is needed.
- Secrets needed: `ODDS_API_KEY` and `NTFY_TOPIC` (GitHub Actions), `ODDS_API_KEY` (Streamlit).
