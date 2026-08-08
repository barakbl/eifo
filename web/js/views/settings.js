/* `#/settings` — services, profile, and the way out. */

import { ApiError, deleteMe, patchMe } from "../api.js";
import { el, replace, stateBlock } from "../ui.js";

export function createSettingsView({ mount, app, router, onSignedOut }) {
  return async function render() {
    const { t, sources, user } = app.get();

    if (!user) {
      replace(
        mount,
        el(
          "div",
          { class: "shell" },
          stateBlock({
            title: t("item.signInToTrack"),
            actionLabel: t("mylist.browse"),
            onAction: () => router.navigate("home"),
          }),
        ),
      );
      return null;
    }

    const problem = el("p", { class: "actions__problem", role: "status" });
    const saved = el("span", { class: "note__status", role: "status" });

    /** Send a profile change and reflect whatever the server accepted. */
    async function save(patch) {
      problem.textContent = "";
      saved.textContent = "";
      try {
        app.set({ user: await patchMe(patch) });
        saved.textContent = t("settings.saved");
        return true;
      } catch (error) {
        problem.textContent =
          error instanceof ApiError ? error.detail || error.message : t("item.saveFailed");
        return false;
      }
    }

    replace(
      mount,
      el("div", { class: "shell settings" }, [
        el("h1", { class: "page__title", text: t("settings.title") }),
        servicesSection({ user, sources, t, save }),
        profileSection({ user: () => app.get().user, t, save }),
        el("div", { class: "settings__status" }, [saved, problem]),
        dangerSection({ t, onSignedOut }),
      ]),
    );
    return null;
  };
}

function servicesSection({ user, sources, t, save }) {
  const chosen = new Set(user.my_source_ids ?? []);

  return el("section", { class: "section" }, [
    el("h2", { class: "section__heading", text: t("settings.services") }),
    el("p", { class: "settings__help", text: t("settings.servicesHelp") }),
    el(
      "div",
      { class: "settings__chips" },
      sources
        .filter((source) => source.active)
        .map((source) =>
          el("button", {
            class: "chip",
            type: "button",
            "aria-pressed": String(chosen.has(source.id)),
            text: source.name,
            onClick: (event) => {
              if (chosen.has(source.id)) chosen.delete(source.id);
              else chosen.add(source.id);
              event.currentTarget.setAttribute("aria-pressed", String(chosen.has(source.id)));
              save({ my_source_ids: [...chosen] });
            },
          }),
        ),
    ),
  ]);
}

function profileSection({ user, t, save }) {
  const name = field({ label: t("settings.displayName"), value: user().display_name });
  const handle = field({
    label: t("settings.handle"),
    value: user().handle ?? "",
    help: t("settings.handleHelp"),
  });

  const isPublic = el("input", { type: "checkbox", id: "is-public" });
  isPublic.checked = user().is_public;
  isPublic.addEventListener("change", async () => {
    const wanted = isPublic.checked;
    const ok = await save({ is_public: wanted });
    // The server refuses a public profile without a handle. The box has to go
    // back, or it would sit there claiming a visibility nobody granted.
    if (!ok) isPublic.checked = !wanted;
  });

  return el("section", { class: "section" }, [
    el("h2", { class: "section__heading", text: t("settings.profile") }),
    name.node,
    handle.node,
    el("button", {
      class: "button",
      type: "button",
      text: t("settings.save"),
      onClick: () =>
        save({
          display_name: name.input.value.trim(),
          handle: handle.input.value.trim() || null,
        }),
    }),
    el("div", { class: "settings__switch" }, [
      isPublic,
      el("label", { for: "is-public", text: t("settings.public") }),
    ]),
    // Spelled out in full rather than summarised: going public is only an
    // informed choice if the copy says exactly what becomes visible.
    el("p", { class: "settings__help", text: t("settings.publicHelp") }),
  ]);
}

function field({ label, value, help }) {
  const input = el("input", { class: "control control--text", type: "text", value });
  const node = el("label", { class: "settings__field" }, [
    el("span", { text: label }),
    input,
    help ? el("span", { class: "settings__help", text: help }) : null,
  ]);
  return { node, input };
}

/**
 * Account deletion.
 *
 * Behind a typed confirmation rather than a dialog: the action is immediate and
 * irreversible, and a native confirm() would block the extension host besides.
 */
function dangerSection({ t, onSignedOut }) {
  const confirmation = el("input", {
    class: "control control--text",
    type: "text",
    placeholder: t("settings.deleteConfirm"),
    "aria-label": t("settings.deleteConfirm"),
  });

  const button = el("button", {
    class: "button button--danger",
    type: "button",
    disabled: true,
    text: t("settings.delete"),
    onClick: async () => {
      await deleteMe();
      onSignedOut?.();
    },
  });

  confirmation.addEventListener("input", (event) => {
    button.disabled = event.currentTarget.value.trim() !== t("settings.deleteWord");
  });

  return el("section", { class: "section section--danger" }, [
    el("h2", { class: "section__heading", text: t("settings.danger") }),
    el("p", { class: "settings__help", text: t("settings.dangerBody") }),
    confirmation,
    button,
  ]);
}
