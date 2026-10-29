# TopGoalPost — guide for agents and developers

A bot that runs a seasonal 1v1 tournament: Signup → Open → Playin →
Swiss → Playoff. Python 3.12, discord.py 2.x, async SQLAlchemy on SQLite,
Pillow for rendered standings.

## Repo map

| Path | Role |
|---|---|
| `main.py` | Bot startup: schema sync, cog loading (`COGS`/`DEV_COGS`), persistent views, global error handlers, audit-actor hooks |
| `bot/cogs/` | Thin Discord layer: parse input → call a service → format the reply |
| `bot/cogs/checks.py` | `require_host[_app]()`, `require_moderator_app()`, `is_moderator()` |
| `bot/services/` | Business logic. **No `discord` imports.** |
| `bot/services/season.py` | State machine (`TRANSITIONS`, `advance_state`), create/close season, active-season guard |
| `bot/services/tournament_flow.py` | The only implementation of stage actions (`start_open/playins/swiss/playoffs`) and `readiness()` |
| `bot/services/match.py` | Scoring, forfeits, downstream resets, bracket propagation |
| `bot/services/match_report.py` | Durable player score reports (pending/confirmed/rejected/disputed) |
| `bot/services/audit.py` | `record()` audit rows; `current_actor` ContextVar |
| `bot/services/health.py` | `/doctor` checks, SQLite backup API |
| `bot/db/` | `models.py`, `repositories.py`, `session.py` (pragmas), `schema.py` (startup additive sync) |
| `bot/ui/` | Views, modals, dashboard, persistent report buttons |
| `bot/utils/` | `errors.py` (error IDs, `ReportingView`), `threads.py` (group threads), `announce.py` |
| `bot/rendering/` | Image generation; always write through `save_png_atomic()` |
| `migrations/versions/` | Alembic migrations (each one guarded so it is safe after startup sync) |
| `tests/` | pytest suite (in-memory SQLite per test) |
| `scripts/` | `dummy.py`, `seed_dummy_players.py` for test data |

## Command surface

Slash commands are canonical. Prefix (`!`) commands remain as compatibility
wrappers for hosts.
hmd
- **Public:** `/signup`, `/unregister`, `/status`, `/editname`, `/report`,
  `/forfeit`, `/open`, `/playins`, `/swiss`, `/playoff`, `/profile`,
  `/playercard`, `/season_summary`, `/history`, `/seasons`, scheduling
  (`/set_timezone`, `/timestamp`, `/add_timeslot`, `/add_date_timeslot`,
  `/check_times`, `/remove_timeslot`, `/offer_time`).
- **Moderator** (`moderator_role` or host): `/reports`, plus confirm/reject
  buttons on any score report.
- **Host** (Discord administrator or a role in `admin_roles`): `/season …`,
  `/tourney …`, `/player …`, `/dashboard`, `/list_matches`, `/set_score`,
  `/set_score_playoff`, `/audit`, `/doctor`.

Host commands do **not** use `default_permissions(administrator=True)`; the
runtime check is the source of truth (a test enforces this).

## Season state machine

```
OFFSEASON → SIGNUP → OPEN → PLAYIN → SWISS → PLAYOFF → OFFSEASON
```

| Move | How | Gate (blocks the move) |
|---|---|---|
| → SIGNUP | `/season advance SIGNUP` | none |
| SIGNUP → OPEN | `/tourney generate_open` | fewer than 2 registered; generation error |
| OPEN → PLAYIN | `/tourney start_playins` | any unplayed Open match |
| PLAYIN → SWISS | `/tourney swiss_round` | Swiss needs exactly 32 eligible players |
| SWISS → PLAYOFF | `/tourney generate_playoffs` | any unplayed Swiss match; ≠16 players with 3 wins |
| PLAYOFF → OFFSEASON | `/season close` | grand final not decided |

Stage actions generate first and advance only on success. Skips/rewinds need
`force` (logged and audited). Score corrections rewind the stage through
`SeasonService.advance_state(..., force=True)`; nothing else writes `stage`.
`/season create N` refuses to archive a season that has events and was never
closed, unless `force`.

## Rules

- No `discord` imports in `bot/services/`.
- Cogs stay thin; shared logic goes in a service.
- All stage changes go through `SeasonService` / `TournamentFlowService`.
  Prefix, slash and dashboard call the same flow method (parity test in
  `tests/test_cogs.py::TestStageActionParity`).
- Archived seasons are read-only: `MatchService` refuses score/forfeit on
  them (`SeasonService.require_active`).
- Every button view subclasses `ReportingView` so failures get an error ID.
- Persistent buttons need a static `custom_id` and registration in
  `setup_hook` (`add_view` for fixed IDs, `add_dynamic_items` for
  `namespace:action:id` IDs such as `matchreport:confirm:<id>`). Never change
  an existing `custom_id` format.
