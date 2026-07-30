# AgentMailBridge agent instructions

## Product boundary

AgentMailBridge is a local-first, single-user, Windows-first email bridge that may be open sourced. Users perceive one product. `AgentMailBridgeMCP.exe` is an internal stdio component started on demand by an Agent and must exit when stdin closes. It must not have a shortcut, tray icon, startup entry, listener or daemon.

Do not expand the project into SaaS, multi-tenant infrastructure, a general email client, a general Gmail MCP or an Agent orchestration platform.

Do not add a draft mode, embedded AI or intent judgment, automatic replies, monitor-triggered sending, scheduled or marketing mail, message deletion/move/read-state mutation, Web API/Webhook, Obsidian/n8n integration, extra dedicated Agents or a GUI framework rewrite.

Do not automatically modify Codex global AGENTS rules, Claude Code user/project instructions, Hermes memory/skills/system prompts or any other Agent's global rules. Managed Client setup may update only its supported MCP configuration entry with preview, backup, conflict detection and rollback.

## Security invariants

- Backward-compatible `submit_result` delivery remains fixed and controlled by `OWNER_GMAIL`.
- General Agent mail sending may target any syntactically valid recipient only when the current registered Client has explicit send permission, the selected sender account is in scope, and the configured confirm or autonomous send mode is enforced.
- Legacy GUI manual send remains a separate user action and does not grant or expand any Agent Client permission.
- Gmail OAuth scope must remain exactly `gmail.readonly`.
- OAuth and Gmail API network operations must never run on the Qt GUI thread.
- OAuth authorization must be cancellable and time-bounded.
- OAuth callback servers must close on success, failure, timeout, cancel and application exit.
- Only Google Desktop installed-app credentials are accepted; Web OAuth credentials are rejected explicitly.
- A failed credentials or Token replacement must preserve the previous working file.
- OAuth authorization URLs, callback state/code, Client Secret and Tokens must never enter logs, reports or diagnostics.
- Successful Token exchange and successful Gmail API verification are distinct states; Gmail API re-verification must reuse a valid Token without reopening browser authorization.
- Real OAuth acceptance requires manual user interaction; Computer Use and browser automation are forbidden.
- MCP archive access is limited to `DATA_ROOT`. Local outbound attachments are limited to the intersection of configured `ALLOWED_SEND_ROOTS` and the current Client's authorized attachment roots.
- A GUI user-selected global file does not expand MCP trust.
- Gmail IMAP and all SMTP/IMAP provider secrets live in Windows Credential Manager and are never echoed back.
- Passwords, authorization codes, OAuth tokens, Client tokens, Credential Manager values and third-party API keys must never enter MCP results, errors, audit details, GUI previews, reports or configuration backups in plaintext.
- `.env`, credentials.json, token.json, secrets, databases, logs, mail and attachments must not enter Git, reports, dist or installers.
- Never silently delete user data, OAuth files or credentials during uninstall.

## Runtime paths

Use `runtime_paths.py`. Frozen program files are read-only under the install directory. Installed configuration, OAuth and data are current-user writable paths under `%LOCALAPPDATA%\AgentMailBridge`. Source mode continues to support the repository `.env`. Do not depend on the current working directory or hard-code a user name or drive.

## Multi-account core invariants

- `mail_accounts.account_id` is the stable ownership and future permission boundary; provider names and email addresses are attributes, not primary identities.
- Existing Gmail receive and Generic/QQ/163 IMAP/SMTP implementations remain adapters behind the unified account model. Do not duplicate or rewrite their protocol logic.
- New mail, mailbox, sync, retry and outbound facts must carry account ownership. Keep `account_ref` only as a backward-compatible v1.3 field.
- Database ownership migration must be transactional, idempotent and preceded by the normal upgrade backup. Do not move old package directories, rewrite raw.eml or recalculate historical hashes merely to change ownership metadata.
- One AgentMailBridge MCP serves all accounts. New reads may filter by `account_id`; do not split provider-specific MCP servers.
- Gmail send and Outlook remain unimplemented. QQ 与 163 已完成真实 E2E，可表述为正式支持；Generic IMAP/SMTP 在独立第三方真实 E2E 通过前必须保持 implementation ready / E2E required。

## Agent permission invariants

- Permissions are granted by the user to one registered Client and are denied by default for existing and newly migrated Clients.
- Read permission independently controls account scope and mailbox scope. Each scope supports `all`, including future enabled objects, or `selected`, which never expands automatically.
- Send permission independently controls sender-account scope and one of exactly two modes: `confirm` or `autonomous`. Do not implement an Agent draft mode.
- General send permission must not inherit from `submit_result`, read permission, legacy Client registration or GUI manual-send capability.
- Authorization order is valid enabled Client, operation gate, account or sender scope, mailbox scope when reading source mail, attachment-root scope, canonical ownership, path safety, then size and SHA-256 verification.
- Paused and revoked Clients are rejected on the next call. Confirmation must revalidate all permissions, scopes, expiry and attachment facts.
- Do not add fixed-recipient, contact, domain, historical-contact, single-recipient, single-attachment or year restrictions.

