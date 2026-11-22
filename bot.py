"""comet-v8: a squad-hunter brain.

Practice matches are us against a 15-bot squad that shares vision and hunts the weakest,
so score is roughly "kills before we die". This brain is organised around that:

  * pick one target and stay on it (sticky, low-HP first), finish wounded mechs at close range
  * step to a nearby spot that keeps line of sight while a shot is ready and breaks it while
    reloading, and avoid spots that other enemies can see
  * back off to cover only while a fight is actually being lost (short retreat window)
  * between fights: grab a laser, farm crates / drones / cubes, keep to the safe zone

Ideas credited in README (public bots read for ideas; code is our own).
"""
import math, os, random, time
import mechfight as mf
from geo import Map, AStar, DET

F = set(os.environ.get("FLAGS", "").split(","))
TAU = 2 * math.pi
LAT = 0.075          # our action lands about one tick after the observation

UPGRADES = ['plating', 'armor', 'laser_damage', 'laser_charge', 'nanites', 'gun_damage', 'gun_rate',
            'lightning_efficiency', 'lightning_range', 'servos', 'gun_mag', 'gun_velocity', 'gyros',
            'lightning_chain', 'reactor', 'capacitor', 'mortar_reload', 'mortar_splash', 'mortar_velocity',
            'sensors', 'jets', 'laser_pierce']
PREF = {'laser': 4, 'lightning': 3}          # we never take mortars

# tuning knobs (env overrides make simulator sweeps easy)
def K(name, default):
    return float(os.environ.get(name, default))

STICK = K('V8_STICK', 200)       # score bonus for staying on the same target
LOWB = K('V8_LOWB', 200)         # score bonus for targets under 30 % HP
HPW = K('V8_HPW', 0.21)          # metres of distance one HP point is worth when ranking targets
REACH = K('V8_REACH', 240)       # max target distance
LASER_R = K('V8_LASER_R', 120)   # preferred fight range while holding a laser
GUN_R = K('V8_GUN_R', 85)
ZAP_R = K('V8_ZAP_R', 28)
FIN_R = K('V8_FIN_R', 20)        # range we close to when finishing a wounded target
SEEK = K('V8_SEEK', 240)         # how far we walk for a laser
LOOT_R = K('V8_LOOT_R', 150)
FARM_HP = K('V8_FARM_HP', 0.5)
CUBE_GRAB = K('V8_CUBE_GRAB', 70)


def wrap(a):
    return math.atan2(math.sin(a), math.cos(a))


def hyp(x, y):
    return math.sqrt(x * x + y * y)


def clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


class Foe:
    __slots__ = ('o', 'seen', 'hit_me', 'dist', 'los', 'closing', 'engage')

    def __init__(self):
        self.o = None; self.seen = -99.0; self.hit_me = -99.0
        self.dist = 1e9; self.los = False; self.closing = 0.0; self.engage = False


