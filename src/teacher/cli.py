"""The `teacher` command line.

    teacher [--db supabase|sqlite] <command> ...
    teacher serve [--host 127.0.0.1] [--port 8000]
    teacher db info
    teacher db copy --from sqlite|supabase --to sqlite|supabase [--dry-run]
    teacher db rebuild-search
    teacher concepts extract <source-slug>
    teacher concepts reindex [--rebuild-index]
    teacher concepts relate <concept-slug> [--regenerate] [--model ID] [-v] [--stub]
    teacher concepts relate --all [--limit N] [--regenerate --yes] [--model ID] [-v] [--stub]
    teacher concepts tag-blocks <artifact-slug> | --all [--retag] [--model ID] [--stub] [-v]
    teacher labs setup
    teacher history export [--format json|markdown] [--output PATH]
    teacher references extract <source-slug> | --all [--redo] [--stub]
    teacher costs backfill [--dry-run]
    teacher discover "<topic>" | --concept SLUG [--level intro|intermediate|advanced] [--json]
    teacher explore "<topic>" [--level intro|intermediate|advanced] [--json]
    teacher explore --enrich <exploration-id> [--yes] [--json]
    teacher explore --roadmap <exploration-id> [--json]
    teacher users list
    teacher users add|set-password|disable|enable <email>

`serve` runs the web app with uvicorn on `--host` and `--port`. With $TEACHER_PASSWORD set,
every page and API route needs signing in with a user account, and the variable's value is the
access code that registering an account needs (`teacher.web.auth`); set but blank is an error.
Without it the app is open to anyone who can reach it, so a `--host` that is not a
loopback address (127.0.0.1 or any 127.x, ::1, localhost) is refused with status 1 and an error
naming the variable, before anything starts. uvicorn trusts the `X-Forwarded-For` and
`X-Forwarded-Proto` headers (the client's address and HTTPS) only from the addresses in
$FORWARDED_ALLOW_IPS (default: loopback); the container image (`Dockerfile`) trusts its host's
proxy.

`concepts relate` grows relations between existing concepts (`teacher.concept_relating`): for
each concept it prints the relations added, how many candidates were judged `none` (listed with
`-v`), the refused ones with the reason, the suggestions the graph has no concept for, the
candidates by origin and the tokens used, and keeps the run's record in
`derived/_concept_relations/<concept-slug>.json`. `--all` runs it for every concept in slug order
(the first N with `--limit`), stops at the first error with status 1 and keeps what earlier
concepts committed. `--stub` uses the offline stubs instead of Claude and OpenAI (scratch homes
only: the stub's vectors replace the stored embeddings). `--model` runs both Claude calls with
another model, one of the study-page models or $TEACHER_CONCEPT_MODEL (anything else is an
error before any call). `--regenerate` re-decides the concept's unreviewed model-made relations
(reviewed ones and those from a paper or a person are kept) and replaces them; with `--all` it
first prints how many relations would be replaced and needs `--yes` to go ahead (without it,
nothing is called or changed and the status is 1). In an `--all --regenerate` pass a relation
made for one concept is re-decided again when the concept at its other end comes up.

`concepts tag-blocks` tags the blocks of an existing study page with the concepts linked to its
sources (`teacher.block_tagging`: one Claude call per page), so answers and time on each block
count towards the concepts it covers: it prints, per page, the tags by role, the blocks and
questions left without a concept and the tokens used, and keeps the run's record in
`derived/_block_concepts/<artifact-slug>.json`. A page tagged before is an error unless
`--retag` (which replaces its model-made tags; tags a person made are kept). `--all` runs it for
every ready study page in creation order, skipping the ones tagged before (all of them are
retagged with `--retag`), stops at the first error with status 1 and keeps the pages tagged
before it, so running it again continues where it stopped; it ends with the totals and tokens.
`--stub` uses the offline stub (tags a block with the concepts its text names; scratch homes
only); `--model` is one of the study-page models or $TEACHER_CONCEPT_MODEL. It migrates the
database to head first.

`concepts extract` runs concept extraction (`teacher.concept_extraction`) for an existing source
without regenerating any study page: it prints what was created, matched exactly and matched by
the judge, replaces the source's concept links, and keeps the run's record in
`derived/<source-slug>/concepts.json`. `concepts reindex` embeds every concept whose embedding
is missing or stale and prints how many, then brings the on-disk vector index of the embedding
model (`teacher.vector_index`) up to date with the embeddings and writes it; with
`--rebuild-index` the index is rebuilt from the embeddings without reading the old files (the
fix for a corrupt or mismatched index). Both migrate the database to head first (as the
server does on startup), and exit with status 1 and the error on stderr when they fail.

`labs setup` downloads and verifies the pinned Pyodide runtime that labs run their Python in
(`teacher.pyodide_runtime.install_runtime`) into `derived/pyodide/<version>`, or into
$TEACHER_PYODIDE_DIR when that is set, printing each file; an install that is already complete is
kept. It then says whether the server can also run labs (that needs Node.js; without it the
reader's browser still can). Status 1 with the error on stderr when the install fails.

`references extract` lists what a source cites (`teacher.reference_extraction`: one Claude call
with the metadata model, $TEACHER_METADATA_MODEL), stores the references, matches them to the
library and prints how many there are and how many are in the library. The run is recorded on
the source like the app's (a failure with its error; it never changes the source), and a run
already going for the source is an error. `--all` runs it for every source in the order they
were added whose references were never extracted or whose last run failed or was interrupted
(also the ones done before with `--redo`); a failure is printed and the next source is tried,
and the status is 1 if any failed. `--stub` uses the offline stub (it reads a "References"
section of text sources; scratch homes only). It migrates the database to head first.

`history export` writes the whole study history (`teacher.managers.study_history`): every
study session with its totals and timeline (pages opened, answers with their feedback, the
tutor chats in full, lab runs with their output and files, time on each page) as JSON (the
default, complete) or Markdown (`teacher.history_export`), to stdout or to `--output`, which
must not exist yet. It migrates the database to head first; status 1 with the error on stderr
when it fails.

`costs backfill` adds the API calls made before the cost ledger existed to it
(`teacher.cost_backfill`): the calls of every generation run (from its stage files where they
hold the full token breakdown, else from the run's totals), the graded answers, the tutor
replies, the reference extractions and the command line's run records in `derived/`. It prints
how many rows it added, how many are exact, how many are estimates (calls of which only the
combined input count was kept: their cost is an upper bound) and how many have no known price,
with the total. It is idempotent (a second run adds nothing) and never duplicates a call the
ledger recorded live. `--dry-run` prints the same without adding anything. It makes no API call
and migrates the database to head first; status 1 with the error on stderr when it fails.

`discover` finds candidate sources for a topic, or for a concept of the graph (`--concept`
with its slug: its name, aliases and summary are searched), at a reader `--level` (default
intermediate), with `teacher.discovery`: Claude with web search (the generation model) and
arXiv in parallel, merged and matched against the library, each candidate fetched without
storing anything, the readable ones scored by one call to the metadata model. It prints the
ranked candidates with their verdicts, scores and reasons, every provider's status (a failed
one also as a warning on stderr), and what the model calls cost; `--json` prints the whole
result as JSON instead. It adds nothing to the library. Its model calls are recorded in the
cost ledger (purpose `source_discovery`) and their cost is printed. Status 1 with the error on
stderr when every provider failed, triage failed, or the topic or concept is not valid. It
migrates the database to head first.

`explore` explores a topic at a reader `--level` (default intermediate) with
`teacher.exploration`, in the foreground: a brief (the generation model, told what the reader
has studied), an outline of at most 12 concepts in ordered steps, each concept matched against
the concept graph (read-only: nothing is written to the graph) and, for each one that is new or
thinly covered, candidate sources found as `discover` finds them (at most two searches and five
candidates per concept). It prints these limits and each stage on stderr as it starts (there
is no price estimate), then the outline with
every concept's match, its candidates with their verdicts and reasons, every concept whose
search failed (also as a warning on stderr), and what the run cost (from the cost ledger, purpose
`exploration`); `--json` prints the whole exploration as the API returns it. The exploration is
stored (without an account, so it is listed on the Explore page when the app runs without
accounts) and nothing is added to the library. Status 1 with the error on stderr when the topic
or level is not valid, the run cannot start (no credentials, a stale embedding index) or it
failed. It migrates the database to head first.

`explore --enrich <id>` enriches a completed exploration stored without an account (those
`explore` made, or the app's when it runs without accounts) with `teacher.exploration.
enrichment`: it prints what will happen (the ticked sources to add or link, the concepts to use
or look up again and create, each prerequisite link and whether it will be added, is present or
will be skipped, and which model calls it makes; there is no price estimate) and asks to go on
(`--yes` goes on without asking; without a terminal to ask on it is an error and nothing
changes). Then, in the foreground with each stage on stderr: the approved sources are ingested
as the Add page adds an address, with concept extraction only (no study page), the outline is
written to the concept graph and each concept's grounding is checked. It prints the summary
(sources added, already held, failed with their errors; concepts matched, found, created;
relations added, present, skipped with reasons; concepts grounded and still ungrounded) and
what it cost; `--json` prints the whole exploration instead. Status 1 with the error on stderr
when the exploration is not found or not completed, one is being enriched, the plan was not
confirmed, it cannot start or it failed (a source that failed is not a failure of the
enrichment: it is listed, kept ticked, and tried again next time).

`explore --roadmap <id>` prints the roadmap of a completed exploration stored without an account
(`teacher.exploration.roadmap`): its overall progress and next action, then each step (done,
current or upcoming) with its concepts: whether each is done and why, the reader's standing, the
primary source (recommended or chosen) and its study page with how far it is, or that it has
none yet, or that no source grounds it. `--json` prints the roadmap as `GET
/api/explorations/{id}/roadmap` returns it. It is read-only: nothing is generated or written
(it does not bring the review queue's items up to date as the web app does first, so a weak
concept's review counts are those of the last queue read) and the gates count as cleared after
the app's default number of attempts (`teacher.reading.DEFAULT_MAX_ATTEMPTS`). Status 1 with
the error on stderr when the exploration is not found or not completed. It migrates the
database to head first.

`db info` prints the database in use (the provider's description: never a password), the
revision it is at and the head revision, and the data root its files live under. It changes
nothing (no migration); status 1 with the error on stderr when the database cannot be read.

`db copy` copies every row and file of one database to the other (`teacher.db_copy`): to seed
Supabase from the local SQLite data (`--from sqlite --to supabase`) or to pull Supabase down
into a local database to test against (`--from supabase --to sqlite`); both use
$TEACHER_HOME, the Supabase one $SUPABASE_DB_URL. It refuses, changing nothing, unless the two
differ, the source is at the head revision (it is never migrated) with its sources' and pages'
folders under its data root, and the target is empty: no
row in any table after it is migrated to head (a SQLite file is created if needed) and empty
`knowledge_sources/`, `artifacts/` and `derived/` folders. It copies the files and then the rows
(one snapshot read, one transaction written), resets Postgres's sequences, prints per table the
row counts of both and whether a digest of every column of every row matches, and rolls
everything back on any difference; after committing it rebuilds the concept search index and
the vector index. `--dry-run` checks the same and prints the tables with their row counts and
the folders with their file counts and sizes, changing nothing. `--db` is not used with it.
`db rebuild-search` rebuilds the concept search index of the database in use from the concepts
and checks it (the retry when that last step of a copy failed). Both: status 1 with the error on
stderr when they fail.

`users` manages the accounts people sign in with when the app runs behind the account gate
(`teacher.managers.users`): `list` prints each account's email, when it was created, when it
last signed in and whether it is disabled; `add <email>` creates an account; `set-password
<email>` gives an account a new password (there is no reset by email: this is how one is reset)
and signs it out everywhere; `disable <email>` stops an account from signing in and signs it out
everywhere; `enable <email>` lets it sign in again. `add` and `set-password` read the password
twice with a hidden prompt (never from an argument or the environment) and never print it. Each
migrates the database to head first; status 1 with the error on stderr when it fails (an email
with no account, an account that exists already, a password that breaks the rules, the two
entries differing, no terminal to prompt on).

Every API call any command makes is recorded in the cost ledger (`teacher.costs`).

The database is Supabase's Postgres ($SUPABASE_DB_URL) unless $TEACHER_DB=sqlite selects the
local SQLite file; `--db` overrides $TEACHER_DB for one command (it sets the variable for the
process, so the server started by `serve` uses it too). Files live under `$TEACHER_HOME` (default:
the current directory), in `supabase/` there for the Supabase database; see `teacher.config`. The
models come from `ModelSettings` ($TEACHER_CONCEPT_MODEL, $TEACHER_EMBEDDING_MODEL); Claude needs
Anthropic credentials and embeddings need $OPENAI_API_KEY.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import sys
from collections.abc import Sequence
from typing import Any

from teacher.config import (
    DB_KINDS,
    DEFAULT_DB_KIND,
    ENV_DB,
    ENV_PASSWORD,
    ConfigError,
    ModelSettings,
    Settings,
    teacher_password_from_env,
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="teacher", description="Local study tool.")
    parser.add_argument(
        "--db",
        choices=DB_KINDS,
        default=None,
        help=f"the database to use; overrides ${ENV_DB} (default {DEFAULT_DB_KIND})",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="run the local web API with uvicorn")
    serve.add_argument("--host", default=DEFAULT_HOST, help=f"bind address (default {DEFAULT_HOST})")
    serve.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help=f"port (default {DEFAULT_PORT})"
    )
    db = commands.add_parser("db", help="the database")
    db_actions = db.add_subparsers(dest="action", required=True)
    db_actions.add_parser(
        "info", help="print the database in use, its revision and the data root"
    )
    db_copy = db_actions.add_parser(
        "copy", help="copy every row and file of one database into the other, empty one"
    )
    db_copy.add_argument(
        "--from", dest="source", choices=DB_KINDS, required=True, help="the database to read"
    )
    db_copy.add_argument(
        "--to", dest="target", choices=DB_KINDS, required=True, help="the empty database to fill"
    )
    db_copy.add_argument(
        "--dry-run", action="store_true", help="check and say what would be copied, change nothing"
    )
    db_actions.add_parser(
        "rebuild-search", help="rebuild the concept search index from the concepts"
    )
    concepts = commands.add_parser("concepts", help="the concept graph")
    actions = concepts.add_subparsers(dest="action", required=True)
    extract = actions.add_parser(
        "extract", help="extract a source's concepts and link them (no page is regenerated)"
    )
    extract.add_argument("slug", help="the knowledge source's slug")
    reindex = actions.add_parser(
        "reindex",
        help="embed every concept whose embedding is stale and bring the vector index up to date",
    )
    reindex.add_argument(
        "--rebuild-index",
        action="store_true",
        help="rebuild the vector index from the embeddings, ignoring the old index files",
    )
    relate = actions.add_parser(
        "relate", help="find relations between existing concepts with Claude (creates none)"
    )
    target = relate.add_mutually_exclusive_group(required=True)
    target.add_argument("slug", nargs="?", help="the concept's slug")
    target.add_argument("--all", action="store_true", help="every concept, in slug order")
    relate.add_argument(
        "--limit", type=int, default=None, help="with --all: only the first N concepts"
    )
    relate.add_argument(
        "-v", "--verbose", action="store_true", help="also list dropped and 'none' candidates"
    )
    relate.add_argument(
        "--stub",
        action="store_true",
        help="offline stubs instead of Claude and OpenAI (scratch homes only)",
    )
    relate.add_argument(
        "--regenerate",
        action="store_true",
        help="re-decide and replace the unreviewed model-made relations (others are kept)",
    )
    relate.add_argument(
        "--model",
        default=None,
        help="the Claude model for this run (default: $TEACHER_CONCEPT_MODEL)",
    )
    relate.add_argument(
        "--yes", action="store_true", help="with --all --regenerate: replace without asking"
    )
    tag = actions.add_parser(
        "tag-blocks", help="tag a study page's blocks with the concepts they cover (Claude)"
    )
    tag_target = tag.add_mutually_exclusive_group(required=True)
    tag_target.add_argument("slug", nargs="?", help="the study page's (artifact's) slug")
    tag_target.add_argument(
        "--all", action="store_true", help="every ready study page not tagged yet, oldest first"
    )
    tag.add_argument(
        "--retag", action="store_true", help="replace the model-made tags of pages tagged before"
    )
    tag.add_argument(
        "--model", default=None, help="the Claude model (default: $TEACHER_CONCEPT_MODEL)"
    )
    tag.add_argument(
        "--stub", action="store_true", help="the offline stub instead of Claude (scratch homes only)"
    )
    tag.add_argument("-v", "--verbose", action="store_true", help="also list every block's tags")
    references = commands.add_parser("references", help="what each source cites")
    reference_actions = references.add_subparsers(dest="action", required=True)
    ref_extract = reference_actions.add_parser(
        "extract", help="extract a source's references with Claude and match them to the library"
    )
    ref_target = ref_extract.add_mutually_exclusive_group(required=True)
    ref_target.add_argument("slug", nargs="?", help="the knowledge source's slug")
    ref_target.add_argument(
        "--all", action="store_true", help="every source not extracted yet (or failed), in order"
    )
    ref_extract.add_argument(
        "--redo", action="store_true", help="with --all: also the sources extracted before"
    )
    ref_extract.add_argument(
        "--stub", action="store_true", help="the offline stub instead of Claude (scratch homes)"
    )
    labs = commands.add_parser("labs", help="hands-on coding labs")
    lab_actions = labs.add_subparsers(dest="action", required=True)
    lab_actions.add_parser(
        "setup", help="download and verify the Python runtime (Pyodide) labs run in"
    )
    history = commands.add_parser("history", help="the study history")
    history_actions = history.add_subparsers(dest="action", required=True)
    export = history_actions.add_parser(
        "export", help="write every study session with its timeline (JSON or Markdown)"
    )
    export.add_argument(
        "--format", choices=("json", "markdown"), default="json", help="default: json"
    )
    export.add_argument(
        "--output", default=None, help="a file to write (must not exist); default: stdout"
    )
    costs = commands.add_parser("costs", help="the cost ledger of the API calls")
    cost_actions = costs.add_subparsers(dest="action", required=True)
    cost_backfill = cost_actions.add_parser(
        "backfill", help="add the API calls made before the ledger existed (idempotent)"
    )
    cost_backfill.add_argument(
        "--dry-run", action="store_true", help="say what would be added, add nothing"
    )
    from teacher.discovery.common import DEFAULT_LEVEL, LEVELS

    discover = commands.add_parser(
        "discover", help="find candidate sources for a topic or a concept (adds nothing)"
    )
    discover_target = discover.add_mutually_exclusive_group(required=True)
    discover_target.add_argument("topic", nargs="?", help="what to find sources for")
    discover_target.add_argument(
        "--concept", default=None, help="a concept's slug: find sources for that concept"
    )
    discover.add_argument(
        "--level", choices=tuple(LEVELS), default=DEFAULT_LEVEL,
        help=f"the reader's level (default {DEFAULT_LEVEL})",
    )  # fmt: skip
    discover.add_argument(
        "--json", action="store_true", help="print the whole result as JSON"
    )
    explore = commands.add_parser(
        "explore",
        help="outline a topic into concepts, match them against the graph and find sources "
        "(adds nothing); or enrich a completed exploration (--enrich)",
    )
    explore_target = explore.add_mutually_exclusive_group(required=True)
    explore_target.add_argument("topic", nargs="?", help="what to explore")
    explore_target.add_argument(
        "--enrich", type=int, default=None, metavar="ID",
        help="add the ticked sources of completed exploration ID to the library and its "
        "outline to the concept graph (asks first)",
    )  # fmt: skip
    explore_target.add_argument(
        "--roadmap", type=int, default=None, metavar="ID",
        help="print the roadmap of completed exploration ID: its steps, progress and next "
        "action (read-only)",
    )  # fmt: skip
    explore.add_argument(
        "--level", choices=tuple(LEVELS), default=None,
        help=f"the reader's level (default {DEFAULT_LEVEL})",
    )  # fmt: skip
    explore.add_argument(
        "--yes", action="store_true", help="with --enrich: go ahead without asking"
    )
    explore.add_argument(
        "--json", action="store_true",
        help="print the whole exploration (with --roadmap: the roadmap) as JSON",
    )
    users = commands.add_parser("users", help="the accounts people sign in with")
    user_actions = users.add_subparsers(dest="action", required=True)
    user_actions.add_parser("list", help="every account")
    for name, text in (
        ("add", "create an account (the password is asked for, hidden)"),
        ("set-password", "give an account a new password (asked for, hidden); signs it out"),
        ("disable", "stop an account from signing in; signs it out everywhere"),
        ("enable", "let a disabled account sign in again"),
    ):
        user_actions.add_parser(name, help=text).add_argument("email")
    return parser


def is_loopback(host: str) -> bool:
    """Whether `host` (a `--host` value) only accepts connections from this machine."""
    name = host.strip()
    if name.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(name.strip("[]")).is_loopback
    except ValueError:
        return False


def serve(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    """Build the app from `Settings.from_env()`, behind the account gate when
    $TEACHER_PASSWORD (the access code for registering) is set, and run it with uvicorn
    (blocks). No access code and a host that is not loopback is a ConfigError, raised before
    anything starts."""
    access_code = teacher_password_from_env()
    if access_code is None and not is_loopback(host):
        raise ConfigError(
            f"refusing to serve on {host!r} without sign-in: anyone who can reach it could "
            f"use the app and spend on its API keys. Set ${ENV_PASSWORD} to the access code "
            "for registering accounts, or serve on 127.0.0.1."
        )
    import uvicorn

    from teacher.web.app import create_app

    uvicorn.run(create_app(access_code=access_code), host=host, port=port)


# -- concepts -------------------------------------------------------------------------------


def build_embedder(models: ModelSettings) -> Any:
    """The embedder the concept commands use (tests replace this function)."""
    from teacher.embeddings import OpenAIEmbedder

    return OpenAIEmbedder(models.embedding)


def build_concept_extractor(models: ModelSettings) -> Any:
    """The concept extractor `concepts extract` uses (tests replace this function)."""
    from teacher.concept_extraction import ConceptExtractor

    return ConceptExtractor(model=models.concept, embedder=build_embedder(models))


def build_concept_relater(models: ModelSettings, *, stub: bool = False) -> Any:
    """The concept relater `concepts relate` uses (tests replace this function)."""
    from teacher.concept_relating import ConceptRelater, stub_relater

    if stub:
        return stub_relater()
    return ConceptRelater(model=models.concept, embedder=build_embedder(models))


def build_block_tagger(models: ModelSettings, *, stub: bool = False) -> Any:
    """The block tagger `concepts tag-blocks` uses (tests replace this function)."""
    from teacher.block_tagging import BlockTagger, stub_tagger

    if stub:
        return stub_tagger()
    return BlockTagger(model=models.concept)


def _fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 1


def concepts_extract(slug: str) -> int:
    """Run concept extraction for source `slug`; print the summary. 0 on success, else 1."""
    from teacher.claude_generator import write_record
    from teacher.concept_extraction import ConceptExtractionError, record_path, source_info
    from teacher.db.engine import session_scope
    from teacher.db.migrate import upgrade_to_head
    from teacher.managers import ManagerError

    try:
        settings = Settings.from_env()
        extractor = build_concept_extractor(ModelSettings.from_env())
        upgrade_to_head(settings)
        with session_scope(settings) as session:
            source = source_info(session, settings, slug)
        extractor.check_available()
        run = extractor.run(settings, source)
    except (ConceptExtractionError, ManagerError, ValueError) as exc:
        return _fail(str(exc))
    for line in run.summary_lines():
        print(line)
    path = record_path(settings, slug)
    write_record(path, run.record("cli"))
    print(f"Record: {path}")
    return 0


def concepts_reindex(*, rebuild_index: bool = False) -> int:
    """Embed every stale concept (committed), then sync the model's vector index with the
    embeddings and write it, or rebuild it from scratch (`rebuild_index`); print what was done.
    0 on success, else 1."""
    from teacher.costs import cost_scope
    from teacher.db.engine import session_scope
    from teacher.db.migrate import upgrade_to_head
    from teacher.embeddings import EmbeddingError
    from teacher.managers import ConceptEmbeddingManager, ManagerError

    try:
        settings = Settings.from_env()
        embedder = build_embedder(ModelSettings.from_env())
        upgrade_to_head(settings)
        with cost_scope(settings, purpose="embedding"), session_scope(settings) as session:
            count = ConceptEmbeddingManager(session, settings).embed_stale(embedder)
    except (EmbeddingError, ManagerError, ValueError) as exc:
        return _fail(str(exc))
    print(f"Embedded {count} stale concept(s) with {embedder.model}.")
    try:
        with session_scope(settings) as session:
            manager = ConceptEmbeddingManager(session, settings)
            if rebuild_index:
                done = manager.rebuild_index(embedder.model)
            else:
                done = manager.sync_index(embedder.model)
    except (ManagerError, ValueError) as exc:
        return _fail(str(exc))
    if done.directory is None:
        print(f"There are no {embedder.model} embeddings, so there is no vector index.")
    elif rebuild_index:
        print(
            f"Rebuilt the vector index ({done.dimensions} dimensions) from {done.total} "
            f"embedding(s): {done.directory}"
        )
    else:
        print(
            f"Vector index ({done.dimensions} dimensions): {done.added} added or replaced, "
            f"{done.removed} removed, {done.total} in total: {done.directory}"
        )
    return 0


def concepts_relate(
    slug: str | None,
    *,
    all_concepts: bool = False,
    limit: int | None = None,
    verbose: bool = False,
    stub: bool = False,
    regenerate: bool = False,
    model: str | None = None,
    yes: bool = False,
) -> int:
    """Find relations for concept `slug`, or for every concept (`all_concepts`, the first
    `limit`); print each run's summary. `regenerate` re-decides the unreviewed model-made
    relations (with `all_concepts` only after `yes`); `model` is the Claude model of the runs.
    0 on success, else 1 at the first error."""
    from teacher.claude_generator import write_record
    from teacher.concept_relating import ConceptRelatingError, record_path, replaceable_relations
    from teacher.db.engine import session_scope
    from teacher.db.migrate import upgrade_to_head
    from teacher.managers import ConceptManager, ManagerError

    errors = (ConceptRelatingError, ManagerError, ValueError)
    try:
        if limit is not None and (not all_concepts or limit < 1):
            raise ValueError("--limit needs --all and a positive number")
        if yes and not (all_concepts and regenerate):
            raise ValueError("--yes is only used with --all --regenerate")
        settings = Settings.from_env()
        relater = build_concept_relater(ModelSettings.from_env(), stub=stub)
        model = relater.choose_model(model)  # an unknown model fails before anything else
        upgrade_to_head(settings)
        with session_scope(settings) as session:
            concepts = ConceptManager(session, settings)
            if all_concepts:
                chosen = concepts.list()[:limit]
                slugs = [concept.slug for concept in chosen]
                replace = len(replaceable_relations(session, [c.id for c in chosen]))
            else:
                assert slug is not None  # argparse requires the slug or --all
                slugs = [concepts.get_by_slug(slug).slug]  # NotFoundError before any check
        if not slugs:
            print("There are no concepts to relate.")
            return 0
        if all_concepts and regenerate:
            print(
                f"Regenerating the relations of {len(slugs)} concept(s) replaces {replace} "
                "unreviewed model-made relation(s); reviewed ones and those from a paper or a "
                "person are kept."
            )
            if not yes:
                return _fail("nothing was changed; pass --yes to replace them")
        relater.check_available()
    except errors as exc:
        return _fail(str(exc))
    totals = {"added": 0, "none": 0, "refused": 0, "removed": 0, "judge_calls": 0, "input": 0,
              "output": 0}
    for number, current in enumerate(slugs, start=1):
        try:
            run = relater.relate(settings, current, regenerate=regenerate, model=model)
        except errors as exc:
            if all_concepts:
                print(
                    f"Stopped at {current!r} ({number} of {len(slugs)}); the relations of the "
                    f"{number - 1} concept(s) before it are kept.",
                    file=sys.stderr,
                )
            return _fail(str(exc))
        for line in run.summary_lines(verbose):
            print(line)
        path = record_path(settings, current)
        write_record(path, run.record("cli"))
        print(f"Record: {path}")
        totals["added"] += len(run.added)
        totals["none"] += len(run.judged_none)
        totals["refused"] += len(run.refused)
        totals["removed"] += len(run.removed)
        totals["judge_calls"] += int(run.judge_called)
        totals["input"] += run.input_tokens
        totals["output"] += run.output_tokens
    if all_concepts:
        removed = f", {totals['removed']} removed" if regenerate else ""
        print(
            f"Related {len(slugs)} concept(s): {totals['added']} added, {totals['none']} judged "
            f"none, {totals['refused']} refused{removed}; {totals['judge_calls']} judge call(s); "
            f"{totals['input']} input / {totals['output']} output tokens."
        )
    return 0


def concepts_tag_blocks(
    slug: str | None,
    *,
    all_pages: bool = False,
    retag: bool = False,
    model: str | None = None,
    stub: bool = False,
    verbose: bool = False,
) -> int:
    """Tag the blocks of study page `slug`, or of every ready study page (`all_pages`; those
    tagged before only with `retag`), with the concepts they cover; print each run's summary
    and tokens. 0 on success, else 1 at the first error (earlier pages keep their tags)."""
    from teacher.block_tagging import BlockTaggingError, check_taggable, record_path
    from teacher.claude_generator import write_record
    from teacher.db.engine import session_scope
    from teacher.db.migrate import upgrade_to_head
    from teacher.labs import LAB_KIND
    from teacher.managers import ArtifactManager, ManagerError

    errors = (BlockTaggingError, ManagerError, ValueError)
    try:
        settings = Settings.from_env()
        tagger = build_block_tagger(ModelSettings.from_env(), stub=stub)
        model = tagger.choose_model(model)  # an unknown model fails before anything else
        upgrade_to_head(settings)
        with session_scope(settings) as session:
            artifacts = ArtifactManager(session, settings)
            if all_pages:
                pages = [a for a in artifacts.list() if a.kind != LAB_KIND]
                ready = [a for a in pages if a.status == "ready"]
                done = [a for a in ready if a.block_concepts_tagged_at is not None and not retag]
                slugs = [a.slug for a in ready if a not in done]
                print(
                    f"{len(slugs)} study page(s) to tag; {len(done)} tagged before skipped"
                    f"{' (pass --retag to redo them)' if done else ''}; "
                    f"{len(pages) - len(ready)} not ready (failed or generating) skipped."
                )
            else:
                assert slug is not None  # argparse requires the slug or --all
                page = artifacts.get_by_slug(slug)  # NotFoundError before any check
                check_taggable(page, retag=retag)
                slugs = [page.slug]
        if not slugs:
            print("There are no study pages to tag.")
            return 0
        tagger.check_available()
    except errors as exc:
        return _fail(str(exc))
    totals = {"tags": 0, "main": 0, "input": 0, "output": 0}
    for number, current in enumerate(slugs, start=1):
        try:
            run = tagger.tag_page(settings, current, retag=retag, model=model)
        except errors as exc:
            if all_pages:
                print(
                    f"Stopped at {current!r} ({number} of {len(slugs)}); the tags of the "
                    f"{number - 1} page(s) before it are kept. Run it again to continue.",
                    file=sys.stderr,
                )
            return _fail(str(exc))
        for line in run.summary_lines(verbose):
            print(line)
        path = record_path(settings, current)
        write_record(path, run.record("cli"))
        print(f"Record: {path}")
        counts = run.tagging.counts()
        totals["tags"] += counts["tags"]
        totals["main"] += counts["main"]
        totals["input"] += run.tagging.input_tokens
        totals["output"] += run.tagging.output_tokens
    if all_pages:
        print(
            f"Tagged {len(slugs)} page(s): {totals['tags']} tags ({totals['main']} main); "
            f"{totals['input']} input / {totals['output']} output tokens."
        )
    return 0


# -- references -----------------------------------------------------------------------------


def build_reference_extractor(models: ModelSettings, *, stub: bool = False) -> Any:
    """The reference extractor `references extract` uses (tests replace this function)."""
    from teacher.reference_extraction import ReferenceExtractor, StubReferenceExtractor

    if stub:
        return StubReferenceExtractor()
    return ReferenceExtractor(model=models.metadata)


def references_extract(
    slug: str | None, *, all_sources: bool = False, redo: bool = False, stub: bool = False
) -> int:
    """Extract the references of source `slug`, or of every source still to do
    (`all_sources`; with `redo` all of them); print each outcome. 0 on success, else 1."""
    from teacher.db.engine import session_scope
    from teacher.db.migrate import upgrade_to_head
    from teacher.db.models import utcnow
    from teacher.managers import KnowledgeSourceManager, ManagerError, SourceReferenceManager
    from teacher.managers.source_references import extraction_state
    from teacher.reference_extraction import ReferenceExtractionError, extract_references

    errors = (ReferenceExtractionError, ManagerError, ValueError)
    try:
        if redo and not all_sources:
            raise ValueError("--redo is only used with --all")
        settings = Settings.from_env()
        extractor = build_reference_extractor(ModelSettings.from_env(), stub=stub)
        upgrade_to_head(settings)
        with session_scope(settings) as session:
            sources = KnowledgeSourceManager(session, settings)
            if all_sources:
                manager = SourceReferenceManager(session, settings)
                now = utcnow()
                states = {
                    s.slug: extraction_state(manager.extraction(s), now) for s in sources.list()
                }
                wanted = {"never_run", "failed", "interrupted"} | ({"done"} if redo else set())
                slugs = [s for s, state in states.items() if state in wanted]
                skipped = len(states) - len(slugs)
                print(
                    f"{len(slugs)} source(s) to extract; {skipped} skipped (done before"
                    f"{', pass --redo to redo them' if not redo else ''}, or running)."
                )
            else:
                assert slug is not None  # argparse requires the slug or --all
                slugs = [sources.get_by_slug(slug).slug]  # NotFoundError before any check
        if not slugs:
            print("There are no sources to extract references from.")
            return 0
        extractor.check_available()
    except errors as exc:
        return _fail(str(exc))
    failed = 0
    totals = {"references": 0, "matched": 0, "input": 0, "output": 0}
    for current in slugs:
        try:
            outcome = extract_references(settings, current, extractor, trigger="cli")
        except errors as exc:
            failed += 1
            print(f"error: {current}: {exc}", file=sys.stderr)
            if not all_sources:
                return 1
            continue
        for line in outcome.summary_lines():
            print(line)
        totals["references"] += outcome.reference_count
        totals["matched"] += outcome.matched
        totals["input"] += outcome.input_tokens or 0
        totals["output"] += outcome.output_tokens or 0
    if all_sources:
        print(
            f"Extracted {len(slugs) - failed} of {len(slugs)} source(s): {totals['references']} "
            f"reference(s), {totals['matched']} in the library; {totals['input']} input / "
            f"{totals['output']} output tokens."
            + (f" {failed} failed (recorded; see above)." if failed else "")
        )
    return 1 if failed else 0


# -- labs -----------------------------------------------------------------------------------


def build_download_client() -> Any:
    """The HTTP client `labs setup` downloads with; None makes the installer create one (tests
    replace this function to return a client with a MockTransport)."""
    return None


def labs_setup() -> int:
    """Install the lab runtime and print where; then report whether Node can run labs on the
    server. 0 on success, else 1."""
    from teacher.pyodide_runtime import (
        PythonRunner,
        RuntimeInstallError,
        RuntimeUnavailableError,
        install_runtime,
    )

    try:
        settings = Settings.from_env()
        path = install_runtime(settings, client=build_download_client(), progress=print)
    except (RuntimeInstallError, ValueError, OSError) as exc:
        return _fail(str(exc))
    print(f"Runtime: {path}")
    try:
        PythonRunner(settings).check_available()
    except RuntimeUnavailableError as exc:
        print(f"Labs cannot be checked on this server yet: {exc}")
    else:
        print("Labs can run in the browser and be checked on this server (Node found).")
    return 0


# -- history --------------------------------------------------------------------------------


def history_export(*, format: str = "json", output: str | None = None) -> int:
    """Write the study history as JSON or Markdown to `output` (a new file) or stdout. 0 on
    success, else 1."""
    import json
    from pathlib import Path

    from teacher.db.engine import session_scope
    from teacher.db.migrate import upgrade_to_head
    from teacher.history_export import history_markdown
    from teacher.managers import ManagerError, StudyHistoryManager

    try:
        if format not in ("json", "markdown"):
            raise ValueError(f"unknown format {format!r}: expected json or markdown")
        target = Path(output) if output is not None else None
        if target is not None and target.exists():
            raise ValueError(f"{target} already exists; choose a new file")
        settings = Settings.from_env()
        upgrade_to_head(settings)
        with session_scope(settings) as session:
            data = StudyHistoryManager(session, settings).export()
        if format == "json":
            text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        else:
            text = history_markdown(data)
        if target is None:
            sys.stdout.write(text)
            return 0
        with target.open("x", encoding="utf-8") as fh:  # "x": never overwrite
            fh.write(text)
    except (ManagerError, ValueError, OSError) as exc:
        return _fail(str(exc))
    print(f"Wrote {len(data['sessions'])} session(s) to {target}", file=sys.stderr)
    return 0


def costs_backfill(*, dry_run: bool = False) -> int:
    """Add the past API calls to the cost ledger (`teacher.cost_backfill`) and print what was
    added. 0 on success, else 1."""
    from teacher.cost_backfill import backfill
    from teacher.db.migrate import upgrade_to_head

    try:
        settings = Settings.from_env()
        upgrade_to_head(settings)
        report = backfill(settings, dry_run=dry_run)
    except (ValueError, OSError) as exc:
        return _fail(str(exc))
    for line in report.lines():
        print(line)
    return 0


# -- discover -------------------------------------------------------------------------------


def build_source_finder(models: ModelSettings) -> Any:
    """The source finder `discover` uses (tests replace this function)."""
    from teacher.discovery import build_source_finder as build

    return build(models)


def discover(
    topic: str | None, *, concept: str | None = None, level: str, as_json: bool = False
) -> int:
    """Find candidate sources for `topic` or the concept `concept` and print them (as JSON with
    `as_json`); nothing is added. A provider that failed is listed (and repeated on stderr);
    the status is 0 as long as a result came back, else 1."""
    import json

    from teacher.costs import cost_scope
    from teacher.db.migrate import upgrade_to_head
    from teacher.discovery import (
        LEDGER_PURPOSE,
        DiscoveryError,
        DiscoveryQuery,
        concept_query,
        find_sources,
    )
    from teacher.managers import ManagerError

    try:
        settings = Settings.from_env()
        finder = build_source_finder(ModelSettings.from_env())
        upgrade_to_head(settings)
        if concept is not None:
            query = concept_query(settings, concept, level)
        else:
            query = DiscoveryQuery(topic or "", level=level)
        with cost_scope(settings, purpose=LEDGER_PURPOSE):
            result = find_sources(settings, finder, query)
    except (DiscoveryError, ManagerError, ValueError) as exc:
        return _fail(str(exc))
    for status in result.providers:
        if not status.ok:
            print(f"warning: the {status.name} provider failed: {status.error}", file=sys.stderr)
    if as_json:
        sys.stdout.write(json.dumps(result.to_dict(), ensure_ascii=False, indent=2) + "\n")
        return 0
    for line in result.summary_lines():
        print(line)
    return 0


# -- explore --------------------------------------------------------------------------------


def build_explorer(models: ModelSettings) -> Any:
    """The explorer `explore` uses (tests replace this function): the default source finder
    and the concept graph's embedder (`teacher.embeddings.OpenAIEmbedder`)."""
    from teacher.embeddings import OpenAIEmbedder
    from teacher.exploration import build_explorer as build

    return build(
        models, finder=build_source_finder(models), embedder=OpenAIEmbedder(models.embedding)
    )


