"""TCP transport and guest-side rendering state for a two-player LAN room."""
import base64
import io
import json
import math
import secrets
import select
import socket
import struct
import threading
import time
from queue import Empty, Full, Queue
from types import SimpleNamespace

from PIL import Image

PORT = 49321
PROTOCOL = 2
MAX_BALLS = 512
MAX_MESSAGE = 32 * 1024 * 1024
FRAME_INTERVAL = 1 / 30
HEADER = struct.Struct('!BI')
KINDS = ('hello', 'accepted', 'rejected', 'input', 'setup', 'state', 'collision', 'ping')
LIMITS = (128, 16, 128, 4096, MAX_MESSAGE, 256 * 1024, 8, 8)


def local_addresses():
    try:
        return sorted({address[4][0] for address in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
                       if not address[4][0].startswith('127.')})
    except OSError:
        return []


class LanRoom:
    """Run all socket I/O off the desktop and Tk threads."""

    def __init__(self, host, address='', port=PORT, code=''):
        self.host = host
        self.address = address
        self.port = port
        self.code = f'{secrets.randbelow(10**8):08d}' if host else code
        self.listening = threading.Event()
        self.connected = threading.Event()
        self.stopping = threading.Event()
        self.finished = threading.Event()
        self.error = ''
        self.incoming = Queue(maxsize=64)
        self.incoming_state = Queue(maxsize=1)
        self.outgoing = Queue(maxsize=64)
        self.lock = threading.Lock()
        self.latest = None
        self.thread = threading.Thread(target=self._run, name='Billiards-LAN', daemon=True)
        self.thread.start()

    def close(self):
        self.stopping.set()

    def send(self, kind, payload, replace=False):
        message = [kind, payload]
        if replace:
            with self.lock:
                self.latest = message
        else:
            self._enqueue(self.outgoing, message)

    def _enqueue(self, queue, message):
        try:
            queue.put_nowait(message)
        except Full:
            self.error = '局域网消息积压，请重新连接。'
            self.close()

    def _run(self):
        try:
            if self.host:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                    listener.bind(('', self.port))
                    listener.listen(1)
                    listener.settimeout(.25)
                    self.port = listener.getsockname()[1]
                    self.listening.set()
                    while not self.stopping.is_set():
                        try:
                            peer, _ = listener.accept()
                        except TimeoutError:
                            continue
                        with peer:
                            if not self._accept(peer):
                                continue
                            listener.close()
                            self._exchange(peer)
                        break
            else:
                with socket.create_connection((self.address, self.port), timeout=4) as peer:
                    peer.settimeout(3)
                    self._write(peer, ['hello', [PROTOCOL, self.code]])
                    kind, payload = self._read(peer, {'accepted', 'rejected'}, time.monotonic() + 4)
                    if kind == 'rejected':
                        raise ValueError('房间码错误，或两台电脑的版本不同。')
                    if payload != PROTOCOL:
                        raise ValueError('两台电脑需要运行相同版本。')
                    self._exchange(peer)
        except (OSError, ValueError, OverflowError, RecursionError) as exc:
            if not self.stopping.is_set():
                self.error = str(exc) or '局域网连接已断开。'
        finally:
            self.connected.clear()
            self.finished.set()

    def _accept(self, peer):
        peer.settimeout(2)
        try:
            _, hello = self._read(peer, {'hello'}, time.monotonic() + 2)
            if (not isinstance(hello, list) or len(hello) != 2 or hello[0] != PROTOCOL
                    or not isinstance(hello[1], str) or not hello[1].isascii()
                    or not secrets.compare_digest(hello[1], self.code)):
                self._write(peer, ['rejected', None])
                return False
            self._write(peer, ['accepted', PROTOCOL])
            return True
        except (OSError, ValueError, RecursionError):
            return False

    def _exchange(self, peer):
        peer.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        peer.settimeout(3)
        self.connected.set()
        received = sent = time.monotonic()
        setup_sent = setup_received = False
        while not self.stopping.is_set():
            while not self.stopping.is_set():
                try:
                    message = self.outgoing.get_nowait()
                except Empty:
                    break
                self._write(peer, message)
                setup_sent |= message[0] == 'setup'
                sent = time.monotonic()
            if not self.host or setup_sent:
                with self.lock:
                    latest, self.latest = self.latest, None
                if latest is not None:
                    self._write(peer, latest)
                    sent = time.monotonic()
            if time.monotonic() - sent > 2:
                self._write(peer, ['ping', None])
                sent = time.monotonic()
            if time.monotonic() - received > 10:
                raise TimeoutError('局域网连接超时。')
            if not select.select([peer], [], [], .015)[0]:
                continue
            allowed = {'input', 'ping'} if self.host else (
                {'state', 'collision', 'ping'} if setup_received else {'setup', 'ping'})
            kind, payload = self._read(peer, allowed, time.monotonic() + 10)
            received = time.monotonic()
            if kind == 'setup':
                if not isinstance(payload, list) or len(payload) != 3:
                    raise ValueError('局域网球阵格式错误。')
                bounds, balls, style = payload
                _numbers(bounds, 4)
                if (not isinstance(balls, list) or not 1 <= len(balls) <= MAX_BALLS
                        or any(not isinstance(ball, str) or len(ball) > 65536 for ball in balls)
                        or style not in ('classic', 'photo')):
                    raise ValueError('局域网球阵格式错误。')
                setup_received = True
            elif kind == 'state':
                if not isinstance(payload, dict) or not isinstance(payload.get('balls'), list):
                    raise ValueError('局域网球局状态无效。')
                if len(payload['balls']) != len(balls):
                    raise ValueError('局域网球阵不匹配。')
            elif kind == 'input':
                if not isinstance(payload, list) or len(payload) != 4:
                    raise ValueError('局域网操作格式错误。')
            elif payload is not None:
                raise ValueError('局域网消息格式错误。')
            if kind == 'state':
                try:
                    self.incoming_state.get_nowait()
                except Empty:
                    pass
                self.incoming_state.put_nowait(payload)
            elif kind != 'ping':
                self._enqueue(self.incoming, (kind, payload))

    def _read(self, peer, allowed, deadline):
        kind_id, size = HEADER.unpack(self._read_exact(peer, HEADER.size, deadline))
        if kind_id >= len(KINDS) or KINDS[kind_id] not in allowed:
            raise ValueError('局域网消息类型无效。')
        if size > LIMITS[kind_id]:
            raise ValueError('局域网消息过大。')
        return KINDS[kind_id], json.loads(self._read_exact(peer, size, deadline))

    def _read_exact(self, peer, size, deadline):
        data = bytearray()
        while len(data) < size and not self.stopping.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('局域网消息接收超时。')
            if not select.select([peer], [], [], min(.25, remaining))[0]:
                continue
            chunk = peer.recv(min(65536, size - len(data)))
            if not chunk:
                raise ConnectionError('对方已离开房间。')
            data.extend(chunk)
        if len(data) != size:
            raise ConnectionError('局域网房间已关闭。')
        return data

    @staticmethod
    def _write(peer, message):
        kind, payload = message
        kind_id = KINDS.index(kind)
        data = json.dumps(payload, separators=(',', ':'), allow_nan=False).encode('utf-8')
        if len(data) > LIMITS[kind_id]:
            raise ValueError('局域网消息过大。')
        peer.sendall(HEADER.pack(kind_id, len(data)) + data)