class Bot:
    name = "comet-v8"

    def __init__(self, hello):
        self.cfg = hello['config']
        self.rng = random.Random(hello.get('seed', 0) or hello.get('slot', 0) + 1)
        self.m = Map()
        self.phase = self.rng.uniform(-math.pi, math.pi)
        self.foes = {}; self.drones = {}
        self.crates = {}; self.cubes = {}; self.picks = {}
        self.banned = {}; self.failed = {}
        self.now = 0.0
        self.hurt_t = -99.0; self.hurt_bearing = 0.0
        self.drone_hurt_t = -99.0; self.drone_bearing = 0.0
        self.threat_t = -99.0
        self.stick_id = None; self.stick_t = -99.0; self.lose_t = -99.0
        self.strafe = 1.0; self.strafe_until = 0.0
        self.cover_goal = None; self.cover_t = -99.0
        self.loot_id = None; self.loot_since = 0.0
        self.goal = None; self.planner = None; self.path = None; self.path_i = 0
        self.stuck_since = None; self.stuck_n = 0; self.unstuck_until = -1.0; self.unstuck_dir = (1.0, 0.0)
        self.hold = None; self.hold_start = None
        self.kills = 0; self.start_alive = None; self.odd_deaths = 0; self.ffa = False
        self.last_aim = None
        self.t0 = 0.0
        ms = self.cfg['mech']
        self.mr, self.mh, self.meye = ms.get('radius', 2.5), ms.get('height', 9), ms.get('eye_height', 8)
        self.spc = self.cfg.get('cubes', {}).get('size_per_cube', 0.05)
        self.cube_cap = self.cfg.get('cubes', {}).get('stat_cap', 10)
        self.dpc = self.cfg.get('cubes', {}).get('damage_per_cube', 0.04)
        self.npc_r = self.cfg.get('npc', {}).get('radius', 1.8)
        self.npc_h = self.cfg.get('npc', {}).get('height', 3.5)
        self.crate_r = self.cfg.get('crates', {}).get('radius', 1.4)
        self.crate_h = self.cfg.get('crates', {}).get('height', 2.4)
        self.pick_r = self.cfg.get('loot', {}).get('pickup_radius', 4)

    # ------------------------------------------------------------ small helpers
    def left(self):
        return 0.042 - (time.perf_counter() - self.t0)

    def scale(self, o):
        return 1 + self.spc * o.get('cubes', 0)

    def centre(self, o):
        p = o['pos']
        h = self.npc_h if o.get('kind') == 'npc' else self.mh * self.scale(o)
        return (p[0], p[1], p[2] + h * 0.5)

    def eye_of(self, o):
        p = o['pos']
        return (p[0], p[1], p[2] + self.meye * self.scale(o))

    # ------------------------------------------------------------ zone
    def zone_at(self, z, t):
        if t <= z['shrink_start'] or z['shrink_end'] <= z['shrink_start']:
            return self.hold
        f = clamp((t - z['shrink_start']) / (z['shrink_end'] - z['shrink_start']), 0, 1)
        hx, hy, hr = self.hold
        nc = z['next_center']
        return (hx + (nc[0] - hx) * f, hy + (nc[1] - hy) * f, hr + (z['next_radius'] - hr) * f)

    def zone_now(self, z):
        live = (z['center'][0], z['center'][1], z['radius'])
        if self.now <= z['shrink_start']:
            return live
        if abs(live[2] - self.hold[2]) > 0.01 or abs(live[0] - self.hold[0]) > 0.01:
            return live
        return self.zone_at(z, self.now)

    def zone_deadline(self, z, x, y, pad):
        nc = z['next_center']
        if hyp(x - nc[0], y - nc[1]) <= z['next_radius'] - pad:
            return 1e18
        for k in range(25):
            t = z['shrink_start'] + (z['shrink_end'] - z['shrink_start']) * k / 24
            cx, cy, cr = self.zone_at(z, t)
            if hyp(x - cx, y - cy) > cr - pad:
                return t
        return z['shrink_end']

    def zone_cost(self, z, x, y):
        cx, cy, cr = self.zone_now(z)
        p = 0.0
        d = hyp(x - cx, y - cy) - cr
        if d > -5:
            p += 150 + 5 * (d + 5)
        nc = z['next_center']
        dn = hyp(x - nc[0], y - nc[1]) - z['next_radius']
        if dn > -5:
            soon = clamp(1 - (z['shrink_start'] - self.now) / 25, 0.1, 1)
            p += soon * (25 + 0.8 * (dn + 5))
        return p

    def zone_goal(self, z, x, y):
        nc = z['next_center']; nr = z['next_radius']
        if nr <= 30:
            return nc[0], nc[1]
        dx, dy = x - nc[0], y - nc[1]
        d = hyp(dx, dy)
        r = max(0.0, nr - clamp(0.3 * nr, 3, 30) - 5)
        if d < 1 or d <= r:
            return x, y
        return nc[0] + dx / d * r, nc[1] + dy / d * r

    # ------------------------------------------------------------ memory
    def observe(self, obs):
        me = obs['self']; now = self.now
        x, y, zf = me['pos']
        muz = (x, y, zf + me['stats'].get('muzzle_height', 7))
        eye = (x, y, zf + me['stats'].get('eye_height', 8))
        for o in obs['mechs']:
            tab = self.drones if o['kind'] == 'npc' else self.foes
            f = tab.get(o['id'])
            if f is None:
                f = tab[o['id']] = Foe()
            f.o = o; f.seen = now
            dx, dy = o['pos'][0] - x, o['pos'][1] - y
            f.dist = hyp(dx, dy)
            f.los = self.m.los(muz, self.centre(o))
            if not f.los and o['kind'] == 'mech':
                p = o['pos']
                f.los = self.m.los(muz, (p[0], p[1], p[2] + self.mh * self.scale(o) * 0.85))
            if f.dist > 1:
                v = o.get('vel', (0, 0, 0)); mv = me['vel']
                f.closing = -((v[0] - mv[0]) * dx + (v[1] - mv[1]) * dy) / f.dist
        for tab, keep in ((self.foes, 20), (self.drones, 120)):
            for k in [k for k, f in tab.items() if now - f.seen > keep]:
                del tab[k]

        def refresh(tab, items):
            for it in items:
                tab[it['id']] = [it, now]
            for k in list(tab):
                it, seen = tab[k]
                if seen == now:
                    continue
                p = it['pos']
                if hyp(p[0] - x, p[1] - y) < 40 and now - seen > 0.35 and self.m.los(eye, (p[0], p[1], p[2] + 1.5)):
                    del tab[k]
        refresh(self.crates, obs['crates']); refresh(self.cubes, obs['cubes']); refresh(self.picks, obs['pickups'])

        for ev in obs['events']:
            ty = ev.get('type')
            if ty == 'damaged':
                if ev.get('weapon') == 'npc' and ev.get('bearing') is not None:
                    self.drone_hurt_t, self.drone_bearing = now, ev['bearing']
                if ev.get('by') is not None:
                    self.hurt_t = now
                    if ev.get('bearing') is not None:
                        self.hurt_bearing = ev['bearing']
                    for tab in (self.foes, self.drones):
                        if ev['by'] in tab:
                            tab[ev['by']].hit_me = now
            elif ty == 'kill':
                self.kills += 1
                self.foes.pop(ev.get('target'), None)
            elif ty == 'take_failed':
                self.failed[ev.get('id')] = now

        # free-for-all detection: other mechs dying without our help means it is not a squad game
        if self.start_alive is None:
            self.start_alive = obs['alive']
        if self.start_alive - obs['alive'] - self.kills > 0:
            self.odd_deaths += 1
        else:
            self.odd_deaths = 0
        if self.odd_deaths >= 5:
            self.ffa = True

        z = obs['zone']
        if self.hold_start != z['shrink_start'] or now <= z['shrink_start']:
            self.hold_start = z['shrink_start']
            self.hold = (z['center'][0], z['center'][1], z['radius'])

    # ------------------------------------------------------------ navigation
    def travel(self, me, gx, gy):
        x, y = me['pos'][0], me['pos'][1]
        d = hyp(gx - x, gy - y)
        if d < 0.8:
            return 0.0, 0.0
        if d < 45 and self.m.walk_line((x, y), (gx, gy)):
            return (gx - x) / d, (gy - y) / d
        if self.goal is None or hyp(self.goal[0] - gx, self.goal[1] - gy) > 5 or (self.path is None and self.planner is None):
            self.goal = (gx, gy); self.path = None; self.path_i = 0
            s = self.m.nearest_walk(x, y, 6); e = self.m.nearest_walk(gx, gy, 10)
            self.planner = AStar(self.m, s, e, expw=0.0) if (s is not None and e is not None) else None
        if self.planner is not None and not self.planner.done:
            self.planner.step(0.008 if DET else clamp(self.left() - 0.012, 0.001, 0.010))
            if self.planner.done:
                self.path = [self.m.cell_xy(c) for c in self.planner.path] + [self.goal] if self.planner.path else None
                self.path_i = 0; self.planner = None
                if self.path is None:
                    self.goal = None
        wp = (gx, gy)
        if self.path:
            while self.path_i < len(self.path) - 1 and hyp(self.path[self.path_i][0] - x, self.path[self.path_i][1] - y) < 4:
                self.path_i += 1
            best = self.path_i
            for k in range(min(len(self.path) - 1, self.path_i + 10), self.path_i, -1):
                if self.m.walk_line((x, y), self.path[k]):
                    best = k; break
            self.path_i = best
            wp = self.path[best]
            if hyp(wp[0] - x, wp[1] - y) > 30:
                self.goal = None; self.path = None
        dx, dy = wp[0] - x, wp[1] - y
        L = hyp(dx, dy)
        return (dx / L, dy / L) if L > 1e-6 else (0.0, 0.0)

    def walkable(self, x, y):
        return self.m.walkable_pt(x, y)

    # ------------------------------------------------------------ fighting position
    def pref_range(self, me, ws, tgt):
        if tgt['kind'] == 'npc':
            return 58.0
        zap = ws.get('lightning'); las = ws.get('laser')
        r = GUN_R
        if zap is not None and me['energy'] > 12:
            r = ZAP_R
        elif las is not None:
            r = LASER_R
        if zap is None:
            held = tgt.get('weapons') or []
            if 'lightning' in held:
                r = max(r, 64)
            if 'mortar' in held:
                r = max(r, 80)
        return r

    def fight_move(self, obs, ws, tgt, seen, want_los, rp=None):
        me = obs['self']; x, y = me['pos'][0], me['pos'][1]
        o = tgt.o
        if rp is None:
            rp = self.pref_range(me, ws, o)
        bx, by = o['pos'][0] - x, o['pos'][1] - y
        bd = hyp(bx, by) + 1e-9
        ux, uy = bx / bd, by / bd
        if self.now >= self.strafe_until:
            if self.rng.random() < 0.6:
                self.strafe = -self.strafe
            self.strafe_until = self.now + 0.3 + self.rng.random() * 0.7
        sx, sy = -uy * self.strafe, ux * self.strafe
        tc = self.centre(o)
        mh = me['stats'].get('muzzle_height', 7); bh = me['stats'].get('height', 9) * 0.6
        others = sorted((f for f in seen if f is not tgt and f.seen == self.now and f.dist < 200), key=lambda f: f.dist)[:3]
        cube = None; cd = 40.0
        for it, _ in self.cubes.values():
            p = it['pos']
            dd = hyp(p[0] - x, p[1] - y)
            if dd < cd and abs(p[2] - me['pos'][2]) < 4:
                cube, cd = p, dd
        z = obs['zone']
        best = -1e18; mx = my = 0.0; side_blocked = False
        for k in range(-1, 16):
            if k >= 0:
                a = k * TAU / 16
                dx, dy = math.cos(a), math.sin(a)
                cx, cy = x + dx * 6, y + dy * 6
                if not self.m.walk_line((x, y), (cx, cy)):
                    if dx * sx + dy * sy > 0.9:
                        side_blocked = True
                    continue
                r = 6.0
            else:
                dx = dy = 0.0; cx, cy = x, y; r = 0.0
            cz = self.m.ground(cx, cy)
            d = hyp(o['pos'][0] - cx, o['pos'][1] - cy)
            s = -1.1 * abs(d - rp) - 0.4 * r
            los = self.m.los((cx, cy, cz + mh), tc)
            if los:
                s += 45 * want_los
            body = (cx, cy, cz + bh)
            for e in others:
                if hyp(e.o['pos'][0] - cx, e.o['pos'][1] - cy) < 170 and self.m.los(self.eye_of(e.o), body):
                    s -= 30
            s -= self.zone_cost(z, cx, cy)
            if cube is not None:
                s += 1.5 * (cd - hyp(cube[0] - cx, cube[1] - cy))
            if want_los > 0:
                s += 9 * (dx * sx + dy * sy)
            if k < 0:
                s -= 8
                if want_los < 0 and not los:
                    s += 14
            if s > best:
                best, mx, my = s, dx, dy
            if self.left() < 0.012:
                break
        if side_blocked:
            self.strafe = -self.strafe
            self.strafe_until = self.now + 0.5
        return mx, my

    # ------------------------------------------------------------ cover
    def cover_move(self, obs, seen, extra=None):
        me = obs['self']; x, y = me['pos'][0], me['pos'][1]
        eyes = [self.eye_of(f.o) for f in sorted(seen, key=lambda f: f.dist) if f.dist < 230][:4]
        if extra is not None:
            eyes.insert(0, extra)
        if not eyes:
            return None
        h = me['stats'].get('height', 9)
        z = obs['zone']

        def hidden(cx, cy):
            gz = self.m.ground(cx, cy)
            for e in eyes:
                if self.m.los(e, (cx, cy, gz + h)) or self.m.los(e, (cx, cy, gz + h * 0.5)):
                    return False
            return True
        if self.cover_goal is not None and self.now - self.cover_t < 0.4:
            return self.go_cover(me)
        self.cover_t = self.now
        if hidden(x, y) and self.zone_cost(z, x, y) == 0:
            self.cover_goal = (x, y)
            return 0.0, 0.0
        m = self.m
        s = m.nearest_walk(x, y, 4)
        if s is None:
            return None
        NX = m.NX; edges = m.edges
        dist = {s: 0}; queue = [s]; h_i = 0
        best = None; bs = 1e18; checked = 0
        while h_i < len(queue) and checked < 140 and self.left() > 0.010:
            c = queue[h_i]; h_i += 1
            st = dist[c]
            if st > 22 or (best is not None and st * 4 > bs + 30):
                break
            cj, ci = divmod(c, NX)
            if st > 0 and (ci + cj) % 2 == 0:
                cx, cy = m.cell_xy(c)
                checked += 1
                if hidden(cx, cy):
                    sc = st * 4 + self.zone_cost(z, cx, cy)
                    near = min(hyp(e[0] - cx, e[1] - cy) for e in eyes)
                    sc -= min(near, 80) * 0.25
                    if sc < bs:
                        bs, best = sc, (cx, cy)
            e = edges[c]
            for k in range(8):
                if e >> k & 1:
                    dx, dy = ((1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1))[k]
                    n = c + dy * NX + dx
                    if n not in dist:
                        dist[n] = st + 1; queue.append(n)
        if best is None:
            self.cover_goal = None
            return None
        self.cover_goal = best
        return self.go_cover(me)

    def go_cover(self, me):
        g = self.cover_goal
        if hyp(g[0] - me['pos'][0], g[1] - me['pos'][1]) < 1.5:
            return 0.0, 0.0
        return self.travel(me, g[0], g[1])

    # ------------------------------------------------------------ loot
    def want(self, me, kind):
        ws = me['weapons']
        if kind == 'gun':
            return ws[0] is None
        if any(w and w['kind'] == kind for w in ws):
            return False
        if kind not in PREF:
            return False
        cur = PREF.get(ws[1]['kind'], 0) if len(ws) > 1 and ws[1] else 0
        return PREF[kind] > cur

    def pickup_race_delay(self, item, distance, walk):
        if distance < 2 * self.pick_r:
            return 0.0
        px, py, pz = item['pos']
        for foe in self.foes.values():
            if foe.seen != self.now or item['kind'] in (foe.o.get('weapons') or []):
                continue
            fx, fy, fz = foe.o['pos']
            dx, dy = px - fx, py - fy
            gap = hyp(dx, dy)
            if gap > 65 or distance - gap < 2.5 * walk or abs(fz - pz) > 4:
                continue
            vx, vy = foe.o['vel'][:2]
            if vx * dx + vy * dy > 0.7 * gap * max(6, hyp(vx, vy)):
                return 8.0
        return 0.0

    def choose_loot(self, obs, hp, danger):
        me = obs['self']; x, y = me['pos'][0], me['pos'][1]
        z = obs['zone']; now = self.now
        cx0, cy0, cr0 = self.zone_now(z)
        walk = me['stats'].get('walk_speed', 11.7)
        best = None; bu = 0.0
        cands = []
        ws = me['weapons']
        empty = len(ws) > 1 and ws[1] is None
        for it, _ in self.picks.values():
            if now - self.failed.get(it['id'], -99) < 3 or not self.want(me, it['kind']):
                continue
            rad = SEEK if it['kind'] == 'laser' else LOOT_R
            if hyp(it['pos'][0] - x, it['pos'][1] - y) < rad:
                v = 130.0 if empty else 60.0
                if it['kind'] == 'laser':
                    v = max(v, 150.0)
                cands.append(('pickup', it, v))
        cube_v = 25.0 if me['cubes'] >= self.cube_cap else 75.0
        for it, _ in self.cubes.values():
            if hyp(it['pos'][0] - x, it['pos'][1] - y) < max(60, LOOT_R * 0.7):
                cands.append(('cube', it, cube_v))
        if hp > FARM_HP and not danger:
            for it, _ in self.crates.values():
                if hyp(it['pos'][0] - x, it['pos'][1] - y) < LOOT_R:
                    cands.append(('crate', it, cube_v * 0.6))
            if me['cubes'] < self.cube_cap:
                for f in self.drones.values():
                    if now - f.seen < 12 and f.dist < LOOT_R:
                        cands.append(('npc', f.o, 85.0))
        for kind, it, v in cands:
            iid = (kind, it['id'])
            if self.banned.get(iid, -1) > now:
                continue
            p = it['pos']
            d = hyp(p[0] - x, p[1] - y)
            if d > 260 or hyp(p[0] - cx0, p[1] - cy0) > cr0 - 6:
                continue
            if now + d * 1.4 / walk + 5 > self.zone_deadline(z, p[0], p[1], 6):
                continue
            if kind != 'npc' and p[2] - self.m.ground(p[0], p[1]) > 3.2:
                continue
            race_delay = self.pickup_race_delay(it, d, walk) if kind == 'pickup' else 0.0
            u = v / (d + 18 + walk * race_delay)
            if iid == self.loot_id:
                u *= 1.35
            if u > bu:
                bu, best = u, (kind, it)
        if best is None:
            self.loot_id = None
            return None
        iid = (best[0], best[1]['id'])
        if iid != self.loot_id:
            self.loot_id = iid; self.loot_since = now
        elif now - self.loot_since > 30:
            self.banned[iid] = now + 60
            self.loot_id = None
            return None
        return best

    # ------------------------------------------------------------ dodging
    def dodge(self, obs):
        me = obs['self']; st = me['stats']
        cx, cy = me['pos'][0], me['pos'][1]; cz = me['pos'][2] + st.get('height', 9) * 0.56
        reach = st.get('radius', 2.5) + 1.1
        dx = dy = 0.0; laser = False
        for p in obs['projectiles']:
            if p['kind'] == 'mortar':
                continue
            vx, vy, vz = p['vel']
            v2 = vx * vx + vy * vy + vz * vz
            if v2 < 1:
                continue
            px, py, pz = p['pos']
            tc = ((cx - px) * vx + (cy - py) * vy + (cz - pz) * vz) / v2
            if tc <= 0 or tc > 0.7:
                continue
            ox, oy = px + vx * tc - cx, py + vy * tc - cy
            if hyp(ox, oy) < reach:
                vh = hyp(vx, vy)
                if vh < 1:
                    continue
                nx, ny = -vy / vh, vx / vh
                if nx * ox + ny * oy > 0:
                    nx, ny = -nx, -ny
                dx += nx; dy += ny
        for o in obs['mechs']:
            if o['kind'] != 'mech' or not o.get('charging'):
                continue
            to_me = math.atan2(cy - o['pos'][1], cx - o['pos'][0])
            if abs(wrap(o['aim'][0] - to_me)) > 0.15:
                continue
            dx += math.cos(to_me + math.pi / 2 * self.strafe) * 1.5
            dy += math.sin(to_me + math.pi / 2 * self.strafe) * 1.5
            laser = True
        L = hyp(dx, dy)
        if L > 0.01:
            return dx / L, dy / L, laser
        return 0.0, 0.0, False

    # ------------------------------------------------------------ aiming
    def lead(self, muz, tp, tv, speed, grounded):
        speed = speed or 252
        t = 0.0
        for _ in range(4):
            tt = LAT + t
            px, py, pz = tp[0] + tv[0] * tt, tp[1] + tv[1] * tt, tp[2]
            if not grounded:
                pz += tv[2] * tt * 0.5
            d = math.sqrt((px - muz[0]) ** 2 + (py - muz[1]) ** 2 + (pz - muz[2]) ** 2)
            t = max(0.0, d - 3) / speed
        return math.atan2(py - muz[1], px - muz[0]), math.atan2(pz - muz[2], hyp(px - muz[0], py - muz[1]))

    # ------------------------------------------------------------ main
    def act(self, obs):
        self.t0 = time.perf_counter()
        try:
            return self.step(obs)
        except Exception:
            if os.environ.get('V8_DEBUG'):
                raise
            a = {'move': [0.0, 0.0]}
            if self.last_aim:
                a['aim'] = list(self.last_aim)
            return a

    def step(self, obs):
        self.now = now = obs['t']
        self.observe(obs)
        me = obs['self']; st = me['stats']
        x, y, zf = me['pos']
        z = obs['zone']
        hp = me['hp'] / max(1.0, me['max_hp'])
        act = {}
        if obs.get('offer') and obs['offer'].get('choices'):
            ch = obs['offer']['choices']
            act['pick'] = min(range(len(ch)), key=lambda i: UPGRADES.index(ch[i]['id']) if ch[i]['id'] in UPGRADES else 99)

        wl = me['weapons']
        ws = {}
        for i, w in enumerate(wl):
            if w:
                ws[w['kind']] = (i, w)
        gun = ws.get('gun', (None, None))[1]
        zap = ws.get('lightning', (None, None))[1]
        las = ws.get('laser', (None, None))[1]
        wk = {k: v[1] for k, v in ws.items()}
        late = z['next_radius'] <= 70 or obs['alive'] <= 3

        # ---------------- who to fight
        seen = []; target = None; bscore = 1e18
        for f in self.foes.values():
            if now - f.seen > 2.5:
                continue
            f.engage = False
            seen.append(f)
            if f.seen != now:
                f.los = False
                continue
            o = f.o
            to_me = math.atan2(y - o['pos'][1], x - o['pos'][0])
            off = abs(wrap(o['aim'][0] - to_me))
            aimed = off < 0.22
            ohp = o.get('hp', 1400); omax = o.get('max_hp', 1400) or 1400
            v = o.get('vel', (0, 0, 0))
            approach = v[0] * math.cos(to_me) + v[1] * math.sin(to_me)
            active = now - f.hit_me < 7 or (aimed and (approach > 2 or o.get('charging')))
            if self.ffa:
                f.engage = (f.dist < 60 or now - f.hit_me < 4 or (aimed and o.get('charging') and f.dist < 120)
                            or (ohp < 350 and f.dist < 120) or obs['alive'] <= 2)
            else:
                f.engage = (f.dist < 38 or active or late or (ohp < 0.4 * omax and f.dist < 160)
                            or (o['id'] == self.stick_id and now - self.stick_t < 5)
                            or (las is not None and hp > .83 and f.dist < 170 and now - self.hurt_t > 9))
            if not f.engage or not f.los or f.dist > REACH:
                continue
            effective_hp = ohp
            if las is not None and f.dist < las['stats']['range'] * 0.97:
                beam = las['stats']['damage']
                cycle = las['stats']['charge'] + las['stats']['cooldown']
                gun_damage = 0.0
                if gun is not None and f.dist < gun['stats']['range'] * 0.96:
                    fire_time = max(0.0, cycle - gun.get('reload', 0))
                    shots = min(gun.get('ammo', 0), fire_time / gun['stats']['cooldown'])
                    gun_damage = shots * gun['stats']['damage']
                bonus = las['stats'].get('cookoff', 0.6) if 'mortar' in (o.get('weapons') or []) else 0.0
                effective_hp *= (gun_damage + beam) / (gun_damage + beam * (1 + bonus))
                damage_scale = 1 + self.dpc * min(me['cubes'], self.cube_cap)
                if las.get('cooldown', 0) <= 0.1 and ohp <= 0.7 * beam * (1 + bonus) * damage_scale:
                    effective_hp *= 0.45
            s = f.dist + HPW * effective_hp
            if ohp <= 0.3 * omax:
                s -= LOWB
            if o['id'] == self.stick_id and now - self.stick_t < 2:
                s -= STICK
            if active:
                s -= 160
            if now - f.hit_me < 4:
                s -= 90
            if f.closing > 3:
                s -= 15
            if s < bscore:
                bscore, target = s, f
        if target is not None:
            self.stick_id, self.stick_t = target.o['id'], now

        danger = any(f.engage or now - f.hit_me < 4 for f in seen)
        crowd = sum(1 for f in seen if f.seen == now and (f.engage or (f.los and f.dist < 150)))
        if danger or now - self.hurt_t < 3:
            self.threat_t = now

        thp = target.o.get('hp', 1400) if target else 1e9
        finishing = target is not None and target.dist < 120 and (thp < 200 or thp < me['hp'] * 0.5)

        # ---------------- lose rule: retreat only while the fight is going badly
        near = sum(1 for f in seen if f.seen == now and f.dist < 160)
        losing = (near >= 2 and hp < 0.55) or (near >= 3 and hp < 0.8)
        if target is not None:
            tf = thp / max(1.0, target.o.get('max_hp', 1400))
            if hp < 0.35 and tf > hp + 0.1:
                losing = True
            if tf < 0.25 and hp > 0.2 and near <= 2:
                losing = False
        if losing:
            self.lose_t = now
        retreat = now - self.lose_t < 1.5

        # ---------------- movement
        mx = my = 0.0; boost = False; mode = 'idle'
        farm = None; farm_pt = None; defend = False
        zgx, zgy = self.zone_goal(z, x, y)
        zdist = hyp(zgx - x, zgy - y)
        walk = st.get('walk_speed', 11.7)
        t_need = zdist * 1.45 / walk + 4
        deadline = self.zone_deadline(z, x, y, 8)
        zone_must = (not me['in_zone']) or now + t_need + 3 >= deadline
        zone_should = now + t_need + 14 >= deadline
        energy = me['energy']

        if now < self.unstuck_until:
            mx, my = self.unstuck_dir; mode = 'unstuck'
            if me['grounded']:
                act['jump'] = True
            boost = energy > 10
        elif zone_must and zdist > 2:
            mx, my = self.travel(me, zgx, zgy)
            boost = ((not me['in_zone']) or now + t_need >= deadline) and energy > 5
            mode = 'zone'
        elif retreat and not finishing and not late:
            mv = self.cover_move(obs, seen)
            if mv is not None:
                mx, my = mv; boost = energy > 10 and (mx or my); mode = 'cover'
            elif target is not None:
                mx, my = self.fight_move(obs, wk, target, seen, 1); mode = 'cornered'
            else:
                mx, my = self.travel(me, zgx, zgy)
                if now - self.drone_hurt_t < 3 and mx == 0 and my == 0:
                    side = 1.0 if (now % 3) <= 1.5 else -1.0
                    mx, my = -math.sin(self.drone_bearing) * side, math.cos(self.drone_bearing) * side
                mode = 'flee'
        elif self.ffa and target is None and now - self.hurt_t < 2.5 and not late:
            ex = (x + math.cos(self.hurt_bearing) * 80, y + math.sin(self.hurt_bearing) * 80, zf + 8)
            mv = self.cover_move(obs, seen, extra=ex)
            if mv is not None:
                mx, my = mv; boost = energy > 30 and (mx or my); mode = 'cover'
        elif target is not None:
            ready = gun is not None and gun.get('ammo', 0) > 0 and gun.get('reload', 0) <= 0.25 and target.dist <= gun['stats'].get('range', 230) - 10
            if las is not None or zap is not None:
                ready = True
            want_los = 1.0 if ready else -1.0
            tmax = target.o.get('max_hp', 1400)
            if thp < 0.38 * tmax and hp > 0.42 and crowd <= 2:
                mx, my = self.fight_move(obs, wk, target, seen, want_los, rp=FIN_R)
                if target.dist > 2 * FIN_R and energy > 30:
                    boost = True
            else:
                mx, my = self.fight_move(obs, wk, target, seen, want_los)
            if zap is not None and target.dist > 50 and energy > 55:
                boost = True
            mode = 'fight'
            r = CUBE_GRAB * (0.57 if hp <= 0.5 else 1.0)
            bc = None
            for it, _ in self.cubes.values():
                d = hyp(it['pos'][0] - x, it['pos'][1] - y)
                if d < r:
                    r, bc = d, it['pos']
            if bc is not None and not (me['cubes'] >= self.cube_cap or (crowd > 1 and r > 12)):
                mx, my = self.travel(me, bc[0], bc[1]); mode = 'fight:cube'
        elif zone_should and zdist > 2:
            mx, my = self.travel(me, zgx, zgy); mode = 'zone'
        else:
            lc = self.choose_loot(obs, hp, danger)
            if lc is not None:
                kind, it = lc
                mode = 'loot:' + kind
                p = it['pos']
                if kind == 'npc':
                    f = self.drones.get(it['id'])
                    if f is not None and now - f.seen < 1 and f.los and f.dist < 75:
                        farm = f.o
                        mx, my = self.fight_move(obs, wk, f, seen, 1)
                    else:
                        mx, my = self.travel(me, p[0], p[1])
                elif kind == 'crate':
                    d = hyp(p[0] - x, p[1] - y)
                    cp = (p[0], p[1], p[2] + self.crate_h * 0.5)
                    pitch = math.atan2(cp[2] - zf - st.get('muzzle_height', 7), max(d, 0.1))
                    if d < 50 and st.get('min_pitch', -0.7) + 0.05 < pitch < st.get('max_pitch', 1.3) - 0.05 and self.m.los((x, y, zf + st.get('muzzle_height', 7)), cp):
                        farm_pt = cp
                        if d < 12:
                            mx, my = (x - p[0]) / max(d, 0.1), (y - p[1]) / max(d, 0.1)
                    else:
                        mx, my = self.travel(me, p[0], p[1])
                else:
                    mx, my = self.travel(me, p[0], p[1])
            elif 'patrol' in F and z['next_radius'] > 30:
                nc = z['next_center']; a = self.phase + now * 0.043
                px_, py_ = nc[0] + math.cos(a) * z['next_radius'] * 0.6, nc[1] + math.sin(a) * z['next_radius'] * 0.6
                mx, my = self.travel(me, px_, py_); mode = 'patrol'
            elif zdist > 3:
                mx, my = self.travel(me, zgx, zgy); mode = 'zone-walk'

        if not zone_must and not retreat and wl[1] is None and now < 100:
            best_pick = None
            best_cost = 10.0 if target is None else 4.0
            for pick, last in self.picks.values():
                if pick['kind'] not in PREF or now - last > 15:
                    continue
                p = pick['pos']
                distance = hyp(p[0] - x, p[1] - y)
                if distance > 130 or abs(p[2] - self.m.ground(p[0], p[1])) > 3.2:
                    continue
                if now + distance / walk + 6 > self.zone_deadline(z, p[0], p[1], 6):
                    continue
                cost = distance / walk + (0 if pick['kind'] == 'laser' else 1.7)
                cost += self.pickup_race_delay(pick, distance, walk)
                if cost < best_cost:
                    best_cost, best_pick = cost, p
            if best_pick is not None:
                mx, my = self.travel(me, best_pick[0], best_pick[1])
                boost = energy > 28 and best_cost > .5
                mode = 'equip'
        dx, dy, beam = self.dodge(obs)
        if (dx or dy) and mode != 'unstuck':
            mx, my = mx * 0.4 + dx * 0.9, my * 0.4 + dy * 0.9
            L = hyp(mx, my)
            if L > 1:
                mx, my = mx / L, my / L
            if beam and energy > 25:
                boost = True

        # stuck detection
        if (mx or my) and hyp(me['vel'][0], me['vel'][1]) < 1.2 and me['grounded'] and mode != 'unstuck':
            if self.stuck_since is None:
                self.stuck_since = now
            elif now - self.stuck_since > 0.7:
                self.stuck_n += 1
                if self.stuck_n % 2 == 1:
                    L = hyp(mx, my) or 1.0
                    self.unstuck_dir = (mx / L, my / L); self.unstuck_until = now + 1.0
                else:
                    a = self.rng.random() * TAU
                    self.unstuck_dir = (math.cos(a), math.sin(a)); self.unstuck_until = now + 0.6
                self.stuck_since = None
                self.goal = None; self.path = None; self.planner = None
                act['jump'] = True
        else:
            self.stuck_since = None

        # ---------------- aim and fire
        muz = (x + me['vel'][0] * LAT, y + me['vel'][1] * LAT, zf + st.get('muzzle_height', 7))
        aim = [me['aim'][0], 0.0]; have_aim = False
        fire = [False] * max(2, len(wl))
        if target is None and now - self.drone_hurt_t < 3:
            worst = None; bo = 0.7
            for f in self.drones.values():
                if now - f.seen > 0.5 or not f.los or f.dist > 150:
                    continue
                o = math.atan2(f.o['pos'][1] - y, f.o['pos'][0] - x)
                off = 0.0 if now - f.hit_me < 3 else abs(wrap(o - self.drone_bearing))
                if off < bo:
                    bo, worst = off, f.o
            if worst is not None:
                farm, farm_pt, defend = worst, None, True
        shoot = None; is_mech = False
        if target is not None:
            o = target.o; sc = self.scale(o)
            tr, th = self.mr * sc, self.mh * sc
            tp = [o['pos'][0], o['pos'][1], o['pos'][2] + th * 0.5]
            if not self.m.los(muz, tp):
                tp[2] = o['pos'][2] + th * 0.85
            shoot = (tp, o.get('vel', (0, 0, 0)), tr, th); is_mech = True
        elif farm is not None:
            tp = [farm['pos'][0], farm['pos'][1], farm['pos'][2] + self.npc_h * 0.5]
            shoot = (tp, farm.get('vel', (0, 0, 0)), self.npc_r, self.npc_h)
        elif farm_pt is not None:
            shoot = (list(farm_pt), (0, 0, 0), self.crate_r, self.crate_h)
        if shoot is not None and not is_mech and (retreat or hp < 0.5) and not defend:
            shoot = None

        if shoot is not None:
            tp, tv, tr, th = shoot
            dist = hyp(tp[0] - x, tp[1] - y)
            grounded = abs(tv[2]) < 0.5
            gy = gp = 0.0; gun_ok = False
            if gun is not None:
                gy, gp = self.lead(muz, tp, tv, gun['stats'].get('speed', 252), grounded)
                gun_ok = dist <= gun['stats'].get('range', 230) * 0.96
            dp = (tp[0] + tv[0] * (LAT + 0.05), tp[1] + tv[1] * (LAT + 0.05), tp[2])
            hy = math.atan2(dp[1] - muz[1], dp[0] - muz[0])
            hpch = math.atan2(dp[2] - muz[2], hyp(dp[0] - muz[0], dp[1] - muz[1]))
            aim = [gy, gp] if gun is not None else [hy, hpch]
            have_aim = True
            tol_y = math.atan2(tr * 0.75, max(dist, 1)); tol_p = math.atan2(th * 0.4, max(dist, 1))
            turn = st.get('turn_rate', 4) * 0.05; pturn = st.get('pitch_rate', 3) * 0.05

            pmin = st.get('min_pitch', -0.7); pmax = st.get('max_pitch', 1.3)

            def slew_ok(wy, wp):
                if wp < pmin + 0.01 or wp > pmax - 0.01:
                    return False          # cannot point the gun there at all
                return (max(0.0, abs(wrap(wy - me['aim'][0])) - turn) < tol_y
                        and max(0.0, abs(wp - me['aim'][1]) - pturn) < tol_p)
            for i, w in enumerate(wl[:2]):
                if not w:
                    continue
                k = w['kind']; wst = w['stats']
                if k == 'gun':
                    if gun_ok and w.get('ammo', 0) > 0 and w.get('reload', 0) <= 0 and slew_ok(gy, gp):
                        fire[i] = True
                elif k == 'lightning':
                    cone = math.radians(wst.get('cone_deg', 30))
                    if dist < wst.get('range', 45) - 1.5 and abs(wrap(hy - me['aim'][0])) < cone * 0.5 - 0.05 and energy >= wst.get('energy', 3):
                        fire[i] = True
                elif k == 'laser':
                    if not is_mech or dist > wst.get('range', 280) * 0.97:
                        continue
                    full = wst.get('charge', 0.75); ch = w.get('charge', 0)
                    tight = abs(wrap(hy - me['aim'][0])) < tol_y * 0.9 and abs(hpch - me['aim'][1]) < tol_p and pmin < hpch < pmax
                    if wst.get('min_charge', 0.15) <= ch < full - 0.051:
                        dmg = wst.get('damage', 315) * (wst.get('min_frac', 0.35) + (1 - wst.get('min_frac', 0.35)) * ch / full)
                        dmg *= 1 + self.dpc * min(me['cubes'], self.cube_cap)
                        if 'mortar' in (target.o.get('weapons') or []):
                            dmg *= 1 + wst.get('cookoff', 0.6)
                        if dmg * 0.7 >= thp and tight:
                            aim = [hy, hpch]; fire[i] = False
                            continue
                    if ch < full - 0.051:
                        fire[i] = True
                        if ch > full * 0.45:
                            aim = [hy, hpch]
                    else:
                        aim = [hy, hpch]
                        fire[i] = not tight
        elif las is not None and las.get('charge', 0) > 0:
            # never dump a charged shot into a wall: keep holding while we can
            wst = las['stats']
            fire[ws['laser'][0]] = las['charge'] < wst.get('charge', 0.75) + wst.get('hold_s', 2) - 0.35

        if not have_aim:
            watch = None
            for f in seen:
                if (f.engage or now - f.hit_me < 6) and (watch is None or f.dist < watch.dist):
                    watch = f
            if watch is not None:
                aim[0] = math.atan2(watch.o['pos'][1] - y, watch.o['pos'][0] - x)
            elif now - self.drone_hurt_t < 2.5:
                aim[0] = self.drone_bearing
            elif now - self.hurt_t < 2.5:
                aim[0] = self.hurt_bearing
            elif now < 8 and wl[1] is None:
                aim[0] = me['aim'][0] + .65
            elif mx or my:
                aim[0] = math.atan2(my, mx) + 1.15 * math.sin(now * 1.3)
            else:
                aim[0] = me['aim'][0] + 0.35
            aim[1] = 0.0
        aim[1] = clamp(aim[1], st.get('min_pitch', -0.7), st.get('max_pitch', 1.3))
        if not (math.isfinite(aim[0]) and math.isfinite(aim[1])):
            aim = list(self.last_aim or me['aim'])
        self.last_aim = aim
        act['aim'] = aim

        if gun is not None and gun.get('reload', 0) <= 0 and gun.get('ammo', 0) < gun['stats'].get('mag', 12):
            if target is None and farm is None and farm_pt is None and (gun.get('ammo', 0) <= gun['stats'].get('mag', 12) * 0.6 or now - self.threat_t > 3):
                act['reload'] = True

        # ---------------- pick up weapons we walk over
        for p in obs['pickups']:
            if hyp(p['pos'][0] - x, p['pos'][1] - y) > self.pick_r - 0.3 or abs(p['pos'][2] - zf) > 3.6:
                continue
            if now - self.failed.get(p['id'], -99) < 2 or not self.want(me, p['kind']):
                continue
            act['take'] = p['id']
            if len(wl) > 1 and wl[1] is not None and wl[0] is not None:
                if wl[1]['kind'] == 'laser' and wl[1].get('charge', 0) > 0:
                    act.pop('take'); break
                act['drop'] = 1
            break

        if not (math.isfinite(mx) and math.isfinite(my)):
            mx = my = 0.0
        act['move'] = [mx, my]
        act['boost'] = bool(boost and (mx or my))
        act['fire'] = fire[:2]
        self.mode = mode
        return act


if __name__ == '__main__':
    mf.run(Bot)
