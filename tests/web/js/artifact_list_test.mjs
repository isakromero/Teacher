// Run by tests/web/test_artifact_list_js.py (`node artifact_list_test.mjs`): which artifacts of
// a list are historical (artifact_list.js: failed generations and superseded versions).

import assert from "node:assert/strict";

const STATIC = new URL("../../../src/teacher/web/static/", import.meta.url);
const m = await import(new URL("js/artifact_list.js", STATIC));

const page = (slug, extra = {}) => ({ slug, kind: "study_page", status: "ready", ...extra });
const lab = (slug, studyPage, extra = {}) => ({
  slug,
  kind: "lab",
  status: "ready",
  params: { study_page: studyPage },
  ...extra,
});
const slugs = (list) => list.map((a) => a.slug);

// Failed and superseded are historical; ready, generating and queued are current.
{
  const list = [
    page("new"),
    page("old", { superseded_by: "new" }),
    page("broken", { status: "failed" }),
    page("running", { status: "generating" }),
  ];
  const { current, historical } = m.splitHistorical(list);
  assert.deepEqual(slugs(current), ["new", "running"]);
  assert.deepEqual(slugs(historical), ["old", "broken"]);
}

// A lab goes with its study page; a failed lab of a current page is historical on its own.
{
  const list = [
    page("new"),
    lab("lab-new", "new"),
    lab("lab-failed", "new", { status: "failed" }),
    page("old", { superseded_by: "new" }),
    lab("lab-old", "old"),
    lab("lab-orphan", "deleted-page"),
  ];
  const { current, historical } = m.splitHistorical(list);
  assert.deepEqual(slugs(current), ["new", "lab-new", "lab-orphan"]);
  assert.deepEqual(slugs(historical), ["lab-failed", "old", "lab-old"]);
}

// Nothing historical, and nothing at all.
assert.deepEqual(m.splitHistorical([page("a")]), { current: [page("a")], historical: [] });
assert.deepEqual(m.splitHistorical([]), { current: [], historical: [] });

console.log("artifact_list_test: ok");