def encode_balls(sources):
    if not 1 <= len(sources) <= MAX_BALLS:
        raise ValueError(f'局域网对战需要 1 至 {MAX_BALLS} 个桌面图标。')
    balls = []
    for source in sources:
        with io.BytesIO() as output:
            source.save(output, format='PNG')
            balls.append(base64.b64encode(output.getvalue()).decode('ascii'))
    return balls


def decode_balls(balls):
    if not isinstance(balls, list) or not 1 <= len(balls) <= MAX_BALLS:
        raise ValueError('房主的球阵数量无效。')
    sources = []
    for ball in balls:
        if not isinstance(ball, str) or len(ball) > 65536:
            raise ValueError('球面图标格式错误。')
        with Image.open(io.BytesIO(base64.b64decode(ball, validate=True)), formats=('PNG',)) as source:
            if source.size != (96, 96):
                raise ValueError('球面图标尺寸无效。')
            sources.append(source.convert('RGBA'))
    return sources


def snapshot(table, serial):
    return {
        'serial': serial,
        'state': table.state,
        'player': table.current_player,
        'scores': table.scores[:],
        'seconds': table.round_seconds,
        'white': [table.white.x, table.white.y, table.white.radius, table.white_sunk],
        'charge': table.charge,
        'balls': [[*pose[:5], index in table.sunk, table.exit_visual(index)]
                  for index, pose in enumerate(table.poses, 1)],
        'cue': table.cue(),
        'guide': table.aim_guide(),
    }


