"""The study history export as Markdown: `history_markdown(StudyHistoryManager.export())`.

The JSON export is the complete record; this is the same history for reading: a table of time
per page, the standing on each concept with the concepts of every tagged block, then every
session (oldest first) with its totals, the time spent on each page (and
each block of a study page), and its timeline: the pages opened and other actions, every answer
with its question, verdict (and the criteria a partly right answer missed), confidence and
feedback, every chat message in full, and every lab run with its output. A prediction is listed
as the guess it was, never as passed (it is not a question). Attention spans are summed per
page rather than listed. Times are UTC.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

Record = Mapping[str, Any]


def duration(seconds: float) -> str:
    """`seconds` for people: "45 s", "12 min", "1 h 5 min"."""
    seconds = max(0, round(seconds))
    if seconds < 60:
        return f"{seconds} s"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if minutes else f"{hours} h"


def _when(value: str | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    if value is None:
        return "-"
    return datetime.fromisoformat(value).strftime(fmt)


def _clock(value: str) -> str:
    return datetime.fromisoformat(value).strftime("%H:%M:%S")


def _title(ref: Record | None, gone: str = "a page that was deleted") -> str:
    return f"*{ref['title']}*" if ref else gone


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _quote(text: str, indent: str = "  ") -> list[str]:
    lines = text.splitlines() or [""]
    return [f"{indent}> {line}" if line else f"{indent}>" for line in lines]


def _fence(text: str, indent: str = "  ") -> list[str]:
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    body = [f"{indent}{line}" for line in text.splitlines()]
    return [f"{indent}{fence}", *body, f"{indent}{fence}"]


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def block_name(key: str | None, label: str | None, gone: str = "a deleted block") -> str:
    """A block for people: its key and its label (heading or opening words), `b03 “Why
    softmax?”`; the key alone when there is no label to show (a block not reached yet)."""
    if key is None:
        return gone
    return f"{key} “{label}”" if label else key


_EVENTS: dict[str, Callable[[Record], str]] = {
    "library_opened": lambda e: "Opened the library.",
    "study_page_opened": lambda e: f"Opened the study page {_title(e['artifact'])}.",
    "lab_opened": lambda e: f"Opened the lab {_title(e['lab'])}.",
    "source_opened": lambda e: f"Opened the source {_title(e['source'], 'a deleted source')}.",
    "source_file_opened": lambda e: (
        f"Opened the original file of {_title(e['source'], 'a deleted source')}."
    ),
    "graph_opened": lambda e: (
        f"Opened the concept graph at *{e['concept']['name']}*."
        if e["concept"] else "Opened the concept graph."
    ),
    "gate_cleared": lambda e: (
        f"Cleared checkpoint {block_name(e['block'], e['block_label'], 'a deleted checkpoint')} "
        f"on {_title(e['artifact'])} "
        f"({e['payload']['state']}, attempt {e['payload']['attempt']}); "
        f"{_plural(len(e['payload']['unlocked']), 'section')} unlocked."
    ),
    "glossary_opened": lambda e: f"Opened the glossary of {_title(e['artifact'])}.",
    "glossary_term_viewed": lambda e: (
        f"Looked up the glossary term *{e['payload']['term']}* on {_title(e['artifact'])}."
    ),
    "glossary_build_requested": lambda e: f"Asked for the glossary of {_title(e['artifact'])}.",
    "chat_thread_started": lambda e: (
        f"Started tutor conversation {e['payload']['thread_id']} on "
        f"{_title(e['lab'] or e['artifact'])}."
    ),
    "lab_workspace_saved": lambda e: (
        f"Saved the code of {_title(e['lab'])} ({_plural(e['payload']['files'], 'file')}, "
        f"{e['payload']['chars']} characters)."
    ),
    "lab_hint_viewed": lambda e: (
        f"Opened the hint of step {e['payload']['step'] + 1} of {_title(e['lab'])}."
    ),
    "lab_solution_viewed": lambda e: (
        f"Viewed the solution of {_title(e['lab'])}"
        f"{' for the first time' if e['payload']['first_time'] else ' again'}."
    ),
    "lab_exported": lambda e: f"Downloaded {_title(e['lab'])}.",
    "concept_inspected": lambda e: (
        f"Inspected the concept *{e['concept']['name']}*."
        if e["concept"] else "Inspected a concept that was deleted."
    ),
    "graph_searched": lambda e: (
        f"Searched the concept graph for *{e['payload']['query']}* "
        f"({_plural(e['payload']['results'], 'result')})."
    ),
    "relations_requested": lambda e: (
        f"Asked for {'regenerated ' if e['payload']['regenerate'] else ''}relations of "
        f"*{e['concept']['name'] if e['concept'] else 'a deleted concept'}*."
    ),
    "source_added": lambda e: f"Added the source {_title(e['source'], 'a deleted source')}.",
    "source_updated": lambda e: (
        f"Edited {', '.join(e['payload']['fields'])} of the source "
        f"{_title(e['source'], 'a deleted source')}."
    ),
    "source_deleted": lambda e: f"Deleted the source *{e['payload']['title']}*.",
    "study_page_requested": lambda e: f"Asked for a new study page {_title(e['artifact'])}.",
    "lab_requested": lambda e: (
        f"Asked for a new lab {_title(e['lab'])} from {_title(e['artifact'])}."
    ),
    "regeneration_requested": lambda e: (
        f"Asked to regenerate *{e['payload']['supersedes']}* as "
        f"{_title(e['lab'] or e['artifact'])}."
    ),
    "artifact_deleted": lambda e: (
        f"Deleted the {'lab' if e['payload']['kind'] == 'lab' else 'study page'} "
        f"*{e['payload']['title']}*."
    ),
}


def describe_event(event: Record) -> str:
    """One sentence for a timeline event (KeyError for a kind this module does not know)."""
    return _EVENTS[event["kind"]](event)


def _entry_lines(entry: Record) -> list[str]:
    at = f"**{_clock(entry['at'])}**"
    kind = entry["type"]
    if kind == "event":
        return [f"- {at} {describe_event(entry)}"]
    if kind in ("answer", "chat_answer") and not entry["is_question"]:
        lines = [
            f"- {at} Made a prediction at checkpoint "
            f"{block_name(entry['block'], entry['block_label'])} on {_title(entry['artifact'])} "
            "(a guess before reading on, not graded)."
            if kind == "answer"
            else f"- {at} Made a prediction in conversation {entry['thread_id']} on "
            f"{_title(entry['artifact'])} (a guess before reading on, not graded)."
        ]
        if entry["question"]:
            lines += ["  - Question:", *_quote(entry["question"], "    ")]
        return lines + _answer_lines(entry["answer"], "Guess")
    if kind in ("answer", "chat_answer"):
        where = (
            f"checkpoint {block_name(entry['block'], entry['block_label'])} "
            f"({_type_name(entry['block_type'])}) on {_title(entry['artifact'])}"
            if kind == "answer"
            else f"a tutor exercise ({_type_name(entry['block_type'])}) in conversation "
            f"{entry['thread_id']} "
            f"on {_title(entry['artifact'])}"
        )
        lines = [
            f"- {at} Answered {where}, attempt {entry['attempt']}: **{_verdict_words(entry)}**."
        ]
        if entry["question"]:
            lines += ["  - Question:", *_quote(entry["question"], "    ")]
        lines += _answer_lines(entry["answer"])
        if entry["confidence"]:
            lines.append(f"  - How sure: {CONFIDENCE_WORDS[entry['confidence']]}")
        if entry["missing"]:
            lines += ["  - Still missing:", *(f"    - {item}" for item in entry["missing"])]
        if entry["feedback"]:
            lines += ["  - Feedback:", *_quote(entry["feedback"], "    ")]
        return lines
    if kind == "chat_message":
        who = "**You**" if entry["role"] == "user" else "**Tutor**"
        label = " (check my understanding)" if entry["kind"] == "check_understanding" else ""
        lines = [
            f"- {at} {who}{label}, conversation {entry['thread_id']} on "
            f"{_title(entry['artifact'])}:"
        ]
        for ref in entry["references"]:
            lines += [
                f"  - About this passage of {ref.get('block_key')}:",
                *_quote(ref.get("text", ""), "    "),
            ]
        lines += _quote(entry["text"]) if entry["text"] else ["  > (no text)"]
        return lines
    if kind == "lab_run":
        step = f" of step {entry['step'] + 1}" if entry["step"] is not None else ""
        what = "Check" if entry["kind"] == "check" else "Run"
        verdict = "passed" if entry["passed"] else "failed"
        lines = [f"- {at} {what}{step} in {_title(entry['lab'])}: **{verdict}**."]
        if entry["error"]:
            lines += ["  - Error:", *_fence(entry["error"], "    ")]
        if entry["output"]:
            lines += ["  - Output:", *_fence(entry["output"], "    ")]
        return lines
    if kind == "span":
        return []
    raise ValueError(f"unknown timeline entry type {kind!r}")


_BLOCK_TYPES = {
    "multiple_choice": "multiple choice",
    "blurb": "written answer",
    "prediction": "prediction",
}
_VERDICT_WORDS = {"passed": "passed", "partial": "partly right", "failed": "not passed"}
# The reader's words for `teacher.grading.CONFIDENCES`, as the study page shows them.
CONFIDENCE_WORDS = {"low": "Guessing", "medium": "Fairly sure", "high": "Certain"}


def _verdict_words(entry: Record) -> str:
    """An answer's verdict for people; a row written outside the managers has no verdict, and
    then `passed` says all there is (passed or not)."""
    verdict = entry["verdict"]
    if verdict is None:
        return "passed" if entry["passed"] else "not passed"
    return _VERDICT_WORDS[verdict]


def _type_name(block_type: str) -> str:
    """A question type for people; a type without words here is shown as it is stored."""
    return _BLOCK_TYPES.get(block_type, block_type)


def _answer_lines(answer: Record, word: str = "Answer") -> list[str]:
    """What the reader answered (`StudyHistoryManager` `answer`): the chosen option, the written
    text, or a response in a shape the history does not know, raw and labelled as such."""
    form = answer["form"]
    if form == "choice":
        return [f"  - {word} (option {answer['choice'] + 1}):", *_quote(answer["text"], "    ")]
    if form == "text":
        return [f"  - {word}:", *_quote(answer["text"], "    ")]
    if form == "raw":
        return [
            f"  - {word}, as stored (not in a form this export knows: {answer['note']}):",
            *_fence(answer["text"], "    "),
        ]
    raise ValueError(f"unknown answer form {form!r}")


def _time_on_pages(timeline: Sequence[Record]) -> list[str]:
    per_page: dict[str, float] = defaultdict(float)
    per_block: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for entry in timeline:
        if entry["type"] != "span":
            continue
        if entry["surface"] == "study_page":
            name = _title(entry["artifact"])
        elif entry["surface"] == "lab":
            name = f"{_title(entry['lab'])} (lab)"
        elif entry["surface"] == "source":
            name = f"{_title(entry['source'], 'a deleted source')} (source)"
        else:
            name = "the concept graph"
        per_page[name] += entry["active_seconds"]
        if entry["block"]:
            per_block[name][block_name(entry["block"], entry["block_label"])] += entry[
                "active_seconds"
            ]
    lines = []
    for name, seconds in sorted(per_page.items(), key=lambda item: -item[1]):
        blocks = per_block.get(name)
        detail = (
            " (" + ", ".join(f"{key}: {duration(s)}" for key, s in blocks.items()) + ")"
            if blocks else ""
        )
        lines.append(f"- {name}: {duration(seconds)}{detail}")
    return lines


def _session_lines(record: Record) -> list[str]:
    start, end = record["started_at"], record["last_activity_at"]
    same_day = _when(start, "%Y-%m-%d") == _when(end, "%Y-%m-%d")
    ends = _when(end, "%H:%M") if same_day else _when(end)
    tag = " (backfilled)" if record["backfilled"] else ""
    length = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
    lines = [f"### Session {record['id']}: {_when(start)} to {ends} UTC{tag}", ""]
    totals = [
        f"{duration(length)} long",
        "time on pages not recorded" if record["backfilled"]
        else f"{duration(record['active_seconds'])} active on pages",
        f"{_plural(record['answers'], 'answer')} ({record['answers_passed']} passed)",
        f"{_plural(record['chat_answers'], 'tutor exercise answer')} "
        f"({record['chat_answers_passed']} passed)",
        f"{_plural(record['user_messages'], 'message')} to the tutor",
        f"{_plural(record['lab_runs'], 'lab run')}",
        f"{_plural(record['lab_checks'], 'lab check')} ({record['lab_checks_passed']} passed)",
    ]
    lines += [", ".join(totals) + ".", ""]
    if record["artifacts"]:
        lines += ["Pages: " + "; ".join(
            f"{_title(a)} ({'lab' if a['kind'] == 'lab' else 'study page'})"
            for a in record["artifacts"]
        ) + ".", ""]
    if record["sources"]:
        lines += ["Sources: " + "; ".join(_title(s) for s in record["sources"]) + ".", ""]
    spent = _time_on_pages(record["timeline"])
    if spent:
        lines += ["Time on pages:", "", *spent, ""]
    entries = [line for entry in record["timeline"] for line in _entry_lines(entry)]
    if entries:
        lines += ["Timeline:", "", *entries, ""]
    return lines


def _standing_lines(export: Record) -> list[str]:
    """The concept standing table (concepts with any tagged block) and each tagged page's
    blocks with their concepts."""
    t = export["standing_thresholds"]
    covered = [c for c in export["concepts"] if c["blocks"] or c["incidental_blocks"]]
    lines = ["## Concept standing", ""]
    if not covered:
        lines += [
            "No study-page block is tagged with a concept yet, so there is no standing "
            "(`teacher concepts tag-blocks` tags existing pages).",
            "",
        ]
        return lines
    lines += [
        f"From the questions tagged with each concept as their main subject: weak below "
        f"{t['weak_below']:.0%} passed at the first attempt; strong from {t['strong_from']:.0%} "
        f"with at least {t['strong_min_questions']} questions answered; developing in between; "
        "unstudied before any answer. Time is on the blocks about the concept.",
        "",
        "| Concept | Status | Answered | Passed first time | Attempts | Time | Last studied |",
        "|---|---|---|---|---|---|---|",
    ]
    for c in covered:
        rate = c["first_attempt_pass_rate"]
        lines.append(
            f"| {_cell(c['concept']['name'])} (`{c['concept']['slug']}`) | {c['status']} | "
            f"{c['questions_answered']} of {c['questions']} | {c['first_attempt_passed']}"
            f"{f' ({rate:.0%})' if rate is not None else ''} | {c['attempts']} | "
            f"{duration(c['active_seconds'])} | {_when(c['last_studied_at'])} |"
        )
    lines.append("")
    by_page: dict[str, list[Record]] = defaultdict(list)
    titles: dict[str, str] = {}
    for tag in export["block_concepts"]:
        by_page[tag["artifact"]["slug"]].append(tag)
        titles[tag["artifact"]["slug"]] = tag["artifact"]["title"]
    lines += ["### Concepts per block", ""]
    for slug, tags in by_page.items():
        lines += [f"*{titles[slug]}* (`{slug}`):", ""]
        per_block: dict[str, list[Record]] = defaultdict(list)
        types: dict[str, str] = {}
        for tag in tags:
            name = block_name(tag["block"], tag["block_label"])
            per_block[name].append(tag)
            types[name] = tag["block_type"] + (
                ", not reached yet" if tag["block_label"] is None else ""
            )
        for block, block_tags in per_block.items():
            named = ", ".join(
                f"{t['concept']['name']} ({t['role']}{', by you' if t['origin'] == 'user' else ''})"
                for t in block_tags
            )
            lines.append(f"- {block} ({types[block]}): {named}")
        lines.append("")
    return lines


def history_markdown(export: Record) -> str:
    """The export (`StudyHistoryManager.export()`) as one Markdown document."""
    sessions = export["sessions"]
    gap = export["session_idle_gap_minutes"]
    lines = [
        "# Study history",
        "",
        f"Exported {_when(export['exported_at'])} UTC. {_plural(len(sessions), 'session')}; "
        f"a session ends after {gap:g} minutes without activity. Times are UTC.",
        "",
    ]
    studied = [a for a in export["artifacts"] if a["sessions"]]
    if studied:
        lines += [
            "## Time per page",
            "",
            "| Page | Kind | Time on page | Time in its labs | Sessions | Answers | Passed "
            "| Pass rate |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for a in studied:
            rate = f"{a['pass_rate']:.0%}" if a["pass_rate"] is not None else "-"
            lines.append(
                f"| {_cell(a['artifact']['title'])} (`{a['artifact']['slug']}`) | "
                f"{a['artifact']['kind']} | "
                f"{duration(a['active_seconds'])} | {duration(a['lab_seconds'])} | "
                f"{a['sessions']} | {a['answers']} | {a['answers_passed']} | {rate} |"
            )
        lines.append("")
    lines += _standing_lines(export)
    lines += ["## Sessions", ""]
    if not sessions:
        lines += ["No study sessions yet.", ""]
    for record in sessions:
        lines += _session_lines(record)
    if export["unassigned"]:
        lines += ["## Activity without a session", ""]
        lines += [line for entry in export["unassigned"] for line in _entry_lines(entry)]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


__all__ = ["block_name", "describe_event", "duration", "history_markdown"]
