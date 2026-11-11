"""Exercise the LAN protocol over TCP and enforce host-only game authority."""
import json
import socket
import unittest
from unittest.mock import Mock

from PIL import Image

from dib.lan import HEADER, KINDS, LIMITS, PROTOCOL, LanRoom, RemoteTable, apply_input, decode_balls, encode_balls, snapshot
from dib.table import BilliardsMotion


class LanTests(unittest.TestCase):
    def test_loopback_game_sync_and_guest_shot(self):
        host = LanRoom(True, port=0)
        self.addCleanup(host.close)
        self.assertTrue(host.listening.wait(3), host.error)
        wrong = '00000000' if host.code != '00000000' else '11111111'
        intruder = LanRoom(False, '127.0.0.1', host.port, code=wrong)
        self.addCleanup(intruder.close)
        self.assertTrue(intruder.finished.wait(3))
        self.assertIn('房间码', intruder.error)
        self.assertFalse(host.connected.is_set())
        self.assertFalse(host.finished.is_set())
        guest = LanRoom(False, '127.0.0.1', host.port, code=host.code)
        self.addCleanup(guest.close)
        self.assertTrue(guest.connected.wait(3), guest.error)
        self.assertTrue(host.connected.wait(3), host.error)

        table = BilliardsMotion((0, 0, 1600, 1000), [(96, 96)] * 2)
        table.reset(2)
        sources = [Image.new('RGBA', (96, 96), color) for color in ('orange', 'cyan')]
        host.send('setup', [table.bounds, encode_balls(sources), 'photo'])
        kind, (bounds, balls, style) = guest.incoming.get(timeout=3)
        self.assertEqual(kind, 'setup')
        self.assertEqual(style, 'photo')
        self.assertEqual([ball.tobytes() for ball in decode_balls(balls)], [ball.tobytes() for ball in sources])
        local = BilliardsMotion((-800, 0, 0, 500), [(96, 96)])
        remote = RemoteTable(guest, local, bounds, len(balls))

        self.assertFalse(apply_input(table, 0, [0, None, .5, ['shoot', .5]]))
        table.current_player = 1
        host.send('collision', None)
        host.send('state', snapshot(table, 0), replace=True)
        self.assertEqual(guest.incoming.get(timeout=3), ('collision', None))
        state = guest.incoming_state.get(timeout=3)
        remote.receive(state)
        self.assertEqual(remote.current_player, 1)
        self.assertAlmostEqual(remote.white.radius, table.white.radius / 2)
        target = remote.point((table.white.x + 150, table.white.y))
        remote.aim_at(*target)
        remote.charge = .6
        remote.shoot(.6)
        remote.send_input()
        kind, payload = host.incoming.get(timeout=3)
        self.assertEqual(kind, 'input')
        self.assertTrue(apply_input(table, 0, payload))
        self.assertEqual(table.state, 'stroke')
        self.assertFalse(apply_input(table, 1, payload))
        self.assertAlmostEqual(table.aim[0], 1)
        guest.close()
        self.assertTrue(host.finished.wait(3))

    def test_oversized_state_is_rejected_from_header(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen(1)
            listener.settimeout(3)
            guest = LanRoom(False, '127.0.0.1', listener.getsockname()[1], code='12345678')
            self.addCleanup(guest.close)
            peer, _ = listener.accept()
            with peer:
                LanRoom._write(peer, ['accepted', PROTOCOL])
                ball = Image.new('RGBA', (96, 96), 'cyan')
                LanRoom._write(peer, ['setup', [[0, 0, 900, 500], encode_balls([ball]), 'classic']])
                state_id = KINDS.index('state')
                peer.sendall(HEADER.pack(state_id, LIMITS[state_id] + 1))
                self.assertTrue(guest.finished.wait(3))
                self.assertIn('消息过大', guest.error)

    def test_snapshot_syncs_pocket_animation_and_score(self):
        table = BilliardsMotion((0, 0, 1920, 1080), [(96, 96)] * 2)
        table.reset(2)
        table.current_player = 1
        table.state = 'rolling'
        table.icons[0].x, table.icons[0].y = table.pockets[0]
        table.step(.01, 1.)
        remote = RemoteTable(Mock(), table, table.bounds, 2)
        remote.receive(json.loads(json.dumps(snapshot(table, 1))))
        self.assertEqual(remote.scores, [0, 1])
        self.assertEqual(remote.pocketed_icons, 1)
        self.assertEqual(remote.exit_visual(1), table.exit_visual(1))


if __name__ == '__main__':
    unittest.main()
