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
