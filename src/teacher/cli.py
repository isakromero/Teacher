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

`serve` runs the web app with uvicorn on `--host` and `--port`. With $TEACHER_PASSWORD set,
every page and API route needs signing in with that password (`teacher.web.auth`); set but blank
is an error. Without it the app is open to anyone who can reach it, so a `--host` that is not a
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
    """Build the app from `Settings.from_env()`, behind the password gate when
    $TEACHER_PASSWORD is set, and run it with uvicorn (blocks). No password and a host that is
    not loopback is a ConfigError, raised before anything starts."""
    password = teacher_password_from_env()
    if password is None and not is_loopback(host):
        raise ConfigError(
            f"refusing to serve on {host!r} without a password: anyone who can reach it could "
            f"use the app and spend on its API keys. Set ${ENV_PASSWORD} to the password for "
            "signing in, or serve on 127.0.0.1."
        )
    import uvicorn

    from teacher.web.app import create_app

    uvicorn.run(create_app(password=password), host=host, port=port)


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
    raise AssertionError(f"unhandled command {args.command!r}")  # argparse requires one


if __name__ == "__main__":
    raise SystemExit(main())