def _exploration_lines(data: dict[str, Any]) -> list[str]:
    """An exploration (`teacher.exploration.view.exploration_detail`) as text."""
    lines = [
        f"Exploration {data['id']}: {data['prompt']!r} (level {data['level']}): {data['status']}."
    ]
    brief = data.get("brief")
    if brief:
        lines.append(f"Brief: {brief['summary']}")
        lines.append(f"  In scope: {'; '.join(brief['in_scope'])}")
        if brief["out_of_scope"]:
            lines.append(f"  Out of scope: {'; '.join(brief['out_of_scope'])}")
        lines.append(f"  Level: {brief['level_note']}")
    for step in data["steps"]:
        lines.append(f"Step {step['number']}: {step['title']}")
        for c in step["concepts"]:
            known = c["known"]
            if c["status"] == "known" and known:
                standing = known["standing"]["status"]
                what = f"known: {known['concept']['slug']} ({c['match']['method']}), standing {standing}"
            elif c["match"] is not None and c["match"]["concept_missing"]:
                what = "known, but its concept was deleted since"
            else:
                what = c["status"] if c["status"] != "planned" else "new"
            lines.append(f"  {c['position']:2}. {c['name']} [{what}; coverage {c['coverage']}]")
            prereqs = ", ".join(p["name"] for p in c["prerequisites"])
            if prereqs:
                lines.append(f"      builds on: {prereqs}")
            for entry in c["discovery"]["log"]:
                if "attempt" in entry:
                    found = (
                        f"failed: {entry['error']}" if entry["error"]
                        else f"{entry['candidates']} candidate(s), {entry['fitting']} fitting"
                    )
                    lines.append(f"      search {entry['attempt']} {entry['query']!r}: {found}")
                elif entry.get("note"):
                    lines.append(f"      {entry['note']}")
            for cand in c["candidates"]:
                score = f"[{cand['overall']:.1f}]" if cand["overall"] is not None else "[--]"
                fit = "; fits" if cand["fits"] else ""
                lines.append(f"      {cand['rank']}. {score} {cand['title']} <{cand['url']}>")
                lines.append(f"         {cand['verdict']}: {cand['verdict_reason']}{fit}")
                if cand["triage"]:
                    lines.append(f"         {cand['triage']['treatment']}: {cand['triage']['reason']}")
                if cand["library"]:
                    held = ", ".join(m["slug"] for m in cand["library"])
                    lines.append(f"         already in the library: {held}")
            if c["discovery"]["error"]:
                lines.append(f"      SOURCE SEARCH FAILED: {c['discovery']['error']}")
    for note in data["notes"]:
        lines.append(f"Note: {note}")
    if data["error"]:
        lines.append(f"Error: {data['error']}")
    cost = data["cost"]
    text = f"Cost: ${cost['total_usd']:.4f} in {cost['calls']} model call(s)"
    if cost["unknown_price_calls"]:
        text += f", plus calls with no known price ({', '.join(cost['unknown_price_models'])})"
    if cost["web_search_requests"]:
        text += f"; {cost['web_search_requests']} web search(es), not priced"
    lines.append(text + "; recorded in the cost ledger.")
    return lines


