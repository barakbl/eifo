/* One title in full: ratings with their sources, and where to watch it. */

import { getTitle, listMyItems } from "../api.js";
import { noteEditor, titleActions } from "../account.js";
import {
  RUNTIME_BANDS,
  countryFlag,
  countryName,
  formatDate,
  formatPrice,
  formatVotes,
  languageName,
  offerState,
  personName,
  runtimeBand,
  sourceColorVar,
} from "../format.js";
import { displayName, secondaryName } from "../i18n.js";
import { el, ratingChip, replace, scorePill, stateBlock } from "../ui.js";

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
    document.title = `${displayName(title, language)} · ${t("app.name")}`;
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
        aggregateBlock(title, { t, language }),
        userSection(title, { t, user, items }),
      ]),
      creditsSection(title, { t, language }),
    ]),
    ratingsSection(title, { t, language }),
    offersSection(title, { t, language }),
  ]);
}

/* Who made it, and the facts about the thing itself.
 *
 * Names link to that person's page, so the block doubles as a way into the
 * catalog sideways: by the people in it rather than by the title. */
const CAST_SHOWN = 6;

function creditsSection(title, { t, language }) {
  const byRole = new Map();
  for (const credit of title.credits ?? []) {
    if (!byRole.has(credit.role)) byRole.set(credit.role, []);
    byRole.get(credit.role).push(credit);
  }

  const facts = factRows(title, { t, language });
  const crew = ["director", "cinematographer"]
    .filter((role) => byRole.has(role))
    .map((role) => {
      const credits = byRole.get(role);
      const key = credits.length > 1 ? `credit.${role}_plural` : `credit.${role}`;
      return metaRow(
        t(key),
        credits.map((credit) => personLink(credit, { language })),
      );
    });

  const cast = byRole.get("cast") ?? [];
  const rows = [...crew, ...(cast.length ? [castRow(cast, { t, language })] : []), ...facts];
  if (!rows.length) return null;

  // A rail beside the description on a wide screen, and just another block
  // under it on a narrow one - the CSS decides, the markup is the same.
  //
  // The heading is there but not on screen: the rows say what they are, so a
  // sighted reader needs no label, while a section a screen reader announces
  // without a name is just "section".
  return el("section", { class: "section detail__meta" }, [
    el("h2", { class: "section__heading visually-hidden", text: t("title.credits") }),
    el("dl", { class: "meta" }, rows),
  ]);
}

/** A label and its values, as one row of the metadata list. */
function metaRow(label, values) {
  return el("div", { class: "meta__row" }, [
    el("dt", { class: "meta__label", text: label }),
    el("dd", { class: "meta__value" }, values),
  ]);
}

function factRows(title, { t, language }) {
  const rows = [];
  // A running time is a fact about a film; a series has seasons instead, and
  // its per-episode runtime would be a different claim than the one we hold.
  if (title.type === "movie" && title.runtime_minutes) {
    rows.push(metaRow(t("title.length"), [runtimeValue(title.runtime_minutes, t)]));
  }
  if (title.original_language) {
    rows.push(
      metaRow(t("title.language"), [
        el("span", { text: languageName(title.original_language, language) }),
      ]),
    );
  }
  const countries = title.origin_countries ?? [];
  if (countries.length) {
    rows.push(metaRow(t("title.country"), countries.map((code) => countryValue(code, language))));
  }
  return rows;
}

/* The minutes, and four dots filled to how long that is - so "can I start this
 * tonight" is answered before the number is even read. The dots are decoration
 * for a screen reader, which gets the band as words instead. */
function runtimeValue(minutes, t) {
  const band = runtimeBand(minutes);
  const spoken = band ? `${t("title.runtime", { count: minutes })} · ${t(band.key)}` : "";

  return el("span", { class: "runtime", title: band ? t(band.key) : undefined }, [
    el("span", { text: t("title.runtime", { count: minutes }) }),
    band
      ? el(
          "span",
          { class: "runtime__dots", "aria-hidden": "true" },
          RUNTIME_BANDS.map((step) =>
            el("span", {
              class: `runtime__dot${step.level <= band.level ? " runtime__dot--on" : ""}`,
            }),
          ),
        )
      : null,
    band ? el("span", { class: "visually-hidden", text: ` ${spoken}` }) : null,
  ]);
}

/** A country, flag first: recognisable before the name is read. */
function countryValue(code, language) {
  const flag = countryFlag(code);
  return el("span", { class: "country" }, [
    flag ? el("span", { class: "country__flag", "aria-hidden": "true", text: flag }) : null,
    el("span", { text: countryName(code, language) }),
  ]);
}

