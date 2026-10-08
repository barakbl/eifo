/* `#/connect?request=...` - an app asking to use Eifo as you ("Sign in with Eifo").
 *
 * Claude on the web or a phone - or any MCP client - sends the member here.
 * The page says who is asking, in the app's own words and marked as such; where
 * approving sends them back, which is the part an app cannot dress up; and
 * exactly what it will and will not be able to do. Nothing is granted until
 * Allow is pressed, and a connection is undone from Settings at any time.
 */

import { ApiError, approveConnect, denyConnect, loginUrl, readConnectRequest } from "../api.js";
import { el, replace, stateBlock } from "../ui.js";

/** What a read-only connection can and cannot do, in that order. */
export const CAN = ["connect.can.catalog", "connect.can.lists", "connect.can.taste"];
export const CANNOT = ["connect.cannot.change", "connect.cannot.others", "connect.cannot.account"];

export function createConnectView({ mount, app }) {
  return async function render(route) {
    const { t, user, loginProviders } = app.get();
    const sealed = new URLSearchParams(route.search).get("request") ?? "";

    if (!sealed) {
      replace(mount, card(stateBlock({ title: t("connect.missing"), body: t("connect.restart") })));
      return null;
    }

    if (!user) {
      // Back here after signing in: the request rides along in the sign-in.
      replace(
        mount,
        card([
          el("h1", { class: "connect__title", text: t("connect.signInTitle") }),
          el("p", { class: "connect__body", text: t("connect.signInBody") }),
          el(
            "div",
            { class: "connect__actions" },
            loginProviders.map((provider) =>
              el("a", {
                class: "button",
                href: loginUrl(provider, `#/connect?${new URLSearchParams({ request: sealed })}`),
                text: t("auth.signInWith", { provider: t(`auth.provider.${provider}`) }),
              }),
            ),
          ),
        ]),
      );
      return null;
    }

    replace(mount, card(el("p", { class: "muted", text: t("results.searching") })));

    let asking;
    try {
      asking = await readConnectRequest(sealed);
    } catch (error) {
      replace(
        mount,
        card(
          stateBlock({
            title: t("connect.invalid"),
            body: error instanceof ApiError && error.detail ? error.detail : t("connect.restart"),
          }),
        ),
      );
      return null;
    }

    const problem = el("p", { class: "form__problem", role: "alert" });
    const answer = async (send, button) => {
      problem.textContent = "";
      for (const each of mount.querySelectorAll("button")) each.disabled = true;
      try {
        const { redirect } = await send(sealed);
        // Back to the app: its own address, which is where it asked to be sent.
        window.location.assign(redirect);
      } catch (error) {
        problem.textContent =
          error instanceof ApiError && error.detail ? error.detail : t("error.body");
        for (const each of mount.querySelectorAll("button")) each.disabled = false;
        button.focus();
      }
    };

    const name = asking.client_name || t("connect.unnamed");
    const allow = el("button", {
      class: "button",
      type: "button",
      text: t("connect.allow"),
      onClick: () => answer(approveConnect, allow),
    });
    const deny = el("button", {
      class: "button button--quiet",
      type: "button",
      text: t("connect.deny"),
      onClick: () => answer(denyConnect, deny),
    });

    replace(
      mount,
      card([
        el("h1", { class: "connect__title", text: t("connect.title", { name }) }),
        el("p", { class: "connect__claim muted", text: t("connect.nameIsTheirs") }),
        el("p", { class: "connect__host" }, [
          el("span", { text: t("connect.sendsBack") }),
          " ",
          el("strong", { class: "connect__hostname", text: asking.redirect_host }),
        ]),
        el("h2", { class: "section__heading", text: t("connect.canTitle") }),
        el("ul", { class: "connect__list" }, CAN.map((key) => el("li", { text: t(key) }))),
        el("h2", { class: "section__heading", text: t("connect.cannotTitle") }),
        el(
          "ul",
          { class: "connect__list connect__list--no" },
          CANNOT.map((key) => el("li", { text: t(key) })),
        ),
        el("p", { class: "connect__body muted", text: t("connect.undo") }),
        problem,
        el("div", { class: "connect__actions" }, [deny, allow]),
      ]),
    );
    // Deny first in the tab order, and focused: Enter on a page somebody was
    // sent to by a stranger should not be the answer that grants access.
    deny.focus();
    return null;
  };
}

function card(children) {
  return el("div", { class: "shell" }, el("section", { class: "connect" }, children));
}