## Mailbox and history invariants

- Mailbox discovery is provider-neutral and stores a stable mailbox ID, account ownership, raw and display names, hierarchy, delimiter, Special-Use role, enabled state and UIDVALIDITY.
- Support Inbox, Sent, Archive, Drafts as read-only server facts, Spam/Junk, Trash, Important/Starred, provider labels and custom mailboxes. Spam and Trash require explicit user authorization.
- Prefer IMAP Special-Use and Gmail label metadata. Provider-specific names may be auditable fallbacks but must not be the sole identification method.
- Each mailbox has an independent checkpoint. UIDVALIDITY changes invalidate the affected checkpoint without changing historical package identity.
- `all` mailbox scope includes newly discovered enabled mailboxes; `selected` scope does not. Disabled or removed mailboxes cannot be accessed through stale IDs.
- Historical import and search support no time limit, all history, arbitrary natural years, cross-year ranges and custom start/end dates. A year is a query value, never a permission boundary.
- Mailbox synchronization must not delete, move, mark read or otherwise modify server messages.

## General Agent outbound invariants

- General Agent mail supports `new`, `reply`, `reply_all` and `forward` through a small provider-neutral API. Do not overload `submit_result` with these semantics.
- Senders may be any enabled send-capable account authorized for the Client. Recipients may be any syntactically valid addresses, with multiple To, Cc and Bcc values.
- Reject invalid addresses, header injection and ownership forgery. Reply prefers Reply-To over From; reply-all excludes sender identities and deduplicates while preserving To/Cc semantics.
- Reply and forward source packages must be readable by the Client. Original attachments are included only when explicitly selected; local attachments must come from authorized roots.
- `confirm` creates a durable pending request without contacting SMTP or creating a sent fact. Only the GUI user action may confirm or cancel it; MCP must never expose a confirmation operation.
- `autonomous` sends immediately after authorization checks. AgentMailBridge does not interpret user intent or run an embedded model.
- Every request has a Client-scoped idempotency key. Retries return the existing result, confirmed requests send at most once, and `delivery_unknown` is never retried automatically.
- Build MIME once. The exact bytes passed to the provider must become outbound `raw.eml`; Bcc recipients must not appear in public message headers.
- Revalidate attachment existence, ownership, size and SHA-256 immediately before SMTP. Any mismatch blocks sending.
- A confirmed or provider-accepted send must remain recoverable when archive publication fails; record `sent_archive_failed` without automatically resending.

## Outbound archive, Sent and threading invariants

- Received and sent messages are equal first-class mail facts. Every formal message has exactly one package and a direction.
- Outbound packages atomically preserve the exact sent MIME bytes, readable bodies, metadata, attachments, hashes, Client identity, send mode, confirmation fact, idempotency key and provider result.
- Synchronize authorized server Sent mailboxes so messages sent by web, mobile and other clients enter the same archive.
- Reconcile locally sent packages with later Sent synchronization using account ownership, Message-ID, provider identifiers, RFC headers, content and attachment fingerprints, and a bounded time window. Never create a second formal package for the same message.
- Thread relationships are deterministic and may use Message-ID, In-Reply-To, References, provider thread/conversation IDs, `reply_to_package_id` and `forward_from_package_id`. Do not use AI or subject-similarity clustering in the fact layer.
- Existing package IDs, account IDs, resource IDs, raw mail and hashes are immutable during migration.

## MCP reliability invariants

- MCP stdout may contain protocol data only; diagnostics belong on stderr or in file logs.
- MCP stdin, stdout and stderr must be explicitly UTF-8, and Chinese paths, filenames, titles, spaces, BOM input, flush and EOF must be tested.
- Agents must not perform ad-hoc Copy-Item staging. AgentMailBridge validates allowed roots and performs atomic controlled staging.
- Source, staged, pre-SMTP attachment and sent archive size/SHA-256 facts must remain auditable and must block sending on a pre-SMTP mismatch.
- Real packaged MCP and loopback E2E evidence cannot be replaced by mocks; unexecuted external steps must be reported as unverified.
- GUI is not required for local mail reads. The MCP service is provider-neutral and client-neutral.
- Mail read access requires the global read gate plus a valid Client's account and mailbox scopes; never add per-message sharing state.
- Mail tools are read-only against the canonical archive; mail content may be read, but credentials and arbitrary filesystem paths may not.
- `get_mail` and `read_mail_resource` must remain bounded and pageable.
- `prepare_mail_resources` may copy only into an authorized workspace and must preserve source/target size and SHA-256.
- Repeated complete-package preparation for the same `package_id`, target directory and identical manifest/hashes must reuse the unchanged existing copy and return `reused=true`; do not create numbered duplicate copies. If the working copy changed, preserve it and never overwrite silently.
- `submit_result` must remain backward compatible.
- MCP audit must not store complete mail bodies, attachment contents or secrets.

