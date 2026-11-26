import WebKit

/// Scripts the app adds to every page, so the unchanged web client works over `teacher://`.
enum PageShims {
    /// WebKit hands a URL scheme handler no body at all for a request whose body holds a Blob
    /// or File (a `FormData` with the Add page's PDF, a Blob): neither `httpBody` nor
    /// `httpBodyStream` is set. This wraps `fetch` so such a body is turned into bytes first,
    /// with the multipart `Content-Type` (and boundary) the browser would have sent. Text-only
    /// bodies are left alone. (The gateway refuses a request whose body went missing anyway,
    /// so a case this does not cover fails loudly instead of reaching the server empty.)
    static let fetchBodies = """
    (() => {
      const original = window.fetch;
      if (typeof original !== "function" || original.airistotleBodies) return;
      const bound = original.bind(window);
      async function flatten(init) {
        if (!init || init.body == null) return init;
        const body = init.body;
        const isForm = typeof FormData !== "undefined" && body instanceof FormData;
        const isBlob = typeof Blob !== "undefined" && body instanceof Blob;
        if (!isForm && !isBlob) return init;
        const encoded = new Response(body);
        const bytes = await encoded.arrayBuffer();
        const headers = new Headers(init.headers || {});
        const type = encoded.headers.get("content-type");
        if (type && (isForm || !headers.has("content-type"))) headers.set("content-type", type);
        return { ...init, body: bytes, headers };
      }
      const wrapped = async (input, init) => bound(input, await flatten(init));
      Object.defineProperty(wrapped, "airistotleBodies", { value: true });
      window.fetch = wrapped;
    })();
    """

    /// The name of the message handler the page reports its scroll position to.
    static let scrollHandler = "airistotleScroll"
    static let styleID = "airistotle-app-style"

    /// What the app changes in how the web client looks (the server's pages stay as they are):
    /// - the header's "Sign out" form (and the email beside it) is hidden; Settings signs out.
    ///   It is a flex item of the header's first row, so the row closes up (or, where it had
    ///   wrapped onto a row of its own, that row goes);
    /// - a long press on a link shows no callout: it could only offer the app's internal
    ///   `teacher://` address;
    /// - the reader's text size (`TextSize`).
    static func appStyle(textSizePercent: Int) -> String {
        """
        .nav-signout { display: none !important; }
        a[href] { -webkit-touch-callout: none; }
        \(TextSize.css(textSizePercent))
        """
    }

    /// A script that puts `css` into the page's app style element (made if it is missing).
    /// At document start the root element exists but nothing else yet, so the element goes
    /// into it; `!important` wins over the page's own stylesheet that follows.
    static func styleScript(_ css: String) -> String {
        """
        (() => {
          const id = \(jsString(styleID));
          let style = document.getElementById(id);
          if (!style) {
            style = document.createElement("style");
            style.id = id;
            (document.head || document.documentElement).appendChild(style);
          }
          style.textContent = \(jsString(css));
        })();
        """
    }

    /// Reports the page's scroll position (CSS pixels) to the app once scrolling has paused,
    /// and when the page is left; `airistotleRestoreScroll(y)` scrolls back to a position once
    /// the page has grown tall enough (a study page loads its blocks after the document), and
    /// gives up when the reader touches the page first or after 6 seconds.
    static let scrollMemory = """
    (() => {
      if (window.airistotleRestoreScroll) return;
      const handler = window.webkit.messageHandlers.\(scrollHandler);
      let timer = null;
      const report = () => {
        timer = null;
        handler.postMessage({ href: location.href, y: Math.round(window.scrollY) });
      };
      addEventListener("scroll", () => {
        if (timer) clearTimeout(timer);
        timer = setTimeout(report, 300);
      }, { passive: true });
      addEventListener("pagehide", report);
      window.airistotleRestoreScroll = (y) => {
        let touched = false;
        addEventListener("touchstart", () => { touched = true; }, { once: true, passive: true });
        const started = Date.now();
        const step = () => {
          if (touched) return;
          const end = document.documentElement.scrollHeight - window.innerHeight;
          if (end >= y || Date.now() - started > 6000) {
            window.scrollTo(0, Math.min(y, Math.max(end, 0)));
            return;
          }
          setTimeout(step, 100);
        };
        step();
      };
    })();
    """

    @MainActor static func install(in controller: WKUserContentController, textSizePercent: Int) {
        controller.removeAllUserScripts()
        controller.addUserScript(WKUserScript(source: fetchBodies, injectionTime: .atDocumentStart, forMainFrameOnly: true))
        controller.addUserScript(WKUserScript(
            source: styleScript(appStyle(textSizePercent: textSizePercent)), injectionTime: .atDocumentStart, forMainFrameOnly: true
        ))
        controller.addUserScript(WKUserScript(source: scrollMemory, injectionTime: .atDocumentStart, forMainFrameOnly: true))
    }

    /// `text` as a JavaScript string literal.
    static func jsString(_ text: String) -> String {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.withoutEscapingSlashes]
        guard let data = try? encoder.encode(text), let literal = String(bytes: data, encoding: .utf8) else {
            preconditionFailure("a String always encodes as JSON")
        }
        return literal
    }
}
