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
