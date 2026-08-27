# Chat History

A running, human-readable log of what was discussed and built with Claude
on this project, session by session. This is separate from
`COMMIT_HISTORY.md` (which is the real, auto-generated `git log`) — this
file captures *why* and *what was decided*, not just the diff.

Claude should append a new dated entry here at the end of any session where
real work happened (a feature built, a decision made, a bug fixed). Keep
entries short — a few lines, not a transcript.

---

## 2026-08-21

**Notes: optional photo attachments.** Added the ability to attach photos
to notes. New `note_images` DB table; new routes to upload, serve, and
delete note photos; storage follows the same local-disk-or-B2 path the
Vault already uses. Deleting a note (single or bulk) or an account now also
cleans up its photo files. Pushed as commit `30f5ebe`.

**Notes: restyled the photo upload control.** The native "Choose Files"
button looked out of place, so it was replaced with a themed dropzone
(click or drag-and-drop) showing thumbnail/filename chips for selected
files with a remove option, matching the app's dark theme and Notes'
violet accent color. Pushed as commit `10b021a`.

**Established the push workflow.** HP works in PowerShell on Windows.
Standard flow for any Claude-delivered change: download the zip →
`Expand-Archive` → `Copy-Item` the specific changed files into
`C:\Users\HP\LifeHub` → `git status` / `git diff` to review →
`git add` / `git commit` / `git push`. This is documented in `CLAUDE.md`
going forward so it doesn't need to be re-explained each session.

**Set up project documentation.** Added `CLAUDE.md` (rules/conventions),
`COMMIT_HISTORY.md` (auto-generated commit log, see
`scripts/Update-CommitHistory.ps1`), and this file, `CHAT_HISTORY.md`
(session-by-session summary), so future sessions have context without
re-deriving it from scratch.

## 2026-08-23

**Shared calendar dates.** New feature: share specific dated notes (e.g. a
birthday) with another LifeHub account without exposing the whole
calendar. Flow: pick dated notes on the Notes page → get a link → send it
however you like → recipient logs in/signs up, previews, and
accepts/declines. Accepted dates read live off the source note (title/date/
recurrence), so edits on the owner's side show up automatically — no
re-sharing needed. Owner can revoke a whole link or remove a single date;
recipient can remove it from their own calendar. New DB tables:
`share_requests`, `share_request_items`.

**Notifications inbox.** Added as a dependency of the sharing feature but
built generic — new `notifications` table, a `/notifications` page, and a
bell icon with unread badge in both nav bars. Currently only sharing
activity (accepted/declined/revoked) writes to it, but any future feature
can drop a row in without new schema.

**Login/register now preserve a `next=` redirect.** Needed so someone who
opens a share link without an account yet gets bounced back to it after
signing up, instead of landing on the dashboard.

**Perf follow-up on shared dates (same day).** Link creation was slow —
each note added to a share was its own `INSERT` (its own network round
trip on Turso), and the Notes page redraw was doing an N+1 query per past
share to list its items. Batched the item inserts into one statement and
rewrote `list_outgoing_shares`/`list_incoming_shares` as single joined
queries grouped in Python. Also added a per-note-card "🔗 Share" button
(single-date share without entering bulk-select mode — bulk select stays
for multi-date shares) and gave the share forms their own "Loading…"
overlay text instead of the generic "Saving…".

## 2026-08-25