def explore(topic: str, *, level: str, as_json: bool = False) -> int:
    """Explore `topic` in the foreground and print the result (JSON with `as_json`)."""
    import json

    from teacher.db.engine import session_scope
    from teacher.db.migrate import upgrade_to_head
    from teacher.exploration import (
        LIMITS,
        ExplorationError,
        exploration_detail,
        run_exploration,
        start_exploration,
    )
    from teacher.managers import ExplorationManager, ManagerError

    try:
        settings = Settings.from_env()
        explorer = build_explorer(ModelSettings.from_env())
        upgrade_to_head(settings)
        exploration_id = start_exploration(settings, explorer, topic, level, owner=None)
    except (ExplorationError, ManagerError, ValueError) as exc:
        return _fail(str(exc))
    print(f"exploration {exploration_id}: started. {LIMITS['note']}", file=sys.stderr)
    run_exploration(
        settings, exploration_id, explorer,
        on_stage=lambda stage: print(f"exploration {exploration_id}: {stage}", file=sys.stderr),
    )  # fmt: skip
    with session_scope(settings) as session:
        row = ExplorationManager(session, settings, owner=None).get(exploration_id)
        data = exploration_detail(session, settings, row)
    for step in data["steps"]:
        for c in step["concepts"]:
            if c["discovery"]["error"]:
                print(
                    f"warning: the source search for {c['name']!r} failed: "
                    f"{c['discovery']['error']}",
                    file=sys.stderr,
                )
    if as_json:
        sys.stdout.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    else:
        for line in _exploration_lines(data):
            print(line)
    if data["status"] != "completed":
        return _fail(data["error"] or f"the exploration is {data['status']}")
    return 0


