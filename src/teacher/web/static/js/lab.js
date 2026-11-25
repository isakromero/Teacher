// Lab page: one page, three states driven by GET /api/artifacts/{slug}, as on the study page
// (generation_view.js draws the shared `generating` and `failed` states). Once the lab is
// `ready`, GET /api/labs/{slug} gives the brief, the steps (with the reader's standing on each)
// and the reader's files, and the page offers:
//
// - the brief and the steps (instructions, a hint behind a disclosure, and for a step with a
//   check a Check button whose result is shown under the step);
// - on a narrow screen (where app.css shows the "Instructions" / "Code" switch), one of the two
//   halves at a time, each keeping its scroll position; the choice is remembered per lab;
// - the code editor (lab_editor.js), with Indent and Outdent buttons for touch keyboards, autosaved with PUT /api/labs/{slug}/workspace a second
//   after the last edit, before every run, check and tutor message, before the download, and
//   when the page is hidden or closed (a keepalive request, for small workspaces); the save
//   state is shown next to the editor and a failed save is shown with its reason and a retry;
// - Run (also Ctrl/Cmd+Enter in the editor) and Check (under each step with a check, and next to
//   Run for the step chosen in "Working on"), which run the code in the browser
//   (lab_sandbox.js: Pyodide in a sandboxed iframe and a worker, one run at a time) and report
//   every run and check to POST /api/labs/{slug}/runs, whose answer updates the steps; a
//   check's verdict and report are shown under its step and in the Output panel, which a narrow
//   screen scrolls into view when a run starts from the Code half;
// - Reset to starter code and Show solution (each confirmed in the page first; the solution is
//   shown read-only under the editor and never replaces the reader's files), and Download as a
//   folder (GET /api/labs/{slug}/export);
// - the tutor (chat.js), without "Ask about this" (a lab's tutor takes no references).
//
// If the Python runtime is not installed, or cannot be loaded, the reason is shown at the top
// and Run and Check are turned off; reading, editing, saving and the download still work.

import { logEvent } from "./activity.js";
import { ApiError, api, describeError, seg } from "./api.js";
import { byId, h, plural, prefersReducedMotion, setChildren, setError, showPageError } from "./dom.js";
import { mountChat } from "./chat.js";
import { pollGeneration, renderMeta, renderNotices, showFailure } from "./generation_view.js";
import { createEditor } from "./lab_editor.js";
import {
  STEP_KIND_LABELS,
  STEP_STATE_LABELS,
  createSaver,
  filesToObject,
  runReport,
  stepProgress,
  stepState,
} from "./lab_model.js";
import { DEFAULT_TIMEOUT_MS, createSandbox } from "./lab_sandbox.js";
import { renderMarkdown } from "./markdown.js";

const SAVE_DELAY_MS = 1000;
const KEEPALIVE_MAX_CHARS = 20000; // keepalive bodies are limited to about 64 KB
const RUNTIME_URL = "/api/labs/runtime";
const NOUN = "lab";

const root = byId("lab-page");
const slug = root.dataset.slug;
const artifactBase = `/api/artifacts/${seg(slug)}`;
const labBase = `/api/labs/${seg(slug)}`;
const pageUrl = (target) => `/labs/${seg(target)}`;
const stepKey = `teacher.lab.step.${slug}`;
const viewKey = `teacher.lab.view.${slug}`;
const panels = {
  generating: byId("state-generating"),
  failed: byId("state-failed"),
  ready: byId("state-ready"),
};

// localStorage can be missing or throw (private windows, blocked site data): never rely on it.
const storage = {
  get(key) {
    try {
      return window.localStorage.getItem(key);
    } catch {
      return null;
    }
  },
  set(key, value) {
    try {
      window.localStorage.setItem(key, value);
    } catch {
      /* the choice just is not remembered */
    }
  },
};

function showPanel(status) {
  for (const [name, el] of Object.entries(panels)) el.hidden = name !== status;
  root.dataset.status = status;
}

// -- header ----------------------------------------------------------------------------------

