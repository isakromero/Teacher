"""Regenerate the relations of every concept that has none.

Run from the project root (the keys come from `.env`):

    uv run --env-file .env python scripts/relate_unrelated_concepts.py --dry-run
    uv run --env-file .env python scripts/relate_unrelated_concepts.py [--model ID] [--limit N] [-v]

A concept "has none" when no relation of any type or origin touches it. Each such concept gets
one regenerate run (`ConceptRelater.relate(..., regenerate=True)`), so pairs a previous run
judged "none" are judged again rather than skipped. A concept that gains a relation from an
earlier run in the same pass is skipped. The data is the one in `$TEACHER_HOME`, or the current
directory.

The script stops at the first error with status 1; the relations written before it are kept,
and running it again continues with the concepts that still have none. `--dry-run` lists the
concepts and calls nothing.
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import select

from teacher.claude_generator import write_record
from teacher.cli import build_concept_relater
from teacher.concept_relating import ConceptRelatingError, record_path
from teacher.config import ModelSettings, Settings
from teacher.db.engine import session_scope
from teacher.db.migrate import upgrade_to_head
from teacher.db.models import ConceptRelation
from teacher.managers import ConceptManager, ManagerError

ERRORS = (ConceptRelatingError, ManagerError, ValueError)


def unrelated_slugs(settings: Settings) -> list[str]:
    """Slugs of the concepts no relation touches, in the concept list's order."""
    with session_scope(settings) as session:
        related: set[int] = set()
        for from_id, to_id in session.execute(
            select(ConceptRelation.from_id, ConceptRelation.to_id)
        ):
            related.update((from_id, to_id))
        return [c.slug for c in ConceptManager(session, settings).list() if c.id not in related]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", help="Claude model for the runs (default: the concept model)")
    parser.add_argument("--limit", type=int, help="run at most this many concepts")
    parser.add_argument("--dry-run", action="store_true", help="list the concepts; call nothing")
    parser.add_argument("-v", "--verbose", action="store_true", help="print each run in detail")
    args = parser.parse_args(argv)

    try:
        if args.limit is not None and args.limit < 1:
            raise ValueError("--limit must be a positive number")
        settings = Settings.from_env()
        relater = build_concept_relater(ModelSettings.from_env())
        model = relater.choose_model(args.model)  # an unknown model fails before anything else
        upgrade_to_head(settings)
        slugs = unrelated_slugs(settings)[: args.limit]
        if not slugs:
            print("Every concept already has at least one relation.")
            return 0
        print(f"{len(slugs)} concept(s) with no relations (model {model}):")
        for slug in slugs:
            print(f"  {slug}")
        if args.dry_run:
            return 0
        relater.check_available()
    except ERRORS as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    ran = skipped = added = 0
    still_none: list[str] = []
    for number, slug in enumerate(slugs, start=1):
        try:
            if slug not in unrelated_slugs(settings):
                print(f"[{number}/{len(slugs)}] {slug}: skipped, an earlier run related it")
                skipped += 1
                continue
            print(f"[{number}/{len(slugs)}] {slug}")
            run = relater.relate(settings, slug, regenerate=True, model=model)
        except ERRORS as exc:
            print(
                f"Stopped at {slug!r} ({number} of {len(slugs)}); relations written so far are "
                "kept.",
                file=sys.stderr,
            )
            print(f"error: {exc}", file=sys.stderr)
            return 1
        for line in run.summary_lines(args.verbose):
            print(line)
        write_record(record_path(settings, slug), run.record("script"))
        ran += 1
        added += len(run.added)
        if not run.added:
            still_none.append(slug)

    print(f"Ran {ran} concept(s), skipped {skipped}: {added} relation(s) added.")
    if still_none:
        print(f"{len(still_none)} still have no relations: {', '.join(still_none)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
