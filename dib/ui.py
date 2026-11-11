"""Tk window, game lifecycle, and single-instance handling."""
from __future__ import annotations

import ctypes
import json
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from ctypes import wintypes

import tkinter as tk
import win32api
import win32con
from PIL import Image, ImageTk
from tkinter import messagebox, scrolledtext, ttk

from . import config
from .display import dpi_context
from .instance import acquire_instance, claim_instance_mutex, publish_version
from .overlay import BilliardsOverlay
from .paths import runtime_directory
from .runner import BilliardsRunner
from .shell import LayoutSession, ShellDesktop
from .update import fetch_update, release_entries

BG = '#091411'
PANEL = '#172a24'
TEXT = '#f0fbf3'
MUTED = '#a5beb1'
ACCENT = '#a6f47a'
GOLD = '#f5c56d'

HINTS = {
    'ready': '瞄准 · 长按蓄力 · 松手击球',
    'stroke': '击球',
    'rolling': '图标滚动中',
    'return_white': '白球入袋 · 正在摆回',
    'ball_in_hand': '白球入袋 · 点击空处摆球',
    'finished': '清台 · 本局完成',
}


def _write_log(name, text):
    try:
        (runtime_directory() / name).write_text(text, encoding='utf-8')
    except OSError:
        pass


