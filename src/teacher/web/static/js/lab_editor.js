// The lab's code editor: one tab per file, each file a plain <textarea> (monospace, no spell
// checking, no wrapping), no editor library and no syntax highlighting.
//
// Keys in a file: Tab indents (four spaces; across a selection, every line), Shift+Tab outdents,
// Enter keeps the line's indentation (one level deeper after a ":"), Ctrl+Enter or Cmd+Enter
// runs the program. Because Tab is taken, Escape followed by Tab leaves the editor (Escape then
// Shift+Tab goes back). Touch keyboards have no Tab key: `indent()` does the same edit for the
// page's Indent and Outdent buttons. Edits go through document.execCommand("insertText") where the browser
// supports it, so that undo keeps working; otherwise through setRangeText. The tabs follow the
// ARIA tabs pattern (arrow keys, Home and End move between them).

import { h } from "./dom.js";
import { indentEdit, lineAndColumn, newlineEdit } from "./lab_model.js";

const MIN_ROWS = 14;
const MAX_ROWS = 34;

function rowsFor(text) {
  const lines = text.split("\n").length + 1;
  return Math.max(MIN_ROWS, Math.min(MAX_ROWS, lines));
}

/** Replace value[start, end) with text as one undoable edit, then select [selStart, selEnd). */
function applyEdit(area, edit) {
  if (area.value.slice(edit.start, edit.end) !== edit.text) {
    area.focus();
    area.setSelectionRange(edit.start, edit.end);
    let done = false;
    try {
      done = document.execCommand("insertText", false, edit.text);
    } catch {
      done = false;
    }
    if (!done || area.value.slice(edit.start, edit.start + edit.text.length) !== edit.text) {
      area.setRangeText(edit.text, edit.start, edit.end, "end");
      area.dispatchEvent(new Event("input", { bubbles: true }));
    }
  }
  area.setSelectionRange(edit.selStart, edit.selEnd);
}

/**
 * Build the editor in `tablist` (the tab buttons) and `panels` (the files). `files` is the
 * API's `[{path, content}]`; `entry` is marked on its tab. `describedBy` is the id of the
 * text explaining the keys. `onChange()` after every edit, `onRun()` for Ctrl/Cmd+Enter,
 * `onCursor({path, line, column})` when the caret moves.
 */
export function createEditor({ tablist, panels, files, entry, describedBy, onChange, onRun, onCursor }) {
  let order = [];
  let areas = new Map(); // path -> textarea
  let tabs = new Map(); // path -> tab button
  let current = null;

  function select(path, { focus = false } = {}) {
    current = path;
    for (const [name, tab] of tabs) {
      const selected = name === path;
      tab.setAttribute("aria-selected", String(selected));
      tab.tabIndex = selected ? 0 : -1;
      areas.get(name).closest("[role=tabpanel]").hidden = !selected;
    }
    if (focus) tabs.get(path).focus();
    reportCursor();
  }

  function reportCursor() {
    if (!current || !onCursor) return;
    const area = areas.get(current);
    onCursor({ path: current, ...lineAndColumn(area.value, area.selectionStart) });
  }

  function onTabKey(event) {
    const index = order.indexOf(current);
    let next = null;
    if (event.key === "ArrowRight") next = order[(index + 1) % order.length];
    else if (event.key === "ArrowLeft") next = order[(index - 1 + order.length) % order.length];
    else if (event.key === "Home") next = order[0];
    else if (event.key === "End") next = order[order.length - 1];
    if (next === null) return;
    event.preventDefault();
    select(next, { focus: true });
  }

  function wire(area) {
    let escaped = false; // Escape was pressed: the next Tab leaves the editor
    area.addEventListener("keydown", (event) => {
      if (event.isComposing || event.keyCode === 229) return;
      if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
        event.preventDefault();
        escaped = false;
        onRun();
        return;
      }
      if (event.key === "Escape") {
        escaped = true;
        return;
      }
      if (event.key === "Tab") {
        if (escaped || event.ctrlKey || event.metaKey || event.altKey) {
          escaped = false;
          return; // the browser moves the focus
        }
        event.preventDefault();
        applyEdit(area, indentEdit(area.value, area.selectionStart, area.selectionEnd, { outdent: event.shiftKey }));
        return;
      }
      if (event.key === "Shift") return;
      escaped = false;
      if (event.key === "Enter" && !event.shiftKey && !event.altKey) {
        event.preventDefault();
        applyEdit(area, newlineEdit(area.value, area.selectionStart, area.selectionEnd));
      }
    });
    area.addEventListener("input", () => onChange());
    for (const type of ["keyup", "click", "select", "focus"]) area.addEventListener(type, reportCursor);
  }

  function build(list) {
    const previous = current;
    order = list.map((f) => f.path);
    areas = new Map();
    tabs = new Map();
    const tabEls = [];
    const panelEls = [];
    list.forEach((file, i) => {
      const tabId = `lab-tab-${i}`;
      const panelId = `lab-file-${i}`;
      const tab = h(
        "button",
        {
          type: "button",
          role: "tab",
          id: tabId,
          class: "lab-tab",
          "aria-controls": panelId,
          onClick: () => select(file.path),
          onKeydown: onTabKey,
        },
        file.path,
        file.path === entry ? h("span", { class: "lab-tab-entry" }, " (runs)") : null
      );
      const area = h("textarea", {
        class: "lab-code",
        "aria-label": `Code of ${file.path}`,
        "aria-describedby": describedBy,
        spellcheck: "false",
        autocapitalize: "off",
        autocomplete: "off",
        autocorrect: "off",
        wrap: "off",
        rows: String(rowsFor(file.content)),
      });
      area.value = file.content;
      wire(area);
      areas.set(file.path, area);
      tabs.set(file.path, tab);
      tabEls.push(tab);
      panelEls.push(h("div", { role: "tabpanel", id: panelId, "aria-labelledby": tabId, class: "lab-file" }, area));
    });
    tablist.replaceChildren(...tabEls);
    panels.replaceChildren(...panelEls);
    select(order.includes(previous) ? previous : order.includes(entry) ? entry : order[0]);
  }

  build(files);

  return {
    /** The files as typed, `[{path, content}]` in tab order. */
    files: () => order.map((path) => ({ path, content: areas.get(path).value })),
    /** Replace every file (e.g. with the starter code); the current tab is kept if it still exists. */
    setFiles: (list) => build(list),
    focus: () => areas.get(current).focus(),
    /** Indent (or, with `outdent`, outdent) the current file's line or selected lines, as Tab
     * and Shift+Tab do; the file keeps the focus. */
    indent({ outdent = false } = {}) {
      const area = areas.get(current);
      applyEdit(area, indentEdit(area.value, area.selectionStart, area.selectionEnd, { outdent }));
      area.focus();
      reportCursor();
    },
    currentPath: () => current,
  };
}
