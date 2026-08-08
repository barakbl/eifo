/* Hebrew and English strings, and the direction that goes with them.
 *
 * Hebrew is the default: this is an Israeli catalog, and most of what it holds
 * has no English name at all.
 */

export const LANGUAGES = ["he", "en"];
export const DEFAULT_LANGUAGE = "he";

const STRINGS = {
  he: {
    "app.name": "TVIL",
    "app.tagline": "מה זמין לצפייה בישראל",
    "search.placeholder": "חיפוש סרטים וסדרות",
    "search.shortcut": "/",
    "search.label": "חיפוש",

    "filters.services": "שירותים",
    "filters.all": "הכול",
    "filters.type.any": "הכול",
    "filters.type.movie": "סרטים",
    "filters.type.series": "סדרות",
    "filters.sort.score": "לפי דירוג",
    "filters.sort.score_israeli": "לפי דירוג ישראלי",
    "filters.sort.year": "לפי שנה",
    "filters.sort.name": "לפי שם",
    "filters.sort.recently_added": "נוספו לאחרונה",
    "filters.available.current": "זמין עכשיו",
    "filters.available.any": "הכול, כולל מה שירד",
    "filters.available.gone": "ירד מהאוויר",

    "results.count": "{count} כותרים",
    "results.countLabel": "כותרים",
    "results.searching": "מחפש…",

    "empty.title": "אין תוצאות",
    "empty.body": "נסו שם אחר, או הסירו חלק מהסינונים.",
    "empty.clear": "נקו סינונים",

    "error.title": "משהו השתבש",
    "error.body": "לא הצלחנו לטעון את הקטלוג.",
    "error.retry": "נסו שוב",
    "error.offline": "אין חיבור לרשת.",

    "title.notFound": "הכותר לא נמצא",
    "title.back": "חזרה לקטלוג",
    "title.overview": "תקציר",
    "title.ratings": "דירוגים",
    "title.aggregate": "ציון משוקלל",
    "title.aggregateIsraeli": "ציון ישראלי",
    "title.components": "איך חושב הציון",
    "title.provider": "מקור",
    "title.normalized": "מנורמל",
    "title.weight": "משקל",
    "title.votes": "הצבעות",
    "title.whereToWatch": "איפה לצפות",
    "title.watch": "לצפייה",
    "title.seasons": "{count} עונות",
    "title.runtime": "{count} דק׳",
    "title.noRatings": "אין עדיין דירוגים לכותר הזה.",
    "title.noOffers": "לא זמין כרגע באף שירות שאנחנו עוקבים אחריו.",

    "offer.verified": "נבדק ב־{date}",
    "offer.gone": "ירד מהאוויר",
    "offer.goneSince": "ירד ב־{date}",
    "offer.untracked": "המקור אינו נתמך עוד",
    "offer.free": "חינם",
    "offer.stream": "במנוי",
    "offer.rent": "השכרה",
    "offer.buy": "רכישה",

    "theme.toggle": "החלפת ערכת נושא",
    "lang.toggle": "EN",
    "footer.data": "נתונים",

    "auth.signIn": "התחברות",
    "auth.signInWith": "התחברות עם {provider}",
    "auth.provider.google": "Google",
    "auth.provider.x": "X",
    "auth.signOut": "התנתקות",
    "auth.account": "החשבון שלי",
    "auth.cancelled": "ההתחברות בוטלה.",
    "auth.failed": "ההתחברות נכשלה. נסו שוב.",
    "auth.unavailable": "התחברות אינה זמינה בהתקנה הזו.",
    "auth.menu": "תפריט חשבון",

    "filters.myServices": "השירותים שלי",
    "filters.myServicesEmpty": "בחרו שירותים בהגדרות",

    "item.watched": "נצפה",
    "item.wantToWatch": "לצפייה בהמשך",
    "item.rating": "הדירוג שלי",
    "item.ratingClear": "ניקוי דירוג",
    "item.rate": "דירוג {value} מתוך 10",
    "item.note": "פתק פרטי",
    "item.notePlaceholder": "רק לעיניכם.",
    "item.noteSave": "שמירה",
    "item.noteSaved": "נשמר",
    "item.saveFailed": "השינוי לא נשמר.",
    "item.signInToTrack": "התחברו כדי לשמור רשימות ודירוגים.",
    "item.remove": "הסרה מהרשימות",

    "mylist.title": "הרשימה שלי",
    "mylist.tab.want": "לצפייה בהמשך",
    "mylist.tab.watched": "נצפו",
    "mylist.tab.rated": "דירגתי",
    "mylist.empty": "עדיין אין כאן כלום",
    "mylist.emptyBody": "סמנו כותרים כ״לצפייה בהמשך״ והם יופיעו כאן.",
    "mylist.browse": "לקטלוג",

    "settings.title": "הגדרות",
    "settings.services": "השירותים שלי",
    "settings.servicesHelp": "משמש לסינון ״השירותים שלי״ בקטלוג.",
    "settings.profile": "פרופיל",
    "settings.displayName": "שם תצוגה",
    "settings.handle": "כינוי",
    "settings.handleHelp": "אותיות קטנות באנגלית, ספרות וקו תחתון.",
    "settings.public": "פרופיל ציבורי",
    "settings.publicHelp":
      "כשהפרופיל ציבורי, כל מי שיש לו את הקישור רואה: שם תצוגה, תמונה, הרשימות והדירוגים שלכם. " +
      "לעולם לא: כתובת המייל, זהות ההתחברות, הפתקים או בחירת השירותים.",
    "settings.save": "שמירה",
    "settings.saved": "נשמר",
    "settings.danger": "מחיקת חשבון",
    "settings.dangerBody":
      "מחיקה מיידית של החשבון, הרשימות והדירוגים. אין תקופת חסד ואי אפשר לבטל.",
    "settings.delete": "מחיקת החשבון",
    "settings.deleteConfirm": "להקליד DELETE כדי לאשר",
    "settings.deleteWord": "DELETE",
  },
  en: {
    "app.name": "TVIL",
    "app.tagline": "What's streaming in Israel",
    "search.placeholder": "Search films and series",
    "search.shortcut": "/",
    "search.label": "Search",

    "filters.services": "Services",
    "filters.all": "All",
    "filters.type.any": "All",
    "filters.type.movie": "Films",
    "filters.type.series": "Series",
    "filters.sort.score": "By score",
    "filters.sort.score_israeli": "By Israeli score",
    "filters.sort.year": "By year",
    "filters.sort.name": "By name",
    "filters.sort.recently_added": "Recently added",
    "filters.available.current": "Available now",
    "filters.available.any": "Everything, including gone",
    "filters.available.gone": "No longer available",

    "results.count": "{count} titles",
    "results.countLabel": "titles",
    "results.searching": "Searching…",

    "empty.title": "Nothing matched",
    "empty.body": "Try a different name, or clear some filters.",
    "empty.clear": "Clear filters",

    "error.title": "Something went wrong",
    "error.body": "The catalog could not be loaded.",
    "error.retry": "Try again",
    "error.offline": "You are offline.",

    "title.notFound": "Title not found",
    "title.back": "Back to the catalog",
    "title.overview": "Overview",
    "title.ratings": "Ratings",
    "title.aggregate": "Weighted score",
    "title.aggregateIsraeli": "Israeli score",
    "title.components": "How this score was computed",
    "title.provider": "Provider",
    "title.normalized": "Normalised",
    "title.weight": "Weight",
    "title.votes": "Votes",
    "title.whereToWatch": "Where to watch",
    "title.watch": "Watch",
    "title.seasons": "{count} seasons",
    "title.runtime": "{count} min",
    "title.noRatings": "No ratings for this title yet.",
    "title.noOffers": "Not currently on any service we track.",

    "offer.verified": "Verified {date}",
    "offer.gone": "No longer available",
    "offer.goneSince": "Gone since {date}",
    "offer.untracked": "Source no longer tracked",
    "offer.free": "Free",
    "offer.stream": "Subscription",
    "offer.rent": "Rent",
    "offer.buy": "Buy",

    "theme.toggle": "Toggle theme",
    "lang.toggle": "עב",
    "footer.data": "Data",

    "auth.signIn": "Sign in",
    "auth.signInWith": "Sign in with {provider}",
    "auth.provider.google": "Google",
    "auth.provider.x": "X",
    "auth.signOut": "Sign out",
    "auth.account": "My account",
    "auth.cancelled": "Sign-in was cancelled.",
    "auth.failed": "Sign-in failed. Please try again.",
    "auth.unavailable": "Sign-in is not available on this installation.",
    "auth.menu": "Account menu",

    "filters.myServices": "My services",
    "filters.myServicesEmpty": "Pick your services in settings",

    "item.watched": "Watched",
    "item.wantToWatch": "Want to watch",
    "item.rating": "My rating",
    "item.ratingClear": "Clear rating",
    "item.rate": "Rate {value} out of 10",
    "item.note": "Private note",
    "item.notePlaceholder": "For your eyes only.",
    "item.noteSave": "Save",
    "item.noteSaved": "Saved",
    "item.saveFailed": "That change was not saved.",
    "item.signInToTrack": "Sign in to keep lists and ratings.",
    "item.remove": "Remove from my lists",

    "mylist.title": "My list",
    "mylist.tab.want": "Want to watch",
    "mylist.tab.watched": "Watched",
    "mylist.tab.rated": "Rated",
    "mylist.empty": "Nothing here yet",
    "mylist.emptyBody": "Mark titles as want-to-watch and they show up here.",
    "mylist.browse": "Browse the catalog",

    "settings.title": "Settings",
    "settings.services": "My services",
    "settings.servicesHelp": "Drives the “My services” filter on the catalog.",
    "settings.profile": "Profile",
    "settings.displayName": "Display name",
    "settings.handle": "Handle",
    "settings.handleHelp": "Lowercase letters, digits and underscores.",
    "settings.public": "Public profile",
    "settings.publicHelp":
      "While your profile is public, anyone with the link sees your display name, avatar, " +
      "lists and ratings. Never your email, sign-in identity, notes or chosen services.",
    "settings.save": "Save",
    "settings.saved": "Saved",
    "settings.danger": "Delete account",
    "settings.dangerBody":
      "Immediately deletes your account, lists and ratings. No grace period, no undo.",
    "settings.delete": "Delete my account",
    "settings.deleteConfirm": "Type DELETE to confirm",
    "settings.deleteWord": "DELETE",
  },
};

