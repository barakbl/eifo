/* The review queue, as a place a person can actually work.
 *
 * A parked listing is not in the catalog at all - no title, no availability,
 * nothing to search for - so this queue is content missing from the product,
 * not housekeeping. Which is why it is built for burst triage rather than for
 * reading: the two things being compared are side by side, the three answers
 * are one tap each, and 1/2/3 and j/k mean a few hundred rulings is an evening
 * rather than a project.
 *
 * Every ruling takes effect when it is made. The CLI it replaces recorded them
 * against the source's next sync, which is how 78 rulings came to be sitting
 * unapplied against a source that had not run for a fortnight.
 */

import {
  attachReview,
  countReviews,
  createFromReview,
  dismissReview,
  listReviews,
  ruleInBulk,
} from "../api.js";
import { displayName } from "../i18n.js";
import { el, replace, stateBlock } from "../ui.js";

const PAGE_SIZE = 25;

const ORDERS = [
  { key: "age", label: "review.order.age" },
  { key: "similarity", label: "review.order.similarity" },
];

export function createReviewView({ app, router }) {
  return {
    /** Render into a panel the Manage view owns; returns a teardown. */
    async mount(panel, params) {
      const { t } = app.get();
      const source = params.get("source") ?? "";
      const order = ORDERS.find((entry) => entry.key === params.get("order"))?.key ?? "age";

      replace(panel, el("p", { class: "muted", text: t("results.searching") }));

      let page;
      let counts;
      try {
        [page, counts] = await Promise.all([
          listReviews({ source, order }, { pageSize: PAGE_SIZE }),
          countReviews(),
        ]);
      } catch (error) {
        replace(
          panel,
          stateBlock({
            title: t("error.title"),
            body: error?.detail || t("error.body"),
          }),
        );
        return null;
      }

      return renderQueue(panel, { app, router, page, counts, source, order });
    },
  };
}

function renderQueue(panel, { app, router, page, counts, source, order }) {
  const { t, language } = app.get();
  const selected = new Set();

  const list = el("ul", { class: "queue" });
  const bulkBar = el("div", { class: "bulk", hidden: true });

  const go = (next) => {
    const params = new URLSearchParams({ tab: "review" });
    if (next.source) params.set("source", next.source);
    if (next.order && next.order !== "age") params.set("order", next.order);
    router.navigate("manage", [], params.toString());
  };

  replace(panel, [
    el("div", { class: "queue__head" }, [
      el("span", {
        class: "queue__count",
        text: t("review.remaining", { count: counts.total }),
      }),
      el("span", { class: "queue__keys muted", text: t("review.keys") }),
    ]),
    sourceChips(counts, { t, source, go }),
    orderChips({ t, order, source, go }),
    bulkBar,
    list,
  ]);

  if (!page.items.length) {
    replace(
      list,
      el("li", {}, stateBlock({ title: t("review.empty"), body: t("review.emptyBody") })),
    );
    return null;
  }

  const cards = page.items.map((item) =>
    reviewCard(item, { t, language, selected, onSelectionChange: refreshBulk }),
  );
  replace(list, cards);

  function refreshBulk() {
    bulkBar.hidden = selected.size === 0;
    if (selected.size === 0) return;
    replace(bulkBar, [
      el("span", { text: t("review.selected", { count: selected.size }) }),
      el("button", {
        class: "button",
        type: "button",
        text: t("review.bulkDismiss"),
        onClick: () => applyBulk("dismiss"),
      }),
      el("button", {
        class: "button",
        type: "button",
        text: t("review.bulkCreate"),
        onClick: () => applyBulk("create"),
      }),
      el("button", {
        class: "button button--quiet",
        type: "button",
        text: t("review.clearSelection"),
        onClick: () => {
          selected.clear();
          for (const card of list.querySelectorAll(".queue__item")) {
            card.querySelector("input[type=checkbox]").checked = false;
          }
          refreshBulk();
        },
      }),
    ]);
  }

  async function applyBulk(decision) {
    const ids = [...selected];
    for (const button of bulkBar.querySelectorAll("button")) button.disabled = true;
    try {
      await ruleInBulk(ids, decision);
      for (const id of ids) settle(list, id, t);
      selected.clear();
      refreshBulk();
    } catch (error) {
      replace(
        bulkBar,
        el("span", {
          class: "queue__error",
          text: error?.detail || t("review.failed"),
        }),
      );
    }
  }

  // 1/2/3 rule the focused card, j/k move between them. Bound to the panel and
  // removed with the view, so it cannot outlive the queue it acts on.
  const onKey = (event) => {
    if (isTypingTarget(event.target)) return;
    const cards = [...list.querySelectorAll(".queue__item")];
    if (!cards.length) return;

    const current = document.activeElement?.closest(".queue__item");
    const index = current ? cards.indexOf(current) : -1;

    if (event.key === "j" || event.key === "k") {
      event.preventDefault();
      const step = event.key === "j" ? 1 : -1;
      const next = cards[Math.min(cards.length - 1, Math.max(0, index + step))] ?? cards[0];
      next.focus();
      return;
    }

    const action = { 1: "same", 2: "different", 3: "junk" }[event.key];
    if (!action || !current) return;
    event.preventDefault();
    current.querySelector(`[data-action="${action}"]`)?.click();
  };

  panel.addEventListener("keydown", onKey);
  return () => panel.removeEventListener("keydown", onKey);
}

