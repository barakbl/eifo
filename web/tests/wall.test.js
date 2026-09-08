import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { wallCopy } from "../js/ui.js";
import { translate } from "../js/i18n.js";

/* The members wall is shown to two different people and used to say the same
 * thing to both: somebody who has not signed in yet, and somebody who just did
 * and was turned away. The second read the first's wording - "sign in to see
 * what is streaming", under a dismissible line - as meaning they were in, and
 * said so. These pin which sentences each of them gets. */

describe("wallCopy", () => {
  it("invites somebody who has not tried yet", () => {
    const copy = wallCopy(null);

    assert.equal(copy.refused, false);
    assert.equal(copy.title, "members.wallTitle");
    assert.equal(copy.action, "auth.signInWith");
  });

  it("does not invite somebody who was just turned away", () => {
    const copy = wallCopy("not_invited");

    assert.equal(copy.refused, true);
    assert.equal(copy.title, "members.refusedTitle");
    assert.equal(copy.body, "members.refusedBody");
  });

  it("offers the only thing that could change the answer", () => {
    // Signing in again with the same account cannot work; a different one can.
    assert.equal(wallCopy("not_invited").action, "auth.tryAnotherAccount");
  });

  it("treats every other outcome as not having signed in", () => {
    // Cancelling or a provider failure leaves you outside, not refused.
    for (const outcome of ["cancelled", "failed", "", undefined]) {
      assert.equal(wallCopy(outcome).refused, false, `outcome: ${outcome}`);
    }
  });

  it("says out loud that the sign-in itself worked", () => {
    // The sentence that stops "I logged in with Google" meaning "I am in".
    const said = translate("en", wallCopy("not_invited").body);

    assert.match(said, /signing in worked/i);
    assert.match(said, /invite-only/i);
  });

  it("says it in Hebrew too", () => {
    for (const key of Object.values(wallCopy("not_invited"))) {
      if (typeof key !== "string") continue;
      assert.notEqual(translate("he", key), key, `untranslated: ${key}`);
    }
  });
});