/** The billed leads, with the rest a click away rather than a scroll away. */
function castRow(cast, { t, language }) {
  const shown = cast.slice(0, CAST_SHOWN).map((credit) => personLink(credit, { language }));
  const rest = cast.slice(CAST_SHOWN).map((credit) => personLink(credit, { language }));
  const value = el("dd", { class: "meta__value" }, shown);

  if (rest.length) {
    let expanded = false;
    const toggle = el("button", {
      class: "meta__more",
      type: "button",
      text: t("credit.showAll", { count: cast.length }),
      onClick: () => {
        expanded = !expanded;
        for (const node of rest) {
          if (expanded) value.insertBefore(node, toggle);
          else node.remove();
        }
        toggle.textContent = expanded
          ? t("credit.showFewer")
          : t("credit.showAll", { count: cast.length });
      },
    });
    value.append(toggle);
  }

  return el("div", { class: "meta__row" }, [
    el("dt", { class: "meta__label", text: t("credit.cast") }),
    value,
  ]);
}

function personLink(credit, { language }) {
  return el("a", {
    class: "meta__person",
    href: `#/people/${credit.person.id}`,
    // A cast credit knows who they played; hovering a name says so.
    title: credit.character || undefined,
    text: personName(credit.person, language),
  });
}

/** Lists, rating and note - or an invitation to sign in and have them. */
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

function aggregateBlock(title, { t, language }) {
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
            el("th", { text: t("title.votes") }),
            el("th", { text: t("title.weight") }),
          ]),
        ),
        el(
          "tbody",
          {},
          entries.map(([provider, detail]) =>
            componentRow(provider, detail, title, { t, language }),
          ),
        ),
      ]),
    ]),
  ]);
}

/**
 * One rater's line of the working.
 *
 * A weight of 0 on its own is not an explanation - it reads as a rater that was
 * switched off rather than as one this title has too few votes to trust - so
 * the reason is spelled out beside it, and the vote count that decided it is
 * shown rather than left in the database.
 */
function componentRow(provider, detail, title, { t, language }) {
  const note = componentNoteKey(detail);

  return el("tr", { class: detail.excluded ? "components__row--excluded" : null }, [
    el("td", { text: providerName(provider, title) }),
    el("td", { text: String(detail.normalized ?? "-") }),
    el("td", { text: formatVotes(detail.vote_count, language) || "-" }),
    el("td", {}, [
      el("span", { text: String(detail.weight ?? 0) }),
      note ? el("span", { class: "components__note", text: t(note) }) : null,
    ]),
  ]);
}

/**
 * Why a rater's weight is not what the config says, if it is not.
 *
 * Returns a translation key, or null when the weight speaks for itself. The
 * two cases are not the same thing and must not read as though they were: a
 * damped rating was counted and a excluded one was not.
 */
export function componentNoteKey(detail) {
  if (detail.excluded) return "title.notCountedTooFewVotes";
  if (detail.damped) return "title.countedHalfThinVotes";
  return null;
}

/**
 * What to call a rater in the table.
 *
 * The aggregate keys its working by the stored provider key; the payload
 * beside it already carries the name, so it is taken from there rather than
 * translated a second time. The raw key is the fallback - a rater whose rating
 * is not in this payload is still better named badly than not at all.
 *
 * The service is named too where it has to be. A chip can show "Audience"
 * under a Rotten Tomatoes logo and be perfectly clear; a table of raters with
 * one row saying "Audience" and another saying "צופים" is a table where two
 * different sites' crowds are indistinguishable. So a rater from a service
 * that reported more than one figure is named by both.
 */
export function providerName(provider, title) {
  const groups = title?.rating_groups ?? [];
  for (const group of groups) {
    const score = (group.scores ?? []).find((candidate) => candidate.provider === provider);
    if (!score) continue;
    return group.scores.length > 1 ? `${group.name} · ${score.provider_name}` : score.provider_name;
  }
  const rating = (title?.ratings ?? []).find((candidate) => candidate.provider === provider);
  return rating?.provider_name || provider;
}

function ratingsSection(title, { t, language }) {
  // By service, not by rater. The flat `ratings` list is still in the payload
  // and still what the aggregate's working reads from, because a weight is per
  // rater; a chip is per place the score came from.
  const groups = title.rating_groups ?? [];

  return el("section", { class: "section" }, [
    el("h2", { class: "section__heading", text: t("title.ratings") }),
    groups.length
      ? el(
          "ul",
          { class: "ratings" },
          groups.map((group) => ratingChip(group, language)),
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
  // What it costs sits with what kind of offer it is, next to the button that
  // charges it - a rental's price is part of the offer, not a footnote.
  const price = formatPrice(offer.price_minor, offer.price_currency, language);

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
          price ? el("span", { class: "offer__price", text: ` · ${price}` }) : null,
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
