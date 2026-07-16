# AgentMailBridge agent instructions

## Product boundary

AgentMailBridge is a local-first, single-user, Windows-first email bridge that may be open sourced. Users perceive one product. `AgentMailBridgeMCP.exe` is an internal stdio component started on demand by an Agent and must exit when stdin closes. It must not have a shortcut, tray icon, startup entry, listener, daemon or arbitrary recipient support.

Do not expand the project into SaaS, multi-tenant infrastructure, a general email client, a general Gmail MCP or an Agent orchestration platform.

## Security invariants

- The recipient is fixed by `OWNER_GMAIL`.
- Gmail OAuth scope must remain exactly `gmail.readonly`.
- MCP file access is limited to `DATA_ROOT` and `ALLOWED_SEND_ROOTS`.
- A GUI user-selected global file does not expand MCP trust.
- Gmail IMAP and QQ SMTP secrets live in Windows Credential Manager and are never echoed back.
- `.env`, credentials.json, token.json, secrets, databases, logs, mail and attachments must not enter Git, reports, dist or installers.
- Never silently delete user data, OAuth files or credentials during uninstall.

## Runtime paths

Use `runtime_paths.py`. Frozen program files are read-only under the install directory. Installed configuration, OAuth and data are current-user writable paths under `%LOCALAPPDATA%\AgentMailBridge`. Source mode continues to support the repository `.env`. Do not depend on the current working directory or hard-code a user name or drive.

## MCP reliability invariants

- MCP stdout may contain protocol data only; diagnostics belong on stderr or in file logs.
- MCP stdin, stdout and stderr must be explicitly UTF-8, and Chinese paths, filenames, titles, spaces, BOM input, flush and EOF must be tested.
- Agents must not perform ad-hoc Copy-Item staging. AgentMailBridge validates allowed roots and performs atomic controlled staging.
- Source, staged, pre-SMTP attachment and sent archive size/SHA-256 facts must remain auditable and must block sending on a pre-SMTP mismatch.
- Real packaged MCP and loopback E2E evidence cannot be replaced by mocks; unexecuted external steps must be reported as unverified.

## Automatic receive invariants

- Automatic receive must start promptly, continue in the tray, recover after long pauses and never depend on the manual button.
- Gmail API/IMAP must use overlapping lookback plus Message-ID/database dedupe; prefer a repeated scan over a missed message.
- A single message or attachment failure must not block later mail. Persist finite retry state and keep global connection backoff separate.
- `no_changes` is healthy, never increments failures and never triggers backoff. `partial` preserves successful work and continues normal scheduling.
- True scheduler state, last check/success/result, next check and retry counts must remain observable after refresh and restart.
- Maximize/restore must use the shared linear icon system, support title-bar double click and preserve a normal geometry constrained to the current Windows work area.

## Unified mail archive invariants

- One received message has exactly one formal archive object, and every body, inline image, attachment, link and downloaded file must belong to its `package_id`.
- Mail files must not be written as unrelated loose objects. New mail is staged and atomically promoted into one package directory.
- `raw.eml` must contain bytes actually obtained from Gmail raw or IMAP `BODY.PEEK[]`; never fabricate raw content for legacy data.
- Manifest file paths are package-relative and must not escape the package root.
- Links are detected offline by default. Trusted domains are empty by default, and trusted downloads must remain HTTPS-only, redirect-aware and SSRF-safe.
- User-facing text must not expose internal resource enums. Full internal values may appear only in structured diagnostic details where useful.
- Account, mailbox and thread identity must not assume that one account exists forever. Preserve safe fallbacks without incorrectly merging unrelated messages.
- Mail Facts Query is read-only and must not execute, send, delete, move or modify messages or resources.
- AgentMailBridge does not provide knowledge management, Obsidian-specific behavior or Agent orchestration.
- Until the second-stage mail-level GUI project, keep the current receive, history and Files & Data compatibility behavior stable.

## Frontend information architecture

- The top-level work area contains only Receive and Send.
- The lower sidebar contains only History, Files & Data, Settings and About.
- Existing account configuration belongs only to Gmail and QQ account cards.
- Add mailbox account is only a future-extension demo and must not route to an existing account editor.
- The receive page must not contain account secrets or OAuth configuration.
- The send page must not contain QQ account configuration.
- Agent/MCP belongs only to Send and must not appear as another primary route or in Settings.
- Advanced Settings is a secondary page reached from Settings and must not contain account-level authentication.
- History records business actions; Files & Data manages stored objects and maintenance. Do not duplicate either list.
- Gmail API and Gmail IMAP must use separate conditional authentication views.
- Do not add duplicate routes for an existing account or application capability.
- Every new backend capability must be assigned to account, receive, send, files/data, settings, advanced, automatic, or CLI-only ownership before adding UI.

## UI quality invariants

- Do not use Emoji or Unicode symbols as formal application icons; use the shared linear icon system.
- Clickable actions must use real button controls, not ordinary QLabel text styled as links.
- `no_changes` is a successful neutral check result and must never increment failure/error statistics.
- `partial` must remain a warning with successful work preserved; it must not be reported as an overall failure.
- File tables must not hide core filename, path, time or action information with generated ellipses.
- New UI must pass Windows screenshot QA at 100%, 125% and 150% DPI, including the supported dark theme.
- Mail lists are for concise summaries; full bodies belong in detail views, and body text must never grow a row beyond its strict bound.
- Received and sent summaries must keep non-zero attachment, inline-image, link and download counts visible even when body text exists.
- Received and sent summary tables must look like unified rows, not independently interactive cells; dark theme must not inherit light item-hover backgrounds or show vertical hover/selection boundaries.
- Inbox search must use mail facts across recipients, readable body, attachment/image names and links; resource matches must never duplicate a mail row.

## Technical log invariants

- Normal automatic no-change checks must not create permanent `app_events` noise; scheduler health belongs in `auto_receive_state`.
- `app_events` retention may delete only technical events. It must never delete business history, outbound records, MCP audit, retry state, mail packages, resources, raw mail or attachments.
- File-log rotation and SQLite event retention are separate mechanisms and must both remain bounded.
- AgentMailBridge v1.1.0 is the current product version. Route B multi-mailbox remains future scope.

## History and managed-file invariants

- `received_messages` is business history; `received_files` is the authoritative source for real received files.
- File management must never derive file sizes from business-history rows.
- Main tables must not display meaningless truncated absolute paths; keep complete paths in DTOs, details and explicit copy/open actions.
- Receive rules must execute in the shared Gmail API/IMAP business-processing layer, and new rules must preserve legacy configuration semantics.
- History and file management have separate responsibilities: history explains business actions, while Files & Data manages stored objects and maintenance.
- User-facing statuses must be productized and localized; raw statuses remain available only in structured details where useful.
- Unknown file size and a real zero-byte file are distinct states and must never share the same display value.

## Development commands

```powershell
python -m pip install -r requirements.txt
python -m pytest -q
python -m agent_mail_bridge --version
python -m agent_mail_bridge.gui
python -m agent_mail_bridge.mcp_server
```

Windows release build:

```powershell
python -m pip install -r requirements-build.txt
powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1
```

The installer source is `packaging/windows/AgentMailBridge.iss`. The single version source is `agent_mail_bridge/version.py`; Python metadata, GUI, MCP, EXE metadata and installer must match it. Do not claim a test passed unless it was actually executed. Before release, run pytest, packaged smoke, secret scan, install/upgrade/uninstall checks, hashes, Defender where available and signature inspection. Never publish a GitHub Release without explicit user approval.
