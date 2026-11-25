// The concept graph page's inspector panel: what is selected (a concept or a relation), or the
// overview (counts, every concept A to Z, usage hints, legend) when nothing is; plus the roadmap section while the
// roadmap highlight is on. Every function returns DOM built with dom.js (no HTML strings); the
// only HTML is a concept summary, rendered by markdown.js, which sanitises it.

import { formatDate, h, plural, svgEl as s } from "./dom.js";
import {
  ORIGINS,
  RELATION_LABELS,
  RELATION_TYPES,
  countBy,
  regenerateScope,
  relateSummary,
  relationGroups,
} from "./graph_model.js";
import { renderMarkdown } from "./markdown.js";

const ORIGIN_LABELS = { paper: "Paper", model: "Model", user: "User" };

export function originChip(origin) {
  if (!ORIGIN_LABELS[origin]) throw new Error(`unknown concept origin ${origin}`);
  return h("span", { class: `origin-chip origin-${origin}` }, ORIGIN_LABELS[origin]);
}

function reviewBadge(reviewedAt, reviewed = Boolean(reviewedAt)) {
  return reviewed
    ? h("span", { class: "badge badge-reviewed", title: reviewedAt ? `Reviewed ${formatDate(reviewedAt)}` : null }, "Reviewed")
    : h("span", { class: "badge badge-unreviewed" }, "Unreviewed");
}

function typeChip(type) {
  return h("span", { class: `type-chip t-${type}` }, RELATION_LABELS[type]);
}

/** A button that selects concept `node` ({id, slug, name, ...}). */
function conceptButton(node, onSelectConcept) {
  return h(
    "button",
    { type: "button", class: "concept-link", onClick: () => onSelectConcept(node) },
    node.name
  );
}

function actionButton(
  label,
  onClick,
  { pressed = null, busy = false, disabled = busy, expanded = null, controls = null, focusKey = null } = {}
) {
  return h(
    "button",
    {
      type: "button",
      class: "button compact",
      "aria-pressed": pressed === null ? null : String(pressed),
      "aria-busy": busy ? "true" : null,
      "aria-expanded": expanded === null ? null : String(expanded),
      "aria-controls": controls,
      "data-focus": focusKey,
      disabled,
      onClick,
    },
    label
  );
}

const CONFIRM_ID = "regenerate-relations-confirm";

/** The inline confirmation before "Regenerate relations": what will be re-decided and kept. */
function regenerateConfirm(c, ctx) {
  const scope = regenerateScope(c.relations);
  const keptText = scope.kept
    ? `Its ${plural(scope.kept, "other relation")} (reviewed, or from a paper or a person) ` +
      `${scope.kept === 1 ? "is" : "are"} kept as ${scope.kept === 1 ? "it is" : "they are"}.`
    : "Reviewed relations and those from a paper or a person are always kept; it has none.";
  const what = scope.replaceable
    ? `This re-decides the ${plural(scope.replaceable, "unreviewed relation")} of ${c.name} made by ` +
      "a model: each is judged again, from scratch, together with any new candidates, and is " +
      "replaced by the new verdict or removed."
    : `${c.name} has no unreviewed relation made by a model, so nothing is replaced: its candidates ` +
      "are judged again from scratch (earlier verdicts are ignored) and what Claude accepts is added.";
  return h(
    "div",
    {
      id: CONFIRM_ID,
      class: "notice regenerate-confirm",
      role: "group",
      "aria-label": "Confirm regenerating relations",
      onKeydown: (event) => {
        if (event.key === "Escape") {
          event.preventDefault();
          ctx.onCancelRegenerate();
        }
      },
    },
    h("p", { class: "small" }, what, " ", keptText, " This makes one or two Claude calls with the model chosen above."),
    h(
      "div",
      { class: "actions" },
      h(
        "button",
        { type: "button", class: "button primary compact", "data-focus": "regenerate-confirm", onClick: ctx.onConfirmRegenerate },
        "Regenerate"
      ),
      h("button", { type: "button", class: "button compact", onClick: ctx.onCancelRegenerate }, "Cancel")
    )
  );
}

function props(rows) {
  return h(
    "dl",
    { class: "props" },
    rows.filter(Boolean).map(([term, value]) => h("div", {}, h("dt", {}, term), h("dd", {}, value)))
  );
}

// -- a concept -------------------------------------------------------------------------------

