/* One title in full: ratings with their sources, and where to watch it. */

import { getTitle, listMyItems } from "../api.js";
import { noteEditor, titleActions } from "../account.js";
import { formatDate, offerState, sourceColorVar } from "../format.js";
import { displayName, secondaryName } from "../i18n.js";
import { el, ratingPill, replace, scorePill, stateBlock } from "../ui.js";

export function createTitleView({ mount, app, router, items }) {
  return async function render(route) {
    const { t, language, user } = app.get();
    const id = route.params[0];

    replace(mount, el("div", { class: "shell state" }, el("div", { class: "state__mark" })));

    let title;
    try {
      title = await getTitle(id);
    } catch (error) {
      replace(
        mount,
        el(
          "div",
          { class: "shell" },
          stateBlock({
            title: error?.status === 404 ? t("title.notFound") : t("error.title"),
            body: error?.status === 404 ? "" : t("error.body"),
            actionLabel: t("title.back"),
            onAction: () => router.navigate("home"),
          }),
        ),
      );
      return null;
    }

    // The user's own entry for this one title, so a deep link into a title page
    // shows the right toggle state without loading their whole list.
    if (user && !items.get(title.id)) {
      await listMyItems({}, { pageSize: 100 })
        .then((page) => items.replaceAll(page.items))
        .catch(() => {});
    }

    replace(mount, buildDetail(title, { t, language, user, items }));
    document.title = `${displayName(title, language)} · TVIL`;
    return null;
  };
}

function buildDetail(title, { t, language, user, items }) {
  const name = displayName(title, language);
  const alternate = secondaryName(title, language);

  const facts = [
    title.year ? String(title.year) : "",
    title.seasons ? t("title.seasons", { count: title.seasons }) : "",
    title.runtime_minutes ? t("title.runtime", { count: title.runtime_minutes }) : "",
    ...title.genres.map((genre) => (language === "he" ? genre.name_he || genre.name_en : genre.name_en)),
  ].filter(Boolean);

  const overview = language === "he" ? title.overview_he || title.overview_en : title.overview_en || title.overview_he;

  return el("article", { class: "detail shell" }, [
    el("a", { class: "button button--quiet", href: "#/", text: t("title.back") }),
    el("div", { class: "detail__hero" }, [
      el(
        "div",
        { class: "detail__poster" },
        title.poster_url ? el("img", { src: title.poster_url, alt: name }) : null,
      ),
      el("div", {}, [
        el("h1", { class: "detail__title", text: name }),
        alternate ? el("p", { class: "detail__subtitle", text: alternate }) : null,
        el(
          "p",
          { class: "detail__facts" },
          facts.map((fact) => el("span", { text: fact })),
        ),
        overview ? el("p", { class: "detail__overview", text: overview }) : null,
        aggregateBlock(title, t),
        userSection(title, { t, user, items }),
      ]),
    ]),
    ratingsSection(title, { t, language }),
    offersSection(title, { t, language }),
  ]);
}

/** Lists, rating and note — or an invitation to sign in and have them. */
function userSection(title, { t, user, items }) {
  if (!user) {
    return el("p", { class: "actions__prompt", text: t("item.signInToTrack") });
  }

  const problem = el("p", { class: "actions__problem", role: "status" });

  return el("div", { class: "actions__block" }, [
    titleActions({
      titleId: title.id,
      items,
      t,
      onError: () => {
        problem.textContent = t("item.saveFailed");
      },
      onChange: () => {
        problem.textContent = "";
      },
    }),
    noteEditor({ titleId: title.id, items, t }),
    problem,
  ]);
}

function aggregateBlock(title, t) {
  const { score, score_israeli: israeli, components } = title.aggregate;
  if (score === null && israeli === null) return null;

  const row = el("div", { class: "aggregate section" }, [
    score !== null
      ? el("span", {}, [
          scorePill(score, { large: true, label: t("title.aggregate") }),
          el("span", { class: "aggregate__label", text: ` ${t("title.aggregate")}` }),
        ])
      : null,
    israeli !== null
      ? el("span", {}, [
          scorePill(israeli, { large: true, label: t("title.aggregateIsraeli") }),
          el("span", { class: "aggregate__label", text: ` ${t("title.aggregateIsraeli")}` }),
        ])
      : null,
  ]);

  const entries = Object.entries(components ?? {});
  if (!entries.length) return row;

  // The working is shown rather than asserted: a combined score is only worth
  // trusting if you can see what went into it.
  return el("div", {}, [
    row,
    el("details", { class: "components" }, [
      el("summary", { text: t("title.components") }),
      el("table", { class: "components__table" }, [
        el(
          "thead",
          {},
          el("tr", {}, [
            el("th", { text: t("title.provider") }),
            el("th", { text: t("title.normalized") }),
            el("th", { text: t("title.weight") }),
          ]),
        ),
        el(
          "tbody",
          {},
          entries.map(([provider, detail]) =>
            el("tr", {}, [
              el("td", { text: provider }),
              el("td", { text: String(detail.normalized ?? "—") }),
              el("td", { text: String(detail.weight ?? "—") }),
            ]),
          ),
        ),
      ]),
    ]),
  ]);
}

function ratingsSection(title, { t, language }) {
  return el("section", { class: "section" }, [
    el("h2", { class: "section__heading", text: t("title.ratings") }),
    title.ratings.length
      ? el(
          "ul",
          { class: "ratings" },
          title.ratings.map((rating) => ratingPill(rating, language)),
        )
      : el("p", { class: "state__body", text: t("title.noRatings") }),
  ]);
}

function offersSection(title, { t, language }) {
  if (!title.availability.length) {
    return el("section", { class: "section" }, [
      el("h2", { class: "section__heading", text: t("title.whereToWatch") }),
      el("p", { class: "state__body", text: t("title.noOffers") }),
    ]);
  }

  // Current offers first: what you can watch now is the point of the page.
  const ordered = [...title.availability].sort(
    (a, b) => Number(b.is_current) - Number(a.is_current),
  );

  return el("section", { class: "section" }, [
    el("h2", { class: "section__heading", text: t("title.whereToWatch") }),
    el(
      "ul",
      { class: "offers" },
      ordered.map((offer) => offerRow(offer, { t, language })),
    ),
  ]);
}

function offerRow(offer, { t, language }) {
  const state = offerState(offer);
  const badge =
    state === "untracked"
      ? el("span", { class: "badge badge--untracked", text: t("offer.untracked") })
      : state === "gone"
        ? el("span", {
            class: "badge badge--gone",
            text: offer.gone_since
              ? t("offer.goneSince", { date: formatDate(offer.gone_since, language) })
              : t("offer.gone"),
          })
        : null;

  const canWatch = state === "available" && offer.deep_link_url;

  return el(
    "li",
    {
      class: `offer${state === "available" ? "" : " offer--gone"}`,
      style: { "--source-color": sourceColorVar(offer.source_key) },
    },
    [
      el("div", {}, [
        el("div", { class: "offer__name", text: offer.source_name }),
        el("div", { class: "offer__note" }, [
          el("span", { text: t(`offer.${offer.offer_type}`) }),
          el("span", {
            text: ` · ${t("offer.verified", { date: formatDate(offer.last_seen, language) })}`,
          }),
        ]),
      ]),
      el("div", { class: "offer__actions" }, [
        badge,
        canWatch
          ? el("a", {
              class: "button",
              href: offer.deep_link_url,
              rel: "noopener noreferrer",
              target: "_blank",
              text: t("title.watch"),
            })
          : null,
      ]),
    ],
  );
}