function sourceChips(counts, { t, source, go }) {
  const entries = Object.entries(counts.by_source ?? {}).sort((a, b) => b[1] - a[1]);
  return el("div", { class: "chips" }, [
    el("button", {
      class: "chip",
      type: "button",
      "aria-pressed": String(!source),
      text: `${t("review.allSources")} (${counts.total})`,
      onClick: () => go({ source: "" }),
    }),
    ...entries.map(([key, count]) =>
      el("button", {
        class: "chip",
        type: "button",
        "aria-pressed": String(key === source),
        text: `${key} (${count})`,
        dir: "ltr",
        onClick: () => go({ source: key }),
      }),
    ),
  ]);
}

function orderChips({ t, order, source, go }) {
  return el(
    "div",
    { class: "chips chips--quiet" },
    ORDERS.map((entry) =>
      el("button", {
        class: "chip",
        type: "button",
        "aria-pressed": String(entry.key === order),
        text: t(entry.label),
        onClick: () => go({ source, order: entry.key }),
      }),
    ),
  );
}

function reviewCard(item, { t, language, selected, onSelectionChange }) {
  const card = el("li", {
    class: "queue__item",
    tabindex: "0",
    dataset: { reviewId: String(item.id) },
  });

  const check = el("input", {
    type: "checkbox",
    "aria-label": t("review.select"),
    onChange: (event) => {
      if (event.currentTarget.checked) selected.add(item.id);
      else selected.delete(item.id);
      onSelectionChange();
    },
  });

  const actions = el("div", { class: "queue__actions" }, [
    ruleButton("same", t("review.same"), () => attachReview(item.id, item.closest?.title_id), {
      disabled: !item.closest,
    }),
    ruleButton("different", t("review.different"), () => createFromReview(item.id)),
    ruleButton("junk", t("review.junk"), () => dismissReview(item.id)),
  ]);

  function ruleButton(action, label, run, { disabled = false } = {}) {
    return el("button", {
      class: `button button--${action}`,
      type: "button",
      text: label,
      disabled,
      dataset: { action },
      onClick: async (event) => {
        for (const button of actions.querySelectorAll("button")) button.disabled = true;
        try {
          await run();
          settle(card.parentElement, item.id, t);
        } catch (error) {
          replace(
            actions,
            el("span", {
              class: "queue__error",
              text: error?.detail || t("review.failed"),
            }),
          );
        }
        event.stopPropagation();
      },
    });
  }

  card.append(
    el("div", { class: "queue__side" }, [check]),
    el("div", { class: "queue__pair" }, [
      offeredSide(item, { t, language }),
      candidateSide(item, { t, language }),
    ]),
    actions,
  );
  return card;
}

function offeredSide(item, { t }) {
  return el("div", { class: "queue__col" }, [
    el("span", { class: "queue__label", text: t("review.source") }),
    poster(item.poster_url, item.name),
    el("span", { class: "queue__name", text: item.name }),
    item.name_alt ? el("span", { class: "queue__alt", text: item.name_alt }) : null,
    el("span", { class: "queue__meta" }, [
      el("span", { text: item.year ? String(item.year) : "—" }),
      el("span", { text: t(`review.kind.${item.kind}`) }),
      el("span", {
        class: "queue__source",
        text: item.source_name ?? item.source_key,
        dir: "ltr",
      }),
    ]),
    item.deep_link_url
      ? el("a", {
          class: "queue__link",
          href: item.deep_link_url,
          rel: "noopener noreferrer",
          target: "_blank",
          text: t("review.open"),
        })
      : null,
  ]);
}

function candidateSide(item, { t, language }) {
  const closest = item.closest;
  if (!closest) {
    return el("div", { class: "queue__col queue__col--empty" }, [
      el("span", { class: "queue__label", text: t("review.candidate") }),
      el("span", { class: "muted", text: t("review.noCandidate") }),
    ]);
  }

  const name = displayName({ name_he: closest.name_he, name_en: closest.name_en }, language);
  return el("div", { class: "queue__col" }, [
    el("span", { class: "queue__label", text: t("review.candidate") }),
    poster(closest.poster_url, name),
    el("a", {
      class: "queue__name",
      href: `#/title/${closest.title_id}`,
      text: name,
    }),
    el("span", { class: "queue__meta" }, [
      el("span", { text: closest.year ? String(closest.year) : "—" }),
      closest.similarity != null
        ? el("span", {
            class: "queue__score",
            text: t("review.similarity", {
              value: Math.round(closest.similarity),
            }),
          })
        : null,
    ]),
  ]);
}

function poster(url, name) {
  if (!url) {
    return el("div", {
      class: "queue__poster card__placeholder",
      text: (name || "?").slice(0, 1),
      "aria-hidden": "true",
    });
  }
  return el("img", {
    class: "queue__poster",
    src: url,
    alt: "",
    loading: "lazy",
  });
}

/** Mark a card as ruled on and take it out of the way, without a reload. */
function settle(list, reviewId, t) {
  const card = list?.querySelector(`[data-review-id="${reviewId}"]`);
  if (!card) return;
  card.classList.add("queue__item--done");
  replace(card, el("span", { class: "queue__done", text: t("review.ruled") }));
}

function isTypingTarget(node) {
  return node instanceof HTMLElement && ["INPUT", "TEXTAREA", "SELECT"].includes(node.tagName);
}
