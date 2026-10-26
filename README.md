<div align="center">

<img src="repopng/TPGheader.png" alt="TopGoalPost — tournament bot for the Pro Bobble League Circuit" width="100%">

### A Discord bot for running Pro Bobble League Circuit (PBLC)

Signups, Open, Play-ins, Swiss and a double-elimination playoff bracket, with
rendered standings images and Discord-thread scheduling.

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)
![discord.py](https://img.shields.io/badge/Discord-discord.py-5865F2?style=for-the-badge&logo=discord&logoColor=white)
![Database](https://img.shields.io/badge/Database-SQLite-003B57?style=for-the-badge&logo=sqlite&logoColor=white)

[Features](#user-content-features) · [Quick start](#user-content-quick-start) · [Season lifecycle](#user-content-season-lifecycle) · [Commands](#user-content-commands) · [Season archive](#user-content-season-archive) · [Configuration](#user-content-configuration) · [Troubleshooting](#user-content-troubleshooting)

</div>

---

<p id="features"><img src="repopng/tpgFEATURES.png" alt="Features" width="250"></p>

| Area | What it does |
|------|--------------|
| **Tournament engine** | Signups, Open, Play-ins, Swiss and a 29-slot double-elimination playoff bracket with a grand-final reset. |
| **Rendered views** | Standings boards, playoff brackets, player cards and season summaries. |
| **Match scheduling** | Timezone-aware availability, match-time proposals and dedicated Discord threads. |
| **Score flow** | Reporting, opponent confirmation, corrections and admin overrides. |
| **Season history** | Archived seasons, player stats, career awards and season recaps. |
| **Host tools** | Dashboard controls, role-gated admin commands, backups and dummy-data utilities. |

---

<p id="quick-start"><img src="repopng/TPGstart.png" alt="Quick start" width="250"></p>

From clone to running bot:

<p><img src="repopng/TPGinstallation.png" alt="Installation" width="200"></p>

```bash
git clone https://github.com/organpy/srtrt.git
cd srtrt
cp .env.example .env             # edit DISCORD_TOKEN, SERVER_ID
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
alembic -c alembic.ini upgrade head   # run migrations once
python main.py
```

The bot logs to stdout. It syncs slash commands to the guild in `SERVER_ID` on
startup and clears global duplicates.

<p><img src="repopng/TPGenvvar.png" alt="Environment variables" width="200"></p>

| Key | Required | Notes |
|-----|----------|-------|
| `DISCORD_TOKEN` | yes | bot token |
| `SERVER_ID` | yes | guild to sync slash commands into |
| `DATABASE_URL` | no | defaults to `sqlite+aiosqlite:///data/bot.db` |
| `COMMAND_PREFIX` | no | defaults to `!` |

---

<p id="season-lifecycle"><img src="repopng/TPGlifecycle.png" alt="Season lifecycle" width="250"></p>

```
OFFSEASON → SIGNUP → OPEN → PLAYIN → SWISS → PLAYOFF → (season ends)
```

| Stage | Admin move | What happens | Player view |
|-------|------------|--------------|-------------|
| `OFFSEASON` | `/season create N` | Archive the old season and create the next one | `/seasons` |
| `SIGNUP` | `/season advance SIGNUP` | Registration opens | `/signup`, `/status` |
| `OPEN` | `/tourney generate_open` (advances SIGNUP → OPEN) | Four Open matches per player | `/open`, `/report` |
| `PLAYIN` | `/tourney start_playins` | Auto-advancing qualifier ladder | `/playins`, `/report` |
| `SWISS` | `/tourney swiss_round` | 66-match survival ladder | `/swiss`, `/report` |
| `PLAYOFF` | `/tourney generate_playoffs` | 16-player double-elimination BO3 bracket | `/playoff`, `/report` |

The season state is stored in the `settings` DB table (key `stage`).
The `SeasonService` enforces the legal path
`OFFSEASON → SIGNUP → OPEN → PLAYIN → SWISS → PLAYOFF → OFFSEASON`.
Skips and backward jumps are refused unless forced
(`/season advance <STATE> force:True`), which is logged. Stage actions
(`/tourney generate_open`, `start_playins`, `swiss_round`, `generate_playoffs`)
generate first and only advance the state when generation succeeds.

Use `/season status` at any time to see the current season and state.
Use `/tourney status` for the recommended next step.

> **Prefix vs slash:** Admin-only commands use `!` (type as text in chat —
> they do NOT appear in Discord's slash picker). Player-facing and `/tourney`
> commands use `/` and appear in the picker. This avoids hitting Discord's
> command-registration timeout (~45s) with too many slash commands.

> Expand a stage for the full walkthrough.

<details>
<summary><strong>1. Create a season</strong> — start the next season</summary>

```
/season create 12          # starts Season 12 in OFFSEASON
/season advance SIGNUP     # open signups
```

</details>

<details>
<summary><strong>2. Signups</strong> — registration and the persistent button</summary>

Players register with:

```
/signup username:Alice country:US
/editname name:NewName     # change display name (SIGNUP state only)
/unregister                # before OPEN only
/status                    # check registration
```

Signups close the moment the season leaves `SIGNUP`: `/signup`, `/editname` and
the persistent button all refuse at that point. A successful signup replies
ephemerally with the next steps (`/set_timezone`, then `/add_timeslot`). Admins
can still override a name with `/player set`.

A persistent signup button can be posted by an admin (only while `SIGNUP`):

```
!signupbutton              # posts button; delete the bot message after to clean up
```

**Config needed:** `signup_role` (set via `!set_config signup_role <role_id>`).

</details>

<details>
<summary><strong>3. Open stage</strong> — generate matches and report scores</summary>

Before generating Open, set the players who skip Open and enter Swiss directly.
The list can be empty (normal for Season 1) and is replaced on each call:

```
/tourney skip_open players:"Alice, Bob, 123456789012345678"
/tourney skip_open players:""        # clear the list
```

Names, Discord IDs, and mentions are accepted. Players must already have an
account (`/signup`); skip setup is only allowed during `SIGNUP` and before Open
matches exist. The dashboard has a **Skip Open** button for the same list.

Each non-skip player plays 4 opponents. Life points: start at 4, lose 1 per loss.

```
/tourney generate_open     # creates 4 matches per player, then SIGNUP → OPEN
```

Players report scores:

```
/report                    # pick match, then preset or custom score
/set_score match_id:X p1_goals:N p2_goals:N   # admin override / correction
/set_score_playoff match_id:X game:g1 p1_score:N p2_score:N
/player forfeit_all target:@P                # forfeit all their matches
```

Open stats are rebuilt automatically after every score. `/tourney sync_open`
remains available as a manual repair command.

`/report` shows the old-style score buttons (`5-0` … `5-4`, `0-5` … `4-5`)
plus a **Custom score** button. Custom opens the numeric modal. Playoff BO3
games use the same flow one game at a time; the next game and bracket slot
fill automatically.

The reporter's `/report` flow is ephemeral, but the opponent confirmation is
posted in the score channel so they can confirm it. When confirmed, the
message is replaced with a full summary:

```
Score confirmed and submitted successfully!
Match 361 · Playoff W-R1-M3 · organ 5-0 dummy · Series 1-0 · G1 5-0, G2 3-2
```

User-facing slash commands (`/signup`, `/profile`, `/playercard`, `/history`,
`/open`, `/playins`, `/swiss`, `/playoff`, `/season_summary`, `/check_times`,
`/set_timezone`, `/forfeit`, `/editname`, …) all reply ephemerally, so the
channel stays clean.

`/set_score` can edit a completed match. The autocomplete searches every
match in the season, including completed ones, shows the score, and marks
them with `[completed — edit]`. Correcting an early result resets downstream
matches whose participants or results depend on it, so they can be rescored.
Open corrections reset Playin/Swiss/Playoff data because qualification
changes.

`/set_score_playoff` sets one BO3 game by series match ID and game number.
`g2` and `g3` are rejected until those games exist.

`/list_matches` lists active matches by default and accepts optional
`stage`, `status`, `round_num`, `player`, and `page` filters. It includes
completed matches when requested and is not capped at 100.

View the standings:

```
/open                      # rendered image, sortable
```

Admin utilities:

```
!sync_hp                   # same as sync_open
!getleaderboard 20         # mention top N players
!unthreadedgames           # list matches with no scheduling thread
```

</details>

<details>
<summary><strong>4. Playins</strong> — auto-advancing qualifier ladder</summary>

Open wins decide where each unseeded player enters:

| Open wins | Path |
|-----------|------|
| 4 | Auto-qualifies for Swiss |
| 3 | Best by Open GD may auto-qualify; the rest enter R3 |
| 2 | Enters R2 |
| 1 | Enters R1 |
| 0 | Eliminated |

Returning seeds (`swiss_seed > 0`, set by `/tourney skip_open` or
`!tourney import_seeds`) skip Playins entirely. Playin always targets
`32 - returning_seeds` qualifiers, so the Swiss field is exactly 32.

```
/tourney start_playins     # creates the complete R1/R2/R3 graph
```

The whole ladder is pre-created and pre-seeded. Later cards contain known
players plus placeholders such as `Winner of R1-1`; scoring a match fills the
next card automatically. No per-round generation command is needed. Byes count
as wins and go to the highest win group first.

```
/playins                   # display-only image, live from DB as scores arrive
```

When every ladder match is complete, qualifiers are ranked automatically.
If the normal ladder underproduces, the best remaining R3 losers fill the
target. If it overproduces, a conditional **CUT** round is created between the
lowest-ranked R3 winners; the cut winners take the last spots. When the math
says R3 will overproduce, `/playins` previews the empty CUT column and
`/tourney start_playins` / `/tourney status` print a predicted-CUT notice.
If every entrant already has four wins, no ladder cards are created; those
players auto-qualify and the next step becomes Swiss.
`/tourney finish_playins` remains available as a manual retry.

**Glossary.** *HP* (life points) starts at 4 in Open and drops by 1 per loss.
*CUT* is the extra Playin round created only when R3 produces more qualifiers
than the 32-player Swiss field can take.

</details>

<details>
<summary><strong>5. Swiss stage</strong> — 66-match survival ladder</summary>

Swiss is a fixed 32-player survival ladder. Returning seeds plus Playin
qualifiers must add up to exactly 32. Three wins qualify; three losses
eliminate.

| Round | Groups | Matches |
|---|---|---:|
| R1 | 0-0 | 16 |
| R2 | 1-0 / 0-1 | 8 + 8 |
| R3 | 2-0 / 1-1 / 0-2 | 4 + 8 + 4 |
| R4 | 2-1 / 1-2 | 6 + 6 |
| R5 | 2-2 decider | 6 |

```
/tourney swiss_round       # create the fixed 66-match Swiss ladder
/swiss                     # survival board, future slots show feeder labels
```

The entire 66-match ladder is pre-created with feeder links, so future slots
are labelled `Winner of 266` / `Loser of 266` instead of `TBA` before they are
played. Scoring a match fills both its winner and loser destinations
immediately; no per-round generation is needed.

R3 upper winners (3-0) qualify and stop; R4 upper winners (3-1) qualify and
stop; R5 winners (3-2) take the last spots. R3/R4 lower losers and R5 losers
are eliminated. The ladder is a survival graph rather than a rematch-avoiding
pairing, so the same two players can meet twice.

The final playoff field is 16 players: 4 at 3-0, 6 at 3-1, and 6 at 3-2.

Before Swiss, `/tourney skip_open` (or `!tourney import_seeds`) sets the
returning seeds.

</details>

<details>
<summary><strong>6. Playoffs</strong> — double-elimination BO3 bracket</summary>

Top 16 from Swiss results enter a 29-slot double-elimination bracket:

```
/tourney generate_playoffs
/report                    # players report the active BO3 game
/set_score_playoff match_id:X game:g1 p1_score:N p2_score:N
/playoff view:full         # full bracket, including grand final + reset
/playoff view:winners      # winners side only (cropped)
/playoff view:losers       # losers side only (cropped)
```

Matches are best-of-3; losers bracket winner triggers a **bracket reset** (GF-M2).
The `winners` view keeps the grand final and the reset card, which is drawn in
red; the `losers` view excludes them. Empty cards name their feeders using real
players, e.g. `Winner of Organ vs Dummy`. Correcting a completed playoff game
clears the dependent downstream series so they can be replayed.

Once the grand final is decided, the champion's card gets a gold border and a
gold name, and a `CHAMPION: <name>` banner is drawn above the grand final column
(full and winners views). A reset series decides the title, so the gold moves to
the reset card if one is played.

</details>

---

<p id="commands"><img src="repopng/TPGcmds.png" alt="Commands" width="250"></p>

> Expand a command group to see its table.

<details>
<summary><strong><code>/season</code> — host slash lifecycle</strong></summary>

| Command | Description |
|---------|-------------|
| `/season status` | Current season number, state and next state |
| `/season create N` | Create Season N (archives the current season, history kept) |
| `/season advance STATE [force]` | Move to the next state; `force` skips/rewinds (logged) |

`!season status|create|advance` remain as prefix equivalents.

</details>

<details>
<summary><strong><code>/tourney</code> — admin slash progression</strong></summary>

| Command | Description |
|---------|-------------|
| `/tourney status` | Summary + recommended next step |
| `/tourney skip_open players:"A, B"` | Set Open-skip / returning seeds before Open |
| `/tourney generate_open` | Create 4 open matches per player |
| `/tourney sync_open` | Rebuild open win/loss from scores |
| `/tourney start_playins` | Create the complete preseeded Playin ladder |
| `/tourney playin_round round_num:N` | Re-run a specific Playin round |
| `/tourney finish_playins` | Manually retry Playin finalization |
| `/tourney swiss_round round_num:N` | Create the Swiss ladder / show a round |
| `/tourney generate_playoffs` | Seed playoff bracket from Swiss results |

</details>

<details>
<summary><strong><code>!tourney</code> — prefix progression</strong></summary>

The same progression engine is also exposed as prefix subcommands:

| Command | Description |
|---------|-------------|
| `!tourney makethreads [stage]` | Create scheduling threads for a stage |
| `!tourney cleansethreads` | Remove non-participants from threads |
| `!tourney generate_open` | Create 4 open matches per player |
| `!tourney skip_open A, B` | Set Open-skip / returning seeds |
| `!tourney generate_playin N` | Run Playin round 1, 2 or 3 |
| `!tourney finish_playins` | Finalize Playin qualifiers |
| `!tourney generate_swiss [N]` | Generate the next (or a specific) Swiss round |
| `!tourney generate_playoffs` | Generate the playoff bracket |
| `!tourney import_seeds @1 @2 ...` | Bulk seed import (1–16, by mention order) |

</details>

<details>
<summary><strong>Player administration</strong> — host-only <code>/player</code> tools</summary>

`/player` is the single way to inspect and edit a player without touching the
database. Every subcommand is host-only and replies ephemerally.

| Command | Description |
|---------|-------------|
| `/player info target:@P [season:N]` | Everything stored: ID, country, timezone, season stats, availability |
| `/player set target:@P [name] [country] [timezone]` | Patch identity; only supplied options change, `country:none` clears |
| `/player stats target:@P [seed] [playin_rank] [hp] [open_wins] [open_losses] [open_gd]` | Patch season stats; `seed:0` / `playin_rank:0` clear |
| `/player add_weekly target:@P weekday:Fri start:19:00 finish:21:30` | Add a recurring slot (times are in the player's timezone) |
| `/player add_date target:@P day:15 start_time:19:00 finish_time:21:00 [preferred]` | Add a one-off slot |
| `/player remove_slot target:@P slot:...` | Delete one slot (autocomplete scoped to the target) |
| `/player clear_slots target:@P` | Delete all of a player's slots (confirm) |
| `/player remove_from_matches target:@P` | Detach a player from uncompleted matches, keeping the matches |
| `/player delete_matches target:@P` | Delete a player's uncompleted Open/Playin matches |
| `/player forfeit_all target:@P` | Forfeit every uncompleted match |
| `/player substitute old:@P new:@P` | Replace a player in all uncompleted matches |

Destructive subcommands ask for a Confirm click first. `/player info` reports
`Skipped Open (seed #N)` for returning seeds and `Did not play` for players
with no Open matches, instead of a misleading `0-0`. `/player set name:` does
not rename the Discord nickname.

</details>

<details>
<summary><strong>Player commands</strong> — registration, stats and match flow</summary>

| Command | Description |
|---------|-------------|
| `/signup username:X country:XX` | Register |
| `/unregister` | Withdraw (before OPEN) |
| `/status` | Check registration |
| `/editname name:X` | Change display name (signups only) |
| `/report` | Pick match, then preset or custom score |
| `/forfeit match_id:X` | Concede a match |
| `/list_matches [stage] [status] [round_num] [player] [page]` | Browse matches with filters |
| `/open`, `/playins`, `/swiss` | Standings images (`season:N` for a past season) |
| `/playoff view:full\|winners\|losers` | Full bracket or one branch (`season:N` for a past season) |
| `/seasons` | List seasons with status, champion and end date |
| `/profile username:Name-or-ID [season:N]` | Season stats: games, wins, goals, GD, per-stage records |
| `/playercard username:Name-or-ID [season:N]` | Rendered player card image (record, stage table, champion, career awards) |
| `/season_summary [season:N]` | Rendered season recap: placements, top scorer, most conceded, games and win leaders |
| `/history username:Name-or-ID [season:N]` | Recent match results |

`season:` defaults to the current season and autocompletes every season that
still has data. Player name fields autocomplete from the local roster as you
type (including an empty field) and also accept internal player IDs or Discord
IDs. Stats are computed from completed matches, so a player who did not play
Open shows `Did not play` and a seeded returner shows
`Skipped Open (seed #N)` — never `0-0`.

All of these reply ephemerally, including the standings images.

</details>

<details>
<summary><strong>Scheduling</strong> — timezones and availability</summary>

| Command | Description |
|---------|-------------|
| `/set_timezone timezone:Europe/Berlin` | Save your timezone (autocomplete) |
| `/timestamp day:15 hour:19 minute:0` | Generate auto-adjusting timestamp |
| `/add_timeslot` | Add recurring weekly availability (weekday + times) |
| `/add_date_timeslot day:15 start_time:19:00 finish_time:21:30` | Add a one-off date slot |
| `/check_times target:@User` | See someone's dated + weekly slots |
| `/remove_timeslot slot:...` | Delete a slot (autocomplete, dated or weekly) |
| `/offer_time target:@User day:15 time:19:00 [text:...]` | Propose match time via DM |

`day`, time, timezone and slot inputs all have autocomplete. Weekly slots are
day-of-week based (`Wednesday 19:00–21:00`), so they repeat every week; use
`/add_date_timeslot` for a specific date. `/offer_time text:` is admin-only —
players may offer a time but not attach a custom message, and admins may
target any member without a shared match.

</details>

<details>
<summary><strong>Prefix commands</strong> — admin-only utilities</summary>

| Command | Description |
|---------|-------------|
| `!season status` | Current season number and state |
| `!season create N` | Create Season N (archives the current season, history kept) |
| `!season advance STATE [force]` | Advance the state machine |
| `!set_config key value` | Set a config value |
| `!remove_config key` | Delete a config value |
| `!playerlist` | List all registered players |
| `!backup` | Backup the DB to `data/backups/` |
| `!reimport` | Hot-reload the standings renderer |
| `!resetimages` | Clear the standings image cache |
| `!sync_commands` | Force-resync slash commands |
| `!getpfps` | Download all server avatars into `assets/pfps/` |
| `!giveroles` | Assign current-stage role to eligible players |
| `!signupbutton` | Post persistent signup button (SIGNUP state only) |
| `!sync_hp` | Rebuild open HP/stats |
| `!getleaderboard N` | Mention the top N open players |
| `!unthreadedgames` | List matches without scheduling threads |
| `!dummy_add N` | Add N dummy players to current season (dev mode only) |
| `!dummy_score` | Randomly score all unplayed matches (dev) |
| `!dummy_replace OLD NEW` | Change a player's Discord ID (dev) |

</details>

<details>
<summary><strong>Permissions</strong> — admin and host gating</summary>

Admin/host commands are gated by the bot, not by Discord command permissions.
Every gated command verifies the caller is a guild administrator **or** holds
a role listed in the `admin_roles` config key. Rejected users get:
*"You need an administrator or host role."*

Slash admin commands are visible to everyone in the command picker so host
roles can find them; the runtime check is the source of truth.

Grant host roles:

```
!set_config admin_roles 1234567890,9876543210
```

</details>

---

<p id="configuration"><img src="repopng/TPGconfig.png" alt="Configuration" width="250"></p>

All config lives in the `settings` DB table (flat key–value). Set via
`!set_config key value` and remove via `!remove_config key`.

| Key | Purpose |
|-----|---------|
| `signup_role` | Role granted on signup (never stripped by `giveroles`) |
| `open_role` | Role for Open participants |
| `playin_role` | Role for Playin participants |
| `swiss_role` | Role for Swiss participants |
| `playoff_role` | Role for Playoff participants |
| `moderator_role` | Role with score-confirm permissions |
| `match_channel` | Parent channel for match scheduling threads |
| `score_channel` | Channel where score confirmations are posted |
| `agreements_channel` | Channel where agreed match times are announced |
| `admin_id` | Discord ID of the head admin (shown in error messages) |
| `admin_roles` | Comma-separated role IDs allowed to use admin/host commands |

---

<p id="season-archive"><img src="repopng/TPGarchive.png" alt="Season archive" width="250"></p>

Ending a season never deletes it. `/season create N`:

1. Archives the current active season (`ended_at` is stamped)
2. Creates Season N and resets the state machine to `OFFSEASON`
3. Clears the rendered-image cache and generated `assets/tables/*.png`

Events, matches, registrations, stats and player avatars stay in the database,
so old brackets and player records remain viewable forever. Archived seasons
are read-only: a season number can never be reused or reopened
(`/season create N` refuses any number that already exists).

Every bracket command takes an optional `season:` (defaults to the active
season), and renders are written per season (`open_s3.png`, `playin_s3.png`,
`swiss_s3.png`, `playoff_s3_full.png`) so looking at an old season can never
overwrite the live one. `/seasons` lists what is available:

```
/seasons
/open season:2
/profile username:Alice season:2
/season_summary season:2
```

Global user preferences (timezones, timeslots, settings) survive across
seasons, and player avatars are kept when rolling over so archived brackets
still show faces.

---

<p id="dashboard"><img src="repopng/TPGdashboard.png" alt="Dashboard" width="275"></p>

`/dashboard` opens an interactive admin panel with buttons for each stage
action. The **Next Step** button follows the season state machine and the
**Skip Open** button edits the returning-seed list before Open generation.

---

<p id="assets"><img src="repopng/TPGassets.png" alt="Assets" width="275"></p>

The `assets/` directory holds:

| Path | Contents |
|------|----------|
| `assets/flags/` | Country flag PNGs (downloaded on demand from twemoji) |
| `assets/fonts/` | `Agdasima-Regular.ttf` (standings renderer) |
| `assets/pfps/` | Player avatars (download with `!getpfps` or auto on user update) |
| `assets/tables/` | Rendered standings images (cached) |

Avatars are saved on `/signup` and the signup button, refreshed by the
`on_user_update` listener, and fetched on-demand by the standings renderer.
Creating a new season removes old player avatars so last season's faces cannot
appear on new standings.

Flags are not committed: `get_flag()` fetches a missing flag from the twemoji
CDN on first use and falls back to a transparent box if the fetch fails.

---

<p id="testing"><img src="repopng/TPGtesting.png" alt="Testing & dummy data" width="275"></p>

```bash
# full suite (in-memory SQLite)
python -m pytest tests -v

# one test
python -m pytest tests/ -k test_backup_creates_file

# dummy tournament data (default: 64 players, 8 returning seeds)
python scripts/dummy.py --db data/my_season.db --overwrite --seed 42 --until open
python scripts/dummy.py --db data/my_season.db --overwrite --seed 42 --until playin
python scripts/dummy.py --db data/my_season.db --overwrite --seed 42 --until swiss
python scripts/dummy.py --db data/my_season.db --overwrite --seed 42 --until playoff

# no returning seeds, 88-player Open field
python scripts/dummy.py --db data/my_season.db --overwrite --players 88 --manual-seeds 0

# seed 64 players for testing
python scripts/seed_dummy_players.py
```

Then point `DATABASE_URL` in `.env` to `sqlite+aiosqlite:///data/my_season.db`.

The `!dummy_*` commands are only loaded in dev mode. Set `ENV=dev` (or
`LOAD_DEV_COGS=1`) together with a separate `DATABASE_URL`; production
startups never load them.

<p id="db-migrations"><img src="repopng/TPGdb.png" alt="Database migrations" width="275"></p>

```bash
alembic -c alembic.ini upgrade head
alembic -c alembic.ini downgrade -1   # roll back last
```

Migrations live in `migrations/versions/`.

On startup the bot applies additive changes itself (new tables, new nullable
columns, new indexes) and stamps the Alembic revision on databases that were
created before Alembic was used. If the log warns that the database is behind,
back up `data/bot.db` and run `alembic -c alembic.ini upgrade head`.

---

<p id="troubleshooting"><img src="repopng/TPGtroubleshooting.png" alt="Troubleshooting" width="250"></p>

> Click an issue to expand the fix.

<details>
<summary><strong>Bot doesn't start</strong></summary>

- Check `DISCORD_TOKEN` and `SERVER_ID` in `.env`
- Run `alembic -c alembic.ini upgrade head`
- Verify `data/` directory exists (bot creates `data/bot.db` on first run)

</details>

<details>
<summary><strong>Tests fail at collection with <code>OSError: cannot open resource</code></strong></summary>

- Ensure `assets/` has all subdirectories (`fonts/`, `pfps/`, `tables/`,
  `flags/`); rendering paths are anchored to the project root regardless of the
  working directory

</details>

<details>
<summary><strong>Standings images show no player avatar</strong></summary>

- Run `!getpfps` to download guild member avatars into `assets/pfps/`
- Avatars are named by Discord user ID (`1234.png`)
- The `on_user_update` listener keeps them fresh on avatar changes

</details>

<details>
<summary><strong>Slash commands don't appear</strong></summary>

- Run `!sync_commands` (admin only; closes all sync paths)
- Restart the bot — it syncs on `setup_hook`
- Make sure `SERVER_ID` is set in `.env`

</details>

<details>
<summary><strong>DB errors / "Database manager not initialized"</strong></summary>

- `init_db_manager()` runs before `get_db_manager()` — the bot calls
  `init_db()` in its `setup_hook` before loading cogs
- Tests handle this via `conftest.py` fixtures

</details>

---

<p id="need-help"><img src="repopng/TPGneedhelp.png" alt="Need help?" width="335"></p>

- **Bugs and feature requests:** open an issue on this repository's tracker, or
  ping the bot's host in your Discord server.
- **Before reporting:** include the command you ran, what you expected, and the
  relevant lines from the bot's stdout log.
- **Never share secrets:** don't paste your bot token, `.env` contents, or
  database files into an issue. Revoke and regenerate the token if it leaks.
