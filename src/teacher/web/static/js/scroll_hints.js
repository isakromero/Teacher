// Edge hints on wide content that scrolls sideways inside its own box: display maths, markdown
// tables and code, and a chart's data table. On a narrow screen such content is cut off at the
// box's edge with nothing to say there is more; this sets `data-overflow` on each such box to
// the side(s) that have more to see ("right", "left" or "both"; removed when it all fits), and
// app.css fades that edge out.
//
// mountScrollHints(root) watches one container (the study page's blocks, the tutor's message
// list): boxes are found when they are added (a MutationObserver, batched per animation frame),
// re-measured when their size changes (a ResizeObserver) or once the web fonts have loaded
// (KaTeX's fonts change the width of maths), and updated as they are scrolled.

const SELECTOR = ".math-display, .md table, .md pre, .chart-table-wrap";

function paint(el) {
  const max = el.scrollWidth - el.clientWidth;
  if (max <= 1) {
    delete el.dataset.overflow;
    return;
  }
  const left = el.scrollLeft > 1;
  const right = el.scrollLeft < max - 1;
  el.dataset.overflow = left && right ? "both" : left ? "left" : "right";
}

/** Keep the hints of the scrolling boxes inside `root` up to date (for the page's lifetime). */
export function mountScrollHints(root) {
  if (!root) throw new Error("mountScrollHints needs the element whose content it watches.");
  const tracked = new Set();
  const resized = new ResizeObserver((entries) => {
    for (const entry of entries) paint(entry.target);
  });
  const onScroll = (event) => paint(event.currentTarget);

  function scan() {
    frame = 0;
    for (const el of tracked) {
      if (el.isConnected) continue;
      resized.unobserve(el);
      tracked.delete(el);
    }
    for (const el of root.querySelectorAll(SELECTOR)) {
      if (tracked.has(el)) {
        paint(el); // its content may have changed (a streaming reply) without its box resizing
        continue;
      }
      tracked.add(el);
      el.addEventListener("scroll", onScroll, { passive: true });
      resized.observe(el); // also paints it once now
    }
  }
  let frame = 0;
  new MutationObserver(() => {
    if (!frame) frame = requestAnimationFrame(scan);
  }).observe(root, { childList: true, subtree: true });
  if (document.fonts) document.fonts.ready.then(() => tracked.forEach(paint));
  scan();
}
