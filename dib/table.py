"""Billiards physics for desktop icons, in desktop pixels.

The table is laid out inside the monitor work area, the white cue ball starts on
the left and the desktop icons are racked on the right. The caller aims with
:meth:`aim_at` and strikes with :meth:`shoot`; :meth:`step` advances the whole
simulation, including pocket exits and the ball-in-hand state after a scratch.
"""
import math
from collections import defaultdict

from .body import Body

# How long a sinking icon keeps its exit animation.
EXIT_SECONDS = .72
MIN_SHOT_CHARGE = .18
RAIL_REBOUND = .88
BALL_IMPULSE = .96
ROLLING_DRAG = .45
STOP_SPEED = .008


class BilliardsMotion:
    """Positions and physics of the white ball and the desktop icon rack."""

    def __init__(self, work_area, sizes):
        self.sizes = sizes
        # The game is always drawn directly on the desktop and always manual.
        self.desktop = True
        self.manual = True
        self.work_area = work_area
        left, top, right, bottom = work_area
        width = right - left
        height = bottom - top
        self.bounds = (round(left + width * .06), round(top + height * .09),
                       round(right - width * .06), round(bottom - height * .09))
        left, top, right, bottom = self.bounds
        self.width = right - left
        self.height = bottom - top
        mid = (left + right) / 2
        self.pockets = ((left, top), (mid, top), (right, top),
                        (left, bottom), (mid, bottom), (right, bottom))
        self.pocket_radius = min(49, max(18, self.height * .047))
        count = len(sizes)
        columns = max(1, math.ceil((math.sqrt(8 * count + 1) - 1) / 2))
        pitch = min(94, self.width * .45 / (columns + .5), self.height * .79 / columns)
        self.tile = min(82, pitch * .88)
        self.radius = pitch * .46
        self.white_radius = min(25, self.radius * .9)
        self.rack = []
        for column in range(columns):
            depth = min(column + 1, count - len(self.rack))
            for i in range(depth):
                x = left + self.width * .52 + column * pitch * .84
                y = (top + bottom) / 2 + (i - (depth - 1) / 2) * pitch * .95
                self.rack.append((x, y))
        self.player_count = 1
        self.reset()

    def reset(self, player_count=None):
        if player_count is not None:
            self.player_count = player_count
        self.current_player = 0
        self.scores = [0] * self.player_count
        self.shot_start_score = 0
        left, top, _, bottom = self.bounds
        self.white = Body(left + self.width * .20, (top + bottom) / 2, self.white_radius, self.bounds)
        self.icons = [Body(x, y, self.radius, self.bounds) for x, y in self.rack]
        self.bodies = [self.white, *self.icons]
        self.sunk = set()
        self.white_sunk = False
        self.exits = {}
        self.collision_impulse = 0.
        self.state = 'ready'
        self.ready_for = 0.
        self.round_started_at = None
        self.round_seconds = 0
        self.stroke = 0.
        self.power = 0.
        self.charge = 0.
        self.break_shot = True
        self.aim = self._aim()
        self.poses = self._poses()

    def active_bodies(self):
        return [body for index, body in enumerate(self.bodies) if index not in self.sunk]

    @property
    def pocketed_icons(self):
        return len(self.sunk) - (0 in self.sunk)

    def _aim(self):
        x, y = self.white.x, self.white.y
        targets = [body for index, body in enumerate(self.icons, 1) if index not in self.sunk]
        target = min(targets, key=lambda body: (body.x - x) ** 2 + (body.y - y) ** 2, default=None)
        if target is None:
            return 1., 0.
        if self.break_shot:
            return 1., .022
        shot = self._pocket_shot(targets)
        dx, dy = (shot[0] - x, shot[1] - y) if shot else (target.x - x, target.y - y)
        distance = max(1, math.hypot(dx, dy))
        return dx / distance, dy / distance

    @staticmethod
    def _clear(x, y, tx, ty, others, radius):
        dx, dy = tx - x, ty - y
        length = dx * dx + dy * dy
        for other in others:
            t = max(0, min(1, ((other.x - x) * dx + (other.y - y) * dy) / max(1, length)))
            if math.hypot(other.x - x - t * dx, other.y - y - t * dy) < other.radius + radius:
                return False
        return True

    def _pocket_shot(self, targets):
        white = self.white
        left, top, right, bottom = self.bounds
        candidates = []
        for target in targets:
            for px, py in self.pockets:
                dx, dy = px - target.x, py - target.y
                distance = math.hypot(dx, dy)
                if distance < 1:
                    continue
                ux, uy = dx / distance, dy / distance
                contact = target.radius + white.radius
                gx, gy = target.x - ux * contact, target.y - uy * contact
                if not (left + white.radius <= gx <= right - white.radius
                        and top + white.radius <= gy <= bottom - white.radius):
                    continue
                wx, wy = gx - white.x, gy - white.y
                approach = math.hypot(wx, wy)
                alignment = (wx * ux + wy * uy) / max(1, approach)
                if alignment > .53:
                    candidates.append(((approach + distance) / alignment, target, px, py, gx, gy))
        for _, target, px, py, gx, gy in sorted(candidates, key=lambda shot: shot[0]):
            others = [body for body in targets if body is not target]
            if (self._clear(white.x, white.y, gx, gy, others, white.radius)
                    and self._clear(target.x, target.y, px, py, others, target.radius)):
                return gx, gy
        return None

    def aim_at(self, x, y):
        if self.state != 'ready':
            return
        dx, dy = x - self.white.x, y - self.white.y
        length = math.hypot(dx, dy)
        if length > 2:
            self.aim = (dx / length, dy / length)

    def shoot(self, power):
        if self.state != 'ready':
            return False
        self.state = 'stroke'
        self.stroke = 0.
        self.power = self._shot_speed(power)
        self.shot_start_score = self.scores[self.current_player]
        self.break_shot = False
        return True

    def _shot_speed(self, power):
        return self.height * (.62 + 2.55 * max(0, min(1, power))) * 1.58

    def cue(self):
        if self.state not in ('ready', 'stroke'):
            return None
        dx, dy = self.aim
        pull = 12 + 34 * self.charge
        gap = pull * max(0, 1 - self.stroke / .082) if self.state == 'stroke' else pull
        tip = (self.white.x - dx * (self.white.radius + gap), self.white.y - dy * (self.white.radius + gap))
        length = min(440, self.height * .48)
        left, top, right, bottom = self.work_area
        for value, direction, low, high in ((tip[0], -dx, left + 5, right - 5),
                                            (tip[1], -dy, top + 5, bottom - 5)):
            if abs(direction) > 1e-6:
                edge = high if direction > 0 else low
                length = min(length, max(0, (edge - value) / direction))
        return tip, (tip[0] - dx * length, tip[1] - dy * length)

    def aim_guide(self):
        """Predict the opening shot and one outgoing leg per moving ball."""
        if self.state != 'ready':
            return ()
        x, y, radius = self.white.x, self.white.y, self.white.radius
        dx, dy = self.aim
        speed = self._shot_speed(max(MIN_SHOT_CHARGE, self.charge))
        targets = [body for index, body in enumerate(self.icons, 1) if index not in self.sunk]
        origin = x, y
        hit, kind, contact, velocity = self._guide_leg(origin, (dx * speed, dy * speed), radius, targets)
        lines = []
        gap = radius + 5
        if math.dist(origin, hit) > gap:
            lines.append(('incoming', (x + dx * gap, y + dy * gap), hit))
        vx, vy = velocity
        outgoing = []
        if kind == 'rail':
            hit_x, hit_y = contact
            rebound = (-vx * RAIL_REBOUND if hit_x else vx, -vy * RAIL_REBOUND if hit_y else vy)
            outgoing.append(('white', hit, rebound, radius, targets))
        elif kind == 'ball':
            target = contact
            target_pos = target.x, target.y
            separation = math.dist(hit, target_pos)
            if separation > 1e-6:
                nx, ny = (target.x - hit[0]) / separation, (target.y - hit[1]) / separation
                impulse = (vx * nx + vy * ny) * BALL_IMPULSE
                pushed = impulse * nx, impulse * ny
                other_targets = [body for body in targets if body is not target]
                outgoing.append(('white', hit, (vx - pushed[0], vy - pushed[1]), radius, other_targets))
                outgoing.append(('target', target_pos, pushed, target.radius, other_targets))
        for role, start, motion, size, obstacles in outgoing:
            end, _, _, _ = self._guide_leg(start, motion, size, obstacles)
            if math.dist(start, end) >= 2:
                lines.append((role, start, end))
        return tuple(lines)

    def _guide_leg(self, origin, velocity, radius, obstacles):
        """Return endpoint, contact type, contact data and remaining velocity."""
        x, y = origin
        speed = math.hypot(*velocity)
        stop = self.height * STOP_SPEED
        if speed <= stop:
            return origin, 'stop', None, (0., 0.)
        dx, dy = velocity[0] / speed, velocity[1] / speed
        drag = self.height * ROLLING_DRAG
        distance = (speed ** 2 - stop ** 2) / (2 * drag)
        kind, contact = 'stop', None
        left, top, right, bottom = self.bounds
        rail_x = ((right - radius if dx > 0 else left + radius) - x) / dx if abs(dx) > 1e-6 else math.inf
        rail_y = ((bottom - radius if dy > 0 else top + radius) - y) / dy if abs(dy) > 1e-6 else math.inf
        rail = min(rail_x, rail_y)
        if rail <= distance:
            distance = max(0., rail)
            kind = 'rail'
            contact = math.isclose(rail_x, rail, abs_tol=1e-6), math.isclose(rail_y, rail, abs_tol=1e-6)
        for body in obstacles:
            entry = self._ray_circle(x, y, dx, dy, body.x, body.y, radius + body.radius)
            if entry is not None and entry < distance:
                distance, kind, contact = entry, 'ball', body
        for px, py in self.pockets:
            entry = self._ray_circle(x, y, dx, dy, px, py, self.pocket_radius + radius * .55)
            if entry is not None and entry <= distance:
                distance, kind, contact = entry, 'pocket', None
        speed = math.sqrt(max(0., speed ** 2 - 2 * drag * distance))
        return (x + dx * distance, y + dy * distance), kind, contact, (dx * speed, dy * speed)

    @staticmethod
    def _ray_circle(x, y, dx, dy, cx, cy, radius):
        offset_x, offset_y = cx - x, cy - y
        forward = offset_x * dx + offset_y * dy
        across = offset_x * dy - offset_y * dx
        clearance = radius ** 2 - across ** 2
        if forward > 0 and clearance > 0:
            return max(0., forward - math.sqrt(clearance))
        return None

    def step(self, dt, now):
        """Advance the simulation with the runner's monotonic frame clock."""
        self.collision_impulse = 0.
        dt = max(0, min(.05, dt))
        if self.exits:
            self.exits = {i: (x, y, tx, ty, age + dt) for i, (x, y, tx, ty, age) in self.exits.items()
                          if age + dt < EXIT_SECONDS}
        if self.state == 'finished':
            return
        if self.round_started_at is None:
            self.round_started_at = now
        self.round_seconds = int(now - self.round_started_at)
        if self.state == 'return_white':
            self.ready_for += dt
            if self.ready_for >= .8:
                self.state = 'ball_in_hand'
                self._auto_place()
            return
        if self.state == 'ball_in_hand':
            return
        if self.state == 'ready':
            self.ready_for += dt
            return
        if self.state == 'stroke':
            self.stroke += dt
            if self.stroke < .082:
                return
            self.white.vx = self.aim[0] * self.power
            self.white.vy = self.aim[1] * self.power
            self.state = 'rolling'
            dt = self.stroke - .082
        speed = max((math.hypot(body.vx, body.vy) for body in self.active_bodies()), default=0)
        steps = max(1, math.ceil(dt * max(180, speed / max(.5, self.white_radius) * 1.7)))
        h = dt / steps
        for _ in range(steps):
            for index, body in enumerate(self.bodies):
                if index in self.sunk:
                    continue
                body.x += body.vx * h
                body.y += body.vy * h
                pocket = self._in_pocket(body)
                if pocket is not None:
                    self.sunk.add(index)
                    if index == 0:
                        self.white_sunk = True
                    else:
                        self.scores[self.current_player] += 1
                        self._start_exit(index, body, pocket)
                else:
                    self._walls(body)
            active = self.active_bodies()
            self._collisions(active)
            for body in active:
                self._walls(body)
                speed = math.hypot(body.vx, body.vy)
                if speed:
                    drag = max(0, 1 - self.height * ROLLING_DRAG * h / speed)
                    body.vx *= drag
                    body.vy *= drag
                body.impact *= math.exp(-h * 12)
                body.angle *= math.exp(-h * 4)
        cleared = self.pocketed_icons == len(self.icons)
        if cleared or max((math.hypot(body.vx, body.vy) for body in self.active_bodies()), default=0) < self.height * STOP_SPEED:
            for body in self.bodies:
                body.vx = body.vy = body.impact = body.angle = 0.
            self.charge = 0.
            self.ready_for = 0.
            if not cleared and (self.white_sunk or self.scores[self.current_player] == self.shot_start_score):
                self.current_player = (self.current_player + 1) % self.player_count
            if cleared:
                self.state = 'finished'
            elif self.white_sunk:
                self.state = 'return_white'
            else:
                self.state = 'ready'
                self.aim = self._aim()
        self.poses = self._poses()

    def _in_pocket(self, body):
        capture = self.pocket_radius + body.radius * .55
        return next(((x, y) for x, y in self.pockets
                     if (body.x - x) ** 2 + (body.y - y) ** 2 < capture * capture), None)

    def _start_exit(self, index, body, pocket):
        px, py = pocket
        left, top, right, bottom = self.work_area
        rail_left, rail_top, rail_right, _ = self.bounds
        # Keep the icon and the small particle halo inside the outer work area.
        inset = min(48, max(5, (rail_top - top) * .42))
        tx = left + inset if px == rail_left else right - inset if px == rail_right else px
        ty = top + inset if py == rail_top else bottom - inset
        self.exits[index] = (body.x, body.y, tx, ty, 0.)

    def exit_visual(self, index):
        exit_state = self.exits.get(index)
        if exit_state is None:
            return None
        x, y, tx, ty, age = exit_state
        progress = age / EXIT_SECONDS
        travel = 1 - (1 - min(1, progress / .73)) ** 2
        fade = max(0, min(1, (progress - .40) / .60))
        return x + (tx - x) * travel, y + (ty - y) * travel, fade

    def place_white(self, x, y):
        if self.state != 'ball_in_hand':
            return False
        left, top, right, bottom = self.bounds
        r = self.white.radius
        if not (left + r <= x <= right - r and top + r <= y <= bottom - r):
            return False
        if any(math.hypot(x - px, y - py) < self.pocket_radius + r for px, py in self.pockets):
            return False
        if any(math.hypot(x - body.x, y - body.y) < r + body.radius + 2 for body in self.active_bodies()):
            return False
        self.white.x = x
        self.white.y = y
        self.sunk.discard(0)
        self.white_sunk = False
        self.state = 'ready'
        self.ready_for = 0.
        self.aim = self._aim()
        return True

    def _auto_place(self):
        left, top, _, _ = self.bounds
        for fx in (.20, .31, .42, .54, .66, .78):
            for fy in (.5, .31, .69, .19, .81):
                if self.place_white(left + self.width * fx, top + self.height * fy):
                    return True
        return False

    def _walls(self, body):
        left, top, right, bottom = self.bounds
        for axis, velocity, low, high in (('x', 'vx', left + body.radius, right - body.radius),
                                          ('y', 'vy', top + body.radius, bottom - body.radius)):
            position = getattr(body, axis)
            speed = getattr(body, velocity)
            if position < low or position > high:
                setattr(body, axis, max(low, min(high, position)))
                if (position < low and speed < 0) or (position > high and speed > 0):
                    setattr(body, velocity, -speed * RAIL_REBOUND)
                    body.impact = max(body.impact, min(.23, abs(speed) / self.height * .25))

    def _collisions(self, bodies):
        # Nearby buckets keep the number of tested pairs low on crowded desktops.
        cell = max(1, self.radius * 2)
        buckets = defaultdict(list)
        for body in bodies:
            gx = math.floor(body.x / cell)
            gy = math.floor(body.y / cell)
            for y in range(gy - 1, gy + 2):
                for x in range(gx - 1, gx + 2):
                    for other in buckets.get((x, y), ()):
                        self._collide(other, body)
            buckets[gx, gy].append(body)

    def _collide(self, a, b):
        dx = b.x - a.x
        dy = b.y - a.y
        limit = a.radius + b.radius
        if dx * dx + dy * dy >= limit * limit:
            return
        distance = math.hypot(dx, dy)
        nx, ny = (dx / distance, dy / distance) if distance > 1e-6 else (1., 0.)
        correction = (limit - distance) * .5
        a.x -= nx * correction
        a.y -= ny * correction
        b.x += nx * correction
        b.y += ny * correction
        closing = (b.vx - a.vx) * nx + (b.vy - a.vy) * ny
        if closing >= 0:
            return
        impulse = -closing * BALL_IMPULSE
        self.collision_impulse = max(self.collision_impulse, impulse)
        a.vx -= nx * impulse
        a.vy -= ny * impulse
        b.vx += nx * impulse
        b.vy += ny * impulse
        impact = min(.23, impulse / self.height * .30)
        a.impact = max(a.impact, impact)
        b.impact = max(b.impact, impact)
        a.angle -= ny * impact * 95
        b.angle += ny * impact * 95

    def _poses(self):
        result = []
        for body, (w, h) in zip(self.icons, self.sizes):
            ratio = self.tile / max(w, h)
            result.append((body.x, body.y, max(1, round(w * ratio * (1 + body.impact * .64))),
                           max(1, round(h * ratio * (1 - body.impact))), body.angle, 0, 0.))
        return result
