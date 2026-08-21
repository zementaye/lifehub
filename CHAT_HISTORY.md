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
