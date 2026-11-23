// The header's review link (base.html): how many review questions are due now, fetched once
// per page (GET /api/review/queue with limit=0, which serves nothing). Nothing is shown when
// none is due; a failure shows "?" on the link with the reason as its title (and in the
// console), rather than a wrong count or a page-wide error on every page.

import { api, describeError } from "./api.js";
import { queueUrl } from "./review_model.js";

const badge = document.getElementById("nav-review-due");
if (badge) {
  const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
  api
    .get(queueUrl({ tz, limit: 0 }))
    .then((queue) => {
      if (typeof queue.due !== "number") throw new Error("the queue sent no due count");
      badge.textContent = queue.due ? String(queue.due) : "";
      badge.title = queue.due ? `${queue.due} ${queue.due === 1 ? "question is" : "questions are"} due for review` : "";
      badge.hidden = !queue.due;
    })
    .catch((err) => {
      const reason = `Could not load the review count: ${describeError(err)}`;
      console.error(reason);
      badge.textContent = "?";
      badge.title = reason;
      badge.hidden = false;
    });
}