def build_enricher(models: ModelSettings) -> Any:
    """The enricher `explore --enrich` uses (tests replace this function): the Add page's URL
    fetcher and metadata extractor, concept extraction and the graph match's resolver, sharing
    one embedder."""
    from teacher.concept_extraction import ConceptExtractor
    from teacher.exploration import GraphMatcher
    from teacher.exploration import build_enricher as build
    from teacher.metadata import MetadataExtractor
    from teacher.url_sources import UrlFetcher

    embedder = build_embedder(models)
    return build(
        fetcher=UrlFetcher(),
        metadata_extractor=MetadataExtractor(model=models.metadata),
        concept_extractor=ConceptExtractor(model=models.concept, embedder=embedder),
        matcher=GraphMatcher(embedder=embedder, model=models.concept),
    )


def confirm(prompt: str) -> bool:
    """Ask a yes/no question on the terminal (tests replace this function); PromptError
    without a terminal to ask on."""
    if not sys.stdin.isatty():
        raise PromptError("no terminal to ask on; pass --yes to go ahead without asking")
    try:
        answer = input(prompt)
    except (EOFError, KeyboardInterrupt):
        raise PromptError("cancelled: nothing was changed") from None
    return answer.strip().lower() in ("y", "yes")


def _preview_lines(preview: dict[str, Any]) -> list[str]:
    """What an enrichment will do (`teacher.exploration.enrichment.enrichment_preview`)."""
    src, con, links = preview["sources"], preview["concepts"], preview["links"]
    lines = [
        f"Enriching exploration {preview['exploration_id']} will:",
        f"  add {src['to_add']} source(s) to the library and link {src['to_link']} it already "
        "holds, each with concept extraction only (no study page):",
    ]
    for item in src["items"]:
        if item["action"] == "link":
            held = item["held"]
            what = f"link {held['slug']} (already in the library, by {held['method']})"
        else:
            what = "add"
        extraction = "" if item["extraction"] == "run" else "; its concepts were extracted before"
        lines.append(f"    - {item['title']} <{item['url']}>: {what}{extraction}")
        if item["last_error"]:
            lines.append(f"      (the last attempt failed: {item['last_error']})")
    lines.append(
        f"  write {con['total']} concept(s) to the concept graph: {con['matched']} already "
        f"there, {con['to_resolve']} looked up again after the ingest and created if absent "
        "(unreviewed, origin model):"
    )
    for item in con["items"]:
        where = f"use {item['concept']['slug']}" if item["concept"] else "look up, create if absent"
        lines.append(f"    - {item['name']}: {where}")
    lines.append(
        f"  write {links['total']} prerequisite link(s) as builds_on: {links['to_add']} to add, "
        f"{links['present']} already present, {links['to_skip']} to skip:"
    )
    for item in links["items"]:
        reason = f" ({item['reason']})" if item["reason"] else ""
        lines.append(f"    - {item['from']} builds on {item['to']}: {item['expected']}{reason}")
    if preview["dropped"]:
        lines.append(f"  leave out the dropped concept(s): {', '.join(preview['dropped'])}")
    lines.append(f"  {preview['model_calls']}")
    return lines


