// Registers the renderer for every known block type. Add new block types here.

import { renderBlurb } from "./blurb.js";
import { renderChart } from "./chart.js";
import { renderFigure } from "./figure.js";
import { renderInteractable } from "./interactable.js";
import { renderMultipleChoice } from "./multiple_choice.js";
import { renderPrediction } from "./prediction.js";
import { renderProse } from "./prose.js";
import { registerBlock } from "./registry.js";

registerBlock("prose", renderProse);
registerBlock("multiple_choice", renderMultipleChoice);
registerBlock("blurb", renderBlurb);
registerBlock("prediction", renderPrediction);
registerBlock("chart", renderChart);
registerBlock("interactable", renderInteractable);
registerBlock("figure", renderFigure);

export { renderBlock } from "./registry.js";
