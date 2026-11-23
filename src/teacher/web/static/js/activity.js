// The study history, from the page's side: `logEvent` reports an action only the page sees
// (POST /api/history/events; the server logs everything that reaches it by itself), and
// `createHeartbeat` reports, while the tab is visible and the reader active, what is on screen
// (POST /api/history/heartbeat), which is how time on a page is measured.
//
// A failure is never swallowed: it is shown in the page's error banner (boot.js). A heartbeat
// that fails stops the heartbeat, so the banner says once that time is no longer recorded
// instead of repeating every few seconds. (The last heartbeat of a closing page is the one
// exception: see attention.js.)

import { api, describeError } from "./api.js";
import { showPageError } from "./dom.js";

export const HEARTBEAT_MS = 15_000; // teacher.managers.study_history.HEARTBEAT_INTERVAL
export const IDLE_MS = 60_000; // no scroll, key or pointer for this long: the reader is away
// The block in view must stay changed this long before a heartbeat says so: long enough that
// scrolling past several blocks sends nothing, short next to HEARTBEAT_MS. The server credits
// the time before a heartbeat to what was on screen at the previous one, so these settling
// seconds go to the block read before.
export const SETTLE_MS = 1_500;

/** Report an action only the page sees; `refs` holds the slugs (`artifact`, `lab`, `concept`,
 * `source`), `block` (a key) and `payload`. Errors go to the page's error banner. */
export async function logEvent(kind, refs = {}) {
  try {
    await api.post("/api/history/events", { kind, ...refs });
  } catch (err) {
    showPageError(`Could not record “${kind}” in your study history: ${describeError(err)}`);
  }
}

/**
 * The heartbeat logic, without timers or DOM so node can test it. `subject()` returns the
 * heartbeat body (`{surface, slug, block}`), `send(body)` posts it (a promise), `sendFinal(body)`
 * posts the last one of a page that is being hidden or closed (a request that outlives the
 * page), `now()` is the clock in ms. Call `activity()` on every scroll, key and pointer event,
 * `settled()` once the block in view may have changed and SETTLE_MS have passed without more
 * scrolling, `visibility(visible)` when the tab is shown or hidden, `leave()` when the page is
 * closed (pagehide), and `tick()` every HEARTBEAT_MS.
 *
 * A heartbeat goes out on a tick when the tab is visible and the reader was active within
 * IDLE_MS; on `settled()` when the subject differs from the one last sent; once more when the
 * tab is hidden or the page closed (if the reader was active); and at once when the reader comes
 * back (activity after IDLE_MS without any, or the tab shown again). A heartbeat after such a
 * pause carries `resumed: true`, so the server does not credit the time since the previous one.
 * `onError(err)` is called for the first failure, after which nothing more is sent.
 */
export function createHeartbeat({ subject, send, sendFinal, now, visible = true, onError }) {
  let isVisible = visible;
  let lastInput = now(); // opening the page counts as activity
  let lastSent = -Infinity;
  let lastSubject = null; // the subject last sent, as JSON
  let inFlight = false;
  let failed = false;
  let resume = false; // the next heartbeat says the reader is back after a pause
  let recheck = false; // settled() came while a heartbeat was in flight

  const active = () => isVisible && now() - lastInput <= IDLE_MS;

  function beat(transport = send) {
    if (failed || inFlight || !isVisible) return false;
    inFlight = true;
    lastSent = now();
    const resumed = resume;
    resume = false;
    Promise.resolve()
      .then(() => {
        const current = subject();
        lastSubject = JSON.stringify(current);
        return transport(resumed ? { ...current, resumed: true } : current);
      })
      .catch((err) => {
        failed = true;
        onError(err);
      })
      .finally(() => {
        inFlight = false;
        if (recheck) {
          recheck = false;
          settled();
        }
      });
    return true;
  }

  function settled() {
    if (failed || !active()) return false;
    if (inFlight) {
      recheck = true;
      return false;
    }
    let current;
    try {
      current = JSON.stringify(subject());
    } catch (err) {
      failed = true;
      onError(err);
      return false;
    }
    return current === lastSubject ? false : beat();
  }

  /** The tab is hidden or the page closed: send what the reader was on until now (when they
   * were active; a heartbeat still in flight already covers it), and credit nothing for the
   * time until they are back. */
  function hide() {
    if (!sendFinal) throw new Error("createHeartbeat: hiding the page needs sendFinal");
    const sent = active() ? beat(sendFinal) : false;
    isVisible = false;
    resume = true;
    return sent;
  }

  return {
    start: () => beat(),
    tick() {
      if (active()) return beat();
      return false;
    },
    activity() {
      const away = now() - lastInput > IDLE_MS;
      lastInput = now();
      if (away) resume = true;
      if (isVisible && (away || now() - lastSent > HEARTBEAT_MS)) return beat();
      return false;
    },
    settled,
    visibility(next) {
      if (!next) return isVisible ? hide() : false;
      if (isVisible) return false;
      isVisible = true;
      lastInput = now();
      return beat();
    },
    leave: () => (isVisible ? hide() : false),
    get stopped() {
      return failed;
    },
  };
}