**"Next Up" dashboard widget.** New vertical-rectangle card at the top of
the Dashboard grid: the nearest upcoming date across reminders, dated
notes (birthdays etc.), dates shared with you, and expiring documents —
big date number, a days-left countdown ("Today" / "Tomorrow" / "N days
left"), and every event that lands on that same date listed underneath
with an icon. Deliberately excludes todos/vault-doc "added" markers
(those describe when something was created, not something coming up).
New `_next_up_summary()` helper in app.py, reuses the existing
`_note_occurrences_in_range()` recurrence projection so birthdays/
anniversaries roll forward correctly.

**Added `/healthz`.** A plain, unauthenticated, DB-free `GET /healthz` →
`200 "ok"` endpoint, for an external uptime pinger (cron-job.org) to hit
every few minutes so Render's free-tier spin-down (15 min idle → 30-60s
cold start → a 502 for whoever's request lands in that gap) doesn't bite
real visitors. Added to `_PUBLIC_ENDPOINTS` so it's exempt from the
login-required guard.

## 2026-08-26

**Added an app footer.** The public landing page (`home.html`) already
had one, but the logged-in app itself (Dashboard, Notes, Calendar, etc.)
had none — added to `base.html` so every page that extends it picks it
up automatically. Login/register/home stay untouched since they're
standalone templates, not extensions of base.html. New
`inject_current_year()` context processor so the year doesn't go stale.

**Footer content + animation (same day).** Expanded the footer to "Made
by Zemen · Contact (mailto:zemexasma@gmail.com) · tagline · © year" —
matches the plain-text convention already used on the public landing
page's footer. Animation reuses the site's existing HUD motifs rather
than introducing a new style: the same `card-scan` sweep-line effect
every `.card` already has on load (now looping across the footer's top
edge, colored via `--tab-color` so it matches whatever accent color the
current page uses), plus the same `brand-breathe` soft glow-pulse the
sidebar logo uses, applied to a small ✦ spark next to "Made by Zemen".

## 2026-08-26 (follow-up)

**Footer pinned to viewport bottom + expanded.** Converted to the
standard sticky-footer flex layout (`body` → full-height flex column,
`.content` → `flex:1 0 auto` flex column, footer → `margin-top:auto`) so
it sits at the bottom of the screen on short pages (e.g. an empty
Notifications list) instead of floating mid-page, while still flowing
naturally below content on long pages. Sidebar/backdrop are
`position:fixed` so unaffected. Footer also gained a quick-links row
(Dashboard/Calendar/Notes/Settings/Notifications/Contact) above the
existing brand/tagline/copyright line.

**Top nav: Settings + notification bell moved to the far right.**
Reordered so Profile sits at the end of the primary section links, and
Settings gets `margin-left:auto` (pushing itself and the bell after it
to the end of the nav bar) — the usual place for account/notification
controls, separated from primary navigation.

**Bell: bigger, rings on hover.** Wrapped the 🔔 in its own span so it
can be sized independently of nav link text and animated without
affecting the unread-count badge; added a `bell-ring` decaying-swing
keyframe animation (not a smooth loop — meant to read as a one-off
"ding" each time you hover/focus, not idle motion) in both the topbar
and mobile sidebar nav.

## 2026-08-27

**Landing page: fixed the "huge gap" bug on the public homepage.**
`body` is `display:flex; flex-direction:column` site-wide (sticky-footer
trick), and every landing-page section (`#features`, `#how-it-works`,
`#preview`, `#proof`, `#faq`, the CTA band, the footer) is a direct
child of it, centered with `max-width:1080px; margin:0 auto;` but no
explicit `width`. Horizontal auto-margins on a flex item cancel the
default cross-axis stretch, so each section was shrinking to fit only
its own content instead of filling up to 1080px — a different, too-
narrow width per section (measured as low as 480px for the dashboard
preview frame vs. its intended 900px). That squeezed the "A peek at
your dashboard" mini-cards into extra wrapped rows, inflated section
heights, and threw off the scroll-reveal `IntersectionObserver`
thresholds, producing a large blank gap while scrolling. Fix: added
`width: 100%;` to `.landing-section` and `.landing-footer` in
`home.html` (and reordered `.cta-band`'s width/max-width) so they
stretch to the row before centering, same as intended. Verified with a
headless-browser render: total page height dropped ~430px and every
section now measures the full 1080px.

**Landing page: fixed a second, still-present cause of the same "huge
gap" symptom — the dashboard preview mockup never revealing.**
`.preview-frame` starts hidden via `clip-path: inset(0 100% 0 0)`
(clipped to zero visible width, curtain-wipe reveal) and is meant to
un-clip via `IntersectionObserver` once scrolled into view. But the
observers watching it used a non-zero `threshold` (`0.15` for the
frame itself, `0.1` for the `.mini-card` stagger group) — and because
the element is clipped to zero width, its `intersectionRatio` is
permanently stuck at `0`, so it can never cross a non-zero threshold.
The reveal condition depended on the element already being visible: a
permanent deadlock. `.in-view` never got added, so the mockup (and its
4 stat cards) rendered as nothing — an empty ~236px box — while
everything else on the page revealed normally. Confirmed directly in a
headless browser: `intersectionRatio` stayed `0` with the clip-path in
place, jumped to `1` the instant it was stripped. Fix: gave
`.preview-frame` its own `IntersectionObserver` with `threshold: 0`
instead of sharing the 0.15-threshold observer, and same for the
`.mini-card` `revealGroup` call (added an optional `threshold` param,
defaulting to the existing `0.1` for every other group so nothing else
changed). Verified with a headless-browser render: `.preview-frame`
and all 4 mini-cards now flip to `.in-view` on scroll.

**Landing page: restyled the dashboard preview mini-cards (looked too
"built-for-a-coder") and made the hero's floating module icons
draggable.** Two separate asks, same session.

*Mini-cards:* titles were `font-mono`, uppercase, letter-spaced —
terminal-label styling that reads as a dev tool rather than a friendly
consumer dashboard. Swapped to `font-display`, sentence case, bolder
weight; added a small colored icon badge (❤️ 🍎 🔥 💰, matching the
feature-grid icons) next to each title; switched the accent from a
thin `border-left` to a `border-top` + a subtle tab-color gradient
wash on the card background; bumped stat-value size/weight; turned
`.mini-tag` ("Healthy") into an actual pill badge instead of plain
colored text; replaced the habit list's plain dots with checkbox-style
squares that show a ✓ when done, and turned the streak count into a
pill; added a slim progress bar to the Budget card (spent vs. left).
Grid was `auto-fit, minmax(200px,1fr)` which let the new icon+title
header's intrinsic content width force it down to 3-across-then-wrap
at the frame's own max-width — changed to explicit `repeat(4,
minmax(0,1fr))` with `min-width:0` on the card (plus a `max-width:
639px` override back to 2 columns for mobile) and `text-overflow:
ellipsis` on titles/checklist labels so a long label truncates instead
of wrapping the card taller. Verified with a headless-browser render:
all 4 cards sit in one row at the frame's 900px width.

*Hero icons:* the 6 floating module glyphs (`.orbit-icon` inside
`.orbit-parallax`) were purely decorative — `pointer-events: none` on
the whole `.hero-orbit` wrapper, position driven only by resting
top/left-or-right percentages plus a scroll-linked parallax
`translate3d` and an idle CSS drift keyframe. Made them draggable:
`.orbit-icon` gets `pointer-events: auto; cursor: grab; touch-action:
none` (the wrapper stays `pointer-events: none` so empty space around
them still doesn't intercept clicks); a new pointerdown/pointermove/
pointerup handler per icon freezes the wrapper's current on-screen
position into explicit `left/top` px (clearing whatever mix of
top/left/right/transform was positioning it), tracks the pointer with
clamping to the hero section's bounds, and stops there on release. A
`data-user-placed` flag on the wrapper tells the existing scroll-
parallax loop to skip that element from then on, so a dragged icon
stays exactly where it's dropped instead of the next scroll tick
snapping it back onto its parallax path; the idle drift keyframe still
plays around the new spot so it doesn't go dead-still. Verified with a
headless-browser drag simulation: dragged icon lands at the expected
offset and is still there, unmoved, after a scroll-down/scroll-up
round trip — the other 5 icons keep drifting with scroll as before.

**Landing page: replaced the dashboard preview's fake-browser-chrome
title bar.** `.preview-chrome` was a generic macOS-style bar — 3
traffic-light dots (red/amber/green) plus a pill showing
"lifehub.app/dashboard" like a URL bar — a common SaaS-marketing
cliché the owner didn't want. Replaced it with an actual app titlebar:
the same brand mark SVG used in the site nav (small bar-chart icon)
plus "Dashboard" in `font-display` on the left, and a pulsing green
dot + "Live preview" label on the right (`@keyframes live-pulse`, a
soft expanding-ring pulse rather than a static dot) instead of a fake
URL. Kept the existing idle shimmer sweep across the bar unchanged.
Verified with a headless-browser render.
