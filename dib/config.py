"""Product constants shared by the window, the single-instance guard and the build."""
from __future__ import annotations

import os
from pathlib import Path

from . import __version__

# Single source of truth for the release version. The build script reads it.
VERSION: str = __version__
VERSION_PARTS = tuple(int(part) for part in VERSION.split('.'))
if len(VERSION_PARTS) != 3 or any(part < 0 for part in VERSION_PARTS):
    raise RuntimeError(f'Invalid version: {VERSION!r}')

PRODUCT_NAME = '桌面图标台球'
PRODUCT_NAME_EN = 'Desktop Icon Billiards'
INTERNAL_NAME = 'DesktopIconBilliards'
AUTHOR = 'LXG'
WINDOW_TITLE = f'{PRODUCT_NAME} {VERSION}'

BASE = Path(__file__).resolve().parent.parent
ASSETS = BASE / 'assets'
ICON_PATH = ASSETS / 'desktop-icon-billiards.ico'
TABLE_STYLES = {'photo': '木边绿绒', 'classic': '经典桌面'}


def load_table_style():
    directory = os.environ.get('LOCALAPPDATA')
    if directory:
        try:
            style = (Path(directory) / INTERNAL_NAME / 'table-style.txt').read_text(encoding='utf-8').strip()
            if style in TABLE_STYLES:
                return style
        except (OSError, UnicodeError):
            pass
    return 'photo'


def save_table_style(style):
    directory = os.environ.get('LOCALAPPDATA')
    if directory:
        folder = Path(directory) / INTERNAL_NAME
        folder.mkdir(parents=True, exist_ok=True)
        temporary = folder / 'table-style.tmp'
        temporary.write_text(style, encoding='utf-8')
        temporary.replace(folder / 'table-style.txt')

DEFAULT_UPDATE_URL = 'https://go.liuxiaogang.com/desktop-icon-billiards-update'
UPDATE_URL = os.environ.get('DIB_UPDATE_URL', DEFAULT_UPDATE_URL).strip()

# Separate app identity and a shared guard for the Explorer icon layer.
DESKTOP_GUARD = r'Local\MusicRhythmNativeV2'
MUTEX = r'Local\DesktopIconBilliards'
VERSION_PROPERTY = 'DesktopIconBilliards.Version'
EXE_PATTERN = r'DesktopIconBilliards-(\d+\.\d+\.\d+)-win-x64\.exe'

# Ctrl+Alt+X stops the game, restores the desktop and exits.
HOTKEY_ID = 0x5A32
HOTKEY_MODIFIERS = 0x4000 | 0x2 | 0x1  # MOD_NOREPEAT | MOD_CONTROL | MOD_ALT
HOTKEY_VK = 0x58  # X