def _summary_lines(enrichment: dict[str, Any]) -> list[str]:
    summary = enrichment["summary"] or {}
    lines = [f"Enrichment {enrichment['id']}: {enrichment['status']}."]
    if summary:
        src, con, rel, gro = (summary[k] for k in ("sources", "concepts", "relations", "grounding"))
        lines.append(
            f"Sources: {src['added']} added, {src['held']} already held, {src['failed']} failed."
        )
        for item in src["items"]:
            if item["outcome"] == "failed":
                lines.append(f"  FAILED {item['title']} <{item['url']}>: {item['error']}")
            else:
                lines.append(f"  {item['outcome']}: {item['title']} -> {item['slug']}")
        lines.append(
            f"Concepts: {con['matched'] + con['found']} matched ({con['found']} found again "
            f"after the ingest), {con['created']} created."
        )
        for item in con["items"]:
            lines.append(f"  {item['outcome']}: {item['name']} -> {item['slug']}")
        lines.append(
            f"Relations: {rel['added']} added, {rel['present']} already present, "
            f"{rel['skipped']} skipped."
        )
        for item in rel["items"]:
            if item["outcome"] == "skipped":
                lines.append(f"  skipped {item['from']} builds on {item['to']}: {item['reason']}")
        lines.append(f"Grounding: {gro['grounded']} grounded, {gro['ungrounded']} still ungrounded.")
        for item in gro["items"]:
            if item["grounded"]:
                lines.append(f"  grounded: {item['name']} by {', '.join(item['sources'])}")
            else:
                lines.append(
                    f"  NOT GROUNDED: {item['name']} ({item['proposed']} proposed candidate(s) "
                    "left to tick)"
                )
    if enrichment["error"]:
        lines.append(f"Error: {enrichment['error']}")
    cost = enrichment["cost"]
    lines.append(
        f"Cost: ${cost['total_usd']:.4f} in {cost['calls']} model call(s)"
        + (f", plus calls with no known price ({', '.join(cost['unknown_price_models'])})"
           if cost["unknown_price_calls"] else "")  # fmt: skip
        + "; recorded in the cost ledger."
    )
    return lines