/**
 * The inspector for a concept (API concept details). `ctx`: {pinned, roadmapOn, relating (null,
 * or "find" / "regenerate" while that run is in progress), relateResult (the last run's
 * result, or null), confirmingRegenerate (the inline confirmation is open), modelControl (the
 * model select, or a note why there is none), onExpand, onHide, onUnpin, onRoadmap, onRelate,
 * onRegenerate, onConfirmRegenerate, onCancelRegenerate, onSelectConcept, onSelectRelation,
 * standingSection (the reader's standing and the questions that test it, standing.js; or a
 * note why there is none)}.
 */
export function conceptPanel(c, ctx) {
  const groups = relationGroups(c.relations);
  return h(
    "div",
    { class: "inspector-section" },
    h("div", { class: "inspector-head" }, originChip(c.origin), reviewBadge(c.reviewed_at)),
    h("h2", { class: "inspector-title", tabindex: "-1" }, c.name),
    h(
      "div",
      { class: "inspector-actions" },
      actionButton("Expand", ctx.onExpand),
      actionButton("Hide", ctx.onHide),
      ctx.pinned ? actionButton("Unpin", ctx.onUnpin) : null,
      actionButton(ctx.roadmapOn ? "Hide roadmap" : "Show roadmap", ctx.onRoadmap, {
        pressed: ctx.roadmapOn,
      }),
      actionButton(ctx.relating === "find" ? "Finding relations..." : "Find relations", ctx.onRelate, {
        busy: ctx.relating === "find",
        disabled: Boolean(ctx.relating),
        focusKey: "relate",
      }),
      actionButton(ctx.relating === "regenerate" ? "Regenerating..." : "Regenerate relations", ctx.onRegenerate, {
        busy: ctx.relating === "regenerate",
        disabled: Boolean(ctx.relating),
        expanded: Boolean(ctx.confirmingRegenerate),
        controls: CONFIRM_ID,
        focusKey: "regenerate",
      })
    ),
    ctx.modelControl,
    ctx.confirmingRegenerate && !ctx.relating ? regenerateConfirm(c, ctx) : null,
    ctx.relating
      ? h(
          "p",
          { class: "status-text is-busy small", role: "status" },
          ctx.relating === "regenerate"
            ? "Asking Claude to re-decide this concept's relations; this takes a little while."
            : "Asking Claude for related concepts and judging them; this takes a little while."
        )
      : null,
    ctx.relateResult ? relatePanel(ctx.relateResult, ctx) : null,
    renderMarkdown(c.summary),
    props([
      c.aliases.length ? ["Also called", c.aliases.join(" · ")] : null,
      [
        "Prerequisites",
        c.prerequisite_count
          ? `${plural(c.prerequisite_count, "concept")} to know first (directly or not)`
          : "None: it builds on nothing",
      ],
      [
        "Unlocks",
        c.unlocks_count ? `${plural(c.unlocks_count, "concept")} build directly on it` : "Nothing builds on it yet",
      ],
      ["Reviewed", c.reviewed_at ? formatDate(c.reviewed_at) : "Not yet"],
      ["Added", formatDate(c.created_at)],
      c.updated_at && c.updated_at !== c.created_at ? ["Changed", formatDate(c.updated_at)] : null,
      ["Slug", h("code", {}, c.slug)],
    ]),
    appearsIn(c.sources),
    ctx.standingSection ?? null,
    groups.length
      ? groups.map((group) =>
          h(
            "section",
            { class: "relation-group", "aria-label": group.label },
            h("h3", {}, `${group.label} (${group.items.length})`),
            h(
              "ul",
              { class: "relation-list" },
              group.items.map((r) =>
                h(
                  "li",
                  {},
                  h(
                    "div",
                    { class: "relation-head" },
                    conceptButton(r.other, ctx.onSelectConcept),
                    r.reviewed_at ? null : h("span", { class: "badge badge-unreviewed" }, "Unreviewed"),
                    r.redundant ? h("span", { class: "badge badge-redundant" }, "Redundant") : null
                  ),
                  h("p", { class: "rationale" }, r.rationale)
                )
              )
            )
          )
        )
      : h("p", { class: "muted small" }, "No relations to other concepts yet.")
  );
}

const ROLE_LABELS = { introduces: "introduces it", uses: "uses it", assumes: "assumes it" };

