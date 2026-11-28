# Teacher

Teacher is a study tool that runs on your own machine. You give it a source (a PDF, a web
page or a YouTube video), and it uses Claude to write a **study page** from it: an ordered sequence of explanations,
charts, small interactive demos and questions. The questions are "gates": you have to answer one
before the page shows what comes next.

The data is kept in a database and in plain folders. The database is Supabase's Postgres in
production and a local SQLite file for working locally or testing (see "Local and production
databases"); the folders are on the machine the server runs on. The only network calls are to
the Supabase database (when that is the one in use), the Anthropic API (writing and grading),
the OpenAI API (embeddings for search), the URLs you add as sources, and for a YouTube video the
YouTube Data API and YouTube itself (for the transcript). "Find sources" and "Explore" also call
arXiv's public API and fetch each address they find (without storing it).

## What it does

- **Library.** Add PDFs or URLs. Teacher saves the source, extracts its text and metadata, and
  lists what it cites. A YouTube video's address adds its transcript (with timestamps linking
  back to the video); this needs `YOUTUBE_API_KEY`.
- **Find sources.** On the Add page (or `teacher discover "<topic>"`), Teacher looks for sources
  on a topic or a concept: Claude with web search and arXiv's search, in parallel. It opens each
  result to check that it can be read (and says why not: too little text, a login, ...), marks
  what is already in the library, and ranks the rest for your level with a one-line reason
  (one Haiku call). Nothing is added until you press Add on a result and then "Add to library".
- **Explore a topic.** On the Explore page (or `teacher explore "<topic>"`), name a topic and
  your level. Teacher writes a brief (what is in and out of scope, told what you have studied),
  outlines at most 12 concepts in ordered steps (asking questions about the topic from a few
  perspectives first), checks each concept against your concept graph (without changing it),
  and finds candidate sources for the concepts that are new or that no library source
  introduces (at most two searches and five candidates each). You review it: rename or drop a
  concept, reorder steps, tick the sources you want. Nothing is added to the library or the
  graph yet.
- **Enrich an exploration.** When you have ticked sources, press Enrich (or run
  `teacher explore --enrich <id>`). Teacher first shows what will happen: which sources it will
  add (or link, when your library has them already), which concepts and prerequisite links it
  will write to your concept graph and which it will skip. Only when you confirm does it add the
  sources as the Add page does, extract their concepts (no study page is made), write the outline
  (concepts it cannot find are created unreviewed; a link that would duplicate another relation
  or close a cycle is skipped and shown), and mark the concepts a source now introduces as
  grounded. A source that fails stays ticked with its error; enrich again to retry it or after
  ticking more.
- **Roadmap.** A completed exploration has a roadmap (Open the roadmap on its page, the
  Explore list, or `teacher explore --roadmap <id>`): its steps in order, each concept with your
  standing on it, whether a source grounds it, the recommended source to study it from (you can
  choose another of its grounding sources) and that source's study page with how far you are,
  or a "Generate page" button, which starts the same generation as the source page's button.
  One source that grounds several concepts gets one page, shown under each. Nothing is generated
  until you press it. A concept is done when your standing on it is strong, its source's study
  page is complete (every gate cleared), or you mark it "I know this" (undo by unticking); a
  step is done when all its concepts that are not dropped are done. The current step is the
  first that is not done; later ones are upcoming, not locked. The roadmap shows your overall
  progress and one next action (open or continue a page, review a weak concept whose questions
  are due, generate a page, or find and enrich sources for a concept no source introduces), and
  home shows the roadmaps in progress under "Continue your roadmap". Progress is computed each
  time; only your source choices and "I know this" marks are stored.
- **Study pages.** Generated from one or more sources. A page mixes prose, multiple-choice and
  short-written-answer gates, prediction prompts, charts, interactive demos and figures cropped
  from the source PDF. A draft is reviewed and revised by a second model pass before it is stored.
- **Grading.** Multiple choice is checked directly; written answers are judged by a model against
  a rubric, with a "partly right" result that says what was missing.
- **Review queue.** Questions you have cleared come back at growing intervals, also as fresh
  variants.
- **Tutor.** A chat next to each page that can set extra exercises.
- **Concept graph.** Concepts are extracted from each source, linked to each other and to the
  blocks that teach them, and used to track what you already know.
- **Labs.** Follow-up coding assignments made from a finished study page. The Python runs in the
  browser (Pyodide in a sandbox).