/** The writing direction for a language. */
export function directionOf(language) {
  return language === "he" ? "rtl" : "ltr";
}

/** Whether a language is one we actually ship strings for. */
export function isSupported(language) {
  return LANGUAGES.includes(language);
}

/**
 * Look up a string, substituting {placeholders}.
 *
 * An unknown key returns the key itself rather than throwing or rendering
 * blank: a missing translation should be obvious in the UI, not invisible.
 */
export function translate(language, key, values = {}) {
  const table = STRINGS[language] ?? STRINGS[DEFAULT_LANGUAGE];
  const template = table[key] ?? STRINGS[DEFAULT_LANGUAGE][key] ?? key;

  return template.replace(/\{(\w+)\}/g, (match, name) =>
    Object.hasOwn(values, name) ? String(values[name]) : match,
  );
}

/** Bind a language so views can call `t("key")`. */
export function translator(language) {
  return (key, values) => translate(language, key, values);
}

/**
 * The name to show for a title in this language.
 *
 * Falls back to the other language rather than showing nothing: plenty of
 * Israeli titles have no English name, and a few foreign ones have no Hebrew.
 */
export function displayName(title, language) {
  const preferred = language === "he" ? title.name_he : title.name_en;
  return preferred || title.name_en || title.name_he || "";
}

/** The other name, when it differs from the one already shown. */
export function secondaryName(title, language) {
  const shown = displayName(title, language);
  const other = language === "he" ? title.name_en : title.name_he;
  return other && other !== shown ? other : "";
}
