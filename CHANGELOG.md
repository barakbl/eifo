# Changelog

## [0.13.1](https://github.com/barakbl/eifo/compare/v0.13.0...v0.13.1) (2026-09-05)


### Bug Fixes

* the changelog stops listing every release twice ([#105](https://github.com/barakbl/eifo/issues/105)) ([3525573](https://github.com/barakbl/eifo/commit/352557372361c33d24b8dff9c682cc8073b3ac93))

## [0.13.0](https://github.com/barakbl/eifo/compare/v0.12.1...v0.13.0) (2026-09-05)


### Features

* issue API tokens from a terminal, and screenshots that show what shipped ([febddd1](https://github.com/barakbl/eifo/commit/febddd12cdcf64078885a03ebdde34838a4f4b90))


### Bug Fixes

* the sidecar can read a members-only catalog, and stops calling 401 a dead server ([af3e05a](https://github.com/barakbl/eifo/commit/af3e05adae971160fc61ac6d3bc3f71888b0992d))
* the sidecar says why "Forget API token" is greyed out ([f20b046](https://github.com/barakbl/eifo/commit/f20b0462ec5984c97b137829802d81672f6ffa5f))

## [0.12.1](https://github.com/barakbl/eifo/compare/v0.12.0...v0.12.1) (2026-09-05)


### Bug Fixes

* report the version the packages were actually released as ([e966754](https://github.com/barakbl/eifo/commit/e966754685195bddba2fde873bdfeba474519dde))

## [0.12.0](https://github.com/barakbl/eifo/compare/v0.11.0...v0.12.0) (2026-09-05)


### Features

* sign-in is by invitation, and the API has tokens of its own ([657dfc3](https://github.com/barakbl/eifo/commit/657dfc3fd0c00050c2d6eaf4118ce92272f3785a))


### Bug Fixes

* say where sign-in goes, and let the API be heard at all ([51b2350](https://github.com/barakbl/eifo/commit/51b23506b43698aa399e8220308cdb8048e5c81e))


### Performance

* the people dropdown stops reading the people table to rank ([8577b3b](https://github.com/barakbl/eifo/commit/8577b3b693f7dcc862f8faec5fa0d0007bfbe4ce))

## [0.11.0](https://github.com/barakbl/eifo/compare/v0.10.0...v0.11.0) (2026-09-05)


### Features

* search ranks by how well the name matches, not by synopsis length ([46e636a](https://github.com/barakbl/eifo/commit/46e636a264ce0f942909f739726ff3edf799c41f))

## [0.10.0](https://github.com/barakbl/eifo/compare/v0.9.0...v0.10.0) (2026-09-04)


### Features

* rating chips are per service, with the provider's own logo ([99f9cab](https://github.com/barakbl/eifo/commit/99f9cab685aec21dfcd429a7f1bf362965f2d13f))

## [0.9.0](https://github.com/barakbl/eifo/compare/v0.8.0...v0.9.0) (2026-09-04)


### Features

* the sidecar shows the run happening, and syncs one service ([0c3b7c1](https://github.com/barakbl/eifo/commit/0c3b7c14dd461d87570ee63f1aff76e9ac195ca4))

## [0.8.0](https://github.com/barakbl/eifo/compare/v0.7.0...v0.8.0) (2026-09-04)


### Features

* suggestions carry their score, in the pill the cards use ([496f0df](https://github.com/barakbl/eifo/commit/496f0df1e84347178dc4d7d7829c8386a28c1269))

## [0.7.0](https://github.com/barakbl/eifo/compare/v0.6.0...v0.7.0) (2026-09-04)


### Features

* a What's new page, by service and by arrival ([6abd6ab](https://github.com/barakbl/eifo/commit/6abd6abff3bf05b209d81613241ec65baa246198))

## [0.6.0](https://github.com/barakbl/eifo/compare/v0.5.1...v0.6.0) (2026-09-04)


### Features

* filter the catalog by how long a film runs ([3813bab](https://github.com/barakbl/eifo/commit/3813babfc2b82932ee55f936a83bf72434179bfb))


### Bug Fixes

* gone means gone from everything, not gone from one thing ([36ac267](https://github.com/barakbl/eifo/commit/36ac267059eabacb4e69f391021ed48016c22ced))

## [0.5.1](https://github.com/barakbl/eifo/compare/v0.5.0...v0.5.1) (2026-09-01)


### Bug Fixes

* read both of the film archive's shelves, not just the Israeli one ([abe79b8](https://github.com/barakbl/eifo/commit/abe79b86e3834940a89e69295a95d54dca560b94))

## [0.5.0](https://github.com/barakbl/eifo/compare/v0.4.0...v0.5.0) (2026-09-01)


### Features

* a rescore command, and a working that says why a weight is nought ([3bf60cb](https://github.com/barakbl/eifo/commit/3bf60cb8d177b0ced0555579fa80454676f6e1b1))

## [0.4.0](https://github.com/barakbl/eifo/compare/v0.3.0...v0.4.0) (2026-09-01)


### Features

* Israeli ratings, by indexing Seret's pages instead of searching them ([2dd5306](https://github.com/barakbl/eifo/commit/2dd53065e36a8cae601a7fb6614f90e5e690948c))

## [0.3.0](https://github.com/barakbl/eifo/compare/v0.2.0...v0.3.0) (2026-08-31)


### Features

* the sidecar checks for updates, and installs them ([a927f18](https://github.com/barakbl/eifo/commit/a927f18921b8ccbde6834309fe60d3b6e5e46a5f))

## [0.2.0](https://github.com/barakbl/eifo/compare/v0.1.0...v0.2.0) (2026-08-31)


### Features

* an interactive install script ([eca72cc](https://github.com/barakbl/eifo/commit/eca72cc4b75fb7fadab87c161b1a0eeb059c4167))


### Bug Fixes

* unbreak CI - rustfmt the sidecar, and match the existing release tag ([a781988](https://github.com/barakbl/eifo/commit/a78198871c20359b605eab9854a4b58048e71ab8))

## 0.1.0 (2026-08-31)

Initial release.
