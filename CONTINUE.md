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
     doesn't need editing again.** (Confirmed working as-is for
     `api_habits.py` in Phase 3 — no before_request edit was needed.)
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
- **Phase 2 — Dashboard** ✅: `api_dashboard.py` (`GET /api/dashboard`) —
  mirrors `app.py`'s `dashboard()` route, **except** the "Next Up" card
  (deliberately deferred — pulls from 4 tables + a recurrence-expansion
  helper, more to duplicate than seemed worth it for this slice).
  Frontend: `/dashboard` shows Body / Today's Nutrition / Today's Habits
  / Focus / Upcoming To-Dos from real data. Added `components/AppNav.js`
  (shared top nav — Dashboard/Habits links + user email + logout) here
  too, used by every authenticated page from this point on.
- **Phase 3 — Habits** ✅ (backend + frontend written, verify live before
  starting Phase 4): `api_habits.py` (list, add, checkin, uncheck,
  delete, set-reminder). Frontend: `/habits` page — add form, grouped by
  frequency (Daily/Weekly/Monthly), checkbox toggle, streak badge,
  delete. **Deliberately NOT built:** the per-habit reminder-time picker
  UI (route exists on the backend — `set_habit_reminder` — but no
  frontend form calls it yet; noted in-page that reminder scheduling
  still requires the old HTML site for now).

- **Phase 4 — Reminders / To-Dos** ✅ (backend + frontend written, verify
  live before starting Phase 5): `api_reminders.py` — one blueprint
  covering both reminders (dated, recurring) and todos (undated,
  one-off), matching how the HTML "To Do" page already pairs them (and
  how `add_reminder()` in `app.py` already branches into a todos-table
  insert when no due date is given). Frontend: `/todo` page — add form
  (title/due-date/recurrence), reminders table (snooze 3d/7d, pause/
  resume, delete), todos checklist (check/uncheck/delete). Added to
  `AppNav`.

- **Phase 5 — Calendar** ✅ (backend + frontend written, verify live
  before starting Phase 6): `api_calendar.py` (`GET /api/calendar?month=YYYY-MM`)
  — month grid + events from reminders/todos/documents/notes/shared-notes,
  mirroring `app.py`'s `calendar_view()`. **Deliberately READ-ONLY**: note
  creation (with image/voice attachments) is left for the separate
  "Notes" slice later in the roadmap, so this didn't have to duplicate
  file-upload handling it doesn't need yet — `add_note()` etc. in
  `app.py` are untouched. Frontend: `/calendar` — month grid, prev/next/
  today nav, event icons per type/state, "+N more" for busy days (no
  expand-on-click yet, just a count). Added to `AppNav`.

- **Phase 6 — Budget** ✅ (backend + frontend written, verify live before
  starting Phase 7): `api_budget.py` — monthly summary (income/expense/net,
  category spend with limits), transactions (add/delete), categories
  (add/delete), recurring transactions (add/toggle/delete), savings
  goals (add/contribute/withdraw/delete/set-target). **Deliberately NOT
  carried over:** the yearly summary/chart section from `app.py`'s
  `budget()` (separate block of queries, left for later), and background
  AI auto-categorization of blank-category expenses (left for the "AI
  features" slice — this didn't need to duplicate `ai.py` integration
  for one field; transactions just stay uncategorized if no category is
  given, same as if the AI call had failed on the HTML side). Frontend:
  `/budget` — month picker, summary stats, add-transaction form,
  transactions table, categories with spend progress bars, savings goals
  with progress/contribute/withdraw, recurring list (toggle/delete only
  — **no add-recurring form built yet**, noted in-page). Added to
  `AppNav`.

- **Phase 7 — Nutrition** ✅ (backend + frontend written, not yet
  deployed/verified — HP now pushes both sides manually, see Repo/deploy
  state below): `api_nutrition.py` — day summary (totals, meal
  breakdown, goals, recommended-intake calc), per-meal detail (entries +
  custom foods), food search (proxies `nutrition_api.search_foods`,
  local-then-USDA fallback — untouched), log/delete food entries,
  add/delete custom foods. Nothing deliberately deferred this slice —
  it's a straight port. Frontend: `/nutrition` (day totals, goal
  progress bars, recommended-intake card, meal cards linking out) and
  `/nutrition/[meal]` (search-and-log, grams input, custom-food list and
  add-form, per-entry delete). Added to `AppNav`. Note: the `[meal]` page
  uses `useSearchParams` (for the `?date=` param) wrapped in a
  `<Suspense>` boundary — Next.js App Router requires that or the build
  can fail; worth remembering for any future page that reads query
  params.

- **Phase 8 — Health** ✅ (backend + frontend written, not yet
  deployed/verified — HP pushes both sides manually): `api_health.py` —
  profile (height/birth date/sex), weight log (add/delete, BMI calc),
  sessions log (add/delete). **Deliberately NOT carried over:** the
  server-rendered inline SVG sparkline chart (`weight_sparkline_svg` in
  app.py) — frontend gets the raw `weights` list instead and can draw
  its own chart with a JS lib later if wanted; not worth duplicating
  SVG-string-building for a chart style the new frontend won't render
  the same way anyway. Frontend: `/health` — BMI card, profile form
  (height/birth date/sex, feeds Nutrition's recommended-intake calc),
  weight log table with add form, sessions log table with add form.
  Added to `AppNav`.

## Remaining roadmap (rough order — matches how the app links together)

1. ~~Auth~~ ✅
2. ~~Dashboard~~ ✅ (minus the "Next Up" card)
3. ~~Habits~~ ✅ (minus the reminder-time picker UI)
4. ~~Reminders / to-dos~~ ✅
5. ~~Calendar~~ ✅ (read-only — no note creation yet)
6. ~~Budget~~ ✅ (minus yearly chart, AI auto-categorize, add-recurring UI)
7. ~~Nutrition~~ ✅
8. ~~Health~~ ✅ (minus the weight sparkline chart)
9. **Vault** (documents — needs multipart file upload handling + presigned URLs) ← next up
10. Notifications
11. Notes (create/edit/delete, with image + voice-memo attachments —
    calendar's read-only note display depends on this being done well)
12. Passwords (password manager feature)
13. Admin panel (users, audit log)
14. AI features (chat, quick-add, budget auto-categorization)
15. "Next Up" dashboard card (deferred from step 2)
16. Habit reminder-time picker (deferred from step 3)
17. Calendar "+N more" expand-on-click (deferred from step 5)
18. Budget yearly summary/chart + add-recurring-transaction form (deferred from step 6)
19. Weight sparkline chart (deferred from step 8)
20. Settings / profile page
21. Email verification & forgot-password flows on the new frontend
    (currently only exist on the old HTML side)

## Repo/deploy state

- **Backend:** HP's local LifeHub repo (Windows, `C:\Users\HP\LifeHub`) →
  `git push` → Render auto-deploys. Env var `CORS_ALLOWED_ORIGIN` is set
  to the Vercel URL above.
- **Frontend:** not in a git repo yet — was deployed straight from files
  via the Vercel MCP connector for Phases 1–6. **As of the Budget slice,
  HP asked to stop that** — both sides are now delivered as zips each
  round and HP pushes/deploys manually (PowerShell for the backend git
  push; frontend deploy method is HP's call — Vercel CLI, a new GitHub
  repo connected to the existing Vercel project, or dragging the folder
  into the Vercel dashboard all work). Worth turning the frontend into a
  real GitHub repo at some point regardless, same reasoning as before.
