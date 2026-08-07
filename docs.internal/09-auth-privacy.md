# 09 — Auth & Privacy

## Login: Google + X (Twitter)

Implemented with Authlib in `tvil-api`:

- **Google** — OpenID Connect (authorization code). We request `openid email profile`
  only. `sub` → `users.auth_subject`.
- **X** — OAuth 2.0 with PKCE, scope `users.read` (+ `tweet.read` only if X's API tier
  requires it for `/2/users/me`). X user id → `auth_subject`; X may not return an email —
  `users.email` is nullable and nothing depends on it.

Flow: `GET /auth/login/{provider}` (sets `state` + PKCE verifier in a short-lived signed
cookie) → provider → `GET /auth/callback/{provider}` → verify → upsert user (keyed on
`(auth_provider, auth_subject)`; no cross-provider account linking — a Google user and an X
user are distinct accounts, documented in the UI) → create session → redirect to app.

OAuth client IDs/secrets from env only: `TVIL_GOOGLE_CLIENT_ID/SECRET`,
`TVIL_X_CLIENT_ID/SECRET`. Redirect URIs configured per environment.

## Sessions

- Server-side sessions: `sessions` table (id = 256-bit random token hash, user_id,
  created_at, expires_at, last_used_at). Cookie holds the raw token:
  `HttpOnly; Secure; SameSite=Lax; Max-Age=30d`, sliding renewal.
  Server-side (vs. JWT) so logout and account deletion revoke instantly.
- **CSRF:** SameSite=Lax plus double-submit token — `GET /me` returns a per-session CSRF
  token; the client sends it as `X-CSRF-Token` on every state-changing request; API rejects
  otherwise.
- Session cleanup: expired rows purged by the fetcher daemon's housekeeping tick (or
  `tvil-fetch db cleanup`).

## Privacy model

- **Private by default.** `users.is_public = false` on creation. A private user is
  invisible: `/users/{handle}` 404s, handles of private users are not enumerable.
- **Going public is explicit** (S7): user must pick a handle and flip the toggle in
  Settings, which spells out exactly what becomes visible: display name, avatar, watched /
  want-to-watch lists, ratings. **Never visible:** email, auth provider identity, notes,
  "my services" selection.
- **Notes are always private**, including on public profiles — they are personal memos,
  not reviews. (Revisit only as an explicit future decision.)
- **Account deletion** (`DELETE /me`): immediate hard delete of user row, sessions, and
  user_items. No soft-delete grace period — this is user data, not catalog data.
- **Data minimization:** we store nothing from providers beyond sub/email/name/avatar; no
  analytics/trackers in the client; server logs exclude query strings on user endpoints.

## Security checklist (enforced by tests where practical)

- All cookies `Secure` outside dev; HSTS at the reverse proxy.
- OAuth `state` verified; PKCE for X; ID token signature + `aud`/`iss`/`exp` verified for
  Google (Authlib does this — asserted in tests with fixed keys).
- Rating/note inputs length- and range-validated server-side (client validation is UX only).
- No user-generated HTML anywhere; notes rendered as text nodes.
- `/api/v1/me*` responses `Cache-Control: no-store`.
- Dependency audit (`pip-audit`) in CI weekly.