- **History.** Every study session is recorded and can be exported.
- **Costs.** Every model and embedding call is recorded with its tokens and its cost in US
  dollars, priced when it is made. A study page or lab shows what generating it cost and what
  has been spent on it since (tutor, grading, ...); the tutor shows what a conversation and
  each reply cost; "Find sources" and an exploration show what they spent. Claude's web
  searches are counted with their calls but not priced (the price table has no rate for them).

## Setup

Requirements:

- [uv](https://docs.astral.sh/uv/) (it installs Python 3.12 and the dependencies)
- An Anthropic API key (required for generating pages, grading written answers and the tutor)
- An OpenAI API key (required only for embeddings: concept search and the concept graph)
- A YouTube Data API v3 key (required only for adding YouTube videos; create one in the Google
  Cloud console with the YouTube Data API v3 enabled, and add `YOUTUBE_API_KEY=<key>` to `.env`)
- Node.js 24 or newer (optional: lets the server verify generated labs, and runs the JS tests)

Steps, from the repository root:

```sh
# 1. Install dependencies into .venv
uv sync

# 2. Create .env with your keys (the file is git-ignored; .env.example lists every variable)
cat > .env <<'EOF'
ANTHROPIC_API_KEY=<your Anthropic key>
OPENAI_API_KEY=<your OpenAI key>
SUPABASE_DB_URL=<the Supabase Postgres connection string; only for the production database>
EOF

# 3a. Start the server on the local SQLite database
TEACHER_DB=sqlite uv run --env-file .env teacher serve

# 3b. Or on the production database, Supabase (the default: needs SUPABASE_DB_URL in .env)
uv run --env-file .env teacher serve
```

Then open http://127.0.0.1:8000. Without `TEACHER_DB=sqlite` the database is Supabase, and
without `SUPABASE_DB_URL` in `.env` `teacher serve` stops with an error saying so. The server
migrates the database to the current schema on startup, so there is no separate migration
step: the SQLite file is created if it does not exist yet; the Supabase database must exist
(the project's own `postgres` database is the one to use) and is migrated in place.

Optional, for labs:

```sh
uv run teacher labs setup    # downloads the Pyodide runtime into derived/pyodide/
```

(`derived/` is the one of the database in use: `supabase/derived/` for Supabase, see "Where
data lives"; pass `--db sqlite` or `--db supabase` to choose.)

### Notes for an AI agent setting this up

- Do not read, print or commit `.env`. If it already exists, leave it as it is; if it does not,
  ask the user for the keys instead of inventing values.
- The server does not reload code. After changing Python files, the server must be restarted for
  the change to take effect.
- To check the install without keys or network calls, run the tests (below). They use fake
  model clients and temporary data folders.
- For experiments, set `TEACHER_HOME` to an empty scratch directory and `TEACHER_DB=sqlite` so
  the real database and data folders are not touched (the default database is Supabase):
  `TEACHER_DB=sqlite TEACHER_HOME=/some/scratch/dir uv run --env-file .env teacher serve --port 8001`
- A missing key is reported as an explicit error when the feature that needs it is used. The app
  itself starts without keys.

## Configuration

All configuration is through environment variables.

| Variable | Purpose | Default |
|---|---|---|
| `ANTHROPIC_API_KEY` | Claude calls | none (required) |
| `OPENAI_API_KEY` | Embeddings | none |
| `YOUTUBE_API_KEY` | YouTube Data API v3 key: a video's title, channel, date and description when a YouTube video is added (`src/teacher/youtube_sources.py`). Set but blank is an error | none (required only for YouTube videos) |
| `TEACHER_HOME` | Where the data folders (and the SQLite file) live | current directory |
| `TEACHER_DB` | The database: `supabase` (production) or `sqlite` (the local file `teacher.db`). `teacher --db ...` overrides it for one command. Set but blank, or any other value, is an error | `supabase` |
| `SUPABASE_DB_URL` | The Supabase Postgres connection string (`postgresql://...`, a secret: never printed). Use the **Session pooler** string from the Supabase project's **Connect** dialog; the transaction pooler (port 6543) also works if the database's own settings are Supabase's defaults (time zone UTC, `extra_float_digits` 1 or more; the app checks and says how to fix them). Required when `TEACHER_DB` is `supabase` | none |
| `TEACHER_GENERATION_MODEL` | Writes study pages | `claude-sonnet-5-5` |
| `TEACHER_CONCEPT_MODEL` | Concept extraction and relating | same as generation |
| `TEACHER_TUTOR_MODEL` | Tutor chat | `claude-opus-5-5` |
| `TEACHER_JUDGE_MODEL` | Grades written answers | `claude-haiku-5-5` |
| `TEACHER_METADATA_MODEL` | Source metadata and references | `claude-haiku-5-5` |
| `TEACHER_EMBEDDING_MODEL` | Embeddings | `text-embedding-3-small` |
| `TEACHER_PYODIDE_DIR` | Use a Pyodide install somewhere else | `derived/pyodide/<version>` |
| `TEACHER_PASSWORD` | Turns on user accounts (`src/teacher/web/auth.py`): with it set, every page and API route needs signing in with your own account (email and password; a session lasts 30 days), and the variable's value is the **access code** that registering an account needs. Nobody signs in with it. Changing it does not sign anyone out; it only changes what new registrations need. Set but blank is an error, and `teacher serve` refuses a `--host` other than 127.0.0.1/localhost without it | none (no accounts, no sign-in; only for 127.0.0.1) |

The model for a single study page can also be picked in the UI when creating it.

Prices are not configured: they are a table in `src/teacher/pricing.py` (per model, with the
date each was checked and its source). A call to a model that is not in the table is recorded
with its tokens and no cost, and every total that includes it says so ("plus N calls with no
known price: <model>"). When prices change, update the table: costs already recorded keep the
figure they were stored with.

## Local and production databases

Teacher runs on one of two databases, chosen by `TEACHER_DB` (or `teacher --db ...` for one
command):

- `supabase`, the default: Supabase's Postgres at `SUPABASE_DB_URL`. This is production.
- `sqlite`: the local file `teacher.db` under `TEACHER_HOME`, for working offline and for
  testing changes without touching production.

On Postgres the first migration installs the `pg_trgm` extension (the concept search uses it).
That needs CREATE privilege on the database; Supabase's default `postgres` role has it.
Transactions behave as on SQLite: the reads of a transaction see one snapshot, and writers take
turns (a writer waits at most 5 seconds for another one to finish); the details are in
`src/teacher/db/providers/supabase.py`.

Both use the same code, tables and migrations, and keep their files apart under the same
`TEACHER_HOME`: the SQLite database's in `knowledge_sources/`, `artifacts/` and `derived/`
directly under it, Supabase's in the same folders under `supabase/` (see "Where data lives").
`teacher db info` says which database is in use, its revision and its data folder.

When the app runs on the local SQLite database the page header shows a small **Local SQLite**
badge (hover it for the file's path), so it is never mistaken for production; on Supabase
there is no badge.

`teacher db copy` copies everything, rows and files, from one database into the other, which
must be empty:

```sh
# Seed production from the local data
uv run --env-file .env teacher db copy --from sqlite --to supabase --dry-run   # check first
uv run --env-file .env teacher db copy --from sqlite --to supabase

# Pull production down into a local database to test against
TEACHER_HOME=/some/new/dir uv run --env-file .env teacher db copy --from supabase --to sqlite
```

The files go to the target's folder on the machine that runs the command: seeding production
from your machine fills `supabase/` under your `TEACHER_HOME`, so when production runs on Fly
that folder must then be copied to the volume's `/data/supabase/`.

Both databases use the same `TEACHER_HOME`, and the target must be empty, so pulling
production down needs a new `TEACHER_HOME` whose `supabase/` folder holds production's files
(on the Fly host they are in `/data/supabase/`): the command refuses a database whose source
or page folders are not under its data folder, rather than copying rows without their files.

It never migrates the source (it must be at the latest revision) and refuses, changing nothing,
when the target has any rows or files. It copies the files, then the rows in one transaction,
prints each table's row count on both sides with a check that every value arrived unchanged, and
rolls everything back on any difference. Then it rebuilds the concept search index and the
vector index. Don't use the app while it runs. The full description is in
`src/teacher/db_copy.py`.

## Hosting

Teacher can also run on [Fly.io](https://fly.io) as the app `airistotle`, from the `Dockerfile`
and `fly.toml` in the repository root:

- The image holds only the code and its locked dependencies; `.dockerignore` keeps `.env*`,
  the database and the data folders out. The keys come from Fly secrets.
- The database is Supabase (`TEACHER_DB=supabase`, set in `fly.toml`). The files (sources,
  generated pages, derived data) live on a Fly volume mounted at `/data` (`TEACHER_HOME`).
- Signing in is required: the server refuses to start without `TEACHER_PASSWORD`. `GET
  /healthz` stays open for Fly's health check. See "Accounts" below.
- One machine, always running (`fly.toml` says why: page generation runs inside the server).
- **Labs are not available there**: the image has no Node.js and no Pyodide. The app reports
  the lab runtime as not installed, and asking for a new lab fails with that reason.

The commands, in order, from the repository root (**not run yet**; needs
[flyctl](https://fly.io/docs/flyctl/install/) and a Fly account). The region is `iad`
(`primary_region` in `fly.toml`), next to the Supabase project in us-east-1:

```sh
fly apps create airistotle
fly volumes create teacher_data --app airistotle --region iad --size 3
fly secrets set --app airistotle --stage \
  ANTHROPIC_API_KEY=<key> OPENAI_API_KEY=<key> \
  SUPABASE_DB_URL=<postgresql://... connection string> TEACHER_PASSWORD=<access code>
fly deploy --app airistotle --ha=false
```

`--ha=false` keeps it to one machine (the volume belongs to one). Then open
https://airistotle.fly.dev/register and make your account with the access code
(`TEACHER_PASSWORD`). To check the image locally without Fly:
`docker build -t airistotle:local .` and
`docker run --rm -e TEACHER_DB=sqlite -e TEACHER_PASSWORD=<test access code> -p 127.0.0.1:8090:8080 airistotle:local`.

### Accounts

With `TEACHER_PASSWORD` set (always, when hosted) everyone signs in with their own account: an
email and a password (8 to 1024 characters). `TEACHER_PASSWORD` is not a password anyone signs
in with: it is the **access code** that registering an account at `/register` needs, so that
strangers who find the site cannot make one (they would see the library and spend the API keys).
Give it only to the people you want in.

- **After deploying, register your own account** at `/register` (email, password, access code).
  No account exists until someone registers; the migration creates none.
- There is no email verification and no reset by email. To reset a friend's password, or to
  lock someone out, use the command line on the server (`fly ssh console --app airistotle`,
  then for example `teacher users set-password friend@example.com`; see "Command line").
  A new password or disabling an account signs that account out everywhere.
- Sessions last 30 days from signing in; "Sign out" ends one. Only a hash of each session token
  and of each password is stored (tables `users` and `user_sessions`).
- Accounts do not own data yet: everyone signed in sees and changes the same library, pages and
  history.
- Clients that are not browsers can use `POST /api/auth/register`, `POST /api/auth/login` (both
  answer a token), then send `Authorization: Bearer <token>`; `GET /api/auth/me` and `POST
  /api/auth/logout`.
- Before accounts, `TEACHER_PASSWORD` was a shared sign-in password. Anyone signed in with it
  is signed out by this change and has to register (with the same value as the access code).

## Where data lives

Under `TEACHER_HOME` (the repository root by default). The rows (including the cost ledger,
table `api_calls`) are in the database; the files are in folders, one set per database:

| Path | Contents | In git |
|---|---|---|
| `teacher.db` | The SQLite database (SQLite only) | no |
| `knowledge_sources/` | The original source files (SQLite) | yes |
| `artifacts/` | Generated study pages and labs (SQLite) | yes |
| `derived/` | Vector index, Pyodide runtime and the command line's run records (SQLite) | no |
| `backups/` | Copies of `teacher.db` taken before each migration (SQLite only) | no |
| `supabase/` | The same `knowledge_sources/`, `artifacts/` and `derived/` for the Supabase database | not ignored |

The Supabase database itself is at Supabase; Teacher takes no backup of it before migrating,
Supabase's own backups (daily, or point-in-time recovery) apply. Every path stored in a database
is relative to its folder (`TEACHER_HOME`, or `TEACHER_HOME/supabase`), so the two databases
never share or overwrite each other's files.

## Command line

```sh
uv run teacher [--db supabase|sqlite] <command> ...   # --db: this command's database
uv run teacher serve [--host 127.0.0.1] [--port 8000]
uv run teacher db info
uv run teacher db copy --from sqlite|supabase --to sqlite|supabase [--dry-run]
uv run teacher db rebuild-search
uv run teacher labs setup
uv run teacher history export [--format json|markdown] [--output PATH]
uv run teacher concepts extract <source-slug>
uv run teacher concepts reindex [--rebuild-index]
uv run teacher concepts relate <concept-slug> | --all
uv run teacher concepts tag-blocks <artifact-slug> | --all
uv run teacher references extract <source-slug> | --all
uv run teacher costs backfill [--dry-run]
uv run teacher discover "<topic>" | --concept SLUG [--level intro|intermediate|advanced] [--json]
uv run teacher explore "<topic>" [--level intro|intermediate|advanced] [--json]
uv run teacher explore --enrich <exploration-id> [--yes] [--json]
uv run teacher explore --roadmap <exploration-id> [--json]
uv run teacher users list
uv run teacher users add|set-password|disable|enable <email>
```

`teacher users` manages the accounts (see "Accounts"): `add` creates one, `set-password` gives one
a new password and `disable` stops one from signing in (both sign it out everywhere), `enable`
lets it in again. Passwords are typed at a hidden prompt, twice; they are never taken from an
argument and never printed.

`teacher costs backfill` adds the API calls made before the cost ledger existed (from the
generation runs and their stage files, graded answers, tutor replies and reference
extractions). It makes no API call, can be run again safely (it adds nothing twice), and prints
how many rows are exact and how many are estimates: calls of which only the combined input
token count was kept are priced at an upper bound and shown as "at most".

`teacher discover` finds candidate sources for a topic or a concept and prints them ranked,
adding nothing. `teacher explore` runs a whole exploration in the foreground (its stages on
stderr) and prints the outline, each concept's match and candidate sources, and what it cost;
the exploration is stored and listed on the Explore page when the app runs without accounts.
`teacher explore --enrich <id>` prints what enriching that exploration will do and asks before
doing it (`--yes` does not ask), then runs it and prints the summary. `teacher explore
--roadmap <id>` prints that exploration's roadmap (progress, next action, each step and concept
with its source and page) and changes nothing; `--json` prints it as the API returns it.

`teacher db info` prints the database in use (never its password), its revision and its data
folder. `teacher db copy` copies one database into the other, empty one (see "Local and
production databases"); `teacher db rebuild-search` rebuilds the concept search index of the
database in use, the retry when that last step of a copy failed.

Commands that call Claude or OpenAI need the keys, so add `--env-file .env` after `uv run`. The
full description of every command and flag is the docstring at the top of `src/teacher/cli.py`.

## Tests

```sh
uv run pytest -q                      # whole suite; takes around 20 minutes
uv run pytest -q tests/test_blocks.py # one file
node tests/web/js/<name>.mjs          # one front-end test
```

The tests make no real model calls and need no keys. Tests that run lab code are skipped unless
`TEACHER_PYODIDE_DIR` points at an installed runtime; tests that need Node are skipped without it.

The suite runs on SQLite by default (a new file per test). To run it on Postgres, point it at
a local Postgres server's admin URL; each test then gets its own database, created and dropped
on that server (a host other than 127.0.0.1, localhost or ::1 is refused, so a test can never
reach Supabase):

```sh
TEACHER_TEST_DB=postgres TEACHER_TEST_POSTGRES_URL=postgresql://postgres:<password>@127.0.0.1:5432/postgres uv run pytest -q
```

Tests marked `sqlite_only` are skipped there, and `postgres_only` ones (for example the
`teacher db copy` round trips through Postgres) are skipped on SQLite.

## Code layout

```
src/teacher/
  cli.py                 the `teacher` command
  config.py              the database choice, data locations and model settings
  db_copy.py             `teacher db copy`: one database into the other
  claude_generator.py    study page generation (read its docstring for the stages)
  page_revision.py       the review-and-revise pass on a draft
  blocks.py              the stored shapes of page blocks
  grading.py, judge.py   grading of gates
  reading.py             gate state while reading a page
  tutor.py               tutor chat
  review_*.py            review queue
  concept_*.py           concept extraction, graph and search
  discovery/             finding candidate sources for a topic (web search, arXiv, triage)
  exploration/           exploring a topic: brief, outline, graph match, sources to review,
                         and enriching it (ingest, write the outline to the graph, grounding)
  source_ingest.py       storing a fetched address as a source (the Add page and enrichment)
  costs.py, pricing.py, cost_backfill.py   the cost ledger, the price table, the backfill
  labs.py, lab_generator.py, pyodide_runtime.py   labs
  db/, managers/         database models and the classes that read and write them
  db/providers/          SQLite and Supabase: what differs between the two databases
  db/search/             the concept search index: FTS5 tables on SQLite, pg_trgm on Postgres
  web/                   FastAPI app: routers/, templates/, static/ (plain ES modules, no build step)
migrations/              alembic migrations (applied automatically on startup)
tests/                   pytest suite; tests/web/js holds the front-end tests
```

Most modules start with a docstring that explains their design, which is the best place to start
when changing one.

## Conventions

- Fail loudly: raise an explicit error; do not fall back silently or drop a feature quietly.
- Keep module docstrings true when behaviour changes.
- New database changes go in a new numbered file in `migrations/versions/`.
