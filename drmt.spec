# -*- mode: python ; coding: utf-8 -*-
import os
import sys

block_cipher = None

# Bundle read-only web frontend assets and database SQL migration files
datas = [
    ('web', 'web'),
    ('migrations', 'migrations'),
]

# Explicit hidden imports for C-extensions, async drivers, and dynamic imports
hidden_imports = [
    'aiosqlite',
    'pydantic',
    'pydantic_core',
    'pydantic_settings',
    'Crypto',
    'Crypto.Cipher.AES',
    'Crypto.Util.Padding',
    'bcrypt',
    'redis.asyncio',
    'aiofiles',
    'dotenv',
    'multidict',
    'yarl',
    'pyotp',
]

a = Analysis(
    ['main.py'],
    pathex=['.'],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['test_terminal_blocklist', 'test_auth_cooldown'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='drmt',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='drmt',
)
