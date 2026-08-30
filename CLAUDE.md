# CLAUDE.md — Working rules for this project

This file documents how Claude and the project owner (HP) work together on
LifeHub. Read this first in any new session before making changes.

## Project basics

- Local folder: `C:\Users\HP\LifeHub`
- GitHub repo: https://github.com/zementaye/lifehub (branch: `main`)
- Deployed on Render (auto-deploys from `main`, if auto-deploy is enabled —
  otherwise trigger a manual deploy from the Render dashboard after pushing)
- Stack: Flask + SQLite (or Turso) + Jinja templates, no build step
- File storage (vault documents, note photos): local disk via `config.UPLOAD_DIR`
  by default, or Backblaze B2 if `USE_B2` is configured — see `storage.py` /
  `config.py`. On Render without B2 configured, locally-stored uploads are
  wiped on redeploy/restart.

## The shell is always PowerShell

HP works on Windows. **Every command given for local execution — from
unzipping a delivered file all the way through to `git push` — must be
PowerShell**, never bash/cmd/WSL syntax. That covers things like:

- `Expand-Archive` (not `unzip`)
- `Copy-Item` (not `cp`)
- `Remove-Item` (not `rm`)
- `D:\Chrome_Downloads\...` style paths (not `~/Downloads/...` or the
  default `$env:USERPROFILE\Downloads\...` — HP's browser download folder
  is `D:\Chrome_Downloads`)

## The end-to-end push workflow

When Claude makes code changes in a session, the deliverable is a zip
containing only the changed files (preserving their folder structure, e.g.
`lifehub-main/templates/notes.html`). The standard flow HP follows to get
that into the real repo is:

```powershell
cd C:\Users\HP\LifeHub

# 1. Unzip the delivered file (adjust the filename to match what was downloaded)
Expand-Archive -Path "D:\Chrome_Downloads\<name>.zip" -DestinationPath "D:\Chrome_Downloads\<name>" -Force

# 2. Copy only the changed files over (one Copy-Item per file, matching folders)
Copy-Item "D:\Chrome_Downloads\<name>\lifehub-main\<path\to\file>" -Destination .\<path\to\> -Force

# 3. Review before committing
git status
git diff

# 4. Commit and push
git add <changed files>
git commit -m "<clear, specific message>"
git push
```

Rules for this flow:

- Claude always lists out the exact `Copy-Item` commands for each changed
  file — never a blind folder copy that could overwrite unrelated files.
- Each delivered zip gets a unique filename — never reuse the same zip name
  across deliverables in a session or across sessions, so old downloads in
  `D:\Chrome_Downloads` don't get confused with new ones.
- HP reviews `git diff` before committing. Claude should tell them what to
  look for if it isn't obvious.
- Commit messages are short, specific, and describe the change (not "update
  files").
- The `LF will be replaced by CRLF` warnings from Git on Windows are
  harmless and can be ignored.
- After `git diff`, press `q` to exit the pager if it opens one.

## History tracking

Two files in this repo exist purely to keep a record over time, and both
need to stay current:

- **`COMMIT_HISTORY.md`** — a plain log of git commits with dates. It's
  regenerated from the real `git log`, never hand-typed, so it's always
  accurate. Run `scripts/Update-CommitHistory.ps1` after pushing to refresh
  it, then commit that file too (small follow-up commit is fine).
- **`CHAT_HISTORY.md`** — a running summary of what was discussed and built
  in each Claude session, in HP's own project. This is *not* generated from
  git — Claude should append a new dated entry to it near the end of any
  session where real work happened (a feature built, a decision made, a
  bug fixed), summarizing what changed and why. Keep entries short —
  a few lines per session, not a transcript.

When asked to "remember" a rule or convention going forward, it goes in
*this* file (CLAUDE.md), not CHAT_HISTORY.md — CLAUDE.md is the rulebook,
CHAT_HISTORY.md is the log.

## Other conventions established so far

- New features get their own zip deliverable containing only the files that
  changed — not the whole project — so `git diff` stays reviewable.
- New DB tables (e.g. `note_images`) rely on the app's existing
  create-tables-on-startup behavior in `db.py` — no manual migration step
  needed after a deploy.
- UI additions match the existing dark theme's CSS variables (`--ink`,
  `--muted`, `--border`, `--surface`, `--violet`, etc. — see `:root` in
  `static/style.css`) rather than introducing new colors.
