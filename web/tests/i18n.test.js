import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
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

/** The key sets of both string tables, read out of the source.
 *
 * The tables are not exported, and exporting them so a test could compare them
 * would be widening a module's surface to let a test look at its insides. */
function keysByLanguage() {
  const source = readFileSync(new URL("../js/i18n.js", import.meta.url), "utf8");
  const blocks = source.split(/^  (?:he|en): \{$/m).slice(1);

  assert.equal(blocks.length, LANGUAGES.length, "expected one block per language");
  return blocks.map(
    (block) =>
      new Set([...block.split(/^  \},?$/m)[0].matchAll(/^\s*"([^"]+)":/gm)].map((m) => m[1])),
  );
}

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

  /* Read from the source text rather than the loaded object, because that is
     the only place a duplicate still exists: a repeated key in an object
     literal silently collapses to the last one, so the parity check above
     cannot see it and the earlier string is simply dead. That is how a second
     "title.votes" got added beside the one already there. */
  it("defines no key twice in the same language", () => {
    const source = readFileSync(new URL("../js/i18n.js", import.meta.url), "utf8");
    const blocks = source.split(/^  (?:he|en): \{$/m).slice(1);

    assert.equal(blocks.length, LANGUAGES.length, "expected one block per language");

    for (const [index, block] of blocks.entries()) {
      const body = block.split(/^  \},?$/m)[0];
      const keys = [...body.matchAll(/^\s*"([^"]+)":/gm)].map((match) => match[1]);
      const seen = new Set();
      const duplicated = keys.filter((key) => (seen.has(key) ? true : (seen.add(key), false)));

      assert.deepEqual(duplicated, [], `${LANGUAGES[index]} defines these twice`);
      assert.ok(keys.length > 100, "the block parser found suspiciously few keys");
    }
  });

  /* This used to name four keys and assert that translate() did not hand back
     the key itself - which it never would, because the fallback returns the
     default language's string. So the test could not fail for the thing its
     name promised, and did not: every `suggest.*` string existed in Hebrew and
     none in English, and an English reader saw "כותרים" above their search
     results from the day the dropdown shipped.

     Read from the source for the same reason the duplicate check is: the
     tables are not exported, and a test that needed them to be would be
     widening a module's surface to look at its insides. */
  it("defines every key in both languages", () => {
    const [he, en] = keysByLanguage();

    assert.deepEqual([...he].filter((key) => !en.has(key)).sort(), [], "missing from en");
    assert.deepEqual([...en].filter((key) => !he.has(key)).sort(), [], "missing from he");
  });

  it("gives a key the same placeholders in both languages", () => {
    /* "{count} titles" against a translation with no {count} is a string that
       silently drops a number, which reads to whoever sees it as a bug in the
       data rather than in the wording. */
    const source = readFileSync(new URL("../js/i18n.js", import.meta.url), "utf8");
    const tables = source.split(/^  (?:he|en): \{$/m).slice(1);
    const entries = (block) =>
      new Map([...block.split(/^  \},?$/m)[0].matchAll(/^\s*"([^"]+)": "(.*)",?$/gm)]
        .map((match) => [match[1], match[2]]));
    const placeholders = (text) => [...text.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort();

    const [he, en] = tables.map(entries);
    for (const [key, hebrew] of he) {
      const english = en.get(key);
      if (english === undefined) continue;
      assert.deepEqual(placeholders(english), placeholders(hebrew), `placeholders differ: ${key}`);
    }
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