/** The breadcrumb to the study page: `page` is `{slug, title}`; null hides it. */
function paintCrumb(page) {
  const crumb = byId("lab-crumb-page");
  crumb.hidden = !page;
  if (!page) return;
  const link = byId("lab-crumb-link");
  link.href = `/artifacts/${seg(page.slug)}`;
  link.textContent = page.title || "Study page";
}

/** Before the lab is ready only the study page's slug is known: look up its title. */
async function crumbFromParams(artifact) {
  const page = artifact.params ? artifact.params.study_page : null;
  if (!page) {
    paintCrumb(null);
    return;
  }
  try {
    const found = await api.get(`/api/artifacts/${seg(page)}`);
    paintCrumb({ slug: found.slug, title: found.title });
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) paintCrumb(null); // the study page was deleted
    else paintCrumb({ slug: page, title: null });
  }
}

function renderHeader(artifact) {
  byId("lab-title").textContent = artifact.title;
  document.title = `${artifact.title} - Teacher`;
  renderMeta(byId("artifact-meta"), artifact);
  renderNotices(byId("artifact-notices"), artifact, { noun: NOUN, pageUrl });
}

// -- the narrow layout: one half at a time ---------------------------------------------------

/** The "Instructions" / "Code" switch. app.css shows it (and hides the other half) only in a
 * narrow container; there each half keeps its own scroll position. Returns `isNarrow()`. */
function mountViews() {
  const ready = panels.ready;
  const bar = byId("lab-views");
  const buttons = [...bar.querySelectorAll("[data-view]")];
  const names = buttons.map((b) => b.dataset.view);
  const scrolls = {}; // view -> window.scrollY when it was left
  const isNarrow = () => getComputedStyle(bar).display !== "none";

  function show(view, { restore = false } = {}) {
    if (!names.includes(view)) throw new Error(`Unknown lab view ${JSON.stringify(view)}.`);
    const previous = ready.dataset.view;
    if (previous && previous !== view && isNarrow()) scrolls[previous] = window.scrollY;
    ready.dataset.view = view;
    for (const b of buttons) b.setAttribute("aria-pressed", String(b.dataset.view === view));
    storage.set(viewKey, view);
    if (!restore || !isNarrow()) return;
    // Back where this half was left; the first time, its top (just under the switch).
    const top = bar.parentElement.querySelector(".lab-grid").getBoundingClientRect().top + window.scrollY - bar.offsetHeight;
    const target = view in scrolls ? scrolls[view] : Math.min(window.scrollY, top);
    window.scrollTo({ top: target, behavior: "auto" });
  }

  for (const b of buttons) b.addEventListener("click", () => show(b.dataset.view, { restore: true }));
  const stored = storage.get(viewKey);
  show(names.includes(stored) ? stored : names[0]);
  return { isNarrow };
}

// -- the ready lab ---------------------------------------------------------------------------

