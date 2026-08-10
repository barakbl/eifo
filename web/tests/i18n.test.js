import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  DEFAULT_LANGUAGE,
  LANGUAGES,
  directionOf,
  displayName,
  isSupported,
  secondaryName,
  translate,
  translator,
} from "../js/i18n.js";

describe("language setup", () => {
  it("defaults to Hebrew, because the catalog is Israeli", () => {
    assert.equal(DEFAULT_LANGUAGE, "he");
  });

  it("maps each language to its writing direction", () => {
    assert.equal(directionOf("he"), "rtl");
    assert.equal(directionOf("en"), "ltr");
  });

  it("recognises only languages we ship strings for", () => {
    assert.ok(isSupported("he"));
    assert.ok(isSupported("en"));
    assert.equal(isSupported("fr"), false);
    assert.equal(isSupported(null), false);
  });
});

describe("translate", () => {
  it("returns the string for the requested language", () => {
    assert.equal(translate("en", "title.watch"), "Watch");
    assert.equal(translate("he", "title.watch"), "לצפייה");
  });

  it("substitutes placeholders", () => {
    assert.equal(translate("en", "title.seasons", { count: 4 }), "4 seasons");
  });

  it("leaves an unsupplied placeholder visible rather than blank", () => {
    assert.equal(translate("en", "title.seasons"), "{count} seasons");
  });

  it("returns the key itself when a string is missing", () => {
    // A missing translation should be obvious in the UI, not invisible.
    assert.equal(translate("en", "nope.not.here"), "nope.not.here");
  });

  it("falls back to the default language for an unknown language", () => {
    assert.equal(translate("fr", "title.watch"), translate("he", "title.watch"));
  });

  it("covers every key in both languages", () => {
    const keys = new Set();
    for (const language of LANGUAGES) {
      for (const key of ["title.watch", "error.retry", "offer.untracked", "empty.title"]) {
        keys.add(`${language}:${translate(language, key)}`);
        assert.notEqual(translate(language, key), key, `${key} missing in ${language}`);
      }
    }
    assert.ok(keys.size > 0);
  });
});

describe("translator", () => {
  it("binds a language so views can call t()", () => {
    const t = translator("en");

    assert.equal(t("title.ratings"), "Ratings");
    assert.equal(t("title.runtime", { count: 113 }), "113 min");
  });
});

describe("title names", () => {
  const bilingual = { name_he: "פאודה", name_en: "Fauda" };

  it("prefers the name in the reading language", () => {
    assert.equal(displayName(bilingual, "he"), "פאודה");
    assert.equal(displayName(bilingual, "en"), "Fauda");
  });

  it("falls back rather than showing nothing", () => {
    // Israeli titles frequently have no English name at all.
    assert.equal(displayName({ name_he: "שטיסל", name_en: null }, "en"), "שטיסל");
    assert.equal(displayName({ name_he: null, name_en: "Fargo" }, "he"), "Fargo");
  });

  it("returns an empty string when a title has no name", () => {
    assert.equal(displayName({}, "he"), "");
  });

  it("offers the other name as a subtitle", () => {
    assert.equal(secondaryName(bilingual, "he"), "Fauda");
    assert.equal(secondaryName(bilingual, "en"), "פאודה");
  });

  it("does not repeat the name it already showed", () => {
    assert.equal(secondaryName({ name_he: "שטיסל", name_en: null }, "en"), "");
  });
});