/** "Appears in": the sources linked to the concept (API `sources`: {slug, title, role}). */
function appearsIn(sources) {
  if (!Array.isArray(sources)) throw new Error("concept details have no sources list");
  return h(
    "section",
    { class: "relation-group", "aria-label": "Appears in" },
    h("h3", {}, `Appears in (${sources.length})`),
    sources.length
      ? h(
          "ul",
          { class: "relation-list" },
          sources.map((source) => {
            if (!ROLE_LABELS[source.role]) throw new Error(`unknown source role ${source.role}`);
            return h(
              "li",
              {},
              h("a", { href: `/sources/${encodeURIComponent(source.slug)}` }, source.title),
              h("span", { class: "muted small" }, ` ${ROLE_LABELS[source.role]}`)
            );
          })
        )
      : h("p", { class: "muted small" }, "No source is linked to this concept yet.")
  );
}

// -- a "Find relations" result ----------------------------------------------------------------

/** The summary of a relate result (API `RelateOut`): what was added (each relation selectable),
 * how many candidates were judged none, what was refused and why, and the suggestions the graph
 * has no concept for, by slot. `ctx`: {onSelectConcept, onSelectRelation}. */
export function relatePanel(result, ctx) {
  const r = relateSummary(result);
  const title = r.regenerate ? "Relations regenerated" : "Relations found";
  const judgedText = r.judgeCalled
    ? `Claude (${r.model}) judged ${plural(r.judged, "candidate")}`
    : r.regenerate
      ? "Nothing to judge: no candidate was left"
      : "Nothing new to judge: every candidate was already related or judged";
  const existingText = r.regenerate ? `, ${r.counts.existing} already related by a model` : "";
  return h(
    "section",
    { class: "relation-group relate-result", "aria-label": title },
    h("h3", {}, title),
    h(
      "p",
      { class: "muted small" },
      `${judgedText} (${r.counts.embedding} by similarity, ${r.counts.proposal} from Claude's ` +
        `suggestions${existingText}; ${r.dropped} skipped before judging).`
    ),
    h("h4", {}, `Added (${r.added.length})`),
    r.added.length
      ? h(
          "ul",
          { class: "relation-list" },
          r.added.map((a) =>
            h(
              "li",
              {},
              h(
                "div",
                { class: "relation-head" },
                typeChip(a.type),
                h(
                  "button",
                  { type: "button", class: "concept-link", onClick: () => ctx.onSelectRelation(a.edge) },
                  a.phrase
                ),
                a.replaces
                  ? h(
                      "span",
                      { class: `badge ${a.replaces.changed ? "badge-changed" : "badge-same"}` },
                      a.replaces.changed ? "Changed" : "Same as before"
                    )
                  : r.regenerate
                    ? h("span", { class: "badge badge-new" }, "New")
                    : null
              ),
              a.replaces && a.replaces.changed
                ? h("p", { class: "small" }, `Replaces: ${a.replaces.phrase}.`)
                : null,
              h("p", { class: "rationale" }, a.rationale)
            )
          )
        )
      : h("p", { class: "muted small" }, "No new relation."),
    r.regenerate
      ? [
          h("h4", {}, `Removed (${r.removed.length})`),
          r.removed.length
            ? h(
                "ul",
                { class: "relation-list" },
                r.removed.map((x) =>
                  h(
                    "li",
                    {},
                    h(
                      "div",
                      { class: "relation-head" },
                      conceptButton(x.other, ctx.onSelectConcept),
                      h("span", { class: "small" }, x.phrase)
                    ),
                    h("p", { class: "rationale" }, `${x.label}: ${x.detail}`)
                  )
                )
              )
            : h("p", { class: "muted small" }, "No old relation was removed."),
          h("h4", {}, `Kept (${r.kept.length})`),
          r.kept.length
            ? h(
                "ul",
                { class: "relation-list" },
                r.kept.map((k) =>
                  h(
                    "li",
                    {},
                    h(
                      "div",
                      { class: "relation-head" },
                      conceptButton(k.other, ctx.onSelectConcept),
                      h("span", { class: "small" }, k.phrase)
                    ),
                    h("p", { class: "rationale" }, `Kept: ${k.because}.`)
                  )
                )
              )
            : h("p", { class: "muted small" }, "No reviewed, paper or user relation to keep."),
        ]
      : null,
    h("p", { class: "small" }, `Judged unrelated: ${plural(r.none, "candidate")}.`),
    r.refused.length
      ? [
          h("h4", {}, `Refused (${r.refused.length})`),
          h(
            "ul",
            { class: "relation-list" },
            r.refused.map((x) =>
              h(
                "li",
                {},
                h("div", { class: "relation-head" }, conceptButton(x.other, ctx.onSelectConcept), h("span", { class: "small" }, x.phrase)),
                h("p", { class: "rationale" }, x.reason)
              )
            )
          ),
        ]
      : null,
    r.suggestions.length
      ? [
          h("h4", {}, "Suggested, but not in the graph"),
          r.suggestions.map((group) =>
            h(
              "section",
              { "aria-label": group.label },
              h("h5", {}, `${group.label} (${group.items.length})`),
              h(
                "ul",
                { class: "relation-list" },
                group.items.map((u) =>
                  h("li", {}, h("strong", {}, u.name), h("p", { class: "rationale" }, u.description))
                )
              )
            )
          ),
        ]
      : null
  );
}