function mountLab(artifact, lab) {
  let steps = lab.steps;
  let stepEls = [];
  let busy = false; // a run or check is in progress
  let unavailable = null; // why Python cannot run here, or null
  let pythonReady = false;
  let sandbox = null;
  let editor = null;

  const runButton = byId("lab-run");
  const checkButton = byId("lab-check"); // checks the step chosen in "Working on"
  const runStatus = byId("lab-run-status");
  const output = byId("lab-output");
  const stepSelect = byId("lab-current-step");

  // -- saving ------------------------------------------------------------------------------

  const saveStatus = byId("lab-save-status");
  const saveError = byId("lab-save-error");
  const SAVE_LABELS = { saved: "All changes saved", dirty: "Unsaved changes", saving: "Saving...", failed: "Not saved" };

  function paintSave(state, error) {
    saveStatus.textContent = SAVE_LABELS[state];
    saveStatus.dataset.state = state;
    if (state === "failed") {
      setError(
        saveError,
        h(
          "span",
          {},
          h("strong", {}, "Your code could not be saved."),
          " ",
          describeError(error),
          h("br"),
          "It is still in the editor. Saving is tried again after your next edit, or ",
          h("button", { type: "button", class: "button compact", onClick: () => saver.flush().catch(() => {}) }, "Try again now"),
          "."
        )
      );
    } else if (state === "saved") {
      setError(saveError, null);
    }
  }

  function putWorkspace() {
    const files = editor.files();
    const size = files.reduce((n, f) => n + f.content.length, 0);
    return api.put(`${labBase}/workspace`, { files }, { keepalive: size <= KEEPALIVE_MAX_CHARS });
  }

  const saver = createSaver({ save: putWorkspace, delay: SAVE_DELAY_MS, onState: paintSave });

  /** Save now; resolves true when saved, false (with the failure shown) when not. */
  async function saveNow() {
    try {
      await saver.flush();
      return true;
    } catch {
      return false;
    }
  }

  // -- steps -------------------------------------------------------------------------------

  function renderSteps() {
    stepEls = steps.map((step) => {
      const number = step.index + 1;
      const badge = h("span", { class: "lab-step-state" });
      const result = h("div", { class: "lab-check-result", "aria-live": "polite" });
      const checkButton = step.check
        ? h(
            "button",
            { type: "button", class: "button", onClick: () => execute("check", step.index) },
            `Check step ${number}`
          )
        : null;
      const li = h(
        "li",
        { class: "lab-step", id: `lab-step-${step.index}` },
        h(
          "div",
          { class: "lab-step-header" },
          h("h3", { class: "lab-step-title" }, `Step ${number}: `, step.title),
          h("span", { class: "tag lab-step-kind" }, STEP_KIND_LABELS[step.kind] || step.kind),
          badge
        ),
        renderMarkdown(step.instructions),
        step.hint
          ? h(
              "details",
              {
                class: "lab-hint",
                // the study history: the reader opened this step's hint
                onToggle: (event) => {
                  if (event.target.open) logEvent("lab_hint_viewed", { lab: slug, payload: { step: step.index } });
                },
              },
              h("summary", {}, "Hint"),
              renderMarkdown(step.hint)
            )
          : null,
        step.check
          ? h(
              "div",
              { class: "lab-step-actions" },
              checkButton,
              h("span", { class: "muted small" }, "Runs your program, then this step's check.")
            )
          : h(
              "p",
              { class: "muted small lab-step-note" },
              "This step has no automatic check: run the code and judge the result yourself, or ask the tutor."
            ),
        step.check
          ? h(
              "details",
              { class: "lab-check-code" },
              h("summary", {}, "What the check runs"),
              h("pre", {}, h("code", {}, step.check))
            )
          : null,
        result
      );
      return { li, badge, result, checkButton };
    });
    setChildren(byId("lab-steps"), stepEls.map((e) => e.li));
    paintSteps();
  }

  function paintSteps() {
    steps.forEach((step, i) => {
      const state = stepState(step);
      const el = stepEls[i];
      el.li.dataset.state = state;
      el.badge.hidden = state === "unchecked";
      el.badge.className = `lab-step-state is-${state}`;
      el.badge.textContent =
        state === "attempted" ? `${STEP_STATE_LABELS[state]}: ${plural(step.attempts, "check")}, not passed yet` : STEP_STATE_LABELS[state];
    });
    const { checked, passed } = stepProgress(steps);
    byId("lab-progress").textContent = checked
      ? `${passed} of ${plural(checked, "checked step")} passed.`
      : "No step has an automatic check.";
  }

  function mountStepChoice() {
    setChildren(stepSelect, steps.map((s) => h("option", { value: String(s.index) }, `Step ${s.index + 1}: ${s.title}`)));
    const stored = Number(storage.get(stepKey));
    stepSelect.value = Number.isInteger(stored) && stored >= 0 && stored < steps.length ? String(stored) : "0";
    stepSelect.addEventListener("change", () => {
      storage.set(stepKey, stepSelect.value);
      paintCheckButton();
    });
    paintCheckButton();
  }

  /** The Check button next to Run checks the chosen step; it is hidden for a step without a check. */
  function paintCheckButton() {
    const step = steps[Number(stepSelect.value)];
    checkButton.hidden = !step.check;
    // (app.css drops "step" from the visible label on the narrowest screens)
    // (one span: .button is a flex box, which would space out loose text nodes)
    setChildren(checkButton, h("span", {}, "Check ", h("span", { class: "lab-check-word" }, "step "), String(step.index + 1)));
    checkButton.setAttribute("aria-label", `Check step ${step.index + 1}`);
  }

  function setCurrentStep(index) {
    stepSelect.value = String(index);
    storage.set(stepKey, stepSelect.value);
    paintCheckButton();
  }

  // -- running -----------------------------------------------------------------------------

  function paintButtons() {
    const off = busy || Boolean(unavailable);
    runButton.disabled = off;
    checkButton.disabled = off;
    for (const el of stepEls) if (el.checkButton) el.checkButton.disabled = off;
  }

  function setRunStatus(text, { working = false } = {}) {
    runStatus.textContent = text;
    runStatus.classList.toggle("is-busy", working);
  }

  function runtimeUnavailable(reason) {
    unavailable = reason;
    if (sandbox) sandbox.dispose();
    sandbox = null;
    const notice = byId("lab-runtime");
    notice.className = "notice notice-error lab-runtime";
    notice.setAttribute("role", "alert");
    setChildren(
      notice,
      h("p", {}, h("strong", {}, "Python cannot run on this page, so Run and Check are turned off.")),
      h("p", { class: "lab-runtime-detail" }, reason),
      h("p", {}, "You can still read the lab, edit and save your code, and download it as a folder.")
    );
    notice.hidden = false;
    setRunStatus("Running is turned off: see the message at the top of the lab.");
    paintButtons();
  }

  async function startRuntime() {
    let info;
    try {
      info = await api.get(RUNTIME_URL);
    } catch (err) {
      runtimeUnavailable(`Could not find out whether the Python runtime is installed: ${describeError(err)}`);
      return;
    }
    if (!info || typeof info.installed !== "boolean") {
      runtimeUnavailable("Unexpected response from the server about the Python runtime.");
      return;
    }
    if (!info.installed) {
      runtimeUnavailable(info.detail || "The Python runtime is not installed.");
      return;
    }
    const missing = lab.packages.filter((p) => !(info.packages || []).includes(p));
    if (missing.length) {
      runtimeUnavailable(`This lab needs ${missing.join(", ")}, which the installed runtime does not offer.`);
      return;
    }
    try {
      sandbox = createSandbox({ runtimeUrl: info.url, packages: lab.packages, container: byId("lab-sandbox") });
    } catch (err) {
      runtimeUnavailable(`The Python sandbox could not be created: ${err.message}`);
      return;
    }
    if (!busy) setRunStatus("Starting Python... The first start takes about ten seconds.", { working: true });
    sandbox.ready.then(
      () => {
        pythonReady = true;
        if (!busy && !unavailable) setRunStatus("Python is ready.");
      },
      (err) => runtimeUnavailable(err.message)
    );
  }

  /** A status line that counts the seconds while a run waits and runs. */
  function ticker(what) {
    const started = Date.now();
    const paint = () => {
      const seconds = Math.floor((Date.now() - started) / 1000);
      const prefix = pythonReady ? "" : "Starting Python, then ";
      const note = seconds >= 3
        ? ` Every run starts in a fresh Python, which can take a few seconds to load; a program is stopped after ${DEFAULT_TIMEOUT_MS / 1000} s.`
        : "";
      setRunStatus(`${prefix}${pythonReady ? what : what.toLowerCase()}... ${seconds} s.${note}`, { working: true });
    };
    paint();
    const timer = setInterval(paint, 1000);
    return {
      stop() {
        clearInterval(timer);
        return (Date.now() - started) / 1000;
      },
    };
  }

  function stream(title, text, cls) {
    return h("div", { class: `lab-stream ${cls}` }, h("h4", {}, title), h("pre", { tabindex: "0" }, text));
  }

  function renderOutput(heading, result, seconds) {
    const run = result.run;
    let outcome = run.timedOut ? "stopped at the time limit" : run.ok ? "finished" : "failed with an error";
    const check = result.checks.length ? result.checks[0] : null;
    if (check && run.ok) outcome = check.passed ? "the program ran and the check passed" : "the program ran, the check did not pass";
    const ok = run.ok && (!check || check.passed);
    const figures = run.figures || [];
    setChildren(
      output,
      h(
        "p",
        { class: `lab-output-summary ${ok ? "is-ok" : "is-failed"}` },
        `${heading}: ${outcome} (${seconds.toFixed(1)} s).`
      ),
      run.stdout ? stream("Printed output", run.stdout, "lab-stdout") : h("p", { class: "muted" }, "The program printed nothing."),
      run.stderr ? stream("Error output (stderr)", run.stderr, "lab-stderr") : null,
      run.error ? stream(run.timedOut ? "Stopped" : "Error", run.error, "lab-error-text") : null,
      // (also shown under the step; repeated here for the narrow layout, where the steps are hidden)
      check && run.ok && !check.passed && check.error ? stream("What the check reported", check.error, "lab-error-text") : null,
      figures.length
        ? h(
            "div",
            { class: "lab-figures" },
            figures.map((src, i) =>
              h(
                "figure",
                { class: "lab-figure" },
                h("img", { src, alt: `Figure ${i + 1} of ${figures.length} drawn by ${lab.entry}` }),
                h("figcaption", { class: "muted small" }, `Figure ${i + 1}`)
              )
            )
          )
        : null
    );
  }

  function renderCheckResult(index, result) {
    const check = result.checks[0];
    const el = stepEls[index].result;
    if (check.passed) {
      setChildren(
        el,
        h("p", { class: "lab-check-verdict is-passed" }, "Passed: the check ran without an error."),
        check.output ? stream("Printed by the check", check.output, "lab-stdout") : null
      );
      return;
    }
    setChildren(
      el,
      h("p", { class: "lab-check-verdict is-failed" }, "Not passed yet."),
      result.run.ok
        ? null
        : h("p", { class: "small" }, "Your program failed before the check could run; its error is in the Output panel."),
      check.error ? stream("What the check reported", check.error, "lab-error-text") : null,
      check.output ? stream("Printed by the check", check.output, "lab-stdout") : null
    );
  }

  function recordFailed(err, index) {
    const message = `This ${index === null ? "run" : "check"} was not recorded: ${describeError(err)}. ` +
      "The tutor does not see it and the steps were not updated.";
    output.append(h("p", { class: "error", role: "alert" }, message));
    if (index !== null) stepEls[index].result.append(h("p", { class: "error", role: "alert" }, message));
  }

  /** Run the entry file (`kind` "run") or the check of step `index` (`kind` "check"). */
  async function execute(kind, index = null, { fromWorkspace = false } = {}) {
    if (busy || unavailable || !sandbox) return;
    busy = true;
    paintButtons();
    if (fromWorkspace) revealOutput();
    if (kind === "check") setCurrentStep(index);
    const what = kind === "check" ? `Checking step ${index + 1}` : `Running ${lab.entry}`;
    const heading = kind === "check" ? `Check of step ${index + 1}` : `Run of ${lab.entry}`;
    if (kind === "check") setChildren(stepEls[index].result, h("p", { class: "muted small" }, `${what}...`));
    const tick = ticker(what);
    try {
      await saveNow(); // a failure is shown; the record below saves the files too
      const files = editor.files();
      let result;
      try {
        result = await sandbox.run({
          files: filesToObject(files),
          entry: lab.entry,
          checks: kind === "check" ? [steps[index].check] : [],
          timeoutMs: DEFAULT_TIMEOUT_MS,
        });
      } catch (err) {
        const seconds = tick.stop();
        const message = `The ${kind === "check" ? "check" : "program"} could not be run (after ${seconds.toFixed(1)} s): ${err.message}. Nothing was recorded.`;
        setChildren(output, h("p", { class: "error", role: "alert" }, message));
        if (kind === "check") setChildren(stepEls[index].result, h("p", { class: "error", role: "alert" }, message));
        setRunStatus(unavailable ? "Running is turned off: see the message at the top of the lab." : "The run failed.");
        return;
      }
      const seconds = tick.stop();
      renderOutput(heading, result, seconds);
      if (kind === "check") renderCheckResult(index, result);
      setRunStatus(`${heading}: done in ${seconds.toFixed(1)} s.`);
      try {
        const body = runReport({ kind, step: kind === "check" ? index : Number(stepSelect.value), files, result });
        const data = await api.post(`${labBase}/runs`, body);
        if (!data || !Array.isArray(data.steps) || data.steps.length !== steps.length) {
          throw new ApiError("Unexpected response from the server: the steps are missing.");
        }
        steps = data.steps;
        paintSteps();
      } catch (err) {
        recordFailed(err, kind === "check" ? index : null);
      }
    } finally {
      tick.stop();
      busy = false;
      paintButtons();
    }
  }

  /** In the one-column layout the Output panel is below the editor and the sticky Run bar:
   * bring its top into view, so that the run's progress and result are seen. */
  function revealOutput() {
    if (!views.isNarrow()) return;
    const panel = byId("lab-output-heading").parentElement;
    const top = panel.getBoundingClientRect().top;
    if (top >= 0 && top < window.innerHeight * 0.5) return;
    panel.scrollIntoView({ behavior: prefersReducedMotion() ? "auto" : "smooth", block: "start" });
  }

  // -- tools: reset, solution, download ----------------------------------------------------

  const toolsError = byId("lab-tools-error");

  /** An in-page confirmation (no browser dialog): `open` toggles `panel`, whose `yes` runs
   * `onYes`; while `skip()` is true, `open` runs `onYes` without asking. */
  function confirmation(open, panel, yes, cancel, onYes, skip = () => false) {
    const show = (visible) => {
      panel.hidden = !visible;
      open.setAttribute("aria-expanded", String(visible));
    };
    open.addEventListener("click", () => {
      setError(toolsError, null);
      if (skip()) {
        onYes();
        return;
      }
      show(panel.hidden);
      if (!panel.hidden) cancel.focus();
    });
    cancel.addEventListener("click", () => {
      show(false);
      open.focus();
    });
    panel.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        show(false);
        open.focus();
      }
    });
    yes.addEventListener("click", async () => {
      show(false);
      await onYes();
    });
    return show;
  }

  confirmation(byId("lab-reset"), byId("lab-reset-confirm"), byId("lab-reset-yes"), byId("lab-reset-cancel"), async () => {
    editor.setFiles(lab.starter_files);
    saver.changed();
    setRunStatus("Your code was reset to the starter code.");
    editor.focus();
    await saveNow();
  });

  const solutionButton = byId("lab-solution-button");
  const solutionSection = byId("lab-solution");
  let revealed = lab.solution_revealed; // seen before: no need to ask again
  async function showSolution() {
    setError(toolsError, null);
    solutionButton.disabled = true;
    let data;
    try {
      data = await api.get(`${labBase}/solution`);
      if (!data || !Array.isArray(data.files)) throw new ApiError("Unexpected response from the server: no solution files.");
    } catch (err) {
      setError(toolsError, `Could not load the solution: ${describeError(err)}`);
      solutionButton.disabled = false;
      return;
    }
    setChildren(
      byId("lab-solution-files"),
      data.files.map((f) =>
        h(
          "figure",
          { class: "lab-solution-file" },
          h("figcaption", {}, f.path),
          h("pre", { tabindex: "0", "aria-label": `Reference solution: ${f.path}` }, h("code", {}, f.content))
        )
      )
    );
    revealed = true;
    solutionSection.hidden = false;
    solutionButton.disabled = false;
    solutionButton.hidden = true;
    byId("lab-solution-heading").tabIndex = -1;
    byId("lab-solution-heading").focus();
  }
  byId("lab-solution-hide").addEventListener("click", () => {
    solutionSection.hidden = true;
    solutionButton.hidden = false;
    solutionButton.textContent = "Show solution again";
    solutionButton.focus();
  });
  if (revealed) solutionButton.textContent = "Show solution again";
  confirmation(
    solutionButton,
    byId("lab-solution-confirm"),
    byId("lab-solution-yes"),
    byId("lab-solution-cancel"),
    showSolution,
    () => revealed
  );

  const exportLink = byId("lab-export");
  exportLink.addEventListener("click", async (event) => {
    setError(toolsError, null);
    if (!saver.isDirty()) return; // the saved files are what is downloaded
    event.preventDefault();
    if (await saveNow()) {
      window.location.assign(exportLink.href);
    } else {
      setError(toolsError, "Your latest changes could not be saved (see above), so the download would not include them. Nothing was downloaded.");
    }
  });

  // -- mount -------------------------------------------------------------------------------

  const views = mountViews();
  setChildren(byId("lab-brief"), renderMarkdown(lab.brief));
  renderSteps();
  mountStepChoice();
  editor = createEditor({
    tablist: byId("lab-tabs"),
    panels: byId("lab-editors"),
    files: lab.files,
    entry: lab.entry,
    describedBy: "lab-editor-keys",
    onChange: () => saver.changed(),
    onRun: () => execute("run", null, { fromWorkspace: true }),
    onCursor: ({ path, line, column }) => {
      byId("lab-cursor").textContent = `${path}, line ${line}, column ${column}.`;
    },
  });
  paintSave("saved", null);
  runButton.addEventListener("click", () => execute("run", null, { fromWorkspace: true }));
  checkButton.addEventListener("click", () => execute("check", Number(stepSelect.value), { fromWorkspace: true }));
  // Touch keyboards have no Tab key. Pressing a button would take the focus (and the on-screen
  // keyboard) away from the file, so the press does not move the focus.
  for (const [id, outdent] of [["lab-indent", false], ["lab-outdent", true]]) {
    const button = byId(id);
    button.addEventListener("pointerdown", (event) => event.preventDefault());
    button.addEventListener("click", () => editor.indent({ outdent }));
  }
  paintButtons();

  // Save when the page is hidden or closed (a keepalive request survives the page).
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden" && saver.isDirty()) saveNow();
  });
  window.addEventListener("pagehide", () => {
    if (saver.isDirty()) saveNow();
  });

  mountChat(artifact, {
    layout: byId("lab-layout"),
    blocksRoot: null,
    async beforeSend() {
      try {
        await saver.flush();
      } catch (err) {
        throw new ApiError(
          `Your code could not be saved first, and the tutor reads the saved code (${err.message})`,
          { status: err.status || 0 }
        );
      }
    },
  });
  startRuntime();
}

