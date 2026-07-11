# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


ROOT = Path(SPECPATH).parent.parent
ICON = ROOT / "agent_mail_bridge" / "resources" / "branding" / "agentmailbridge.ico"
VERSION_FILE = ROOT / "packaging" / "windows" / "version_info.txt"
MCP_VERSION_FILE = ROOT / "packaging" / "windows" / "version_info_mcp.txt"
RESOURCE_DIR = ROOT / "agent_mail_bridge" / "resources"

common_datas = [
    (str(RESOURCE_DIR), "agent_mail_bridge/resources"),
    (str(ROOT / "LICENSE"), "."),
    (str(ROOT / "THIRD_PARTY_NOTICES.md"), "."),
]
common_datas += collect_data_files("certifi")
common_hiddenimports = (
    collect_submodules("googleapiclient")
    + collect_submodules("google_auth_oauthlib")
    + collect_submodules("google.auth")
)

gui_analysis = Analysis(
    [str(ROOT / "agent_mail_bridge" / "gui.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=common_datas,
    hiddenimports=common_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
gui_pyz = PYZ(gui_analysis.pure)
gui_exe = EXE(
    gui_pyz,
    gui_analysis.scripts,
    [],
    exclude_binaries=True,
    name="AgentMailBridge",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON),
    version=str(VERSION_FILE),
)

mcp_analysis = Analysis(
    [str(ROOT / "agent_mail_bridge" / "mcp_server.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=common_datas,
    hiddenimports=common_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["PySide6"],
    noarchive=False,
    optimize=0,
)
mcp_pyz = PYZ(mcp_analysis.pure)
mcp_exe = EXE(
    mcp_pyz,
    mcp_analysis.scripts,
    [],
    exclude_binaries=True,
    name="AgentMailBridgeMCP",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON),
    version=str(MCP_VERSION_FILE),
)

dist = COLLECT(
    gui_exe,
    mcp_exe,
    gui_analysis.binaries,
    gui_analysis.datas,
    mcp_analysis.binaries,
    mcp_analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="AgentMailBridge",
)