- Destructive actions call `audit.record(...)`. Never log secrets.
- Images are written with `save_png_atomic()`.
- Schema changes: model + Alembic migration (guard with `has_table` /
  column checks) + repository. Back up the DB first.
- Every user-facing `` `/cmd` `` or `` `!cmd` `` in bot copy must exist (test
  `test_bot_messages_reference_existing_commands`).

## How to add…

**A stage action.** Add `start_<x>(season)` to `TournamentFlowService`:
check state with `_require_state`, generate (catch `ValueError` → `{"error"}`),
then `_advance(target)`, return `{"ok", "state", "advanced_from", "message"}`.
Call it from the cog through `_stage_prefix` / `_stage_slash` (they clean
roles and announce) and from the dashboard through `_run_stage`. Add it to
`readiness()` and `BUTTON_STATES`.

**A command.** Slash first: `@app_commands.command` (or a group subcommand) in
the right cog, `@require_host_app()` / `@require_moderator_app()` if gated.
Keep the body to: defer → service call → reply. Add a prefix wrapper only if
hosts need it.

**A persistent view.** Subclass `discord.ui.DynamicItem` with a `template`
regex (see `bot/ui/report.py::ReportActionButton`), keep state in the DB, and
register it in `main.py` `setup_hook`.

**A model.** Add it to `bot/db/models.py`, export it from `bot/db/__init__.py`,
add a repository class, write a guarded migration in `migrations/versions/`
(next `down_revision` = current head), and extend `tests/test_migrations.py`.
Startup `sync_schema` adds new tables/nullable columns/indexes automatically
and stamps Alembic on old databases.

**A renderer.** Put it in `bot/rendering/`, draw in a worker thread
(`asyncio.to_thread`), save via `save_png_atomic`, and add a cooldown to the
command that triggers it.

**A test.** Use the `db`, `seed_data`, `open_event` fixtures in
`tests/conftest.py`. Mock Discord objects with `MagicMock`/`AsyncMock`; call
slash commands as `cog.cmd.callback(cog, interaction, ...)`. Concurrency tests
need a file DB (the in-memory fixture shares one connection).

## Testing

```bash
python -m pytest -q                         # full suite
python -m pytest -q tests/test_services.py -k Playoff
python -m pytest -q tests/test_migrations.py
```

CI (`.github/workflows/tests.yml`) runs the suite on every push and PR.
Dev dependencies: `pip install -r requirements-dev.txt`.

## Commits

Small commits, one feature each, conventional prefixes (`feat:`, `fix:`,
`refactor:`, `docs:`, `ci:`, `chore:`). Run the suite before committing.
Phases are tagged `refactor-phase-N`; the pre-refactor state is
`refactor-baseline`.

## Common pitfalls

- **State/bracket divergence:** never call `Config().set_season_state`
  directly; generation must succeed before the stage moves.
- **Old-season matches:** queries for players must filter `Event.season` to
  the current season (`MatchService.active_matches_for_player`).
- **Threads:** scheduling uses group threads named `<Stage> · Group N`
  (`bot/utils/threads.py`). There is no per-match thread; `Match.thread_id`
  is unused.
- **Image races:** don't `img.save()` directly into `assets/tables/`.
- **Dev commands:** `!dummy_*` load only with `ENV=dev` or `LOAD_DEV_COGS=1`.
  Never point dev mode at the live database.
- **SQLite:** connections use WAL, `foreign_keys=ON` and a 30 s busy timeout.
  Back up with `!backup` (SQLite backup API), not a file copy.

## Configuration and setup

Environment (`.env`, see `.env.example`): `DISCORD_TOKEN`, `SERVER_ID`,
`DATABASE_URL`, `COMMAND_PREFIX`, optional `ENV=dev` / `LOAD_DEV_COGS=1`.

Config keys (`!set_config key value`): required `match_channel`,
`score_channel`; recommended `agreements_channel`, `announcements_channel`,
`admin_roles`, `moderator_role`, `signup_role`, stage roles
(`open_role`, `playin_role`, `swiss_role`, `playoff_role`), `admin_id`.
`!setup_roles [prefix]` creates any missing signup/stage roles and stores
their IDs. Run `/doctor` after setup.

Bot permissions: Send Messages, Embed Links, Attach Files, Create Public
Threads, Manage Threads (match channel), Manage Roles (stage roles),
plus the Server Members intent.

Privacy: avatars are stored only for registered players
(`assets/pfps/<discord_id>.png`); delete a player's file to remove it.
Backups in `data/backups/` keep the newest 10.