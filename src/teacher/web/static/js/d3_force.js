// The vendored d3-force 3.0.0 (with d3-dispatch, d3-quadtree and d3-timer), as an ES module.
//
// d3-force ships only a UMD build, so the page loads the four UMD files with classic <script>
// tags before its module script (see templates/graph.html); they define `window.d3`. This is
// the only module that reads that global: everything else imports from here. If the scripts did
// not load, importing this module throws, and the page shows the error (boot.js).

const d3 = globalThis.d3;
const NEEDED = ["forceSimulation", "forceManyBody", "forceLink", "forceCollide", "forceX", "forceY"];
const missing = NEEDED.filter((name) => !d3 || typeof d3[name] !== "function");
if (missing.length) {
  throw new Error(
    `The graph layout library (d3-force) did not load: missing ${missing.join(", ")}. ` +
      "Reload the page to try again."
  );
}

export const { forceSimulation, forceManyBody, forceLink, forceCollide, forceX, forceY } = d3;
