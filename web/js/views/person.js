/* `#/people/{id}` - one page per person.
 *
 * Someone who directs and also acts is one human, so they get one page with a
 * section per role rather than two pages that never mention each other.
 */

import { getPerson } from "../api.js";
import { personName } from "../format.js";
import { el, replace, skeletonCards, stateBlock, titleCard } from "../ui.js";

/* Where else to read about them.
 *
 * Only TMDB for now, because only TMDB is a real deep link: the catalog stores
 * the person's TMDB id. IMDb and Rotten Tomatoes would have to be name
 * searches until this holds an id for them too, so they wait.
 */
const EXTERNAL_LINKS = [
  {
    label: "TMDB",
    kind: "person.profileOn",
    href: (person) => person.tmdb_id && `https://www.themoviedb.org/person/${person.tmdb_id}`,
  },
];

/* Sections in the order a career reads: what they made, then what they shot,
 * then what they appeared in. A role with no credits draws nothing. */
const ROLE_SECTIONS = [
  { role: "director", heading: "person.asDirector" },
  { role: "cinematographer", heading: "person.asCinematographer" },
  { role: "cast", heading: "person.asCast" },
];

export function createPersonView({ mount, app, router }) {
  return async function render(route) {
    const { t, language } = app.get();
    const personId = route.params[0];

    if (!personId) {
      replace(mount, notFound(t, router));
      return null;
    }

    replace(mount, el("div", { class: "shell" }, el("ul", { class: "grid" }, skeletonCards(6))));

    let person;
    try {
      person = await getPerson(personId);
    } catch {
      // A guessable URL that names nobody is ordinary, not an error worth a
      // stack trace on screen.
      replace(mount, notFound(t, router));
      return null;
    }

    replace(mount, buildPerson(person, { t, language }));
    document.title = `${personName(person, language)} · ${t("app.name")}`;
    return null;
  };
}

function notFound(t, router) {
  return el(
    "div",
    { class: "shell" },
    stateBlock({
      title: t("person.notFound"),
      actionLabel: t("title.back"),
      onAction: () => router.navigate("home"),
    }),
  );
}

function buildPerson(person, { t, language }) {
  const name = personName(person, language);
  // The other language's name, when there is one and it is not what we already
  // printed: Israeli people often have both, and both are worth showing.
  const alternate = [person.name_he, person.name_en].find((other) => other && other !== name);
  // Someone credited twice on one film is still one title in their body of work.
  const titles = new Set(person.credits.map((credit) => credit.title.id));

  return el("article", { class: "person shell" }, [
    el("a", { class: "button button--quiet", href: "#/", text: t("title.back") }),
    el("header", { class: "person__head" }, [
      person.profile_url
        ? el("img", { class: "person__portrait", src: person.profile_url, alt: "" })
        : null,
      el("div", {}, [
        el("h1", { class: "person__name", text: name }),
        alternate ? el("p", { class: "person__alternate", text: alternate }) : null,
        el("p", { class: "person__role", text: t("person.credits", { count: titles.size }) }),
        externalLinks(person, t),
      ]),
    ]),
    ...ROLE_SECTIONS.map(({ role, heading }) => roleSection(person, role, heading, { t, language })),
  ]);
}

/** Chips linking out to the sites that know more about them. */
function externalLinks(person, t) {
  const links = EXTERNAL_LINKS.map(({ label, kind, href }) => {
    const url = href(person);
    return url
      ? el("a", {
          class: "person__link",
          href: url,
          rel: "noopener noreferrer",
          target: "_blank",
          title: t(kind, { site: label }),
          text: label,
        })
      : null;
  }).filter(Boolean);

  return links.length ? el("p", { class: "person__links" }, links) : null;
}

/** One role's filmography, or nothing when they never wore that hat. */
function roleSection(person, role, heading, { t, language }) {
  const credits = person.credits.filter((credit) => credit.role === role);
  if (!credits.length) return null;

  return el("section", { class: "section" }, [
    el("h2", { class: "section__heading", text: `${t(heading)} · ${credits.length}` }),
    el(
      "ul",
      { class: "grid" },
      credits.map((credit, index) => titleCard(credit.title, language, index)),
    ),
  ]);
}