## Automatic receive invariants

- Automatic receive must start promptly, continue in the tray, recover after long pauses and never depend on the manual button.
- Gmail API/IMAP must use overlapping lookback plus Message-ID/database dedupe; prefer a repeated scan over a missed message.
- A single message or attachment failure must not block later mail. Persist finite retry state and keep global connection backoff separate.
- `no_changes` is healthy, never increments failures and never triggers backoff. `partial` preserves successful work and continues normal scheduling.
- True scheduler state, last check/success/result, next check and retry counts must remain observable after refresh and restart.
- Maximize/restore must use the shared linear icon system, support title-bar double click and preserve a normal geometry constrained to the current Windows work area.

## Unified mail archive invariants

- One received or sent message has exactly one formal archive object, and every body, inline image, attachment, link and downloaded file must belong to its `package_id`.
- Mail files must not be written as unrelated loose objects. New mail is staged and atomically promoted into one package directory.
- Inbound `raw.eml` must contain bytes actually obtained from Gmail raw or IMAP `BODY.PEEK[]`; outbound `raw.eml` must contain the exact bytes sent. Never fabricate or rewrite raw content for legacy data.
- Manifest file paths are package-relative and must not escape the package root.
- Links are detected offline by default. Trusted domains are empty by default, and trusted downloads must remain HTTPS-only, redirect-aware and SSRF-safe.
- User-facing text must not expose internal resource enums. Full internal values may appear only in structured diagnostic details where useful.
- Account, mailbox and thread identity must not assume that one account exists forever. Preserve safe fallbacks without incorrectly merging unrelated messages.
- Mail Facts Query is read-only and must not execute, send, delete, move or modify messages or resources.
- AgentMailBridge does not provide knowledge management, Obsidian-specific behavior or Agent orchestration.
- Keep existing receive, history and Files & Data compatibility behavior stable while adding outbound facts and complete timelines.

## Frontend information architecture

- The top-level work area contains only Receive and Send.
- Agent Integration is an independent left-side entry joined visually with History, Files & Data, Settings and About; it must not leave a detached gap.
- Existing account configuration belongs only to unified account cards; the current Gmail and QQ cards open their provider-specific authentication views.
- Add mailbox account creates a new unified account; it must not overwrite or silently route into an existing account identity.
- The receive page must not contain account secrets or OAuth configuration.
- The send page must not contain QQ account configuration.
- Agent Integration owns one independent page and must not be duplicated in Send or Settings.
- Agent Integration owns per-Client read scope, mailbox scope, send scope, send mode and attachment-root authorization.
- Pending Send is an Agent Integration workflow. It shows full content and attachment facts and permits only user confirmation, cancellation or return to the Agent for regeneration.
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
- Recent MCP calls and compact resource tables are unified rows with no vertical cell boundaries, focus bars or selection blocks.
- Literal `|` or `｜` separators must not be used for the recent-call layout.

## Technical log invariants

- Normal automatic no-change checks must not create permanent `app_events` noise; scheduler health belongs in `auto_receive_state`.
- `app_events` retention may delete only technical events. It must never delete business history, outbound records, MCP audit, retry state, mail packages, resources, raw mail or attachments.
- File-log rotation and SQLite event retention are separate mechanisms and must both remain bounded.
- AgentMailBridge v1.7.1 is the current product version. It hardens v1.7.0 with durable mail facts and multi-mailbox membership, send leases and crash recovery, conservative Sent reconciliation, protected checkpoints, bounded temporary data, consistency repair and long-running health visibility.
- QQ and 163 remain formally supported for real receive/send. Gmail remains receive-only with exactly `gmail.readonly`. Generic remains implementation ready / E2E required until an independent provider E2E passes. Outlook/Microsoft, Gmail send and ordinary Claude Desktop remain outside this release scope.

## Agent integration invariants

- Anonymous or unknown MCP clients are denied by default; every packaged MCP session must authenticate with a registered client identity.
- Client tokens are independently generated, hashed for validation, stored in Windows Credential Manager for GUI-managed reuse, never logged, and never grant access to mailbox credentials.
- Read and send authorization use their independent gates and scopes defined above; neither permission implies the other.
- Paused and revoked clients are rejected on the next call without restarting other clients.
- Managed Client configuration requires redacted preview, backup, hash/mtime conflict detection, atomic replacement and rollback; removal may delete only the AgentMailBridge entry.

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

For v1.7.1, develop directly on `master`. Do not create a branch, force-push, rewrite history, create a Tag or publish a GitHub Release. Commit the completed implementation, tests, migrations, GUI, documentation and final report normally, then push to `origin/master`.
