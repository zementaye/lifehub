# LifeHub → Vercel migration: status & continuation prompt

Paste this whole file into a new conversation, along with the latest
`lifehub-api-*.zip` and `lifehub-frontend-scaffold.zip`, and say:
**"Continue the LifeHub Vercel migration — do the next slice from the
roadmap below."**

## Goal

Split LifeHub (a Flask monolith on Render, server-rendered Jinja
templates) into: the same Flask app trimmed down to a JSON API (stays on
Render) + a separate Next.js frontend (Vercel). One feature at a time,
each delivered as its own reviewable diff — not a single big-bang
rewrite.

## Architecture decisions already made (don't re-litigate these)

1. **Auth is a Bearer token, NOT a session cookie.** First attempt used
   the shared Flask session cookie via CORS + `SameSite=None`. It didn't
   work reliably — Safari, Firefox, and privacy-conscious Chrome profiles
   block third-party cookies by default, so the cookie silently never
   made it back. `api_auth.py` now issues a signed, timestamped token
   (`itsdangerous`, already a Flask dependency) on login/register; the
   frontend stores it in `sessionStorage` and attaches it as
   `Authorization: Bearer <token>` on every request. Token payload is
   `{user_id, issued_at}`; `issued_at` is checked against
   `users.sessions_invalidated_at` so "log out everywhere" / a password
   change still invalidates outstanding tokens with no server-side
   revocation list needed.
2. **Every `api_*.py` blueprint is CSRF-exempt** (`csrf.exempt(bp)` in
   app.py). CSRF defends against a browser auto-attaching a cookie to a
   forged cross-site request — these blueprints don't use cookies, so
   there's nothing for CSRF to exploit.
3. **`app.py`'s global `before_request` hook (`load_logged_in_user`)
   auto-exempts anything by convention** — no per-blueprint edit needed
   going forward:
   - Any request with `request.method == "OPTIONS"` (CORS preflight)
     returns immediately, before any auth check.
   - Any request whose blueprint name starts with `api_` (checked via
     `request.blueprint.startswith("api_")`) returns immediately instead
     of redirecting to `/login` — a redirect can't be parsed as JSON by
     `fetch()`.
   - **This means a new `api_<feature>.py` blueprint just needs to be
     named `api_<feature>` and registered — the before_request hook
     doesn't need editing again.**
4. **CORS** is gated by the `CORS_ALLOWED_ORIGIN` env var in `config.py`
   (Render side), matched as an exact string (no trailing slash) against
   the Vercel frontend's origin. Currently set to
   `https://lifehub-frontend-zemen1.vercel.app`.
5. **Per-slice file pattern:** one new `api_<feature>.py` file — a
   self-contained Flask blueprint, imported + `register_blueprint` +
   `csrf.exempt` in `app.py`. Small pure-logic helpers (BMI calc,
   get-or-create-profile, etc.) get **duplicated** into the blueprint
   file rather than imported from `app.py`, because `app.py` imports
   these blueprint files near the top of the file, before its own helper
   functions are defined further down — importing back would be
   circular. Comment in each file flags what was duplicated and from
   where.
6. **`sqlite3.Row` objects aren't JSON-serializable** — always
   `dict(row)` before `jsonify()`. Same for `date`/`datetime` objects —
   `.isoformat()` them.
7. **Frontend:** Next.js 14 (App Router, plain JS, no TypeScript,
   deliberately kept simple). Styled to match LifeHub's existing dark HUD
   theme — CSS variables ported from `static/style.css` into
   `app/globals.css`. Deployed directly via Claude's Vercel MCP connector
   (no GitHub repo behind it yet) to Vercel team `zemen1`
   (`team_Yyf2vliUcSHa8BrONS0onA92`), project `lifehub-frontend`, live at
   **https://lifehub-frontend-zemen1.vercel.app**.

## Gotchas already hit — don't repeat these

- A redirect-to-login on an OPTIONS preflight → browser refuses to follow
  it → "CORS blocked" error that has nothing to do with CORS config
  itself. (Fixed by #3 above.)
- `CSRFError` handler unconditionally redirecting to `/login` → same
  silent-HTML-instead-of-JSON problem for any `/api/*` call that fails
  CSRF. (Now returns JSON for `/api/*` paths.)
- Cross-site cookies are fundamentally unreliable for this split — don't
  reach for cookies again for any future auth-adjacent feature.

## Completed slices

- **Phase 1 — Auth** ✅: `api_auth.py` (register / login / login/2fa /
  logout / me, bearer token issuance). Frontend: `/login`, `/register`
  pages, `lib/api.js` (`apiFetch` + `auth` object), `components/AuthShell.js`.
- **Phase 2 — Dashboard** ✅ (backend + frontend written, verify live
  before starting Phase 3): `api_dashboard.py` (`GET /api/dashboard`) —
  mirrors `app.py`'s `dashboard()` route, **except** the "Next Up" card
  (deliberately deferred — pulls from 4 tables + a recurrence-expansion
  helper, more to duplicate than seemed worth it for this slice).
  Frontend: `/dashboard` rewritten to show Body / Today's Nutrition /
  Today's Habits / Focus / Upcoming To-Dos from real data.

## Remaining roadmap (rough order — matches how the app links together)

1. ~~Auth~~ ✅
2. ~~Dashboard~~ ✅ (minus the "Next Up" card)
3. **Habits** (list, check-in, streak, create/edit/delete) ← next up
4. Reminders / to-dos (list, create/edit/delete, mark done)
5. Calendar (view + note creation, recurrence)
6. Budget (transactions, savings goals)
7. Nutrition (food log, food search, meal breakdown)
8. Health (weight entries, BMI history, sessions)
9. Vault (documents — needs multipart file upload handling + presigned URLs)
10. Notifications
11. Notes
12. Passwords (password manager feature)
13. Admin panel (users, audit log)
14. AI features (chat, quick-add)
15. "Next Up" dashboard card (deferred from step 2)
16. Settings / profile page
17. Email verification & forgot-password flows on the new frontend
    (currently only exist on the old HTML side)

## Repo/deploy state

- **Backend:** HP's local LifeHub repo (Windows, `C:\Users\HP\LifeHub`) →
  `git push` → Render auto-deploys. Env var `CORS_ALLOWED_ORIGIN` is set
  to the Vercel URL above.
- **Frontend:** not in a git repo yet — deployed straight from files via
  the Vercel MCP connector each round. Source delivered each round as
  `lifehub-frontend-scaffold.zip`; worth turning into a real GitHub repo
  before this goes much further.
