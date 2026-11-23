// Run by tests/web/test_pages.py (`node graph_search_test.mjs`): the pure logic behind the graph
// page's search results: how each result was found (the lexical match and/or "similar meaning"),
// and the notice shown with every search saying whether meaning was searched, and if not, why.

import assert from "node:assert/strict";

const STATIC = new URL("../../../src/teacher/web/static/", import.meta.url);
const m = await import(new URL("js/graph_model.js", STATIC));

const result = (fields) => ({
  slug: "x",
  match_kind: null,
  match_field: null,
  matched: null,
  similarity: null,
  ...fields,
});

// -- how a result was found ------------------------------------------------------------------
{
  // The concept's own name matching exactly needs no note.
  assert.deepEqual(m.searchNotes(result({ match_kind: "exact", match_field: "name", matched: "Attention" })), []);
  assert.deepEqual(m.searchNotes(result({ match_kind: "exact", match_field: "alias", matched: "PG" })), [
    { kind: "exact", text: "exact match: alias “PG”" },
  ]);
  assert.deepEqual(m.searchNotes(result({ match_kind: "word", match_field: "summary" })), [
    { kind: "word", text: "matches: in the summary" },
  ]);
  assert.deepEqual(m.searchNotes(result({ match_kind: "prefix", match_field: "name", matched: "Gradient" })), [
    { kind: "prefix", text: "starts with your search: in the name" },
  ]);
  assert.deepEqual(m.searchNotes(result({ match_kind: "fuzzy", match_field: "name", matched: "Entropy" })), [
    { kind: "fuzzy", text: "close match: in the name" },
  ]);
  // Found only by meaning.
  assert.deepEqual(m.searchNotes(result({ similarity: 0.6234 })), [{ kind: "semantic", text: "similar meaning 0.62" }]);
  // Found both ways: both pieces of evidence, the lexical one first.
  assert.deepEqual(m.searchNotes(result({ match_kind: "substring", match_field: "alias", matched: "k-NN", similarity: 0.405 })), [
    { kind: "substring", text: "contains your search: alias “k-NN”" },
    { kind: "semantic", text: "similar meaning 0.41" },
  ]);
  assert.deepEqual(m.searchNotes(result({ match_kind: "exact", match_field: "name", similarity: 0.9 })), [
    { kind: "semantic", text: "similar meaning 0.90" },
  ]);
  // Never shown half-understood.
  assert.throws(() => m.searchNotes(result({})), /neither how it matched nor its similarity/);
  assert.throws(() => m.searchNotes(result({ match_kind: "telepathy", match_field: "name" })), /Unknown match kind/);
  assert.throws(() => m.searchNotes(result({ match_kind: "word", match_field: "body" })), /Unknown match field/);
  assert.throws(() => m.searchNotes(result({ similarity: "high" })), /has similarity high/);
  assert.throws(() => m.searchNotes(result({ similarity: NaN })), /has similarity NaN/);
}

// -- the semantic notice ---------------------------------------------------------------------
{
  const base = { model: "text-embedding-3-small", floor: 0.25, stale_count: null, concept_count: null, fix: null };
  assert.deepEqual(m.semanticNotice({ ...base, status: "ok", reason: null }), {
    degraded: false,
    text: "Searched names and meaning.",
  });
  assert.deepEqual(
    m.semanticNotice({ ...base, status: "unavailable", reason: "Embedding is unavailable: no OpenAI API key was found. Set it, then try again." }),
    {
      degraded: true,
      text:
        "Only names, aliases and summaries were searched, not meaning: Embedding is unavailable: no OpenAI " +
        "API key was found. Set it, then try again.",
    }
  );
  assert.deepEqual(
    m.semanticNotice({
      ...base,
      status: "stale",
      reason: "3 of 10 concepts have no up-to-date embedding for 'text-embedding-3-small', so a search by meaning would miss them",
      stale_count: 3,
      concept_count: 10,
      fix: "teacher concepts reindex",
    }),
    {
      degraded: true,
      text:
        "Only names, aliases and summaries were searched, not meaning: 3 of 10 concepts have no up-to-date " +
        "embedding for 'text-embedding-3-small', so a search by meaning would miss them. To fix it, run " +
        "“teacher concepts reindex”.",
    }
  );
  assert.deepEqual(m.semanticNotice({ ...base, status: "failed", reason: "Embedding failed: the OpenAI API had a server error" }), {
    degraded: true,
    text:
      "Searching by meaning failed, so only names, aliases and summaries were searched: Embedding failed: " +
      "the OpenAI API had a server error.",
  });
  assert.equal(m.semanticNotice({ ...base, status: "skipped", reason: "the search is 600 characters long" }).degraded, true);
  assert.throws(() => m.semanticNotice({ ...base, status: "maybe", reason: "x" }), /Unknown semantic search status/);
  assert.throws(() => m.semanticNotice(undefined), /Unknown semantic search status/);
  assert.throws(() => m.semanticNotice({ ...base, status: "failed", reason: null }), /has no reason/);
  assert.throws(() => m.semanticNotice({ ...base, status: "stale", reason: "3 stale" }), /names no fix/);
  assert.deepEqual(m.SEMANTIC_STATES, ["ok", "unavailable", "stale", "failed", "skipped"]);
}

console.log("graph search ok");