// -- a relation ------------------------------------------------------------------------------

/** The inspector for a relation (API edge) between concepts `from` and `to` (API nodes).
 * `ctx`: {onSelectConcept}. */
export function relationPanel(edge, from, to, ctx) {
  const directed = edge.type === "builds_on";
  return h(
    "div",
    { class: "inspector-section" },
    h("div", { class: "inspector-head" }, typeChip(edge.type), reviewBadge(edge.reviewed_at, edge.reviewed)),
    h(
      "h2",
      { class: "inspector-title", tabindex: "-1" },
      `${from.name} ${RELATION_LABELS[edge.type]} ${to.name}`
    ),
    directed
      ? h("p", { class: "muted small" }, `${to.name} is a prerequisite of ${from.name}.`)
      : h("p", { class: "muted small" }, "This relation goes both ways."),
    props([
      [directed ? "From" : "Between", conceptButton(from, ctx.onSelectConcept)],
      [directed ? "To" : "And", conceptButton(to, ctx.onSelectConcept)],
      ["Why", edge.rationale],
      ["Origin", originChip(edge.origin)],
      ["Reviewed", edge.reviewed_at ? formatDate(edge.reviewed_at) : "Not yet"],
    ]),
    edge.redundant
      ? h(
          "p",
          { class: "notice small" },
          "Redundant: a longer builds-on path already leads from ",
          from.name,
          " to ",
          to.name,
          ", so this relation adds no new prerequisite. It is kept, and drawn faintly."
        )
      : null
  );
}

// -- the roadmap -----------------------------------------------------------------------------

/** The roadmap section: `roadmap` (API nodes in study order, the target last) as numbered
 * buttons; `ctx`: {selectedId, onSelectConcept, onClose}. */
export function roadmapPanel(roadmap, ctx) {
  const target = roadmap[roadmap.length - 1];
  return h(
    "section",
    { class: "inspector-section roadmap", "aria-labelledby": "roadmap-heading" },
    h("h2", { id: "roadmap-heading", class: "inspector-subtitle" }, `Roadmap to ${target.name}`),
    h(
      "p",
      { class: "muted small" },
      roadmap.length === 1
        ? `${target.name} builds on nothing, so there is nothing to study first.`
        : `Study in this order: ${plural(roadmap.length - 1, "prerequisite")}, then ${target.name}. ` +
            "The rest of the graph is dimmed."
    ),
    h(
      "ol",
      { class: "roadmap-list" },
      roadmap.map((node) =>
        h(
          "li",
          { "aria-current": node.id === ctx.selectedId ? "true" : null },
          conceptButton(node, ctx.onSelectConcept)
        )
      )
    ),
    h("div", { class: "inspector-actions" }, actionButton("Turn off roadmap", ctx.onClose))
  );
}

// -- nothing selected ------------------------------------------------------------------------

/** The overview of what is on the canvas: counts by origin and relation type, every concept
 * on it A to Z (folded; on a phone this list is the easy way to reach one), how to use the
 * graph, and the legend. `edges` are the drawn relations; `ctx`: {onSelectConcept}. */
