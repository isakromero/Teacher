# Teacher

Teacher is a study tool that runs on your own machine. You give it a source (a PDF, a web
page or a YouTube video), and it uses Claude to write a **study page** from it: an ordered sequence of explanations,
charts, small interactive demos and questions. The questions are "gates": you have to answer one
before the page shows what comes next.

Everything is stored locally, in a SQLite database and plain folders. The only network calls are
to the Anthropic API (writing and grading), the OpenAI API (embeddings for search), the URLs
you add as sources, and for a YouTube video the YouTube Data API and YouTube itself (for the
transcript).

## What it does

- **Library.** Add PDFs or URLs. Teacher saves the source, extracts its text and metadata, and
  lists what it cites. A YouTube video's address adds its transcript (with timestamps linking
  back to the video); this needs `YOUTUBE_API_KEY`.
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

# 2. Create .env with your keys (the file is git-ignored)
cat > .env <<'EOF'
ANTHROPIC_API_KEY=<your Anthropic key>
OPENAI_API_KEY=<your OpenAI key>
EOF

# 3. Start the server
uv run --env-file .env teacher serve
```

Then open http://127.0.0.1:8000. The server creates and migrates the database on startup, so
there is no separate migration step.

Optional, for labs:

```sh
uv run teacher labs setup    # downloads the Pyodide runtime into derived/pyodide/
```

### Notes for an AI agent setting this up

- Do not read, print or commit `.env`. If it already exists, leave it as it is; if it does not,
  ask the user for the keys instead of inventing values.
- The server does not reload code. After changing Python files, the server must be restarted for
  the change to take effect.
- To check the install without keys or network calls, run the tests (below). They use fake
  model clients and temporary data folders.
- For experiments, set `TEACHER_HOME` to an empty scratch directory so the real database and
  data folders are not touched:
  `TEACHER_HOME=/some/scratch/dir uv run --env-file .env teacher serve --port 8001`
- A missing key is reported as an explicit error when the feature that needs it is used. The app
  itself starts without keys.

## Configuration

All configuration is through environment variables.

| Variable | Purpose | Default |
|---|---|---|
| `ANTHROPIC_API_KEY` | Claude calls | none (required) |
| `OPENAI_API_KEY` | Embeddings | none |
| `YOUTUBE_API_KEY` | YouTube Data API v3 key: a video's title, channel, date and description when a YouTube video is added (`src/teacher/youtube_sources.py`). Set but blank is an error | none (required only for YouTube videos) |
| `TEACHER_HOME` | Where the database and data folders live | current directory |
| `TEACHER_GENERATION_MODEL` | Writes study pages | `claude-sonnet-5-5` |
| `TEACHER_CONCEPT_MODEL` | Concept extraction and relating | same as generation |
| `TEACHER_TUTOR_MODEL` | Tutor chat | `claude-opus-5-5` |
| `TEACHER_JUDGE_MODEL` | Grades written answers | `claude-haiku-5-5` |
| `TEACHER_METADATA_MODEL` | Source metadata and references | `claude-haiku-5-5` |
| `TEACHER_EMBEDDING_MODEL` | Embeddings | `text-embedding-3-small` |
| `TEACHER_PYODIDE_DIR` | Use a Pyodide install somewhere else | `derived/pyodide/<version>` |

The model for a single study page can also be picked in the UI when creating it.

## Where data lives

Under `TEACHER_HOME` (the repository root by default):

| Path | Contents | In git |
|---|---|---|
| `teacher.db` | SQLite database | no |
| `knowledge_sources/` | The original source files | yes |
| `artifacts/` | Generated study pages and labs | yes |
| `derived/` | Rebuildable data: extracted text, vector index, Pyodide runtime, run records | no |
| `backups/` | Database backups | no |

## Command line

```sh
uv run teacher serve [--host 127.0.0.1] [--port 8000]
uv run teacher labs setup
uv run teacher history export [--format json|markdown] [--output PATH]
uv run teacher concepts extract <source-slug>
uv run teacher concepts reindex [--rebuild-index]
uv run teacher concepts relate <concept-slug> | --all
uv run teacher concepts tag-blocks <artifact-slug> | --all
uv run teacher references extract <source-slug> | --all
```

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

## Code layout

```
src/teacher/
  cli.py                 the `teacher` command
  config.py              data locations and model settings
  claude_generator.py    study page generation (read its docstring for the stages)
  page_revision.py       the review-and-revise pass on a draft
  blocks.py              the stored shapes of page blocks
  grading.py, judge.py   grading of gates
  reading.py             gate state while reading a page
  tutor.py               tutor chat
  review_*.py            review queue
  concept_*.py           concept extraction, graph and search
  labs.py, lab_generator.py, pyodide_runtime.py   labs
  db/, managers/         database models and the classes that read and write them
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
