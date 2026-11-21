"""Mech Royale placement bot. All distances and timers use match units."""
from array import array
from collections import deque
import heapq
import json
import math
from pathlib import Path
import random
import sys
import time

import mechfight as mf


def distance(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def angle(a, b):
    return math.atan2(b[1] - a[1], b[0] - a[0])


def wrap(a):
    return math.atan2(math.sin(a), math.cos(a))


def target_size(config, target):
    kind = target.get("kind", "crate")
    body = config[{"crate": "crates", "npc": "npc", "mech": "mech"}[kind]]
    scale = 1 + config["cubes"]["size_per_cube"] * target.get("cubes", 0)
    return body["radius"] * scale, body["height"] * scale


class Arena:
    """Coarse ground and solid spans for scoring short movement choices."""
    cell = 4

    def __init__(self, folder):
        folder = Path(folder) / "v2"
        world = json.loads((folder / "world.json").read_bytes())
        self.bounds = world["bounds"]
        self.terrain = t = world["terrain"]
        self.heights = array("h")
        self.heights.frombytes((folder / t["file"]).read_bytes())
        if sys.byteorder != "little":
            self.heights.byteswap()
        self.solids = {}
        for prism in world["prisms"]:
            edges = []
            for ring in [prism["ring"], *prism["holes"]]:
                points = list(zip(ring[::2], ring[1::2]))
                edges.extend(zip(points, points[1:] + points[:1]))
            ys = prism["ring"][1::2]
            for j in range(math.floor(min(ys)/self.cell), math.ceil(max(ys)/self.cell)):
                y = (j + 0.5) * self.cell
                xs = sorted(x1 + (x2-x1)*(y-y1)/(y2-y1)
                            for (x1,y1),(x2,y2) in edges if (y1 > y) != (y2 > y))
                for left, right in zip(xs[::2], xs[1::2]):
                    for i in range(math.ceil(left/self.cell-0.5), math.ceil(right/self.cell-0.5)):
                        self.solids.setdefault((i,j), []).append((prism["base"], prism["top"]))

    def ground(self, x, y):
        t = self.terrain
        u = min(t["nx"]-1, max(0, (x-t["minX"])/t["step"]))
        v = min(t["ny"]-1, max(0, (y-t["minY"])/t["step"]))
        i, j = min(int(u), t["nx"]-2), min(int(v), t["ny"]-2)
        u, v = u-i, v-j
        k = j*t["nx"] + i
        h = self.heights
        low = h[k]*(1-u) + h[k+1]*u
        k += t["nx"]
        return (low*(1-v) + (h[k]*(1-u) + h[k+1]*u)*v)*t["scale"]

    def obstructed(self, x, y, feet, stats):
        b = self.bounds
        if not (b["minX"] < x < b["maxX"] and b["minY"] < y < b["maxY"]):
            return True
        spans = self.solids.get((math.floor(x/self.cell), math.floor(y/self.cell)), ())
        return any(top > feet + stats["step_up"] and base < feet + stats["height"]
                   for base, top in spans)

    def clear_shot(self, origin, target):
        dx, dy, dz = (target[i] - origin[i] for i in range(3))
        steps = min(96, max(1, math.ceil(math.hypot(dx, dy) / 2.5)))
        for i in range(1, steps):
            t = i / steps
            x, y, z = origin[0] + t*dx, origin[1] + t*dy, origin[2] + t*dz
            spans = self.solids.get((math.floor(x/self.cell), math.floor(y/self.cell)), ())
            if any(base + 0.4 < z < top - 0.4 for base, top in spans):
                return False
            if z < self.ground(x, y) - 0.3:
                return False
        return True


class WalkGrid:
    def __init__(self, arena, stats):
        size = self.size = arena.cell
        b = arena.bounds
        xs = range(math.floor(b["minX"]/size), math.ceil(b["maxX"]/size))
        ys = range(math.floor(b["minY"]/size), math.ceil(b["maxY"]/size))
        heights = {}
        blocked = set()
        for i in xs:
            for j in ys:
                x, y = self.center((i, j))
                z = heights[i, j] = arena.ground(x, y)
                if arena.obstructed(x, y, z, stats):
                    blocked.update((i+di, j+dj) for di in (-1, 0, 1) for dj in (-1, 0, 1))
        walkable = heights.keys() - blocked
        max_rise = size * math.tan(math.radians(stats["max_slope_deg"]))
        self.edges = {}
        for i, j in walkable:
            self.edges[i, j] = [n for n in self.neighbors((i, j))
                                if n in walkable and abs(heights[n]-heights[i, j]) <= max_rise]

        # Project blocked endpoints (including rooftops) to a nearby approach cell.
        self.approach = {cell: cell for cell in self.edges}
        queue = deque(self.edges)
        while queue:
            cell = queue.popleft()
            for n in self.neighbors(cell):
                if n in heights and n not in self.approach:
                    self.approach[n] = self.approach[cell]
                    queue.append(n)

    @staticmethod
    def neighbors(cell):
        i, j = cell
        return ((i-1, j), (i+1, j), (i, j-1), (i, j+1))

    def center(self, cell):
        return ((cell[0]+0.5)*self.size, (cell[1]+0.5)*self.size)

    def cell(self, pos):
        return math.floor(pos[0]/self.size), math.floor(pos[1]/self.size)

    def search(self, start, end):
        # Yield on long detours so one search cannot monopolize a decision tick.
        frontier = [(0, 0, start)]
        parent = {start: None}
        costs = {start: 0}
        while frontier:
            for _ in range(32):
                if not frontier:
                    break
                _, neg_cost, cell = heapq.heappop(frontier)
                if cell == end:
                    path = deque()
                    while cell is not None:
                        path.appendleft(self.center(cell))
                        cell = parent[cell]
                    return path
                if -neg_cost != costs[cell]:
                    continue
                cost = 1-neg_cost
                for n in self.edges[cell]:
                    if cost < costs.get(n, math.inf):
                        costs[n], parent[n] = cost, cell
                        h = abs(n[0]-end[0]) + abs(n[1]-end[1])
                        heapq.heappush(frontier, (cost+h, -cost, n))
            yield
        return deque()


class Bot:
    name = "Pocket Comet"

    def __init__(self, hello):
        self.cfg = hello["config"]
        self.arena = Arena(hello["map"]["dir"])
        self.phase = random.Random(hello["seed"]).uniform(-math.pi, math.pi)
        self.heading = self.phase
        self.anchor = None
        self.anchor_time = self.jet_until = 0
        self.target_id = None
        self.damage_bearing = None
        self.damage_time = -math.inf
        self.last_hit_time = -math.inf
        self.attackers = {}  # mech id -> last time it damaged us
        self.full_since = None

        self.grid = WalkGrid(self.arena, self.cfg["mech"])
        self.lasers = {}
        self.path_key = self.search = None
        self.path = deque()
        self.path_time = -math.inf

    def remember_lasers(self, obs, pos, stats):
        seen = {p["id"]: tuple(p["pos"]) for p in obs["pickups"] if p["kind"] == "laser"}
        self.lasers.update(seen)
        zone = obs["zone"]
        eye = (pos[0], pos[1], pos[2] + stats["eye_height"])
        for ident, point in list(self.lasers.items()):
            if distance(point, zone["center"]) > zone["radius"] or (
                    ident not in seen and math.dist(pos, point) <= 15
                    and self.arena.clear_shot(eye, point)):
                del self.lasers[ident]

    def laser_waypoint(self, now, pos, ident):
        point = self.lasers[ident]
        cell = self.grid.cell(pos)
        key = (ident, point, cell)
        if key != self.path_key and now-self.path_time >= 1:
            start = self.grid.approach.get(cell)
            end = self.grid.approach.get(self.grid.cell(point))
            self.path_key, self.path_time = key, now
            self.search = self.grid.search(start, end) if start and end else None
            self.path.clear()
        if self.path_key[:2] != key[:2]:
            return point
        if self.search is not None:
            deadline = time.perf_counter() + 0.001
            try:
                while time.perf_counter() < deadline:
                    next(self.search)
            except StopIteration as done:
                self.path, self.search = done.value, None
        while self.path and distance(pos, self.path[0]) <= 6:
            self.path.popleft()
        return self.path[0] if self.path else point

    def incoming_shots(self, obs, pos, stats):
        threats = []
        for shot in obs["projectiles"]:
            if shot["kind"] == "mortar":
                continue
            x, y, z = shot["pos"]
            vx, vy, vz = shot["vel"]
            speed_sq = vx*vx + vy*vy
            if speed_sq < 1:
                continue
            eta = ((pos[0]-x)*vx + (pos[1]-y)*vy) / speed_sq
            if not 0.025 < eta < 0.55:
                continue
            if not pos[2] - 1 < z + eta*vz < pos[2] + stats["height"] + 1:
                continue
            miss = ((pos[0]-x)*vy - (pos[1]-y)*vx) / math.sqrt(speed_sq)
            if abs(miss) < stats["radius"] + 8:
                threats.append((eta, miss, vy/math.sqrt(speed_sq), -vx/math.sqrt(speed_sq)))
        return sorted(threats)[:10]

    def act(self, obs):
        me, zone, now = obs["self"], obs["zone"], obs["t"]
        pos, stats = me["pos"], me["stats"]
        enemies = sorted((m for m in obs["mechs"] if m["kind"] == "mech"),
                         key=lambda m: distance(pos, m["pos"]))
        secondary0 = me["weapons"][1]
        sniping = bool(secondary0) and secondary0["kind"] == "laser"
        nearby = [m for m in enemies if distance(pos, m["pos"]) < (150 if sniping else 105)]
        wounded = me["hp"] < 0.62 * me["max_hp"]
        for event in obs["events"]:
            if event["type"] == "damaged" and event.get("bearing") is not None:
                self.damage_bearing, self.damage_time = event["bearing"], now
                if event.get("by") is not None:
                    self.attackers[event["by"]] = now
            elif event["type"] == "hit":
                self.last_hit_time = now
        secondary = me["weapons"][1]
        held = secondary["kind"] if secondary else None
        rank = {None: 0, "gun": 0, "mortar": 0, "lightning": 1, "laser": 2}
        loot = [p for p in obs["pickups"] if rank.get(p["kind"], 0) > rank.get(held, 0)]
        act = {"tick": obs["tick"]}
        reachable = [p for p in loot if distance(pos, p["pos"]) <= self.cfg["loot"]["pickup_radius"]
                     and abs(pos[2] - p["pos"][2]) <= 4]
        if reachable:
            act["take"] = max(reachable, key=lambda p: rank[p["kind"]])["id"]
            if held:
                act["drop"] = 1
        if obs["offer"]:
            # Survival first: house guns barely hit at range, so effective HP decides how many
            # hunters we outlast (plating +15% HP, armor -8% damage, nanites faster regen).
            order = ["plating", "armor", "nanites", "laser_charge", "laser_damage", "gun_damage", "gun_rate",
                     "lightning_efficiency", "lightning_range", "gun_mag", "lightning_chain", "gyros",
                     "gun_velocity", "reactor", "servos", "mortar_reload", "mortar_splash", "capacitor",
                     "mortar_velocity", "jets", "sensors", "laser_pierce"]
            choices = obs["offer"]["choices"]
            act["pick"] = min(range(len(choices)), key=lambda i:
                              order.index(choices[i]["id"]) if choices[i]["id"] in order else len(order))

        center, radius = zone["next_center"], zone["next_radius"]
        margin = min(18, radius*0.25)
        excess = distance(pos, center) - radius + margin
        travel = max(0, excess) / stats["walk_speed"]
        urgent = (not me["in_zone"] or (excess > 0 and
                  zone["shrink_end"] - now < 10 + travel*1.8) or
                  distance(pos, zone["center"]) > zone["radius"] - 6)
        # Patrol a safe interior band, leaving the middle available for rotation.
        a = self.phase + now*0.043
        goal = (center[0]+math.cos(a)*radius*0.52, center[1]+math.sin(a)*radius*0.52)
        target = self.choose_target(obs, pos, stats, enemies, wounded, urgent)
        if target:
            self.target_id = target["id"]
        target_kind = target.get("kind", "crate") if target else None
        target_distance = distance(pos, target["pos"]) if target else math.inf
        finishing = (target_kind == "mech" and not wounded and len(nearby) <= 1
                     and target_distance < 95 and target.get("hp", math.inf) < me["hp"] * 0.43)
        opening = now < 65 and me["cubes"] < 5
        capture_radius = 0
        laser_run = False
        if urgent:
            goal = center
            if held != "laser":
                close = [it for it in loot if it["kind"] == "laser" and distance(pos, it["pos"]) < 35
                         and distance(it["pos"], zone["center"]) < zone["radius"] - 6
                         and it["pos"][2] - self.arena.ground(it["pos"][0], it["pos"][1]) < 2.5]
                if close:
                    item = min(close, key=lambda it: distance(pos, it["pos"]))
                    goal, capture_radius, laser_run = item["pos"], 2, True
        else:
            opportunities = []
            def consider(item, value, stop_radius=0):
                p = item["pos"]
                # Reachable if it lies on the ground (any terrain height), or on a roof low
                # enough to jet onto (worth it only for valuable items).
                above = p[2] - self.arena.ground(p[0], p[1])
                reachable = above < 2.5 or (above < 30 and value >= 80 and me["energy"] > 40)
                if distance(p, zone["center"]) < zone["radius"] - 5 and reachable:
                    opportunities.append((distance(pos,p)-value, p, stop_radius))
            room_to_farm = not nearby or (opening and
                           distance(pos, nearby[0]["pos"]) > 76 and now-self.damage_time > 5)
            if room_to_farm:
                for drop in obs["drops"]:
                    if drop["lands_in"] < 5 + distance(pos,drop["pos"])/stats["walk_speed"]:
                        consider(drop, 175 if wounded else 105, drop["radius"] * 0.65)
                for crate in obs["crates"]:
                    if me["cubes"] < self.cfg["cubes"]["stat_cap"]:
                        consider(crate, 66 if opening else 48, 8)
                if target_kind == "npc" and me["cubes"] < self.cfg["cubes"]["stat_cap"]:
                    consider(target, 85, 22)
            for item in loot:
                if item["kind"] == "laser":
                    consider(item, 260 if opening else 200, 2)
                elif item["kind"] == "lightning":
                    consider(item, 120 if opening else 80, 2)
                else:
                    consider(item, 83 if opening else 52, 2)
            for cube in obs["cubes"]:
                consider(cube, 86 if not nearby else 24, 2)
            if opportunities:
                cost, point, stop_radius = min(opportunities, key=lambda item: item[0])
                if cost < 0:
                    goal, capture_radius = point, stop_radius
                    laser_run = held != "laser" and any(
                        it["kind"] == "laser" and distance(it["pos"], point) < 0.5 for it in loot)

        self.remember_lasers(obs, pos, stats)
        loot_destination = goal
        if not urgent and held != "laser" and self.lasers:
            ident = min(self.lasers, key=lambda k: distance(pos, self.lasers[k]))
            if distance(pos, self.lasers[ident]) <= 250:
                loot_destination = self.lasers[ident]
                goal = self.laser_waypoint(now, pos, ident)
                capture_radius, laser_run = 2, True

        wanted = angle(pos, goal)
        best = -math.inf
        heading = wanted
        incoming = self.incoming_shots(obs, pos, stats)
        pressure = []
        for enemy in nearby:
            d = distance(pos, enemy["pos"])
            weight = (1-d/110) * (3.5 if wounded or len(nearby) > 1 else 1.45)
            # Stay in the outer edge of arc range during favorable duels.
            if held == "laser":
                weight = max(0.3, min(3.0, (150 - d) / 25)) * (1.4 if wounded else 1.0)
            elif held == "lightning" and len(nearby) == 1 and not wounded:
                ideal = secondary["stats"]["range"] * (0.52 if finishing else 0.69)
                weight = max(-3.4, min(2.2, (ideal-d)/7))
            elif finishing and enemy["id"] == target["id"]:
                weight = max(-2.7, (31-d)/10)
            if laser_run and d > 40:
                weight *= 0.35  # don't let distant enemies push us off a laser pickup
            pressure.append((angle(pos, enemy["pos"]), weight))
        slope = math.tan(math.radians(stats["max_slope_deg"]))
        for index in range(16):
            theta = wanted + index*math.tau/16
            dx, dy = math.cos(theta), math.sin(theta)
            score = math.cos(theta-wanted)*(3.8 if urgent else 1.25)
            score += 0.28*math.cos(theta-self.heading)
            for bearing, weight in pressure:
                score -= math.cos(theta-bearing)*weight
            if pressure:
                side = 1 if math.sin(self.phase+now*1.6) >= 0 else -1
                score += side*math.sin(theta-pressure[0][0])*1.2
            for eta, miss, nx, ny in incoming:
                # At short horizons inertia dominates; direction matters further out.
                steer_time = max(0, eta - 0.075)
                px = me["vel"][0]*(eta-steer_time) + dx*stats["walk_speed"]*steer_time
                py = me["vel"][1]*(eta-steer_time) + dy*stats["walk_speed"]*steer_time
                score -= max(0, stats["radius"] + 1.5 - abs(miss + nx*px + ny*py))*0.75
            for step, penalty in ((5, 5), (12, 2.8), (23, 1.4)):
                x, y = pos[0]+step*dx, pos[1]+step*dy
                ground = self.arena.ground(x,y)
                if (self.arena.obstructed(x,y,pos[2],stats) or
                        ground-pos[2] > step*slope):
                    score -= penalty
            if score > best:
                best, heading = score, theta
        self.heading = heading
        stop_dist = capture_radius or min(2.3, max(0.15, radius*0.2))
        stop = distance(pos, goal) < stop_dist and (not nearby or radius < 4)
        act["move"] = [0,0] if stop else [math.cos(self.heading), math.sin(self.heading)]
        if self.anchor is None or now-self.anchor_time >= 1.2:
            if self.anchor and distance(pos,self.anchor)<2 and not stop and me["energy"]>28:
                self.jet_until = now + 1.8
            self.anchor, self.anchor_time = pos, now
        jet = now < self.jet_until and not stop
        act["jump"] = jet and me["grounded"]
        can_sprint = not stop and me["grounded"]
        rush_loot = opening and not nearby and capture_radius > 0 and distance(pos, loot_destination) > 22
        chase = finishing and target_distance > (38 if held == "lightning" else 50)
        kite = sniping and can_sprint and any(distance(pos, e["pos"]) < 95 for e in nearby) and me["energy"] > 25
        act["boost"] = jet or kite or (can_sprint and (
            (urgent or (bool(nearby) and wounded)) and me["energy"] > 30
            or (rush_loot or chase) and me["energy"] > 66))

        gun = me["weapons"][0]
        act["reload"] = not target and gun["ammo"] < 0.78*gun["stats"]["mag"]
        if target:
            p = target["pos"]
            d = distance(pos,p)
            velocity = target.get("vel", (0,0,0))
            width, height = target_size(self.cfg, target)
            muzzle = (pos[0], pos[1], pos[2] + stats["muzzle_height"])
            laser = (held == "laser" and secondary["cooldown"] <= 0
                     and d < secondary["stats"]["range"] and (not wounded or len(nearby) <= 1))
            lead = 0.1 if laser else d/gun["stats"]["speed"] + 0.035
            x, y = p[0]+velocity[0]*lead-pos[0], p[1]+velocity[1]*lead-pos[1]
            yaw = math.atan2(y,x)
            aim_z = p[2] + height*0.52 + velocity[2]*lead
            pitch = math.atan2(aim_z-muzzle[2], math.hypot(x,y))
            act["aim"] = [yaw,max(stats["min_pitch"],min(stats["max_pitch"],pitch))]
            yaw_error = abs(wrap(yaw-me["aim"][0]))
            pitch_error = abs(pitch-me["aim"][1])
            aligned = (yaw_error < min(0.22, math.atan2(width, d) + stats["turn_rate"]*0.022)
                       and pitch_error < min(0.20, math.atan2(height*0.42, d) + stats["pitch_rate"]*0.022))
            clear = self.arena.clear_shot(muzzle, (pos[0]+x, pos[1]+y, aim_z))
            breathing_room = d > 70 and now-self.damage_time > 2.3 and now-self.last_hit_time > 1.4
            engage = not (wounded and target_kind == "mech" and breathing_room and obs["alive"] > 2
                          and target.get("hp", math.inf) > 150)
            fire = [clear and aligned and engage and d < gun["stats"]["range"], False]
            if laser and engage:
                full = secondary.get("charge", 0) >= secondary["stats"]["charge"] - 0.01
                if full and self.full_since is None:
                    self.full_since = now
                if not full:
                    self.full_since = None
                tight = (yaw_error < math.atan2(width * 0.55, d) + 0.003
                         and pitch_error < math.atan2(height * 0.35, d) + 0.004)
                overdue = self.full_since is not None and now - self.full_since > 1.6
                charging = secondary.get("charge", 0) > 0
                if charging:
                    # never release into cover; hold until lined up (the game auto-fires at 2 s)
                    fire[1] = not (clear and full and (tight or overdue))
                else:
                    fire[1] = clear and yaw_error < 0.35
                # The protocol accepts both slot triggers together. Spend a
                # round if target motion during flight fits its hit cylinder.
                flight = max(0, d / gun["stats"]["speed"] - lead)
                lateral = abs(velocity[0]*math.sin(yaw) - velocity[1]*math.cos(yaw))
                shared_aim = lateral*flight < width and abs(velocity[2])*flight < height*0.45
                fire[0] = fire[0] and (d < 60 or shared_aim)
            elif held == "lightning":
                shot_range = math.dist(muzzle, (p[0], p[1], p[2] + height*0.5))
                fire[1] = (clear and engage and shot_range < secondary["stats"]["range"]
                           and yaw_error < math.radians(secondary["stats"]["cone_deg"])*0.82)
            if not clear and gun["ammo"] < gun["stats"]["mag"] * 0.35:
                act["reload"] = True
            act["fire"] = fire
        else:
            scan = math.sin(now*2.3) * (0.9 if opening else 0.53)
            yaw = self.damage_bearing if now-self.damage_time < 2.8 else wrap(self.heading + scan)
            act["aim"] = [yaw, 0]
        return act

    def choose_target(self, obs, pos, stats, enemies, wounded, urgent):
        muzzle = (pos[0], pos[1], pos[2] + stats["muzzle_height"])
        # Practice house bots hunt as a squad: only fight mechs that threaten us or are nearly dead,
        # so we do not wake up passive squad members far away.
        now = obs["t"]
        snipe = bool(obs["self"]["weapons"][1]) and obs["self"]["weapons"][1]["kind"] == "laser"
        enemies = [e for e in enemies if distance(pos, e["pos"]) < (150 if snipe else 90)
                   or now - self.attackers.get(e["id"], -99) < 6
                   or e.get("hp", 1400) < 450
                   or e["id"] == self.target_id and distance(pos, e["pos"]) < 130]
        # Shoot back at drones that hit us in the last 5 s, unless a mech is within 60 m.
        if not any(distance(pos, e["pos"]) < 60 for e in enemies):
            drones = sorted((m for m in obs["mechs"] if m["kind"] == "npc"
                             and now - self.attackers.get(m["id"], -99) <= 5
                             and distance(pos, m["pos"]) < 90),
                            key=lambda m: distance(pos, m["pos"]))
            for drone in drones:
                q = drone["pos"]
                _, h = target_size(self.cfg, drone)
                if self.arena.clear_shot(muzzle, (q[0], q[1], q[2] + h * 0.52)):
                    return drone
        visible = []
        for enemy in enemies[:7]:
            d = distance(pos, enemy["pos"])
            _, height = target_size(self.cfg, enemy)
            p = enemy["pos"]
            clear = self.arena.clear_shot(muzzle, (p[0], p[1], p[2] + height*0.52))
            if clear:
                health = enemy.get("hp", 1400)
                priority = 0.58*d + 0.072*health - 14*(enemy["id"] == self.target_id)
                visible.append((priority, enemy))
        if visible:
            return min(visible, key=lambda item: item[0])[1]
        if wounded or urgent or (enemies and distance(pos, enemies[0]["pos"]) < 85):
            return enemies[0] if enemies else None
        farm = [m for m in obs["mechs"] if m["kind"] == "npc" and distance(pos, m["pos"]) < 77]
        farm.extend(c for c in obs["crates"] if distance(pos, c["pos"]) < 62)
        ordered = sorted(farm, key=lambda item: (distance(pos, item["pos"])
                         - 17*(item.get("kind") == "npc") - 14*(item["id"] == self.target_id)))
        for item in ordered[:6]:
            p = item["pos"]
            _, height = target_size(self.cfg, item)
            if self.arena.clear_shot(muzzle, (p[0], p[1], p[2] + height*0.52)):
                return item
        return enemies[0] if enemies else None


if __name__ == "__main__":
    mf.run(Bot)