export function overviewPanel(nodes, edges, ctx) {
  const origins = countBy(nodes, "origin", ORIGINS);
  const types = countBy(edges, "type", RELATION_TYPES);
  const unreviewedNodes = nodes.filter((n) => !n.reviewed).length;
  const unreviewedEdges = edges.filter((e) => !e.reviewed).length;
  return h(
    "div",
    { class: "inspector-section" },
    h("h2", { class: "inspector-subtitle" }, "Overview"),
    h(
      "p",
      { class: "small" },
      `On the canvas: ${plural(nodes.length, "concept")} (${unreviewedNodes} unreviewed) and ` +
        `${plural(edges.length, "relation")} (${unreviewedEdges} unreviewed).`
    ),
    h(
      "ul",
      { class: "chip-row", "aria-label": "Concepts by origin" },
      [...origins].map(([origin, n]) => h("li", {}, originChip(origin), ` ${n}`))
    ),
    h(
      "ul",
      { class: "chip-row", "aria-label": "Relations by type" },
      [...types].map(([type, n]) => h("li", {}, typeChip(type), ` ${n}`))
    ),
    conceptIndex(nodes, ctx.onSelectConcept),
    h(
      "p",
      { class: "muted small" },
      "Click a concept or relation to inspect it; double-click a concept to add the concepts " +
        "related to it. Drag a concept to move it (it stays pinned where you drop it), drag the " +
        "background to pan, and zoom with ctrl/⌘ + scroll, a pinch, or the buttons."
    ),
    h(
      "p",
      { class: "muted small" },
      "On a touch screen: tap a concept or relation to inspect it and tap the background to let " +
        "go; double-tap a concept to add the concepts related to it; swipe to pan and pinch to " +
        "zoom; press and hold a concept, then drag, to move it. Names show for the concepts that " +
        "fit; zoom in to see more."
    ),
    legend()
  );
}

/** Every concept on the canvas A to Z, as buttons that select it; folded away. */
function conceptIndex(nodes, onSelectConcept) {
  const sorted = [...nodes].sort((a, b) => String(a.name).localeCompare(String(b.name), undefined, { sensitivity: "base" }));
  return h(
    "details",
    { class: "concept-index" },
    h("summary", {}, `All ${plural(nodes.length, "concept")} on the canvas, A to Z`),
    h("ul", { class: "concept-index-list" }, sorted.map((n) => h("li", {}, conceptButton(n, onSelectConcept))))
  );
}

function nodeSwatch(cls) {
  return s(
    "svg",
    { width: 22, height: 22, viewBox: "-11 -11 22 22", "aria-hidden": "true", class: "legend-swatch" },
    s("circle", { r: 8, class: `legend-node ${cls}` }),
    cls === "is-pinned" ? s("circle", { r: 3, cx: 6, cy: -6, class: "legend-pin" }) : null
  );
}

function standingSwatch(cls) {
  return s(
    "svg",
    { width: 22, height: 22, viewBox: "-11 -11 22 22", "aria-hidden": "true", class: "legend-swatch" },
    s("circle", { r: 8, class: "legend-node" }),
    s("circle", { r: 4, cx: 6, cy: 6, class: `legend-standing ${cls}` })
  );
}

function lineSwatch(cls, { arrow = false } = {}) {
  return s(
    "svg",
    { width: 44, height: 12, viewBox: "0 0 44 12", "aria-hidden": "true", class: "legend-swatch" },
    s("path", { d: "M2 6H40", class: `legend-line ${cls}` }),
    cls.includes("t-different_version_of") ? s("path", { d: "M2 6H40", class: "legend-inner" }) : null,
    arrow ? s("path", { d: "M36 2L42 6L36 10z", class: `legend-arrow ${cls}` }) : null
  );
}

/** What the colours, outlines and line styles mean. */
export function legend() {
  const row = (swatch, text) => h("li", {}, swatch, h("span", {}, text));
  return h(
    "section",
    { class: "legend small", "aria-label": "Legend" },
    h("h3", {}, "Legend"),
    h(
      "ul",
      {},
      row(nodeSwatch("origin-paper"), "Concept from a paper"),
      row(nodeSwatch("origin-model"), "Concept proposed by a model"),
      row(nodeSwatch("origin-user"), "Concept added by you"),
      row(nodeSwatch("is-unreviewed"), "Dashed outline: concept not reviewed yet"),
      row(nodeSwatch("is-pinned"), "Dot: pinned where you dropped it"),
      row(standingSwatch("standing-weak"), "Dot at the lower right: your standing is weak"),
      row(standingSwatch("standing-developing"), "Your standing is developing"),
      row(standingSwatch("standing-strong"), "Your standing is strong (no dot: not studied yet)"),
      row(lineSwatch("t-builds_on", { arrow: true }), "Builds on: the arrow points at the prerequisite"),
      row(lineSwatch("t-different_version_of"), "Different version of (double line)"),
      row(lineSwatch("t-relates_to"), "Relates to (dotted)"),
      row(lineSwatch("t-builds_on is-unreviewed", { arrow: true }), "Lighter colour: relation not reviewed yet"),
      row(lineSwatch("t-builds_on is-redundant", { arrow: true }), "Faint and thin: redundant builds-on (implied by a longer path)")
    )
  );
}
