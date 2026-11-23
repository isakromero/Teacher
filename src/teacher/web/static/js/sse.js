// A small, pure parser for a `text/event-stream` body read with fetch (POST requests cannot
// use EventSource). Feed it the raw byte chunks from `response.body.getReader()` in order; it
// returns the complete events found so far. Follows the WHATWG event-stream rules that matter
// here: lines end in \n, \r\n or \r (a \r\n pair may be split across chunks), "data:" lines
// accumulate with "\n" between them, ":" lines are comments, a blank line dispatches the event,
// and an unfinished event at the end of the stream is discarded (and reported by `end()`).
//
// Bytes are decoded with a streaming TextDecoder, so a UTF-8 character split across two chunks
// is decoded correctly.

/**
 * @typedef {{ event: string, data: string, id: string }} SSEEvent
 */

export function createSSEParser() {
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  let eventType = "";
  let data = [];
  let lastId = "";
  let sawData = false;
  let sawField = false;

  function dispatch(out) {
    if (sawData) {
      out.push({ event: eventType || "message", data: data.join("\n"), id: lastId });
    }
    eventType = "";
    data = [];
    sawData = false;
    sawField = false;
  }

  function processLine(line, out) {
    if (line === "") {
      dispatch(out);
      return;
    }
    if (line.startsWith(":")) return; // comment
    const colon = line.indexOf(":");
    let field;
    let value;
    if (colon === -1) {
      field = line;
      value = "";
    } else {
      field = line.slice(0, colon);
      value = line.slice(colon + 1);
      if (value.startsWith(" ")) value = value.slice(1);
    }
    sawField = true;
    switch (field) {
      case "event":
        eventType = value;
        break;
      case "data":
        data.push(value);
        sawData = true;
        break;
      case "id":
        if (!value.includes("\0")) lastId = value;
        break;
      default:
        break; // "retry" and unknown fields are ignored
    }
  }

  function drain(final, out) {
    let start = 0;
    for (let i = 0; i < buffer.length; i++) {
      const ch = buffer[i];
      if (ch === "\n") {
        processLine(buffer.slice(start, i), out);
        start = i + 1;
      } else if (ch === "\r") {
        if (i + 1 < buffer.length) {
          processLine(buffer.slice(start, i), out);
          if (buffer[i + 1] === "\n") i += 1;
          start = i + 1;
        } else if (final) {
          processLine(buffer.slice(start, i), out);
          start = i + 1;
        } else {
          break; // a lone \r at the end may be the first half of \r\n: wait for more
        }
      }
    }
    buffer = buffer.slice(start);
  }

  return {
    /**
     * Add one chunk (Uint8Array of UTF-8 bytes, or an already decoded string).
     * @returns {SSEEvent[]} the events completed by this chunk, in order
     */
    push(chunk) {
      buffer += typeof chunk === "string" ? chunk : decoder.decode(chunk, { stream: true });
      const out = [];
      drain(false, out);
      return out;
    },

    /**
     * The stream has ended. Returns any events completed by the final bytes; `incomplete` is
     * true when an unfinished event (no blank line after it) had to be discarded.
     * @returns {{ events: SSEEvent[], incomplete: boolean }}
     */
    end() {
      buffer += decoder.decode();
      const out = [];
      drain(true, out);
      const incomplete = buffer.length > 0 || sawField;
      buffer = "";
      eventType = "";
      data = [];
      sawData = false;
      sawField = false;
      return { events: out, incomplete };
    },
  };
}
