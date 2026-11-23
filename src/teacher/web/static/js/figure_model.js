// The checked view of a `figure` block's content (see teacher.blocks), without DOM, so it can be
// tested under node. `figureModel` throws on content that is not in the stored shape: the block
// registry then shows an error in the block's place instead of a broken image.

// Mirrors teacher.blocks.FIGURE_ASSET_RE: relative segments, no "." or "..", a PNG.
const ASSET_RE = /^(?!.*(?:^|\/)\.\.?(?:\/|$))[A-Za-z0-9._-]+(?:\/[A-Za-z0-9._-]+)*\.png$/;

function text(content, field) {
  const value = content[field];
  if (typeof value !== "string" || !value.trim()) {
    throw new Error(`figure content.${field} is not a non-blank string`);
  }
  return value;
}

function count(content, field) {
  const value = content[field];
  if (!Number.isInteger(value) || value < 1) {
    throw new Error(`figure content.${field} is not a positive whole number`);
  }
  return value;
}

/** The URL of a figure's image: `baseUrl` (the artifact's API base, ending in "/") then
 * `assets/` and the asset path, each segment encoded. */
export function assetUrl(baseUrl, asset) {
  if (typeof asset !== "string" || !ASSET_RE.test(asset)) {
    throw new Error(`figure asset ${JSON.stringify(asset)} is not a PNG path inside the assets folder`);
  }
  if (typeof baseUrl !== "string" || !baseUrl.endsWith("/")) {
    throw new Error(`figure base URL ${JSON.stringify(baseUrl)} must end with "/"`);
  }
  return `${baseUrl}assets/${asset.split("/").map(encodeURIComponent).join("/")}`;
}

/** Alt text: the figure's label and the source's own caption, which says what it shows. */
export function altText(label, sourceCaption) {
  const caption = sourceCaption.replace(/\s+/g, " ").trim();
  return `${label} from the source: ${caption}`;
}

/** `{ src, alt, label, caption, sourceCaption, page, width, height }` for a figure block's
 * content, or an Error naming the first field that is wrong. */
export function figureModel(content, baseUrl) {
  if (!content || typeof content !== "object") throw new Error("figure content is missing");
  const label = text(content, "label");
  const sourceCaption = text(content, "source_caption");
  return {
    src: assetUrl(baseUrl, content.asset),
    alt: altText(label, sourceCaption),
    label,
    caption: text(content, "caption"),
    sourceCaption,
    page: count(content, "page"),
    width: count(content, "width"),
    height: count(content, "height"),
  };
}
