"""Tk window, game lifecycle, and single-instance handling."""
from __future__ import annotations

import ctypes
import ipaddress
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
import win32gui
from PIL import Image, ImageTk
from tkinter import messagebox, scrolledtext, ttk

from . import config
from .display import dpi_context
from .glass import StatCard, frost_style
from .instance import acquire_instance, claim_instance_mutex, publish_version
from .lan import PORT, LanRoom, local_addresses
from .overlay import BilliardsOverlay
from .paths import runtime_directory
from .runner import BilliardsRunner
from .shell import LayoutSession, ShellDesktop
from .update import fetch_update, release_entries

BG = '#101921'
PANEL = '#1e2c37'
TEXT = '#eff8fa'
MUTED = '#aabec9'
ACCENT = '#b8efd9'
GOLD = '#f0d3a5'
FROST = ('#2a3b48', '#1a2732', '#435967')
MINT_FROST = ('#30534f', '#203b39', '#4c7168')

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
        self.lan_room = None
        self.lan_dialog = None
        self.lan_status = tk.StringVar(root)
        self.closing = False
        self.player_count = 1
        self.table_style = config.load_table_style()
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
        root.frost_images = []
        gap = max(6, round(root.winfo_fpixels('1i') / 16))
        style.configure('Panel.TFrame', background=BG)
        style.configure('Table.TLabel', background=BG, foreground=TEXT,
                        font=('Microsoft YaHei UI', 9))
        style.configure('Muted.TLabel', background=BG, foreground=MUTED,
                        font=('Microsoft YaHei UI', 8))
        style.configure('Hero.TLabel', background=BG, foreground=TEXT,
                        font=('Microsoft YaHei UI', 17, 'bold'))
        frost_style(style, root, 'Status.TLabel', 'Label', MINT_FROST)
        style.configure('Status.TLabel', foreground=ACCENT,
                        font=('Microsoft YaHei UI', 10, 'bold'), padding=(13, 9))
        for name, palette, color in (
            ('Score.TLabel', FROST, MUTED),
            ('Active.Score.TLabel', MINT_FROST, ACCENT),
            ('Winner.Score.TLabel', ('#554534', '#392d25', '#806c52'), GOLD),
        ):
            frost_style(style, root, name, 'Label', palette)
            style.configure(name, foreground=color, font=('Microsoft YaHei UI', 10, 'bold'), padding=(12, 7))
        style.configure('Table.TButton', padding=(11, 6), font=('Microsoft YaHei UI', 9),
                        foreground=TEXT)
        style.map('Table.TButton', foreground=[('disabled', MUTED)])
        variants = (
            ('disabled', ('#202b34', '#19242e', '#2c3d48')),
            ('pressed', ('#253644', '#17252f', '#53727d')),
            ('focus', ('#344d59', '#263a46', '#8bc6b8')),
            ('active', ('#3a4e5b', '#283c48', '#5f7d89')),
        )
        frost_style(style, root, 'Table.TButton', 'Button', FROST, variants)
        frost_style(style, root, 'GuideOn.Table.TButton', 'Button', MINT_FROST, variants)
        frost_style(style, root, 'Primary.Table.TButton', 'Button', ('#bcebd9', '#91cbb9', '#e3fff3'), (
            variants[0],
            ('pressed', ('#8ec6b5', '#75a999', '#c5f3e4')),
            ('focus', ('#c9f6e6', '#a8dbcc', '#ffffff')),
            ('active', ('#d5faed', '#a5dfce', '#f1fff9')),
        ))
        style.configure('Primary.Table.TButton', foreground='#102d28')
        style.configure('GuideOn.Table.TButton', foreground=ACCENT)
        style.configure('GuideOff.Table.TButton', foreground=MUTED)
        style.configure('Donate.Table.TButton', foreground=GOLD)
        style.configure('Table.TCombobox', foreground=TEXT, background=PANEL, arrowcolor=ACCENT,
                        bordercolor='#435967', lightcolor=PANEL, darkcolor=PANEL,
                        padding=5, font=('Microsoft YaHei UI', 9))
        style.map('Table.TCombobox', fieldbackground=[('readonly', PANEL), ('disabled', BG)],
                  selectbackground=[('readonly', PANEL)], selectforeground=[('readonly', TEXT)])

        tk.Frame(root, bg='#4c7168', height=1).pack(fill='x')
        hero = ttk.Frame(root, padding=(18, 14, 18, 12), style='Panel.TFrame')
        hero.pack(fill='x')
        ttk.Label(hero, text=config.PRODUCT_NAME, style='Hero.TLabel').pack(side='left')
        ttk.Label(hero, text=f'v{config.VERSION}', style='Muted.TLabel').pack(side='right')
        self.table_picker = ttk.Combobox(hero, values=tuple(config.TABLE_STYLES.values()), width=10,
                                        state='readonly', style='Table.TCombobox')
        self.table_picker.set(config.TABLE_STYLES[self.table_style])
        self.table_picker.pack(side='right', padx=(8, 9))
        self.table_picker.bind('<<ComboboxSelected>>', self.change_table_style)
        controls = ttk.Frame(root, padding=(16, 0, 16, 10), style='Panel.TFrame')
        controls.pack(fill='x')
        self.mode_buttons = []
        for players, title in ((1, '单人'), (2, '双人')):
            button = ttk.Button(controls, text=title, width=5,
                                command=lambda count=players: self.change_mode(count),
                                style='Primary.Table.TButton' if players == 1 else 'Table.TButton')
            button.pack(side='left', padx=(0, gap))
            self.mode_buttons.append(button)
        self.status = tk.StringVar(root, value='正在摆球…')
        self.guide_enabled = True
        self.guide_button = ttk.Button(controls, text='辅助线 · 开', width=9, command=self.toggle_guide,
                                       style='GuideOn.Table.TButton')
        self.guide_button.pack(side='right')
        self.sound_enabled = False
        self.sound_button = ttk.Button(controls, text='声音 · 关', width=8, command=self.toggle_sound,
                                       style='GuideOff.Table.TButton')
        self.sound_button.pack(side='right', padx=(0, gap))
        ttk.Label(root, textvariable=self.status, width=33, style='Status.TLabel').pack(
            fill='x', padx=16, pady=(0, 9))

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
            card = StatCard(stats, title, value, BG, MINT_FROST if column == 1 else FROST,
                            MUTED, ACCENT if column == 1 else TEXT)
            card.grid(row=0, column=column, sticky='nsew', padx=(0, gap) if column < 2 else 0)

        bar = ttk.Frame(root, padding=(14, 10, 14, 7), style='Panel.TFrame')
        bar.pack(fill='x')
        self.restart_button = ttk.Button(bar, text='重新开始', command=self.restart, style='Primary.Table.TButton')
        self.restart_button.pack(side='left')
        self.lan_button = ttk.Button(bar, text='局域网', command=self.show_lan, style='Table.TButton')
        self.lan_button.pack(side='left', padx=(gap, 0))
        self.update_button = ttk.Button(bar, text='检查更新', command=lambda: self.check_updates(manual=True),
                                        style='Table.TButton')
        self.update_button.pack(side='left', padx=(gap, 0))
        ttk.Button(bar, text='退出', command=self.close, style='Table.TButton').pack(side='right')
        ttk.Button(bar, text='打赏', command=self.show_donation,
                   style='Donate.Table.TButton').pack(side='right', padx=gap)
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
        hwnd = win32gui.GetAncestor(root.winfo_id(), win32con.GA_ROOT)
        dark = ctypes.c_int(1)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), 20, ctypes.byref(dark), ctypes.sizeof(dark))
        monitor = win32api.MonitorFromPoint((0, 0), win32con.MONITOR_DEFAULTTOPRIMARY)
        left, top, right, _ = win32api.GetMonitorInfo(monitor)['Work']
        root.geometry(f'+{max(left, right - root.winfo_reqwidth() - 18)}+{top + 8}')
        self.hotkey = bool(ctypes.windll.user32.RegisterHotKey(
            None, config.HOTKEY_ID, config.HOTKEY_MODIFIERS, config.HOTKEY_VK))
        root.after(20, self.start)
        root.after(1500, self.check_updates)

    def show_lan(self):
        if self.lan_dialog is not None and self.lan_dialog.winfo_exists():
            self.lan_dialog.lift()
            return
        self.lan_dialog = dialog = tk.Toplevel(self.root)
        dialog.title('局域网对战')
        dialog.transient(self.root)
        dialog.resizable(False, False)
        panel = ttk.Frame(dialog, padding=16)
        panel.pack(fill='both', expand=True)
        addresses = '、'.join(local_addresses()) or '请用 ipconfig 查看本机 IPv4 地址'
        ttk.Label(panel, text=f'本机 IPv4：{addresses}', wraplength=340).pack(anchor='w')
        ttk.Label(panel, text=f'双方需处于同一局域网。房主共享球面图标，使用 TCP {PORT}。',
                  wraplength=340).pack(anchor='w', pady=(5, 12))
        entry = ttk.Entry(panel, width=27)
        entry.pack(anchor='w', fill='x')
        ttk.Label(panel, text='房主 IPv4 地址填在上方，8 位房间码填在下方。').pack(anchor='w', pady=(4, 4))
        code = ttk.Entry(panel, width=12)
        code.pack(anchor='w', pady=(0, 8))
        actions = ttk.Frame(panel)
        actions.pack(fill='x')
        ttk.Button(actions, text='创建房间', command=lambda: self.start_lan(True)).pack(side='left')
        ttk.Button(actions, text='加入房间', command=lambda: self.start_lan(False, entry.get(), code.get())).pack(
            side='left', padx=6)
        ttk.Button(actions, text='离开房间', command=self.leave_lan).pack(side='right')
        ttk.Label(panel, textvariable=self.lan_status, wraplength=340).pack(anchor='w', pady=(10, 0))
        for field in (entry, code):
            field.bind('<Return>', lambda event: self.start_lan(False, entry.get(), code.get()))
        dialog.bind('<Escape>', lambda event: dialog.destroy())
        entry.focus_set()

    def start_lan(self, host, address='', code=''):
        if self.lan_room is not None:
            return
        if (self.runner is None or not self.runner.shown.is_set()
                or self.runner.overlay.table_style != self.table_style):
            self.lan_status.set('请等待球台摆好后再联机。')
            return
        if not host:
            try:
                address = str(ipaddress.IPv4Address(address.strip()))
            except ValueError:
                self.lan_status.set('请输入有效的 IPv4 地址。')
                return
            code = code.strip()
            if len(code) != 8 or not code.isascii() or not code.isdecimal():
                self.lan_status.set('请输入 8 位房间码。')
                return
        self.lan_room = self.runner.overlay.room = LanRoom(host, address, code=code)
        self.lan_status.set('正在创建房间…' if host else '正在连接房主…')
        self.lan_button.configure(text='联机中')
        self.restart_button.state(['disabled'])
        self.table_picker.state(['disabled'])
        for button in self.mode_buttons:
            button.state(['disabled'])
        self.show_scores(True)

    def leave_lan(self):
        if self.lan_room is not None:
            self.lan_room.close()
            self.lan_status.set('正在离开房间…')

    def lan_hint(self, table, label):
        room = self.lan_room
        if room is None:
            return label
        if room.finished.is_set():
            self.lan_room = None
            self.runner.overlay.room = None
            self.lan_button.configure(text='局域网')
            self.restart_button.state(['!disabled'])
            self.table_picker.state(['!disabled'])
            self.table_picker.set(config.TABLE_STYLES[self.table_style])
            for button in self.mode_buttons:
                button.state(['!disabled'])
            self.show_scores(self.player_count == 2)
            reason = room.error or '已离开房间，返回本地模式。'
            self.lan_status.set(reason)
            return '联机结束 · 返回本地模式'
        if room.stopping.is_set():
            return '正在离开房间…'
        if not room.connected.is_set():
            if room.host and room.listening.is_set():
                self.lan_status.set(f'等待对方加入。把本机 IPv4 地址和房间码 {room.code} 发给朋友。')
                return '等待对方加入…'
            return self.lan_status.get()
        local_player = 0 if room.host else 1
        self.lan_status.set(f'连接成功，你是玩家 {local_player + 1}。关闭此窗口即可返回球台。')
        if room.host:
            self.restart_button.state(['!disabled'])
        if table.player_count != 2 or getattr(table, 'serial', 0) < 0:
            return '正在同步球阵…'
        if table.state != 'finished':
            return f'你的回合 · {label}' if table.current_player == local_player else '对方回合 · 请等待'
        return label

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
                overlay = BilliardsOverlay(desktop, items, self.table_style)
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
                self.status.set(self.lan_hint(table, label))
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
        if self.runner and (self.lan_room is None or self.lan_room.host and self.lan_room.connected.is_set()):
            self.runner.overlay.request_restart(self.player_count)

    def change_table_style(self, _event):
        if self.lan_room is not None:
            return
        selected = self.table_picker.current()
        if selected < 0:
            return
        style = tuple(config.TABLE_STYLES)[selected]
        if style == self.table_style:
            return
        self.table_style = style
        if self.runner:
            self.runner.overlay.request_restart(self.player_count, style)
        try:
            config.save_table_style(style)
        except OSError as exc:
            _write_log('settings.log', str(exc))

    def show_scores(self, show):
        if show:
            for player, label in enumerate(self.score_labels):
                label.configure(text=f'玩家 {player + 1}   0 分',
                                style='Active.Score.TLabel' if player == 0 else 'Score.TLabel')
            self.scoreboard.pack(before=self.stats, fill='x', padx=16, pady=(0, 6))
        else:
            self.scoreboard.pack_forget()

    def change_mode(self, player_count):
        if self.lan_room is not None or player_count == self.player_count:
            return
        self.player_count = player_count
        for players, button in enumerate(self.mode_buttons, 1):
            button.configure(style='Primary.Table.TButton' if players == player_count else 'Table.TButton')
        self.show_scores(player_count == 2)
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
        if self.lan_room is not None:
            self.lan_room.close()
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
