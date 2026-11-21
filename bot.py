"""Mech Royale bot v2: laser sniper that keeps house bots at long range, camps the zone edge
away from enemies, farms cubes early, and breaks line of sight when focused."""
import math, random, sys, time, os
F = set(os.environ.get("FLAGS", "patrol,nohide,zfix").split(","))
E = lambda k, d: float(os.environ.get(k, d))
PFRAC = E("PFRAC", 0.52); PSPD = E("PSPD", 0.043); FARMT = E("FARMT", 150); FARMC = E("FARMC", 6); TRL = E("TRL", 220); TRN = E("TRN", 180); NL = E("NL", 150); NN = E("NN", 105); KW = E("KW", 1.0); WOUND = E("WOUND", 0.62); AVR = E("AVR", 130); AVW = E("AVW", 0.8)
import mechfight as mf
from geo import Map, AStar, DET

TAU = 2 * math.pi
UPG_ORDER = ['plating', 'armor', 'nanites', 'laser_charge', 'laser_damage', 'gun_damage', 'gun_rate',
             'lightning_efficiency', 'lightning_range', 'gun_mag', 'servos', 'lightning_chain', 'gyros',
             'gun_velocity', 'reactor', 'capacitor', 'jets', 'sensors', 'mortar_reload', 'mortar_splash',
             'mortar_velocity', 'laser_pierce']
RANK = {None: 0, 'gun': 0, 'mortar': 0, 'lightning': 1, 'laser': 2}


def wrap(a):
    return math.atan2(math.sin(a), math.cos(a))


