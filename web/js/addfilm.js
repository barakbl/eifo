/* "I watched a film that isn't here" - or mean to: find it on TMDB and add it.
 *
 * For the films no tracked service carries - a cinema, a flight, a festival -
 * which a member still wants to rate, remember, or not forget to see. Picking
 * one puts it on that list and opens its page, where the rating and the note are the same controls as on
 * every other title rather than a second, smaller copy of them in here.
 *
 * Nothing about the film is sent but its TMDB id. The server reads the name,
 * the year and the poster from TMDB itself.
 */

import { ApiError, addFilm, searchFilms } from "./api.js";
import { WANT_TO_WATCH, WATCHED } from "./items.js";
import { el, replace } from "./ui.js";

/** One letter matches half of TMDB; the server says the same. */
export const MIN_LENGTH = 2;

/** A little slower than the header's: each answer here is a trip to TMDB. */
const DEBOUNCE_MS = 250;

/**
 * What choosing a result does.
 *
 * A film the catalog already has is opened, not added: adding it would only
 * mark it watched, which its own page does with the rest of the controls, and
 * offering "add" for something that is there reads as though it is not.
 */
export function resultAction(movie) {
  return movie.title_id
    ? { kind: "open", titleId: movie.title_id }
    : { kind: "add", tmdbId: movie.tmdb_id };
}

/** The year and, when it says something new, the name it was made under. */
export function resultMeta(movie) {
  return [movie.year ? String(movie.year) : "", movie.original_name ?? ""]
    .filter(Boolean)
    .join(" · ");
}

export function openAddFilm({ app, router, query = "" }) {
  const { t } = app.get();
  let inFlight = null;
  let timer = null;
  let adding = false;

  const input = el("input", {
    class: "addfilm__search",
    type: "search",
    autocomplete: "off",
    enterkeyhint: "search",
    maxlength: "100",
    "aria-label": t("addFilm.search"),
    placeholder: t("addFilm.search"),
  });
  input.value = query;

  const status = el("p", { class: "addfilm__status muted", role: "status" });
  const results = el("ul", { class: "addfilm__results" });

  const dialog = el("dialog", { class: "confirm addfilm", "aria-labelledby": "addfilm-title" }, [
    el("h2", { class: "confirm__title", id: "addfilm-title", text: t("addFilm.title") }),
    el("p", { class: "confirm__body", text: t("addFilm.body") }),
    input,
    status,
    results,
    el("div", { class: "confirm__actions" }, [
      el("button", {
        class: "button button--quiet",
        type: "button",
        text: t("addFilm.close"),
        onClick: () => dialog.close(),
      }),
    ]),
  ]);

  function say(text) {
    status.textContent = text;
  }

  async function search(text) {
    inFlight?.abort();
    inFlight = new AbortController();
    say(t("addFilm.searching"));
    try {
      const found = await searchFilms(text, { signal: inFlight.signal });
      // An answer to a query that has since changed would replace a newer one.
      if (input.value.trim() !== text) return;
      render(found);
      say(found.length ? "" : t("addFilm.none"));
    } catch (error) {
      if (error?.name === "AbortError") return;
      replace(results);
      say(problem(error, t));
    }
  }

  function render(found) {
    replace(
      results,
      found.map((movie) => {
        const action = resultAction(movie);
        const buttons =
          action.kind === "open"
            ? el("a", {
                class: "button button--quiet addfilm__go",
                href: `#/title/${action.titleId}`,
                text: t("addFilm.open"),
                onClick: () => dialog.close(),
              })
            : el("span", { class: "addfilm__actions" }, [
                listButton(action.tmdbId, WATCHED, "button addfilm__go", "addFilm.add"),
                listButton(action.tmdbId, WANT_TO_WATCH, "button button--quiet addfilm__go", "addFilm.want"),
              ]);
        return el("li", { class: "addfilm__result" }, [
          movie.thumbnail_url
            ? el("img", {
                class: "addfilm__thumb",
                src: movie.thumbnail_url,
                alt: "",
                loading: "lazy",
                // TMDB is told nothing about the page the image is on.
                referrerpolicy: "no-referrer",
              })
            : el("span", { class: "addfilm__thumb addfilm__thumb--blank", "aria-hidden": "true" }),
          el("span", { class: "addfilm__text" }, [
            el("span", { class: "addfilm__name", text: movie.name }),
            el("span", { class: "addfilm__meta muted", text: resultMeta(movie) }),
          ]),
          buttons,
        ]);
      }),
    );
  }

  function listButton(tmdbId, status, className, label) {
    return el("button", {
      class: className,
      type: "button",
      text: t(label),
      onClick: () => add(tmdbId, status),
    });
  }

  async function add(tmdbId, status) {
    if (adding) return;
    adding = true;
    const buttons = results.querySelectorAll("button");
    for (const button of buttons) button.disabled = true;
    say(t("addFilm.adding"));
    try {
      const added = await addFilm(tmdbId, { status });
      if (added.created) {
        app.set({ userAddedCount: (app.get().userAddedCount ?? 0) + 1 });
      }
      dialog.close();
      router.navigate("title", [added.title_id]);
    } catch (error) {
      say(problem(error, t));
      for (const button of buttons) button.disabled = false;
    } finally {
      adding = false;
    }
  }

  input.addEventListener("input", () => {
    window.clearTimeout(timer);
    const text = input.value.trim();
    if (text.length < MIN_LENGTH) {
      inFlight?.abort();
      replace(results);
      say("");
      return;
    }
    timer = window.setTimeout(() => search(text), DEBOUNCE_MS);
  });

  // A press on the backdrop lands on the dialog itself - but so does one on
  // its own padding, an easy miss beside a button. Only outside its box closes.
  dialog.addEventListener("click", (event) => {
    if (event.target !== dialog) return;
    const box = dialog.getBoundingClientRect();
    const inside =
      event.clientX >= box.left &&
      event.clientX <= box.right &&
      event.clientY >= box.top &&
      event.clientY <= box.bottom;
    if (!inside) dialog.close();
  });
  dialog.addEventListener("close", () => {
    window.clearTimeout(timer);
    inFlight?.abort();
    dialog.remove();
  });

  document.body.append(dialog);
  dialog.showModal();
  input.focus();
  if (query.trim().length >= MIN_LENGTH) search(query.trim());
}

/** The server's own words when it has them; they say what to do next. */
function problem(error, t) {
  if (error instanceof ApiError && error.detail) return error.detail;
  return t("addFilm.failed");
}