def explore_enrich(exploration_id: int, *, yes: bool = False, as_json: bool = False) -> int:
    """Show what enriching exploration `exploration_id` will do, ask (unless `yes`), then run
    it in the foreground and print the summary (the exploration as JSON with `as_json`)."""
    import json

    from teacher.db.engine import session_scope
    from teacher.db.migrate import upgrade_to_head
    from teacher.exploration import (
        ExplorationError,
        enrichment_preview,
        exploration_detail,
        run_enrichment,
        start_enrichment,
    )
    from teacher.managers import ExplorationManager, ManagerError

    try:
        settings = Settings.from_env()
        enricher = build_enricher(ModelSettings.from_env())
        upgrade_to_head(settings)
        with session_scope(settings) as session:
            row = ExplorationManager(session, settings, owner=None).get(exploration_id)
            preview = enrichment_preview(session, settings, row)
        if not preview["can_start"]:
            raise ValueError(preview["blocked"])
        for line in _preview_lines(preview):
            print(line, file=sys.stderr if as_json else sys.stdout)
        if not yes and not confirm("Go ahead? [y/N] "):
            return _fail("nothing was changed: the enrichment was not confirmed")
        enrichment_id = start_enrichment(
            settings, enricher, exploration_id, owner=None, digest=preview["digest"]
        )
    except (ExplorationError, ManagerError, ValueError, PromptError) as exc:
        return _fail(str(exc))
    print(f"enrichment {enrichment_id} of exploration {exploration_id}: started", file=sys.stderr)
    run_enrichment(
        settings, enrichment_id, enricher,
        on_stage=lambda stage: print(f"enrichment {enrichment_id}: {stage}", file=sys.stderr),
    )  # fmt: skip
    with session_scope(settings) as session:
        row = ExplorationManager(session, settings, owner=None).get(exploration_id)
        data = exploration_detail(session, settings, row)
    enrichment = data["enrichment"]
    if as_json:
        sys.stdout.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    else:
        for line in _summary_lines(enrichment):
            print(line)
    for item in (enrichment["summary"] or {}).get("sources", {}).get("items", []):
        if item["outcome"] == "failed":
            print(f"warning: {item['title']!r} was not ingested: {item['error']}", file=sys.stderr)
    if enrichment["status"] != "completed":
        return _fail(enrichment["error"] or f"the enrichment is {enrichment['status']}")
    return 0


