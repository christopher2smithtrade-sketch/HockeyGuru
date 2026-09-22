# CLAUDE.md — Hockey Guru

## Purpose

Nightly NHL anytime-goal board for DraftKings. For every skater on the slate it
projects the chance of scoring a goal (the headline) and shots on goal (the
floor), plus a goalie board on projected DK points. Same playbook as Baseball
Guru (daily slate) and Gridiron Guru (matchup engine, results tracking,
Actions cron, ntfy pushes) — hockey-specific data underneath.

## Scripts

| File | Purpose |
|------|---------|
| `HockeyGuru.py` | The whole thing: data pull, scoring engine, HTML board, prediction log + scorecard, GitHub Pages deploy, ntfy |
| `.github/workflows/hockey.yml` | Cron schedule (10 runs/day across four ET windows) |
| `predictions/pred_*.json` | One per slate, logged at puck drop, graded next morning (synced to the repo) |
| `predictions/backtest_*.json` | Rebuilt past slates used to calibrate the model (not part of the live scorecard) |
| `cache/season/` | Completed-season pulls (NHL stats, MoneyPuck, last season's game logs) — committed, never change |
| `cache/daily/` | Odds, rosters, current game logs, box scores — refreshed, git-ignored |
| `reports/` | Generated HTML boards (local only) |

Single file, edited in place — git history is the versioning.

## Running

```powershell
python "C:\Hockey Guru\HockeyGuru.py"                                   # tonight, deploy + push
python "C:\Hockey Guru\HockeyGuru.py" --no-deploy --no-notify           # local board only
python "C:\Hockey Guru\HockeyGuru.py" --date 2026-01-15 --backtest --no-deploy
                                                                        # rebuild a past slate, grade it now
```

First run of a season pulls ~800 player game logs (3-5 min at 3 parallel calls — the
NHL API returns 429 above ~5 req/s); after that they are cached.

## Model in one paragraph

P(goal) = 1 − exp(−λ), where λ = [5v5 minutes × 5v5 shot rate × opp shot
suppression × implied-goals environment × home/rest × recent form] × finishing
(actual sh% shrunk toward his own xG-per-shot, position-calibrated) × opp chance
quality + [PP minutes (his share of team PP time × PP time this matchup should
produce) × PP shot rate] × PP finishing × opp PK quality — all × the opposing
starter's GSAx-based factor × 1.19 for goals outside 5v5/5v4 (4v4, OT, 5v3,
empty net, measured from MoneyPuck). SOG floor = the same volume × 1.10.
Composite = 100·P(goal) + 3·(SOG − 2.5); grade anchors are absolute
(A+ ≥ 40, A ≥ 33, B+ ≥ 27, B ≥ 21, C ≥ 15).

## Calibration (backtests, Sept 2026)

Four 2025-26 slates (Jan 15, Jan 17, Feb 5, Mar 12 — 1,516 skater calls),
lineups from the box scores, no odds (neutral environment):

- projected 15.3% vs actual 16.0% scored; SOG 1.59 proj vs 1.54 actual; Brier 0.122
- by grade: A+/A ~50% actual, B+/B ~26%, C 19%, D 9%
- top 25 per slate scored 46% vs a 16% field

Season-level rates leak the future in a backtest (form, starters and lineups do
not), so treat it as a sanity check; the live scorecard is the real test.

## Data sources (all free)

- **NHL API** `api-web.nhle.com/v1` — schedule, rosters, player game logs, box scores (no key)
- **NHL stats REST** `api.nhle.com/stats/rest` — league-wide season lines in one call each
- **MoneyPuck** CSVs — xG by situation for skaters/goalies/teams, 5v5 line combos (season file 404s until a few games are in)
- **ESPN** — injuries
- **DailyFaceoff** — starting goalies (Confirmed vs projected); page-embedded JSON, may break
- **The Odds API** — DK totals + moneylines, 2 credits per run window, cached 3 h, skipped for past dates (`ODDS_API_KEY`, shared with Gridiron)

## Key config (top of HockeyGuru.py)

- `SKATER_GRADES` / `GOALIE_GRADES` — grade cut-offs (move only when the scorecard says so)
- `TOP_PER_POS` — cards shown before "Show all"
- `MAIN_SLATE_START_ET` — what counts as the DK main slate for the toggle
- `INCLUDE_PRESEASON` — exhibition slates used only when no regular-season games (camp dry run)
- `GAME_LOG_WORKERS` — keep at 3; 6 drew 429s
- `ODDS_CACHE_HOURS` / `ODDS_RESERVE` — Odds API budget guard
- `GITHUB_TOKEN` / `ODDS_API_KEY` — env vars (repo secrets on Actions)
- `NTFY_TOPIC` — `hockey-guru`

## Gotchas

- Slate date is the **Eastern** date; a 10 PM ET game is tonight's slate.
- Predictions freeze per game once it starts; anything logged after is flagged `late` and not scored.
- Goalie decisions are not in the box score: the goalie with the most TOI on each side gets W / L / OTL.
- MoneyPuck's current-season files 404 until a few games are in; the model runs on last season until then.
- `.claude/launch.json` serves `reports/` on :8765 for previewing boards in the app.
