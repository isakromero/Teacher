"""Pure helpers for concept search: query parsing, FTS5 expressions, folding and scoring.

`ConceptManager.search_hits` runs up to five kinds of match, best first (see `KINDS`); this
module holds everything about them that needs no database, so it can be tested on its own.

Folding. `fold(text)` is `normalize_name` (NFKC, whitespace collapsed, casefold) with
combining marks removed (NFKD, drop category Mn, NFC): "Café" and "CAFE" both fold to "cafe",
"Straße" to "strasse". The trigram index stores folded text and the substring and fuzzy tiers
fold the query the same way, so case and diacritic handling is decided here, in Python, and not
by the tokenizer (whose own folding differs, e.g. it keeps "ß"). `compact(text)` keeps only the
word characters (letters, digits, marks): "k-NN" and "kNN" both compact to "knn".

Query text never reaches FTS5 unquoted: every term or trigram becomes one double-quoted string
(inner quotes doubled), so quotes, `*`, `-`, `^`, `:`, parentheses, `AND`/`OR`/`NOT`/`NEAR` are
matched as text, never parsed as operators. Only this module adds operators (`*`, `OR`, `AND`,
parentheses) around those strings. The expressions are built from `word_phrases`,
`substring_clauses` and `trigrams`, which the Postgres backend (`teacher.db.search.postgres`)
turns into SQL instead, with every string a bound parameter, so both databases search for
exactly the same thing.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache

from teacher.concepts import normalize_name

# The kinds of match, best first. A concept found by several is reported once, at the best.
EXACT, WORD, PREFIX, SUBSTRING, FUZZY = "exact", "word", "prefix", "substring", "fuzzy"
KINDS: tuple[str, ...] = (EXACT, WORD, PREFIX, SUBSTRING, FUZZY)

# Where a hit matched: the concept's name, one of its aliases, or (word/prefix only) its summary.
FIELD_NAME, FIELD_ALIAS, FIELD_SUMMARY = "name", "alias", "summary"

MIN_PREFIX = 2  # a term's last word must be this long to be matched as a prefix
TRIGRAM = 3  # the trigram tokenizer cannot match anything shorter than this
FUZZY_MIN_WORD = 4  # query words shorter than this must match a word exactly in the fuzzy tier
FUZZY_TWO_EDITS = 9  # query words at least this long may be 2 edits away (shorter ones: 1)
# Scores are rounded, so equal matches reached by different arithmetic tie (and the tie-break
# decides) instead of differing in the last bit.
SCORE_DIGITS = 6


def _searchable(ch: str) -> bool:
    """Letters, digits and symbols (a query of only punctuation has nothing to search for)."""
    return unicodedata.category(ch)[0] in "LNS"


def _word_char(ch: str) -> bool:
    return unicodedata.category(ch)[0] in "LNM"


def fold(text: str) -> str:
    """`normalize_name(text)` without combining marks: the form the trigram index stores."""
    decomposed = unicodedata.normalize("NFKD", normalize_name(text))
    stripped = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    return unicodedata.normalize("NFC", stripped)


def compact(text: str) -> str:
    """Only the word characters of `text` (separators and punctuation dropped)."""
    return "".join(ch for ch in text if _word_char(ch))


def words(text: str) -> list[str]:
    """The runs of word characters in `text`, in order."""
    out: list[str] = []
    current: list[str] = []
    for ch in text:
        if _word_char(ch):
            current.append(ch)
        elif current:
            out.append("".join(current))
            current = []
    if current:
        out.append("".join(current))
    return out


def quote(text: str) -> str:
    """`text` as one FTS5 string (matched as text whatever it contains)."""
    return '"' + text.replace('"', '""') + '"'


def _without_controls(text: str) -> str:
    """`text` with control characters (NUL, ESC, ...) as spaces: they cannot be searched for,
    and a NUL would end an FTS5 string early (a syntax error)."""
    return "".join(" " if unicodedata.category(ch) == "Cc" else ch for ch in text)


def _strip_punctuation(term: str) -> str:
    start, end = 0, len(term)
    while start < end and unicodedata.category(term[start])[0] == "P":
        start += 1
    while end > start and unicodedata.category(term[end - 1])[0] == "P":
        end -= 1
    return term[start:end]


@dataclass(frozen=True, slots=True)
class ParsedQuery:
    """A search query split up for each kind of match.

    - `norm`: `normalize_name(query)`, compared with names and aliases by the exact tier.
    - `word_terms`: the NFKC whitespace-separated terms that contain a word character, for the
      word index (its tokenizer splits them into words; terms of only symbols are dropped).
    - `terms`: the folded terms with outer punctuation stripped ("(softmax" -> "softmax",
      "k-NN" stays "k-nn"), for the substring tier.
    - `words`: the folded words of the query, for the fuzzy tier.
    """

    norm: str
    word_terms: tuple[str, ...]
    terms: tuple[str, ...]
    words: tuple[str, ...]


def parse_query(query: object) -> ParsedQuery:
    """Split `query` for every tier. TypeError for a non-string; ValueError for an empty query
    or one of only punctuation (a letter, digit or symbol such as "ε", "∇" or "β" is enough)."""
    if not isinstance(query, str):
        raise TypeError(f"search query must be a string, got {query.__class__.__name__}")
    if not query.strip():
        raise ValueError("search query must not be empty")
    if not any(_searchable(ch) for ch in query):
        raise ValueError(f"search query {query!r} has no letters or digits to search for")
    cleaned = _without_controls(query)
    nfkc = unicodedata.normalize("NFKC", cleaned)
    folded = fold(cleaned)
    terms = tuple(t for t in (_strip_punctuation(t) for t in folded.split(" ")) if t)
    return ParsedQuery(
        norm=normalize_name(cleaned),
        word_terms=tuple(t for t in nfkc.split() if any(_word_char(ch) for ch in t)),
        terms=terms,
        words=tuple(words(folded)),
    )


# -- the word index ------------------------------------------------------------------------


def word_phrases(terms: Sequence[str], *, prefix: bool = False) -> list[tuple[str, bool]]:
    """The phrases of the word index's query: every term, and whether its last token may match
    as a prefix (with `prefix`, when the term's last word has at least MIN_PREFIX characters).
    ValueError if `terms` is empty."""
    if not terms:
        raise ValueError("no terms to search the word index for")
    phrases = []
    for term in terms:
        last = words(term)
        phrases.append((term, prefix and bool(last) and len(last[-1]) >= MIN_PREFIX))
    return phrases


def word_expression(terms: Sequence[str], *, prefix: bool = False) -> str:
    """The FTS5 expression for the word index: every term one quoted phrase, ANDed. With
    `prefix`, a term whose last word has at least MIN_PREFIX characters also matches words that
    word only starts with (`"term"*`; see `word_phrases`). ValueError if `terms` is empty."""
    return " ".join(
        quote(term) + ("*" if star else "") for term, star in word_phrases(terms, prefix=prefix)
    )


# -- the trigram index ---------------------------------------------------------------------


def substring_clauses(terms: Sequence[str]) -> list[list[str]]:
    """What the substring tier looks up in the trigram index: one clause per term of at least
    TRIGRAM characters (the term, and its compact form if that differs and is long enough too),
    every clause required, any string of a clause enough; a string matches a row whose text or
    compact text contains it. Empty if no term is long enough. Shorter terms cannot be looked
    up in a trigram index; `substring_matches` checks them on the rows found."""
    clauses = []
    for term in terms:
        if len(term) < TRIGRAM:
            continue
        variants = [term]
        squeezed = compact(term)
        if squeezed != term and len(squeezed) >= TRIGRAM:
            variants.append(squeezed)
        clauses.append(variants)
    return clauses


def substring_expression(terms: Sequence[str]) -> str | None:
    """The FTS5 expression for the substring tier (`substring_clauses`), or None if no term is
    long enough."""
    parts = []
    for variants in substring_clauses(terms):
        quoted = [quote(v) for v in variants]
        parts.append(quoted[0] if len(quoted) == 1 else f"({' OR '.join(quoted)})")
    return " AND ".join(parts) if parts else None


def substring_matches(terms: Sequence[str], text: str, compact_text: str) -> bool:
    """Whether every term occurs in `text`, or its compact form in `compact_text`."""
    for term in terms:
        squeezed = compact(term)
        if term not in text and not (squeezed and squeezed in compact_text):
            return False
    return True


def substring_score(terms: Sequence[str], compact_text: str) -> float:
    """How much of the text the terms cover (0..1]: the closer the text is to the query, the
    better ("dient" scores higher for "Gradient" than for "Policy gradient theorem")."""
    covered = sum(len(compact(term) or term) for term in terms)
    return round(min(1.0, covered / max(1, len(compact_text))), SCORE_DIGITS)


def trigrams(query_words: Sequence[str]) -> list[str]:
    """The distinct trigrams of the query words, sorted: a row containing any of them is a
    fuzzy candidate. Empty when no word has TRIGRAM characters."""
    return sorted({w[i : i + TRIGRAM] for w in query_words for i in range(len(w) - TRIGRAM + 1)})


def trigram_expression(query_words: Sequence[str]) -> str | None:
    """The FTS5 expression fetching fuzzy candidates: any trigram of any query word (OR), or
    None when no word has TRIGRAM characters."""
    grams = trigrams(query_words)
    return " OR ".join(quote(g) for g in grams) if grams else None


# -- fuzzy scoring -------------------------------------------------------------------------


def edit_budget(word: str) -> int:
    """How many edits a query word may be from a word of a name or alias: none for words
    shorter than FUZZY_MIN_WORD or containing a digit ("gpt4" must not find "GPT-3", "rnn" not
    "CNN"), 1 up to FUZZY_TWO_EDITS - 1 characters, else 2."""
    if len(word) < FUZZY_MIN_WORD or any(ch.isdigit() for ch in word):
        return 0
    return 1 if len(word) < FUZZY_TWO_EDITS else 2


def osa_distance(a: str, b: str, cutoff: int) -> int:
    """Optimal-string-alignment edit distance (insert, delete, substitute, swap two adjacent
    characters), or `cutoff + 1` as soon as it is known to exceed `cutoff`."""
    if abs(len(a) - len(b)) > cutoff:
        return cutoff + 1
    previous2: list[int] = []
    previous = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        current = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            value = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                value = min(value, previous2[j - 2] + 1)
            current[j] = value
        if min(current) > cutoff:
            return cutoff + 1
        previous2, previous = previous, current
    return previous[-1]


@lru_cache(maxsize=1 << 16)  # pure, and names share few distinct words: most calls are repeats
def _similarity(query_word: str, word: str) -> float | None:
    """1 - distance / longer length, if `word` is within the query word's edit budget and
    starts with the same character (typos in the first letter are not corrected); else None."""
    if query_word == word:
        return 1.0
    budget = edit_budget(query_word)
    if budget == 0 or query_word[0] != word[0]:
        return None
    distance = osa_distance(query_word, word, budget)
    if distance > budget:
        return None
    return 1.0 - distance / max(len(query_word), len(word))


@dataclass(frozen=True, slots=True)
class FuzzyScore:
    """`score` (0..1, higher is closer) and `extra` (words of the text no query word matched,
    the tie-break: fewer is better)."""

    score: float
    extra: int


def fuzzy_score(query_words: Sequence[str], text: str) -> FuzzyScore | None:
    """How close a folded name or alias is to the query, or None if it is not close enough.

    Two ways, the better one counts:
    - word by word: every query word is within its edit budget of some word of the text; the
      score is the similarity of each to its best word, weighted by query-word length;
    - compact: the query without separators against the text without separators, within the
      budget of the compact query ("pretrainig" finds "Pre-training").
    """
    if not query_words:
        return None
    text_words = words(text)
    results: list[FuzzyScore] = []
    best: list[tuple[float, int]] = []
    for query_word in query_words:
        candidates = [
            (s, i)
            for i, w in enumerate(text_words)
            if (s := _similarity(query_word, w)) is not None
        ]
        if not candidates:
            break
        best.append(max(candidates, key=lambda c: (c[0], -c[1])))
    else:
        total = sum(len(w) for w in query_words)
        score = sum(len(w) * s for w, (s, _) in zip(query_words, best, strict=True)) / total
        unmatched = len(text_words) - len({i for _, i in best})
        results.append(FuzzyScore(round(score, SCORE_DIGITS), unmatched))
    squeezed_query, squeezed_text = "".join(query_words), compact(text)
    if squeezed_query and squeezed_text:
        similarity = _similarity(squeezed_query, squeezed_text)
        if similarity is not None:
            results.append(FuzzyScore(round(similarity, SCORE_DIGITS), 0))
    if not results:
        return None
    return max(results, key=lambda r: (r.score, -r.extra))


__all__ = [
    "EXACT",
    "FIELD_ALIAS",
    "FIELD_NAME",
    "FIELD_SUMMARY",
    "FUZZY",
    "FUZZY_MIN_WORD",
    "FUZZY_TWO_EDITS",
    "FuzzyScore",
    "KINDS",
    "MIN_PREFIX",
    "PREFIX",
    "ParsedQuery",
    "SCORE_DIGITS",
    "SUBSTRING",
    "TRIGRAM",
    "WORD",
    "compact",
    "edit_budget",
    "fold",
    "fuzzy_score",
    "osa_distance",
    "parse_query",
    "quote",
    "substring_clauses",
    "substring_expression",
    "substring_matches",
    "substring_score",
    "trigram_expression",
    "trigrams",
    "word_expression",
    "word_phrases",
    "words",
]