def _roadmap_lines(view: dict[str, Any]) -> list[str]:
    """A roadmap (`teacher.exploration.roadmap.roadmap_view`) as text."""
    x, p = view["exploration"], view["progress"]
    state = "complete" if p["complete"] else f"step {view['current_step']} of {p['steps_total']}"
    lines = [
        f"Roadmap of exploration {x['id']}: {x['prompt']!r} (level {x['level']}): {state}.",
        f"Progress: {p['concepts_done']} of {p['concepts_total']} concept(s) done, "
        f"{p['steps_done']} of {p['steps_total']} step(s) done.",
        f"Next: {view['next_action']['text']}",
    ]
    if view["next_action"]["url"]:
        lines[-1] += f" <{view['next_action']['url']}>"
    for step in view["steps"]:
        lines.append(
            f"Step {step['number']}: {step['title']} [{step['state']}; {step['done']} of "
            f"{step['total']} done]"
        )
        for c in step["concepts"]:
            mark = "x" if c["done"] else " "
            standing = c["standing"]["status"] if c["standing"] else "not in the graph"
            lines.append(f"  [{mark}] {c['name']} (standing {standing})")
            if c["done"]:
                lines.append(f"      done: {', '.join(c['done_by'])}")
            source = c["primary_source"]
            if source is not None:
                how = "chosen" if source["chosen"] else "recommended"
                lines.append(f"      source ({how}): {source['title']} [{source['slug']}]")
                others = [s["slug"] for s in c["sources"] if s["slug"] != source["slug"]]
                if others:
                    lines.append(f"      other grounding sources: {', '.join(others)}")
            if c["choice_problem"]:
                lines.append(f"      NO SOURCE: {c['choice_problem']}")
            page = c["page"]
            if page is not None:
                if page["status"] == "generating":
                    how_far = "being generated"
                elif page["complete"]:
                    how_far = "complete"
                else:
                    how_far = f"{page['gates_cleared']} of {page['gates_total']} gate(s) cleared"
                lines.append(f"      page: {page['title']} [{page['slug']}]: {how_far}")
            elif c["can_generate"]:
                lines.append("      page: none yet (generate it on the roadmap page)")
            elif c["state"] == "ungrounded":
                lines.append("      NOT GROUNDED: no library source introduces it; enrich first")
            if c["failed_page"]:
                lines.append(f"      the last generation failed: {c['failed_page']['slug']}")
            if c["shared_with"]:
                lines.append(f"      its source is also the source of: {', '.join(c['shared_with'])}")
    if view["dropped"]:
        lines.append(f"Dropped (not counted): {', '.join(d['name'] for d in view['dropped'])}")
    return lines


def explore_roadmap(exploration_id: int, *, as_json: bool = False) -> int:
    """Print the roadmap of exploration `exploration_id` (JSON with `as_json`). Read-only."""
    import json

    from teacher.db.engine import session_scope
    from teacher.db.migrate import upgrade_to_head
    from teacher.exploration import roadmap_view
    from teacher.managers import ExplorationManager, ManagerError
    from teacher.reading import DEFAULT_MAX_ATTEMPTS

    try:
        settings = Settings.from_env()
        upgrade_to_head(settings)
        with session_scope(settings) as session:
            row = ExplorationManager(session, settings, owner=None).get(exploration_id)
            view = roadmap_view(session, settings, row, max_attempts=DEFAULT_MAX_ATTEMPTS)
            session.rollback()  # read-only: nothing to keep
    except (ManagerError, ValueError) as exc:
        return _fail(str(exc))
    if as_json:
        sys.stdout.write(json.dumps(view, ensure_ascii=False, indent=2) + "\n")
    else:
        for line in _roadmap_lines(view):
            print(line)
    return 0


# -- users ----------------------------------------------------------------------------------


class PromptError(Exception):
    """The password could not be read: no terminal, cancelled, or the two entries differ."""


def prompt_password(prompt: str) -> str:
    """Read one password without echoing it (tests replace this function). Without a
    terminal to read it from (getpass would fall back to reading stdin with echo) it is a
    PromptError."""
    import getpass
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass(prompt)
        except getpass.GetPassWarning:
            raise PromptError(
                "no terminal to read the password from; run the command in an interactive "
                "terminal"
            ) from None
        except (EOFError, KeyboardInterrupt):
            raise PromptError("cancelled: no password was entered") from None


