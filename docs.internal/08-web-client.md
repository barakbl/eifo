# 08 — Web Client (`web/`)

A static, no-build HTML+JS app. Vanilla ES modules and modern CSS — no framework, no
bundler, no npm at runtime. This keeps it fast, auditable, and directly wrappable by
Capacitor at S8. "Polished and clean" is a requirement, not decoration: the bar is that it
does not look like a default Bootstrap page.

## Structure

```
web/
├── index.html
├── css/
│   ├── tokens.css        # design tokens: colors, type scale, spacing, radii
│   └── app.css
├── js/
│   ├── api.js            # fetch wrapper: base URL, CSRF header, problem-detail errors
│   ├── store.js          # tiny observable state (current user, filters, lists) — ~80 lines
│   ├── router.js         # hash-based routing (#/, #/title/123, #/me, #/u/handle)
│   ├── i18n.js           # he/en strings + dir switching
│   ├── ui.js             # dom helpers, component builders (card, badge, score pill)
│   └── views/            # home.js, title.js, mylist.js, profile.js, settings.js
└── assets/               # source logos, placeholder poster, favicon
```

Rules: no `innerHTML` with untrusted data (build DOM via helpers — XSS safety), every view
is a function `(el, params) => teardown`, state flows one way (store → render).

## Pages & UX

### Home / Browse (`#/`)
- **Header:** logo, language toggle (עב/EN), search box (debounced 250 ms,
  search-as-you-type against `GET /titles?q=`), login button / avatar menu.
- **Filter bar:** service chips with logos (multi-select). For a logged-in user a
  **"My services"** toggle applies their saved `my_source_ids` preset — this is the "filter
  by what I have" requirement; "All" is one tap away. Plus: type (movie/series), genre,
  minimum score, sort.
- **Grid:** responsive poster cards (2-col mobile → 6-col desktop). Card = poster
  (aspect 2:3, skeleton shimmer while loading), title in UI language, year, aggregate score
  pill, and the source spine.
- **The first screen of posters loads eagerly**, the rest lazily. Lazy-loading images that
  are already visible only delays the largest paint, which is exactly what the performance
  budget measures.
- Infinite scroll (IntersectionObserver) over the paginated API. Filters serialize to the
  URL hash — every view is shareable/bookmarkable.

### Title detail (`#/title/{id}`)
- Backdrop header, poster, both names (he+en), year, runtime/seasons, genres, overview in
  UI language.
- **Ratings row:** one pill per provider — logo/name, native score ("IMDb 8.4",
  "🍅 92%", "סרט 8.9"), each linking out to the provider page. Aggregate + Israeli
  aggregate shown large, with a popover explaining components (from `aggregate.components`).
- **Availability list:** per source — logo, name, offer type, "Watch" deep-link button,
  "verified <date>" caption. Badge states:
  - `is_current=false` → gray "לא זמין יותר / no longer available (since <date>)"
  - `source.active=false` → amber "המקור אינו נתמך עוד / source no longer tracked"
- **User actions (logged in):** Watched ✓ / Want-to-watch 🔖 toggles, 1–10 star rating,
  note editor (S7). Optimistic UI with rollback on API error.

### My list (`#/me`)
Tabs: Want to watch / Watched / Rated. Same cards + my rating shown; quick actions inline.

### Settings (`#/settings`)
My services (drives the preset), display name, handle, public-profile toggle (with a plain
explanation of exactly what becomes visible), account deletion.

### Public profile (`#/u/{handle}`) — S7
Read-only lists + ratings of a public user. Notes never shown.

## Design system

- **Tokens first:** all colors/spacing/type in `tokens.css` custom properties; components
  never hardcode values. Dark theme is the default; light theme via
  `prefers-color-scheme` + manual toggle (`data-theme` on `<html>`).
- **Type:** `system-ui` first, which carries a Hebrew face on every target platform, plus a
  monospace stack for data (scores, counts, dates) so figures read as figures. Self-hosting
  Heebo is a follow-up — it needs the actual font files, and a CDN link is not an option.
- **Feel:** generous whitespace, 8px spacing grid, 12px radii, subtle shadows, one accent
  color; score pills color-coded (≥75 green, 50–74 amber, <50 red, neutral gray when null).
- **Palette:** indigo ink with a warm amber accent — evening light rather than the neutral
  grey of a dashboard. Deliberately not near-black plus an acid accent.

- **Motion:** 150ms ease transitions only (hover lift on cards, toggle states). No
  gratuitous animation, and `prefers-reduced-motion` disables it entirely.
- **Empty/error states** are designed, not default: friendly empty-list illustrations,
  retry buttons on fetch failure, offline banner.

### The signature: the source spine

Every card carries a segmented colour bar along the poster's bottom edge, one segment per
service currently offering that title, using the same hue as that service's filter chip.
Scanning the grid you read **availability as colour** before you read titles — which is the
question the product exists to answer. It is the one loud element; everything around it
stays quiet.

Source colours live in `tokens.css` as `--source-<key>`. They carry information rather than
mood, so they are chosen for distinguishability, and the spine is always paired with a text
label (the offer list, the card's accessible name) — never colour alone.

## RTL & i18n

- `i18n.js` holds all strings (`he` default, `en`); switching sets `<html lang dir>`.
  Layout MUST be logical-property based (`margin-inline-start`, not `-left`) so RTL is
  free, not a special case.
- Titles display in UI language with fallback to whichever name exists.

## Accessibility & quality bar

- Keyboard: full tab order, visible focus rings, `Esc` closes overlays; search reachable
  via `/` shortcut.
- ARIA on toggles/tabs; poster alt = title name; color contrast AA on both themes.
- Lighthouse budget (checked manually per release, automated later): Performance ≥ 90,
  Accessibility ≥ 95 on the home grid.

## Attribution footer

Required by data licenses ([03-sources.md](03-sources.md)): "Streaming availability by
JustWatch · Metadata & posters by TMDB · Ratings © their providers (IMDb, Rotten Tomatoes,
Seret)". Rendered from `GET /api/v1/meta` so wording lives server-side.