def d2(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def ang(a, b):
    return math.atan2(b[1] - a[1], b[0] - a[0])


class Bot:
    name = "comet-v4"

    def __init__(self, hello):
        self.cfg = hello['config']
        self.id = hello['slot']
        self.rng = random.Random(hello.get('seed', 0))
        self.phase = self.rng.uniform(-math.pi, math.pi)
        self.m = Map()
        self.attackers = {}
        self.dmg_bearing = None; self.dmg_t = -99
        self.last_hit_t = -99
        self.target_id = None
        self.lasers = {}
        self.enemy_seen = {}          # id -> (t, pos)
        self.goal = None; self.planner = None; self.path = None; self.path_i = 0
        self.camp = None; self.camp_t = -99; self.camp_key = None
        self.anchor = None; self.anchor_t = 0; self.jet_until = -1
        self.heading = self.phase
        self.full_since = None
        self.cover = None; self.cover_t = -99; self.hiding_since = None
        self.kills_at = []
        self.mode = ''
        self.failed = {}

    # ---------------------------------------------------------------- helpers
    def size(self, tgt):
        k = tgt.get('kind', 'crate')
        if k == 'mech':
            s = 1 + 0.05 * tgt.get('cubes', 0); return 2.5 * s, 9 * s
        if k == 'npc':
            return 1.8, 3.5
        return 1.4, 2.4

    def reachable_item(self, p, value=0, energy=0):
        above = p[2] - self.m.ground(p[0], p[1])
        return above < 2.5

    # ---------------------------------------------------------------- navigation
    def set_goal(self, g, me):
        if self.goal is not None and d2(self.goal, g) < 4 and (self.planner or self.path):
            return
        self.goal = (g[0], g[1]); self.path = None; self.path_i = 0
        s = self.m.nearest_walk(me['pos'][0], me['pos'][1], 6)
        e = self.m.nearest_walk(g[0], g[1], 10)
        self.planner = AStar(self.m, s, e, expw=0.0) if (s is not None and e is not None) else None

    def waypoint(self, me, budget):
        px, py = me['pos'][0], me['pos'][1]
        if self.goal is None:
            return None
        if self.planner is not None and not self.planner.done:
            self.planner.step(budget)
            if self.planner.done:
                self.path = [self.m.cell_xy(c) for c in self.planner.path] + [self.goal] if self.planner.path else None
                self.path_i = 0; self.planner = None
        if self.path:
            while self.path_i < len(self.path) - 1 and d2(self.path[self.path_i], (px, py)) < 4:
                self.path_i += 1
            best = self.path_i
            for k in range(min(len(self.path) - 1, self.path_i + 8), self.path_i, -1):
                if self.m.walk_line((px, py), self.path[k]):
                    best = k; break
            self.path_i = best
            w = self.path[best]
            if d2(w, (px, py)) > 35:
                g = self.goal; self.goal = None; self.set_goal(g, me)
            return w
        return self.goal

    def blocked_ahead(self, x, y, z):
        k = self.m.vidx(x, y)
        if k < 0:
            return True
        if self.m.walk1[k]:
            return False
        # not in the walk raster: blocked if an obstacle spans our body or terrain rises steeply
        zc = z * 100
        if self.m.obase[k] < zc + 900 and self.m.otop[k] > zc + 120:
            return True
        return self.m.surf[k] > zc + 150

    # ---------------------------------------------------------------- cover
    def find_cover(self, me, eyes, zone_c, zone_r):
        m = self.m; px, py = me['pos'][0], me['pos'][1]
        best = None; bs = 1e9
        for r in (8, 14, 22, 32, 42):
            for k in range(16):
                a = k * TAU / 16 + r * 0.1
                c = m.nearest_walk(px + math.cos(a) * r, py + math.sin(a) * r, 1)
                if c is None:
                    continue
                cx, cy = m.cell_xy(c)
                if d2((cx, cy), zone_c) > zone_r - 5:
                    continue
                gz = m.ground(cx, cy)
                if any(m.los(e, (cx, cy, gz + 4.5)) or m.los(e, (cx, cy, gz + 8.8)) for e in eyes):
                    continue
                s = r
                for e in eyes:
                    de = d2((cx, cy), e)
                    if de < 50:
                        s += (50 - de) * 1.5
                if s < bs:
                    bs = s; best = (cx, cy)
            if best is not None and r >= 14:
                break
        return best

    # ---------------------------------------------------------------- camp spot
    def pick_camp(self, obs, me, enemies_mem):
        z = obs['zone']; nc = z['next_center']; nr = z['next_radius']
        if nr < 6:
            return (nc[0], nc[1])
        t = obs['t']
        # direction away from where enemies were seen recently
        vx = vy = 0.0
        for (tt, p) in enemies_mem.values():
            if t - tt < 40:
                a = ang(nc, p); w = 1.0
                vx += math.cos(a) * w; vy += math.sin(a) * w
        px, py = me['pos'][0], me['pos'][1]
        mine = ang(nc, (px, py)) if d2(nc, (px, py)) > 1 else self.phase
        best = None; bs = -1e9
        frac = 0.66 if nr > 30 else 0.4
        for k in range(24):
            a = k * TAU / 24
            x = nc[0] + math.cos(a) * nr * frac; y = nc[1] + math.sin(a) * nr * frac
            c = self.m.nearest_walk(x, y, 4)
            if c is None:
                continue
            cx, cy = self.m.cell_xy(c)
            if d2((cx, cy), nc) > nr * 0.85:
                continue
            s = -(math.cos(a) * vx + math.sin(a) * vy) * 3.0 + math.cos(a - mine) * 2.0
            s -= d2((cx, cy), (px, py)) / max(60.0, nr)
            for (tt, p) in enemies_mem.values():
                if t - tt < 30:
                    dd = d2((cx, cy), p)
                    if dd < 150:
                        s -= (150 - dd) / 30.0
            for dr in obs.get('drops', []):
                if d2((cx, cy), dr['pos']) < 70:
                    s -= 2.0
            if s > bs:
                bs = s; best = (cx, cy)
        return best or (nc[0], nc[1])

    # ---------------------------------------------------------------- targets
    def choose_target(self, obs, me, enemies, muzzle, wounded, urgent, sniping):
        t = obs['t']; pos = me['pos']
        cand = [e for e in enemies if d2(pos, e['pos']) < (TRL if sniping else TRN)
                or t - self.attackers.get(e['id'], -99) < 6 or e.get('hp', 1400) < 450
                or (e['id'] == self.target_id and d2(pos, e['pos']) < 130)]
        if not any(d2(pos, e['pos']) < 60 for e in cand):
            drones = sorted((m for m in obs['mechs'] if m['kind'] == 'npc' and t - self.attackers.get(m['id'], -99) <= 5
                             and d2(pos, m['pos']) < 90), key=lambda m: d2(pos, m['pos']))
            for dr in drones:
                q = dr['pos']
                if self.m.los(muzzle, (q[0], q[1], q[2] + 1.8)):
                    return dr
        vis = []
        for e in cand[:7]:
            d = d2(pos, e['pos']); _, h = self.size(e); p = e['pos']
            if self.m.los(muzzle, (p[0], p[1], p[2] + h * 0.52)):
                pr = 0.58 * d + 0.072 * e.get('hp', 1400) - 14 * (e['id'] == self.target_id) - 30 * (t - self.attackers.get(e['id'], -99) < 3)
                vis.append((pr, e))
        if vis:
            return min(vis, key=lambda v: v[0])[1]
        if wounded or urgent or (cand and d2(pos, cand[0]['pos']) < 85):
            return cand[0] if cand else None
        farm = [m for m in obs['mechs'] if m['kind'] == 'npc' and d2(pos, m['pos']) < 77]
        reach = 110 if t < FARMT and me['cubes'] < FARMC else 62
        farm += [dict(c, kind='crate') for c in obs['crates'] if d2(pos, c['pos']) < reach]
        farm.sort(key=lambda it: d2(pos, it['pos']) - 17 * (it.get('kind') == 'npc') - 14 * (it['id'] == self.target_id))
        for it in farm[:6]:
            p = it['pos']; _, h = self.size(it)
            if self.m.los(muzzle, (p[0], p[1], p[2] + h * 0.52)):
                return it
        return cand[0] if cand else None

    def incoming(self, obs, pos, st):
        out = []
        for s in obs['projectiles']:
            if s['kind'] == 'mortar':
                continue
            x, y, z = s['pos']; vx, vy, vz = s['vel']
            sp2 = vx * vx + vy * vy
            if sp2 < 1:
                continue
            eta = ((pos[0] - x) * vx + (pos[1] - y) * vy) / sp2
            if not 0.025 < eta < 0.55:
                continue
            if not pos[2] - 1 < z + eta * vz < pos[2] + st.get('height', 9) + 1:
                continue
            sp = math.sqrt(sp2)
            miss = ((pos[0] - x) * vy - (pos[1] - y) * vx) / sp
            if abs(miss) < st.get('radius', 2.5) + 8:
                out.append((eta, miss, vy / sp, -vx / sp))
        return sorted(out)[:10]

    # ---------------------------------------------------------------- main
    def act(self, obs):
        t0 = time.perf_counter()
        me = obs['self']; z = obs['zone']; t = obs['t']
        pos = me['pos']; st = me['stats']; px, py, pz = pos
        act = {}
        for ev in obs['events']:
            ty = ev['type']
            if ty == 'damaged':
                if ev.get('bearing') is not None:
                    self.dmg_bearing = ev['bearing']; self.dmg_t = t
                if ev.get('by') is not None:
                    self.attackers[ev['by']] = t
            elif ty == 'hit':
                self.last_hit_t = t
            elif ty == 'take_failed':
                self.failed[ev['id']] = t
        enemies = sorted((m for m in obs['mechs'] if m['kind'] == 'mech'), key=lambda m: d2(pos, m['pos']))
        for e in enemies:
            self.enemy_seen[e['id']] = (t, (e['pos'][0], e['pos'][1]))
        ws = me['weapons']
        kinds = [w['kind'] if w else None for w in ws]
        laser_i = kinds.index('laser') if 'laser' in kinds else None
        gun_i = kinds.index('gun') if 'gun' in kinds else None
        lig_i = kinds.index('lightning') if 'lightning' in kinds else None
        mort_i = kinds.index('mortar') if 'mortar' in kinds else None
        sniping = laser_i is not None
        nearby = [e for e in enemies if d2(pos, e['pos']) < (NL if sniping else NN)]
        wounded = me['hp'] < WOUND * me['max_hp']
        h_me = st.get('height', 9)
        muzzle = (px, py, pz + st.get('muzzle_height', 7))
        eye_me = (px, py, pz + h_me * 0.5)

        # ---------------- loot take
        other = [w for w in ws if w and w['kind'] != 'gun']
        held_best = max([RANK[w['kind']] for w in other], default=0)
        free_slot = None in ws
        def want(kind):
            if kind in kinds or kind == 'gun':
                return False
            return free_slot or RANK[kind] > held_best
        loot = [p for p in obs['pickups'] if want(p['kind']) and t - self.failed.get(p['id'], -99) > 2]
        reach = [p for p in loot if d2(pos, p['pos']) <= self.cfg['loot']['pickup_radius'] - 0.2 and abs(pz - p['pos'][2]) <= 3.9]
        if reach:
            p = max(reach, key=lambda p: RANK[p['kind']])
            act['take'] = p['id']
            if not free_slot:
                # replace the worst non-gun weapon
                worst = min((i for i, w in enumerate(ws) if w and w['kind'] != 'gun'), key=lambda i: RANK[ws[i]['kind']], default=None)
                if worst is not None and not (ws[worst]['kind'] == 'laser' and ws[worst].get('charge', 0) > 0):
                    act['drop'] = worst
                else:
                    act.pop('take')
        if obs.get('offer'):
            ch = obs['offer']['choices']
            if 'pcupg' in F:
                ch2 = ["laser_charge", "laser_damage", "plating", "armor", "nanites", "gun_damage", "gun_rate"]
                act['pick'] = min(range(len(ch)), key=lambda i: ch2.index(ch[i]['id']) if ch[i]['id'] in ch2 else 99)
            if 'pick' not in act:
                act['pick'] = min(range(len(ch)), key=lambda i: UPG_ORDER.index(ch[i]['id']) if ch[i]['id'] in UPG_ORDER else 99)

        # ---------------- zone
        nc, nr = z['next_center'], z['next_radius']
        margin = min(18, nr * 0.25)
        excess = d2(pos, nc) - nr + margin
        travel = max(0, excess) / st.get('walk_speed', 11.7)
        urgent = (not me['in_zone']) or (excess > 0 and z['shrink_end'] - t < 10 + travel * 1.8) or d2(pos, z['center']) > z['radius'] - 6
        # current interpolated circle
        if t <= z['shrink_start']:
            cc, cr = z['center'], z['radius']
        elif t >= z['shrink_end']:
            cc, cr = nc, nr
        else:
            f = (t - z['shrink_start']) / max(1e-6, z['shrink_end'] - z['shrink_start'])
            cc = (z['center'][0] + (nc[0] - z['center'][0]) * f, z['center'][1] + (nc[1] - z['center'][1]) * f)
            cr = z['radius'] + (nr - z['radius']) * f

        # ---------------- focus / cover
        focus = [i for i, tt in self.attackers.items() if i < 100 and t - tt < 6]
        seers = [e for e in enemies if e['id'] in focus and self.m.los((e['pos'][0], e['pos'][1], e['pos'][2] + 8), eye_me)]
        if 'nohide' in F:
            seers = []
        if len(seers) >= 2 or (wounded and len(seers) >= 1 and me['hp'] < 0.35 * me['max_hp']):
            if self.hiding_since is None:
                self.hiding_since = t
        if self.hiding_since is not None and (me['hp'] >= 0.85 * me['max_hp'] or t - self.hiding_since > 40 or urgent):
            self.hiding_since = None; self.cover = None
        hiding = self.hiding_since is not None

        # ---------------- target
        target = self.choose_target(obs, me, enemies, muzzle, wounded, urgent, sniping)
        if target:
            self.target_id = target['id']
        tkind = target.get('kind', 'crate') if target else None
        tdist = d2(pos, target['pos']) if target else 1e9
        finishing = tkind == 'mech' and not wounded and len(nearby) <= 1 and tdist < 95 and target.get('hp', 1e9) < me['hp'] * 0.43
        opening = t < 65 and me['cubes'] < 5
        farm_window = t < FARMT and me['cubes'] < FARMC and not any(d2(pos, e['pos']) < 90 for e in enemies)

        # ---------------- goal selection
        goal = None; stop_r = 0; laser_run = False
        zk = (round(nc[0]), round(nc[1]), round(nr))
        if self.camp is None or self.camp_key != zk or t - self.camp_t > 12 or (nearby and t - self.camp_t > 4):
            if time.perf_counter() - t0 < 0.01 or DET:
                self.camp = self.pick_camp(obs, me, self.enemy_seen); self.camp_key = zk; self.camp_t = t
        goal = self.camp
        if 'patrol' in F:
            a_ = self.phase + t * PSPD
            goal = (nc[0] + math.cos(a_) * nr * PFRAC, nc[1] + math.sin(a_) * nr * PFRAC)
        stop_r = min(3.0, max(0.5, nr * 0.2))
        if urgent:
            goal = self.camp if d2(self.camp, nc) < nr else nc
            if not sniping:
                close = [it for it in loot if it['kind'] == 'laser' and d2(pos, it['pos']) < 35 and d2(it['pos'], z['center']) < z['radius'] - 6 and self.reachable_item(it['pos'])]
                if close:
                    it = min(close, key=lambda it: d2(pos, it['pos'])); goal = it['pos']; stop_r = 1.5; laser_run = True
        elif hiding:
            if self.cover is None or t - self.cover_t > 1.5:
                eyes = [(e['pos'][0], e['pos'][1], e['pos'][2] + 8) for e in seers] or \
                       [(e['pos'][0], e['pos'][1], e['pos'][2] + 8) for e in enemies if e['id'] in focus]
                if eyes:
                    self.cover = self.find_cover(me, eyes, cc, cr) or self.cover
                self.cover_t = t
            if self.cover:
                goal = self.cover; stop_r = 1.5
        else:
            opps = []
            def consider(item, value, sr=0):
                p = item['pos']
                if d2(p, z['center']) < z['radius'] - 5 and self.reachable_item(p):
                    opps.append((d2(pos, p) - value, p, sr))
            room = not nearby or (opening and d2(pos, nearby[0]['pos']) > 76 and t - self.dmg_t > 5)
            if room or farm_window:
                for dr in obs.get('drops', []):
                    if dr['lands_in'] < 5 + d2(pos, dr['pos']) / st.get('walk_speed', 11.7):
                        consider(dr, 175 if wounded else 105, dr['radius'] * 0.65)
                for c in obs['crates']:
                    if me['cubes'] < 10:
                        consider(c, 130 if farm_window else (66 if opening else 48), 8)
                if tkind == 'npc' and me['cubes'] < 10:
                    consider(target, 85, 22)
            for it in loot:
                v = {'laser': 260 if opening else 200, 'lightning': 120 if opening else 80}.get(it['kind'], 83 if opening else 52)
                consider(it, v, 1.5)
            for c in obs['cubes']:
                consider(c, 86 if not nearby else 24, 1.5)
            if opps:
                cost, p, sr = min(opps, key=lambda o: o[0])
                if cost < 0:
                    goal = p; stop_r = sr
                    laser_run = (not sniping) and any(it['kind'] == 'laser' and d2(it['pos'], p) < 0.5 for it in loot)
            # remembered lasers
            for p in obs['pickups']:
                if p['kind'] == 'laser':
                    self.lasers[p['id']] = tuple(p['pos'])
            for k, p in list(self.lasers.items()):
                if d2(p, z['center']) > z['radius'] or (k not in {q['id'] for q in obs['pickups']} and math.dist(pos, p) < 15):
                    del self.lasers[k]
            if not sniping and self.lasers and not laser_run:
                k = min(self.lasers, key=lambda k: d2(pos, self.lasers[k]))
                p = self.lasers[k]
                if d2(pos, p) <= 250 and self.reachable_item(p):
                    goal = p; stop_r = 1.5; laser_run = True

        # ---------------- heading
        budget = 0.008 if DET else max(0.002, 0.016 - (time.perf_counter() - t0))
        if goal is not None:
            self.set_goal(goal, me)
            wp = self.waypoint(me, budget) or goal
        else:
            wp = (px, py)
        wanted = ang(pos, wp) if d2(pos, wp) > 0.5 else self.heading
        incoming = self.incoming(obs, pos, st)
        pressure = []
        for e in nearby:
            d = d2(pos, e['pos'])
            w = (1 - d / 110) * (3.5 if wounded or len(nearby) > 1 else 1.45)
            if sniping:
                w = max(0.3, min(3.0, (150 - d) / 25)) * (1.4 if wounded else 1.0)
            elif lig_i is not None and len(nearby) == 1 and not wounded:
                ideal = ws[lig_i]['stats'].get('range', 45) * (0.52 if finishing else 0.69)
                w = max(-3.4, min(2.2, (ideal - d) / 7))
            elif finishing and target and e['id'] == target['id']:
                w = max(-2.7, (31 - d) / 10)
            if laser_run and d > 40:
                w *= 0.35
            if hiding:
                w *= 0.5
            if 'zfix' in F and (not me['in_zone'] or d2(pos, cc) > cr - 8):
                w *= 0.25
            pressure.append((ang(pos, e['pos']), w * KW))
        if 'avoid' in F:
            ids = {e['id'] for e in nearby}
            for e in enemies:
                d = d2(pos, e['pos'])
                if e['id'] not in ids and d < AVR:
                    pressure.append((ang(pos, e['pos']), AVW * (1 - d / AVR)))
        best = -1e9; heading = wanted
        side = 1 if math.sin(self.phase + t * 1.6) >= 0 else -1
        for i in range(16):
            th = wanted + i * TAU / 16
            dx, dy = math.cos(th), math.sin(th)
            s = math.cos(th - wanted) * (3.8 if (urgent or hiding) else 1.25)
            if 'zfix' in F and not me['in_zone']:
                s += math.cos(th - ang(pos, cc)) * 6.0
            s += 0.28 * math.cos(th - self.heading)
            for b, w in pressure:
                s -= math.cos(th - b) * w
            if pressure:
                s += side * math.sin(th - pressure[0][0]) * 1.2
            for eta, miss, nx_, ny_ in incoming:
                stt = max(0, eta - 0.075)
                qx = me['vel'][0] * (eta - stt) + dx * st.get('walk_speed', 11.7) * stt
                qy = me['vel'][1] * (eta - stt) + dy * st.get('walk_speed', 11.7) * stt
                s -= max(0, st.get('radius', 2.5) + 1.5 - abs(miss + nx_ * qx + ny_ * qy)) * 0.75
            for step, pen in ((5, 5), (12, 2.8), (23, 1.4)):
                if self.blocked_ahead(px + step * dx, py + step * dy, pz):
                    s -= pen
            # stay inside the current circle
            q = (px + 12 * dx, py + 12 * dy)
            if d2(q, cc) > cr - 3 and d2(q, cc) > d2(pos, cc):
                s -= 3.0
            if s > best:
                best = s; heading = th
        self.heading = heading
        gd = d2(pos, goal) if goal is not None else 0
        stop = goal is not None and gd < stop_r and (not nearby or nr < 4)
        act['move'] = [0.0, 0.0] if stop else [math.cos(heading), math.sin(heading)]
        # unstick
        if self.anchor is None or t - self.anchor_t >= 1.2:
            if self.anchor and d2(pos, self.anchor) < 2 and not stop and me['energy'] > 28:
                self.jet_until = t + 1.8
            self.anchor = (px, py); self.anchor_t = t
        jet = t < self.jet_until and not stop
        if jet and me['grounded']:
            act['jump'] = True
        can_sprint = not stop and me['grounded']
        rush = opening and not nearby and stop_r > 0 and gd > 22
        chase = finishing and tdist > (38 if lig_i is not None else 50)
        kite = sniping and can_sprint and any(d2(pos, e['pos']) < 95 for e in nearby) and me['energy'] > 25
        act['boost'] = bool(jet or kite or (can_sprint and (((urgent or hiding or (nearby and wounded)) and me['energy'] > 30)
                                                       or ((rush or chase or laser_run) and me['energy'] > 66))))

        # ---------------- aim & fire
        fire = [False, False]
        gun = ws[gun_i] if gun_i is not None else None
        if gun is not None:
            act['reload'] = (not target) and gun.get('ammo', 12) < 0.78 * gun['stats'].get('mag', 12) and gun.get('reload', 0) <= 0
        if target:
            p = target['pos']; d = d2(pos, p)
            vel = target.get('vel', (0, 0, 0))
            wdt, hgt = self.size(target)
            laser_ok = sniping and ws[laser_i]['cooldown'] <= 0 and d < ws[laser_i]['stats'].get('range', 280) and (not wounded or len(nearby) <= 1) and tkind != 'crate'
            charging = sniping and ws[laser_i].get('charge', 0) > 0
            gspeed = gun['stats'].get('speed', 252) if gun else 252
            lead = 0.1 if (laser_ok or charging) else d / gspeed + 0.035
            x = p[0] + vel[0] * lead - px; y = p[1] + vel[1] * lead - py
            yaw = math.atan2(y, x)
            az = p[2] + hgt * 0.52 + vel[2] * lead
            pitch = math.atan2(az - muzzle[2], math.hypot(x, y))
            act['aim'] = [yaw, max(st.get('min_pitch', -0.7), min(st.get('max_pitch', 1.3), pitch))]
            ye = abs(wrap(yaw - me['aim'][0])); pe = abs(pitch - me['aim'][1])
            aligned = ye < min(0.22, math.atan2(wdt, d) + st.get('turn_rate', 4) * 0.022) and pe < min(0.2, math.atan2(hgt * 0.42, d) + st.get('pitch_rate', 3) * 0.022)
            clear = self.m.los(muzzle, (px + x, py + y, az))
            breathing = d > 70 and t - self.dmg_t > 2.3 and t - self.last_hit_t > 1.4
            engage = not (wounded and tkind == 'mech' and breathing and obs['alive'] > 2 and target.get('hp', 1e9) > 150)
            if hiding and d > 40 and tkind == 'mech':
                engage = False
            if gun is not None and gun.get('reload', 0) <= 0:
                fire[gun_i] = clear and aligned and engage and d < gun['stats'].get('range', 230)
            if sniping and (laser_ok or charging) and engage:
                w = ws[laser_i]
                full = w.get('charge', 0) >= w['stats'].get('charge', 0.75) - 0.01
                if full and self.full_since is None:
                    self.full_since = t
                if not full:
                    self.full_since = None
                tight = ye < math.atan2(wdt * 0.55, d) + 0.003 and pe < math.atan2(hgt * 0.35, d) + 0.004
                overdue = self.full_since is not None and t - self.full_since > 1.6
                if charging:
                    fire[laser_i] = not (clear and full and (tight or overdue))
                else:
                    fire[laser_i] = clear and ye < 0.35
                if gun is not None:
                    flight = max(0, d / gspeed - lead)
                    lat = abs(vel[0] * math.sin(yaw) - vel[1] * math.cos(yaw))
                    shared = lat * flight < wdt and abs(vel[2]) * flight < hgt * 0.45
                    fire[gun_i] = fire[gun_i] and (d < 60 or shared)
            elif charging:
                fire[laser_i] = True  # keep holding; never dump a charge into a wall
            if lig_i is not None:
                sr = math.dist(muzzle, (p[0], p[1], p[2] + hgt * 0.5))
                fire[lig_i] = clear and engage and sr < ws[lig_i]['stats'].get('range', 45) and ye < math.radians(ws[lig_i]['stats'].get('cone_deg', 30)) * 0.82 and me['energy'] > 6
            if mort_i is not None and engage and tkind == 'mech' and ws[mort_i]['cooldown'] <= 0 and 30 < d < 200 and not any(fire):
                ms = ws[mort_i]['stats']; v = ms.get('speed', 72); g = ms.get('gravity', 22)
                tube = (px, py, pz + h_me + 0.5)
                tof = d / v
                q = (p[0] + vel[0] * tof, p[1] + vel[1] * tof, p[2] + 1)
                dd = d2(tube, q); dz = q[2] - tube[2]
                disc = v ** 4 - g * (g * dd * dd + 2 * dz * v * v)
                if disc > 0:
                    th = math.atan2(v * v - math.sqrt(disc) if clear else v * v + math.sqrt(disc), g * dd)
                    act['aim'] = [math.atan2(q[1] - py, q[0] - px), th]
                    if abs(wrap(act['aim'][0] - me['aim'][0])) < 0.03 and abs(th - me['aim'][1]) < 0.03:
                        fire[mort_i] = True
            if gun is not None and not clear and gun.get('ammo', 12) < gun['stats'].get('mag', 12) * 0.35 and gun.get('reload', 0) <= 0:
                act['reload'] = True
        else:
            if sniping and ws[laser_i].get('charge', 0) > 0:
                fire[laser_i] = True
            scan = math.sin(t * 2.3) * (0.9 if opening else 0.53)
            yaw = self.dmg_bearing if t - self.dmg_t < 2.8 else wrap(self.heading + scan)
            act['aim'] = [yaw, 0.0]
        act['fire'] = fire
        self.mode = ('hide' if hiding else 'urgent' if urgent else 'laser' if laser_run else 'go')
        return act


if __name__ == '__main__':
    mf.run(Bot)
