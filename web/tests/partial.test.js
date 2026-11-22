import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { partialBadge } from "../js/ui.js";

describe("partialBadge", () => {
  const t = (key) => key;

  it("is nothing for a complete catalog", () => {
    assert.equal(partialBadge({ key: "netflix", partial: false }, t), null);
    assert.equal(partialBadge({ key: "netflix" }, t), null);
  });
});
