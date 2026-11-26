import CoreGraphics

/// Whether the bottom bar shows, decided from the web view's scrolling the way Safari does it:
/// it hides once the reader has scrolled down a little, comes back on scrolling up a little,
/// near the top of the page and at its end, and whenever the page is too short to scroll.
///
/// Rubber-banding past either end and scrolling the reader did not do (a restored position,
/// content arriving) never change it, and a direction has to be kept for `threshold` points
/// before it counts, so small back-and-forth movements do not make the bar flicker. The bar
/// only ever overlays the page (the space it needs is reserved for good), so showing or
/// hiding it never moves the page.
struct BarVisibility: Equatable {
    struct Sample: Equatable {
        /// How far the page is scrolled: 0 at the top (contentOffset.y + adjustedContentInset.top).
        var offset: CGFloat
        /// The page's height.
        var contentHeight: CGFloat
        /// The height of the part of the web view the page is read in.
        var viewportHeight: CGFloat
        /// The reader is dragging, or the page is still moving from their flick.
        var byReader: Bool
    }

    static let threshold: CGFloat = 24
    static let nearTop: CGFloat = 44
    static let nearEnd: CGFloat = 2

    private(set) var isVisible = true
    private var last: CGFloat?
    /// Where the current direction of scrolling started.
    private var anchor: CGFloat?

    mutating func update(_ sample: Sample) {
        let end = max(0, sample.contentHeight - sample.viewportHeight)
        let offset = sample.offset
        if end < Self.nearTop + Self.threshold || offset <= Self.nearTop {
            show(at: max(offset, 0))
            return
        }
        if offset > end {
            return // rubber-banding past the end: no change
        }
        guard sample.byReader else {
            last = offset
            anchor = offset
            return
        }
        if offset >= end - Self.nearEnd {
            show(at: offset)
            return
        }
        guard let last else {
            self.last = offset
            anchor = offset
            return
        }
        let delta = offset - last
        if delta == 0 { return }
        let anchor = self.anchor ?? last
        // A change of direction starts a new run from where it turned.
        let turned = (delta > 0) != (offset - anchor > 0) && offset != anchor
        let start = turned ? last : anchor
        self.anchor = start
        self.last = offset
        if offset - start > Self.threshold {
            isVisible = false
        } else if start - offset > Self.threshold {
            isVisible = true
        }
    }

    /// A new page: the bar shows, and the next scrolling starts a fresh run.
    mutating func reset() {
        isVisible = true
        last = nil
        anchor = nil
    }

    private mutating func show(at offset: CGFloat) {
        isVisible = true
        last = offset
        anchor = offset
    }
}
