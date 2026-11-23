// Small DOM helpers shared by the page modules.

/**
 * Create an element. `props`: `class`, `dataset` (object), `on<Event>` (listener), any other
 * key becomes an attribute (`true` -> empty attribute, null/false -> omitted). Children may be
 * nodes, strings, numbers or (nested) arrays; null/false children are skipped.
 */
export function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "dataset") Object.assign(el.dataset, value);
    else if (key.startsWith("on") && typeof value === "function") {
      el.addEventListener(key.slice(2).toLowerCase(), value);
    } else el.setAttribute(key, value === true ? "" : String(value));
  }
  append(el, children);
  return el;
}

const SVG_NS = "http://www.w3.org/2000/svg";

/** Create an SVG element: every prop is an attribute (`class` included; null/false omitted),
 * children as in `h`. No event props: SVG drawings here take their input on a parent. */
export function svgEl(tag, props = {}, ...children) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    el.setAttribute(key, value === true ? "" : String(value));
  }
  append(el, children);
  return el;
}

export function append(el, children) {
  for (const child of [children].flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : String(child));
  }
  return el;
}

/** Replace an element's children, with the same child handling as `h` (the native
 * `replaceChildren` would render null as "null" and stringify arrays). */
export function setChildren(el, ...children) {
  el.replaceChildren();
  return append(el, children);
}

export function byId(id) {
  const el = document.getElementById(id);
  if (!el) throw new Error(`page element #${id} is missing`);
  return el;
}

/** Show `message` in an error element (or hide it when message is empty). */
export function setError(el, message) {
  el.replaceChildren();
  if (message) {
    el.append(message instanceof Node ? message : String(message));
    el.hidden = false;
  } else {
    el.hidden = true;
  }
}

export function showPageError(message) {
  if (window.teacherShowError) window.teacherShowError(message);
  else console.error(message);
}

export function formatDate(iso) {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

const STATUS_LABELS = {
  generating: "Generating",
  ready: "Ready",
  failed: "Failed",
  running: "Running",
  succeeded: "Done",
};

export function statusBadge(status) {
  return h("span", { class: `badge badge-${status}` }, STATUS_LABELS[status] || status);
}

export function plural(n, one, many = `${one}s`) {
  return `${n} ${n === 1 ? one : many}`;
}

export function prefersReducedMotion() {
  return window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}