// -- states ----------------------------------------------------------------------------------

async function showReady(artifact) {
  const lab = await api.get(labBase);
  if (!lab || !Array.isArray(lab.steps) || !Array.isArray(lab.files)) {
    throw new ApiError("Unexpected response from the server: the lab has no steps or files.");
  }
  paintCrumb(lab.study_page);
  showPanel("ready");
  mountLab(artifact, lab);
}

async function enter(artifact) {
  renderHeader(artifact);
  switch (artifact.status) {
    case "generating":
      crumbFromParams(artifact);
      showPanel("generating");
      return pollGeneration(artifactBase, enterSafely);
    case "failed":
      crumbFromParams(artifact);
      showPanel("failed");
      return showFailure(artifactBase, artifact, { noun: NOUN, pageUrl });
    case "ready":
      return showReady(artifact);
    default:
      showPanel("none");
      setError(byId("artifact-error"), `This page cannot show a lab with status “${artifact.status}”.`);
  }
}

async function enterSafely(artifact) {
  try {
    await enter(artifact);
  } catch (err) {
    showPageError(`Could not show this lab: ${describeError(err)}`);
  }
}

async function main() {
  let artifact;
  try {
    artifact = await api.get(artifactBase);
  } catch (err) {
    showPageError(`Could not load this lab: ${describeError(err)}`);
    return;
  }
  await enterSafely(artifact);
}

main();