def _new_password(email: str) -> str:
    """Ask twice for a new password for `email`, hidden; it must follow the rules."""
    from teacher.passwords import check_password_rules

    first = prompt_password(f"New password for {email}: ")
    check_password_rules(first)
    if prompt_password("Repeat the password: ") != first:
        raise PromptError("the two passwords differ; nothing was changed")
    return first


def _when(value: Any) -> str:
    return value.strftime("%Y-%m-%d %H:%M UTC") if value is not None else "never"


def users_command(action: str, email: str | None = None) -> int:
    """`teacher users <action> [email]`. 0 on success, else 1."""
    from sqlalchemy.exc import SQLAlchemyError

    from teacher.db.engine import session_scope
    from teacher.db.migrate import upgrade_to_head
    from teacher.managers import ManagerError, UserManager
    from teacher.managers.users import normalise_email

    try:
        settings = Settings.from_env()
        upgrade_to_head(settings)
        if action == "list":
            with session_scope(settings) as session:
                accounts = UserManager(session, settings).list()
                if not accounts:
                    print("No accounts yet.")
                for user in accounts:
                    state = "active"
                    if user.disabled_at is not None:
                        state = f"disabled {_when(user.disabled_at)}"
                    print(
                        f"{user.email}  created {_when(user.created_at)}  "
                        f"last sign-in {_when(user.last_login_at)}  {state}"
                    )
            return 0
        if email is None:
            raise ValueError(f"users {action} needs an email")
        address = normalise_email(email)
        if action == "add":
            with session_scope(settings) as session:
                if UserManager(session, settings).find(address) is not None:
                    raise ValueError(f"an account with the email {address} already exists")
            password = _new_password(address)
            with session_scope(settings) as session:
                UserManager(session, settings).create(address, password)
            print(f"Added the account {address}.")
            return 0
        if action == "set-password":
            with session_scope(settings) as session:
                UserManager(session, settings).get_by_email(address)  # fail before asking
            password = _new_password(address)
            with session_scope(settings) as session:
                users = UserManager(session, settings)
                signed_out = users.set_password(users.get_by_email(address), password)
            print(f"Set a new password for {address}; signed out {signed_out} session(s).")
            return 0
        with session_scope(settings) as session:
            users = UserManager(session, settings)
            user = users.get_by_email(address)
            if action == "disable":
                signed_out = users.disable(user)
                print(f"Disabled {address}; signed out {signed_out} session(s).")
            elif action == "enable":
                users.enable(user)
                print(f"Enabled {address}.")
            else:
                raise ValueError(f"unknown users action {action!r}")
    except (ManagerError, ValueError, PromptError, OSError) as exc:
        return _fail(str(exc))
    except SQLAlchemyError as exc:  # the first line only: the rest can quote rows
        orig = getattr(exc, "orig", None) or exc
        return _fail((str(orig).strip().splitlines() or [type(exc).__name__])[0])
    return 0


# -- db -------------------------------------------------------------------------------------


def db_info() -> int:
    """Print the database (`describe()`), its current and head revision and the data root. 0 on
    success, else 1."""
    from sqlalchemy.exc import SQLAlchemyError

    from teacher.db.migrate import current_revision, head_revision
    from teacher.db.providers import create_db_provider

    try:
        settings = Settings.from_env()
        described = create_db_provider(settings).describe()
        current = current_revision(settings)
        head = head_revision(settings)
    except (ValueError, OSError, SQLAlchemyError) as exc:
        return _fail(str(exc))
    print(f"Database: {described}")
    print(f"Revision: {current or 'none (not migrated yet)'}")
    print(f"Head revision: {head}")
    print(f"Data root: {settings.data_root}")
    return 0


def db_copy(source: str, target: str, *, dry_run: bool = False) -> int:
    """Copy every row and file of database `source` into the empty `target`
    (`teacher.db_copy`), or with `dry_run` print what would be copied. 0 on success, else 1."""
    from sqlalchemy.exc import SQLAlchemyError

    from teacher.db_copy import DbCopyError, copy_database, plan_copy

    try:
        if source == target:
            raise DbCopyError(f"--from and --to are both {source}; copy between two databases")
        source_settings = Settings.from_env(db=source)
        target_settings = Settings.from_env(db=target)
        if dry_run:
            for line in plan_copy(source_settings, target_settings).lines():
                print(line)
            print("Dry run: nothing was changed.")
            return 0
        copy_database(source_settings, target_settings, out=print)
    except (DbCopyError, ValueError, OSError) as exc:
        return _fail(str(exc))
    except SQLAlchemyError as exc:  # the first line only: the rest can quote rows
        orig = getattr(exc, "orig", None) or exc
        return _fail((str(orig).strip().splitlines() or [type(exc).__name__])[0])
    return 0


def db_rebuild_search() -> int:
    """Rebuild and check the concept search index of the database in use. 0 on success, else
    1."""
    from sqlalchemy.exc import SQLAlchemyError

    from teacher.db.migrate import upgrade_to_head
    from teacher.db.providers import create_db_provider
    from teacher.db_copy import rebuild_search_index
    from teacher.managers import ManagerError

    try:
        settings = Settings.from_env()
        upgrade_to_head(settings)
        count = rebuild_search_index(settings)
    except (ManagerError, ValueError, RuntimeError, OSError, SQLAlchemyError) as exc:
        orig = getattr(exc, "orig", None) or exc
        return _fail((str(orig).strip().splitlines() or [type(exc).__name__])[0])
    described = create_db_provider(settings).describe()
    print(f"Rebuilt the concept search index of {described} ({count} concept(s)).")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "db" and args.action == "copy":
        if args.db is not None:
            return _fail("db copy names both databases with --from and --to; --db is not used")
        return db_copy(args.source, args.target, dry_run=args.dry_run)
    if args.db is not None:
        os.environ[ENV_DB] = args.db
    if args.command == "db" and args.action == "info":
        return db_info()
    if args.command == "db" and args.action == "rebuild-search":
        return db_rebuild_search()
    if args.command == "serve":
        try:
            serve(args.host, args.port)
        except ConfigError as exc:  # e.g. no $SUPABASE_DB_URL: say so, without a traceback
            return _fail(str(exc))
        return 0
    if args.command == "concepts":
        if args.action == "extract":
            return concepts_extract(args.slug)
        if args.action == "reindex":
            return concepts_reindex(rebuild_index=args.rebuild_index)
        if args.action == "relate":
            return concepts_relate(
                args.slug,
                all_concepts=args.all,
                limit=args.limit,
                verbose=args.verbose,
                stub=args.stub,
                regenerate=args.regenerate,
                model=args.model,
                yes=args.yes,
            )
        if args.action == "tag-blocks":
            return concepts_tag_blocks(
                args.slug,
                all_pages=args.all,
                retag=args.retag,
                model=args.model,
                stub=args.stub,
                verbose=args.verbose,
            )
    if args.command == "references" and args.action == "extract":
        return references_extract(args.slug, all_sources=args.all, redo=args.redo, stub=args.stub)
    if args.command == "labs" and args.action == "setup":
        return labs_setup()
    if args.command == "history" and args.action == "export":
        return history_export(format=args.format, output=args.output)
    if args.command == "costs" and args.action == "backfill":
        return costs_backfill(dry_run=args.dry_run)
    if args.command == "users":
        return users_command(args.action, getattr(args, "email", None))
    if args.command == "discover":
        return discover(args.topic, concept=args.concept, level=args.level, as_json=args.json)
    if args.command == "explore":
        from teacher.discovery.common import DEFAULT_LEVEL

        if args.enrich is not None:
            if args.level is not None:
                return _fail("--level is for exploring a topic, not with --enrich")
            return explore_enrich(args.enrich, yes=args.yes, as_json=args.json)
        if args.roadmap is not None:
            if args.level is not None:
                return _fail("--level is for exploring a topic, not with --roadmap")
            if args.yes:
                return _fail("--yes is only used with --enrich")
            return explore_roadmap(args.roadmap, as_json=args.json)
        if args.yes:
            return _fail("--yes is only used with --enrich")
        return explore(args.topic, level=args.level or DEFAULT_LEVEL, as_json=args.json)
    raise AssertionError(f"unhandled command {args.command!r}")  # argparse requires one


if __name__ == "__main__":
    raise SystemExit(main())