class DesktopBilliardsApp:
    """Own the Tk window, the layout journal and the animation runner."""

    def __init__(self, root, instance_handle):
        self.root = root
        self.instance_handle = instance_handle
        self.runner = None
        self.session = None
        self.watchdog = None
        self.donation = None
        self.closing = False
        self.player_count = 1
        self.deadline = 0.0
        self.journal = runtime_directory() / 'layout.json'
        root.title(config.WINDOW_TITLE)
        root.configure(bg=BG)
        root.resizable(False, False)
        try:
            root.iconbitmap(str(config.ICON_PATH))
        except tk.TclError:
            pass
        ctypes.windll.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-2))
        publish_version(root, config.VERSION)
        style = ttk.Style(root)
        style.theme_use('clam')
        style.configure('Panel.TFrame', background=BG)
        style.configure('Stats.TFrame', background=PANEL)
        style.configure('Table.TLabel', background=BG, foreground=TEXT,
                        font=('Microsoft YaHei UI', 9))
        style.configure('Muted.TLabel', background=BG, foreground=MUTED,
                        font=('Microsoft YaHei UI', 8))
        style.configure('Hero.TLabel', background=BG, foreground=TEXT,
                        font=('Microsoft YaHei UI', 17, 'bold'))
        style.configure('Status.TLabel', background=PANEL, foreground=ACCENT,
                        font=('Microsoft YaHei UI', 10, 'bold'), padding=(12, 9))
        style.configure('StatTitle.TLabel', background=PANEL, foreground=MUTED,
                        font=('Microsoft YaHei UI', 9))
        style.configure('StatValue.TLabel', background=PANEL, foreground=TEXT,
                        font=('Segoe UI', 23, 'bold'))
        style.configure('Score.TLabel', background=PANEL, foreground=MUTED,
                        font=('Microsoft YaHei UI', 10, 'bold'), padding=(12, 8))
        style.configure('Active.Score.TLabel', background='#36572c', foreground='#e2ffcb')
        style.configure('Winner.Score.TLabel', background='#5b4424', foreground='#fff0c6')
        style.configure('Table.TButton', padding=(11, 6), font=('Microsoft YaHei UI', 9),
                        background=PANEL, foreground=TEXT, borderwidth=0,
                        focusthickness=1, focuscolor=ACCENT)
        style.map('Table.TButton',
                  background=[('active', '#1d3531'), ('pressed', '#24423c')],
                  foreground=[('disabled', MUTED)])
        style.configure('Primary.Table.TButton', background=ACCENT, foreground=BG)
        style.map('Primary.Table.TButton', background=[('pressed', '#87d15c'), ('active', '#c3ffa0')])
        style.configure('GuideOn.Table.TButton', background='#234e42', foreground='#a5f8d5')
        style.map('GuideOn.Table.TButton', background=[('pressed', '#1d4438'), ('active', '#2c5e50')])
        style.configure('GuideOff.Table.TButton', background='#303d3b', foreground='#c0cecb')
        style.map('GuideOff.Table.TButton', background=[('pressed', '#25302e'), ('active', '#3d4b48')])
        style.configure('Donate.Table.TButton', foreground=GOLD)

        tk.Frame(root, bg=ACCENT, height=3).pack(fill='x')
        hero = ttk.Frame(root, padding=(16, 10, 16, 9), style='Panel.TFrame')
        hero.pack(fill='x')
        ttk.Label(hero, text=config.PRODUCT_NAME, style='Hero.TLabel').pack(side='left')
        ttk.Label(hero, text=f'v{config.VERSION}', style='Muted.TLabel').pack(side='right')
        controls = ttk.Frame(root, padding=(16, 0, 16, 10), style='Panel.TFrame')
        controls.pack(fill='x')
        self.mode_buttons = []
        for players, title in ((1, '单人'), (2, '双人')):
            button = ttk.Button(controls, text=title, width=5,
                                command=lambda count=players: self.change_mode(count),
                                style='Primary.Table.TButton' if players == 1 else 'Table.TButton')
            button.pack(side='left', padx=(0, 4))
            self.mode_buttons.append(button)
        self.status = tk.StringVar(root, value='正在摆球…')
        self.guide_enabled = True
        self.guide_button = ttk.Button(controls, text='辅助线 · 开', width=9, command=self.toggle_guide,
                                       style='GuideOn.Table.TButton')
        self.guide_button.pack(side='right')
        self.sound_enabled = False
        self.sound_button = ttk.Button(controls, text='声音 · 关', width=8, command=self.toggle_sound,
                                       style='GuideOff.Table.TButton')
        self.sound_button.pack(side='right', padx=(0, 6))
        ttk.Label(root, textvariable=self.status, width=33, style='Status.TLabel').pack(
            fill='x', padx=16, pady=(0, 6))

        self.scoreboard = ttk.Frame(root, style='Panel.TFrame')
        self.score_labels = []
        for player in range(2):
            label = ttk.Label(self.scoreboard, text=f'玩家 {player + 1}   0 分', style='Score.TLabel')
            label.pack(side='left', fill='x', expand=True, padx=(0, 3) if player == 0 else (3, 0))
            self.score_labels.append(label)

        stats = ttk.Frame(root, style='Panel.TFrame')
        stats.pack(fill='x', padx=16)
        self.stats = stats
        self.time_title = tk.StringVar(root, value='本局用时')
        self.time_value = tk.StringVar(root, value='00:00')
        self.count_title = tk.StringVar(root, value='图标入袋')
        self.count_value = tk.StringVar(root, value='—')
        remaining_title = tk.StringVar(root, value='剩余图标')
        self.remaining_value = tk.StringVar(root, value='—')
        for column, (title, value) in enumerate(((self.time_title, self.time_value),
                                                (self.count_title, self.count_value),
                                                (remaining_title, self.remaining_value))):
            stats.columnconfigure(column, weight=1, uniform='stats')
            card = ttk.Frame(stats, padding=(11, 7), style='Stats.TFrame')
            card.grid(row=0, column=column, sticky='nsew', padx=(0, 6) if column < 2 else 0)
            ttk.Label(card, textvariable=title, style='StatTitle.TLabel').pack(anchor='w')
            ttk.Label(card, textvariable=value, style='StatValue.TLabel').pack(anchor='w')

        bar = ttk.Frame(root, padding=(14, 10, 14, 7), style='Panel.TFrame')
        bar.pack(fill='x')
        ttk.Button(bar, text='重新开始', command=self.restart, style='Primary.Table.TButton').pack(side='left')
        self.update_button = ttk.Button(bar, text='检查更新', command=lambda: self.check_updates(manual=True),
                                        style='Table.TButton')
        self.update_button.pack(side='left', padx=(6, 0))
        ttk.Button(bar, text='退出', command=self.close, style='Table.TButton').pack(side='right')
        ttk.Button(bar, text='打赏', command=self.show_donation,
                   style='Donate.Table.TButton').pack(side='right', padx=(0, 6))
        footer = ttk.Frame(root, padding=(14, 0, 14, 9), style='Panel.TFrame')
        footer.pack(fill='x')
        ttk.Label(footer, text='Esc / Ctrl+Alt+X 退出并恢复桌面', style='Muted.TLabel').pack(side='left')
        self.update_results = queue.SimpleQueue()
        self.update_feedback = tk.StringVar(root)
        ttk.Label(footer, textvariable=self.update_feedback, width=14, anchor='e',
                  style='Muted.TLabel').pack(side='right', padx=(0, 8))
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.bind('<Escape>', lambda event: self.close())
        root.update_idletasks()
        monitor = win32api.MonitorFromPoint((0, 0), win32con.MONITOR_DEFAULTTOPRIMARY)
        left, top, right, _ = win32api.GetMonitorInfo(monitor)['Work']
        root.geometry(f'+{max(left, right - root.winfo_reqwidth() - 18)}+{top + 8}')
        self.hotkey = bool(ctypes.windll.user32.RegisterHotKey(
            None, config.HOTKEY_ID, config.HOTKEY_MODIFIERS, config.HOTKEY_VK))
        root.after(20, self.start)
        root.after(1500, self.check_updates)

    def show_donation(self):
        if self.donation is not None and self.donation.winfo_exists():
            self.donation.lift()
            return
        try:
            with Image.open(config.ASSETS / 'donation.png') as source:
                image = source.convert('RGB')
            image.thumbnail((340, 340), Image.Resampling.LANCZOS)
        except OSError:
            messagebox.showwarning('打赏', '暂时无法打开赞赏图片。', parent=self.root)
            return
        self.donation = dialog = tk.Toplevel(self.root)
        dialog.title('打赏')
        dialog.transient(self.root)
        dialog.resizable(False, False)
        body = ttk.Frame(dialog, padding=14, style='Panel.TFrame')
        body.pack()
        ttk.Label(body, text='微信扫码赞赏，感谢支持！', style='Table.TLabel').pack(pady=(0, 10))
        photo = ImageTk.PhotoImage(image, master=dialog)
        label = ttk.Label(body, image=photo)
        label.image = photo
        label.pack()
        ttk.Button(body, text='关闭', command=dialog.destroy, style='Table.TButton').pack(pady=(10, 0))
        dialog.bind('<Escape>', lambda event: dialog.destroy())

    def check_updates(self, manual=False):
        if self.closing or self.update_button.instate(('disabled',)):
            return
        self.update_button.configure(state='disabled', text='检查中…')
        self.update_feedback.set('')

        def fetch():
            try:
                info = fetch_update(config.UPDATE_URL, config.VERSION)
            except Exception:
                _write_log('update.log', traceback.format_exc())
                self.update_results.put((False, None))
            else:
                self.update_results.put((True, info))

        threading.Thread(target=fetch, name='Billiards-update', daemon=True).start()
        self.root.after(100, self.finish_update_check, manual)

    def finish_update_check(self, manual):
        if self.closing:
            return
        try:
            success, info = self.update_results.get_nowait()
        except queue.Empty:
            self.root.after(100, self.finish_update_check, manual)
            return
        self.update_button.configure(state='normal', text='检查更新')
        if info:
            self.show_update(info)
        elif manual:
            self.update_feedback.set('已是最新版' if success else '检查失败，请稍后重试')

    def show_update(self, info):
        dialog = tk.Toplevel(self.root)
        dialog.title(f'发现新版本 {info["version"]}')
        dialog.transient(self.root)
        body = ttk.Frame(dialog, padding=14)
        body.pack(fill='both', expand=True)
        notes = scrolledtext.ScrolledText(body, width=46, height=12, wrap='word')
        notes.pack(fill='both', expand=True)
        notes.tag_configure('latest', font=('Microsoft YaHei UI', 10, 'bold'))
        for index, entry in enumerate(release_entries(info)):
            notes.insert('end', f'{entry["version"]}\n', 'latest' if index == 0 else ())
            notes.insert('end', (entry['notes'] or '暂无更新说明') + '\n\n')
        notes.configure(state='disabled')

        def download():
            if webbrowser.open(info['download_url']):
                dialog.destroy()
            else:
                messagebox.showerror('打开下载失败', '请检查默认浏览器设置。', parent=dialog)

        ttk.Button(body, text='去更新', command=download).pack(side='right', pady=(10, 0))
        ttk.Button(body, text='稍后', command=dialog.destroy).pack(side='right', padx=6, pady=(10, 0))
        dialog.geometry(f'+{self.root.winfo_x()}+{self.root.winfo_y()+self.root.winfo_height()+10}')

    # -- game lifecycle ----------------------------------------------------
    def start(self):
        if self.closing:
            return
        try:
            desktop = ShellDesktop()
            # Recover a journal left behind by an earlier crash before starting.
            self.session = LayoutSession(desktop, self.journal)
            self.session.restore()
            self.session = LayoutSession(desktop, self.journal)
            items = self.session.begin(overlay_only=True)
            for item in items:
                item['name'] = desktop.folder.GetDisplayNameOf(
                    [desktop.cache[item['id']]], desktop.shellcon.SHGDN_NORMAL)
            with dpi_context(-4):
                overlay = BilliardsOverlay(desktop, items)
                overlay.guide_enabled = self.guide_enabled
                overlay.sound_enabled = self.sound_enabled
                if self.player_count == 2:
                    overlay.request_restart(self.player_count)
            self.watchdog = self.spawn_watchdog()
            self.runner = BilliardsRunner(overlay)
            self.runner.start()
            self.deadline = time.monotonic() + 30
            self.poll()
        except Exception as exc:
            self.fail(exc)

    def spawn_watchdog(self):
        frozen = bool(getattr(sys, 'frozen', False))
        command = [sys.executable]
        if not frozen:
            command.append(str(config.BASE / 'app.pyw'))
        command.extend(['--watchdog', str(self.journal), self.session.snapshot['session_id'], str(os.getpid())])
        environment = {**os.environ, 'PYINSTALLER_RESET_ENVIRONMENT': '1'}
        return subprocess.Popen(command, env=environment,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW,
                                cwd=str(runtime_directory()))

    def poll(self):
        if self.closing:
            return
        try:
            if self.hotkey:
                message = wintypes.MSG()
                if ctypes.windll.user32.PeekMessageW(ctypes.byref(message), None, 0x312, 0x312, 1) \
                        and message.wParam == config.HOTKEY_ID:
                    self.close()
                    return
            if self.runner.error:
                raise self.runner.error
            if not self.runner.shown.is_set():
                if time.monotonic() > self.deadline:
                    raise RuntimeError('等待桌面恢复守护就绪超时。')
                ready = self.journal.with_name(self.journal.stem + '.watchdog.json')
                if ready.exists():
                    data = json.loads(ready.read_text(encoding='utf-8'))
                    if data.get('session_id') == self.session.snapshot['session_id'] and self.runner.prepared.is_set():
                        if not data.get('ready'):
                            raise RuntimeError('无法启动桌面恢复守护。')
                        self.runner.request_show(self.session.snapshot, self.journal)
            else:
                overlay = self.runner.overlay
                table = overlay.table
                state = table.state
                label = HINTS.get(state, state)
                if overlay.transition:
                    label = '正在摆球…'
                elif state == 'rolling' and table.white_sunk:
                    label = '白球入袋 · 等其他球停稳'
                elif state == 'ready' and table.charge:
                    label = '松手击球'
                if table.player_count == 2:
                    scores = tuple(table.scores)
                    winner = None
                    if state == 'finished':
                        if scores[0] == scores[1]:
                            label = '清台 · 平局'
                        else:
                            winner = scores.index(max(scores))
                            label = f'清台 · 玩家 {winner + 1} 获胜'
                    for player, score_label in enumerate(self.score_labels):
                        active = state != 'finished' and player == table.current_player
                        score_style = 'Winner.Score.TLabel' if player == winner else (
                            'Active.Score.TLabel' if active else 'Score.TLabel')
                        score_label.configure(text=f'玩家 {player + 1}   {scores[player]} 分', style=score_style)
                self.status.set(label)
                minutes, seconds = divmod(table.round_seconds, 60)
                self.time_value.set(f'{minutes:02d}:{seconds:02d}')
                total = len(table.icons)
                pocketed = table.pocketed_icons
                finished = state == 'finished'
                self.time_title.set('总用时' if finished else '本局用时')
                self.count_title.set('图标总数' if finished else '图标入袋')
                self.count_value.set(str(total) if finished else f'{pocketed}/{total}')
                self.remaining_value.set(str(total - pocketed))
            self.root.after(50, self.poll)
        except Exception as exc:
            self.fail(exc)

    def restart(self):
        if self.runner:
            self.runner.overlay.request_restart(self.player_count)

    def change_mode(self, player_count):
        if player_count == self.player_count:
            return
        self.player_count = player_count
        for players, button in enumerate(self.mode_buttons, 1):
            button.configure(style='Primary.Table.TButton' if players == player_count else 'Table.TButton')
        if player_count == 2:
            for player, label in enumerate(self.score_labels):
                label.configure(text=f'玩家 {player + 1}   0 分',
                                style='Active.Score.TLabel' if player == 0 else 'Score.TLabel')
            self.scoreboard.pack(before=self.stats, fill='x', padx=16, pady=(0, 6))
        else:
            self.scoreboard.pack_forget()
        self.restart()

    def toggle_guide(self):
        self.guide_enabled = not self.guide_enabled
        self.guide_button.configure(text='辅助线 · 开' if self.guide_enabled else '辅助线 · 关',
                                    style='GuideOn.Table.TButton' if self.guide_enabled else 'GuideOff.Table.TButton')
        if self.runner:
            self.runner.overlay.guide_enabled = self.guide_enabled

    def toggle_sound(self):
        self.sound_enabled = not self.sound_enabled
        self.sound_button.configure(text='声音 · 开' if self.sound_enabled else '声音 · 关',
                                    style='GuideOn.Table.TButton' if self.sound_enabled else 'GuideOff.Table.TButton')
        if self.runner:
            self.runner.overlay.sound_enabled = self.sound_enabled

    def fail(self, error):
        try:
            self.restore()
        except Exception as exc:
            self.status.set('请再次点击退出')
            messagebox.showerror(config.PRODUCT_NAME, f'{error}\n\n{exc}', parent=self.root)
            return
        _write_log('error.log', traceback.format_exc())
        messagebox.showerror(config.PRODUCT_NAME, str(error), parent=self.root)
        self.close()

    def restore(self):
        if self.runner:
            self.runner.stop()
            self.runner = None
        if self.session:
            self.session.restore()
            self.session = None

    def close(self):
        if self.closing:
            return
        try:
            self.restore()
        except Exception as exc:
            self.status.set(str(exc))
            messagebox.showwarning(config.PRODUCT_NAME, f'桌面恢复未完成：{exc}\n\n请重试退出。',
                                   parent=self.root)
            return
        self.closing = True
        if self.hotkey:
            ctypes.windll.user32.UnregisterHotKey(None, config.HOTKEY_ID)
        ctypes.windll.kernel32.CloseHandle(self.instance_handle)
        self.root.destroy()


def main():
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
    app = None
    desktop_guard = None
    try:
        handle = acquire_instance(config.VERSION)
        desktop_guard = claim_instance_mutex(config.DESKTOP_GUARD)
        if desktop_guard is None:
            ctypes.windll.kernel32.CloseHandle(handle)
            root = tk.Tk()
            root.withdraw()
            messagebox.showinfo(config.PRODUCT_NAME,
                                '检测到「桌面蹦迪」正在运行。\n请先退出它，再打开桌面图标台球。',
                                parent=root)
            root.destroy()
            return
        root = tk.Tk()
        app = DesktopBilliardsApp(root, handle)
        root.mainloop()
    except SystemExit:
        raise
    except Exception:
        _write_log('error.log', traceback.format_exc())
        if app is not None:
            try:
                app.close()
            except Exception:
                pass
        else:
            try:
                messagebox.showerror(config.PRODUCT_NAME, '启动失败，详情已写入日志。')
            except Exception:
                pass
        raise
    finally:
        if desktop_guard:
            ctypes.windll.kernel32.CloseHandle(desktop_guard)
