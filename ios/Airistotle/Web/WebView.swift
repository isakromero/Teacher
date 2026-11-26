import SwiftUI
import WebKit

/// The host's web view in SwiftUI.
struct WebView: UIViewRepresentable {
    let host: WebViewHost

    func makeUIView(context: Context) -> WKWebView {
        host.webView
    }

    func updateUIView(_ uiView: WKWebView, context: Context) {}
}

/// The system share sheet for a downloaded file.
struct ShareSheet: UIViewControllerRepresentable {
    let items: [URL]

    func makeUIViewController(context: Context) -> UIActivityViewController {
        UIActivityViewController(activityItems: items, applicationActivities: nil)
    }

    func updateUIViewController(_ controller: UIActivityViewController, context: Context) {}
}