def _numbers(values, count, limit=100000):
    if not isinstance(values, (tuple, list)) or len(values) != count or any(
            type(value) not in (int, float) or not math.isfinite(value) or abs(value) > limit for value in values):
        raise ValueError('局域网坐标无效。')
    return values


class RemoteTable:
    """Translate authoritative host coordinates and relay only guest input."""

    player_count = 2

    def __init__(self, room, local_table, host_bounds, count):
        left, top, right, bottom = _numbers(host_bounds, 4)
        if right - left < 32 or bottom - top < 32:
            raise ValueError('房主球台尺寸无效。')
        x, y, end_x, end_y = local_table.bounds
        self.room = room
        self.bounds = local_table.bounds
        self.work_area = local_table.work_area
        self.height = end_y - y
        self.scale_x = (end_x - x) / (right - left)
        self.scale_y = (end_y - y) / (bottom - top)
        self.scale = min(self.scale_x, self.scale_y)
        self.offset_x = x - left * self.scale_x
        self.offset_y = y - top * self.scale_y
        self.icons = range(count)
        self.serial = -1
        self.current_player = 0
        self.state = 'ready'
        self.scores = [0, 0]
        self.round_seconds = 0
        self.white = SimpleNamespace(x=x, y=y, radius=1)
        self.white_sunk = True
        self.charge = 0.
        self.sunk = set()
        self.poses = []
        self.exits = {}
        self.cue_points = None
        self.guide_lines = []
        self.aim_point = None
        self.action = None
        self.pending_serial = None

    @property
    def pocketed_icons(self):
        return len(self.sunk)

    def point(self, point):
        x, y = _numbers(point, 2)
        left, top, right, bottom = self.work_area
        return (min(right, max(left, x * self.scale_x + self.offset_x)),
                min(bottom, max(top, y * self.scale_y + self.offset_y)))

    def host_point(self, x, y):
        return (x - self.offset_x) / self.scale_x, (y - self.offset_y) / self.scale_y

    def receive(self, data):
        if not isinstance(data, dict) or data['state'] not in (
                'ready', 'stroke', 'rolling', 'return_white', 'ball_in_hand', 'finished'):
            raise ValueError('局域网球局状态无效。')
        serial, player, seconds = _numbers([data['serial'], data['player'], data['seconds']], 3, 2**31)
        scores = _numbers(data['scores'], 2, len(self.icons))
        if any(type(value) is not int or value < 0 for value in (serial, player, seconds, *scores)) or player > 1:
            raise ValueError('局域网计分数据无效。')
        balls = data['balls']
        if not isinstance(balls, list) or len(balls) != len(self.icons):
            raise ValueError('局域网球阵不匹配。')
        poses, sunk, exits = [], set(), {}
        for index, ball in enumerate(balls, 1):
            x, y, width, height, angle = _numbers(ball[:5], 5)
            if not 0 < min(width, height) <= max(width, height) <= 256 or type(ball[5]) is not bool:
                raise ValueError('局域网球体数据无效。')
            x, y = self.point((x, y))
            poses.append((x, y, min(112, width * self.scale), min(112, height * self.scale), angle, 0, 0))
            if ball[5]:
                sunk.add(index)
            if ball[6] is not None:
                px, py, fade = _numbers(ball[6], 3)
                if not 0 <= fade <= 1:
                    raise ValueError('局域网落袋数据无效。')
                exits[index] = (*self.point((px, py)), fade)
        wx, wy, radius = _numbers(data['white'][:3], 3)
        if not 0 < radius <= 64 or type(data['white'][3]) is not bool:
            raise ValueError('局域网白球数据无效。')
        cue = data['cue']
        if cue is not None and len(cue) != 2:
            raise ValueError('局域网球杆数据无效。')
        if cue is not None:
            tip, tail = map(self.point, cue)
            length = math.dist(tip, tail)
            if length > 440:
                tail = tuple(start + (end - start) * 440 / length for start, end in zip(tip, tail))
            cue = tip, tail
        guide = data['guide']
        if not isinstance(guide, list) or len(guide) > 3:
            raise ValueError('局域网辅助线数据无效。')
        lines = []
        for role, start, end in guide:
            if role not in ('incoming', 'white', 'target'):
                raise ValueError('局域网辅助线数据无效。')
            lines.append((role, self.point(start), self.point(end)))
        charge = _numbers([data['charge']], 1)[0]
        if not 0 <= charge <= 1:
            raise ValueError('局域网击球数据无效。')
        self.serial, self.current_player, self.round_seconds = serial, player, seconds
        self.state, self.scores = data['state'], scores
        self.poses, self.sunk, self.exits = poses, sunk, exits
        self.white.x, self.white.y = self.point((wx, wy))
        self.white.radius = min(56, radius * self.scale)
        self.white_sunk = data['white'][3]
        if player != 1 or self.state != 'ready':
            self.charge = charge
        self.cue_points, self.guide_lines = cue, lines

    def cue(self):
        return self.cue_points

    def aim_guide(self):
        return self.guide_lines

    def exit_visual(self, index):
        return self.exits.get(index)

    def aim_at(self, x, y):
        if self.state == 'ready':
            self.aim_point = self.host_point(x, y)

    def shoot(self, power):
        self.action = ['shoot', power]

    def place_white(self, x, y):
        self.action = ['place', *self.host_point(x, y)]

    def send_input(self):
        if self.pending_serial == self.serial:
            return
        payload = [self.serial, self.aim_point, self.charge, self.action]
        self.room.send('input', payload, replace=self.action is None)
        if self.action is not None:
            if self.action[0] == 'shoot':
                self.pending_serial = self.serial
            self.action = None


def apply_input(table, serial, payload):
    """Apply a guest command only during the guest's current turn."""
    if not isinstance(payload, list) or len(payload) != 4:
        raise ValueError('局域网操作格式错误。')
    sequence, aim, charge, action = payload
    if type(sequence) is not int or sequence != serial or table.current_player != 1:
        return False
    if table.state == 'ready':
        if aim is not None:
            x, y = _numbers(aim, 2)
            left, top, right, bottom = table.bounds
            table.aim_at(min(right, max(left, x)), min(bottom, max(top, y)))
        table.charge = min(1, max(0, _numbers([charge], 1)[0]))
        if isinstance(action, list) and len(action) == 2 and action[0] == 'shoot':
            power = min(1, max(.18, _numbers(action[1:], 1)[0]))
            return table.shoot(power)
    elif table.state == 'ball_in_hand' and isinstance(action, list) and len(action) == 3 and action[0] == 'place':
        table.place_white(*_numbers(action[1:], 2))
    return False
