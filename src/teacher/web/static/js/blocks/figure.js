// `figure` block: { asset, label, source_caption, page, width, height, caption (Markdown) }, an
// image cropped from the source PDF (teacher.figures). The image is scaled to the text column
// (never beyond its own pixel width) on a light backing, so a paper's white-background figure
// reads in dark mode too without being inverted; the width and height attributes reserve its
// space before it loads. Below it: the label and the writer's caption, then the source's own
// caption on request. Clicking the image opens it larger in a modal <dialog>. An image that
// fails to load is replaced by an error saying so (fail loudly). Content checks live in
// ../figure_model.js.

import { h } from "../dom.js";
import { figureModel } from "../figure_model.js";
import { renderMarkdown } from "../markdown.js";

function enlarged(model, opener) {
  const close = h("button", { type: "button", class: "button compact figure-dialog-close" }, "Close");
  const dialog = h(
    "dialog",
    { class: "figure-dialog", "aria-label": `${model.label}, enlarged` },
    h(
      "div",
      { class: "figure-dialog-bar" },
      h("p", { class: "figure-dialog-title" }, model.label),
      close
    ),
    h(
      "div",
      { class: "figure-frame figure-dialog-frame" },
      h("img", {
        class: "figure-dialog-image",
        src: model.src,
        alt: model.alt,
        width: model.width,
        height: model.height,
        style: `max-width: ${model.width}px`,
      })
    )
  );
  close.addEventListener("click", () => dialog.close());
  // A click on the backdrop (the dialog element itself, outside its content) closes it too.
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
  dialog.addEventListener("close", () => opener.focus());
  return dialog;
}

export function renderFigure(block, ctx) {
  const model = figureModel(block.content, ctx.baseUrl);
  const captionId = `figure-${block.key}-caption`;
  const image = h("img", {
    class: "figure-image",
    src: model.src,
    alt: model.alt,
    width: model.width,
    height: model.height,
    loading: "lazy",
    decoding: "async",
    style: `max-width: min(100%, ${model.width}px)`,
  });
  const open = h(
    "button",
    {
      type: "button",
      class: "figure-open",
      title: "View larger",
      "aria-label": `View ${model.label} larger`,
    },
    image
  );
  const frame = h("div", { class: "figure-frame" }, open);
  const dialog = enlarged(model, open);
  open.addEventListener("click", () => dialog.showModal());
  image.addEventListener("error", () => {
    frame.replaceWith(
      h(
        "div",
        { class: "notice notice-error", role: "alert" },
        `The image of ${model.label} could not be loaded (${model.src}).`
      )
    );
  });

  const caption = renderMarkdown(model.caption, { baseUrl: ctx.baseUrl });
  caption.classList.add("figure-caption-text");
  return h(
    "figure",
    { class: "figure", "aria-describedby": captionId },
    frame,
    h(
      "figcaption",
      { class: "figure-caption", id: captionId },
      h("span", { class: "figure-label" }, model.label),
      caption,
      h(
        "details",
        { class: "figure-source" },
        h("summary", {}, `Caption in the source (page ${model.page})`),
        h("p", {}, model.sourceCaption)
      )
    ),
    dialog
  );
}
