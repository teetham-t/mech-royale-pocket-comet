"""Mech Royale placement bot. All distances and timers use match units."""
from array import array
import json
import math
from pathlib import Path
import random
import sys

import mechfight as mf


def distance(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def angle(a, b):
    return math.atan2(b[1] - a[1], b[0] - a[0])


def wrap(a):
    return math.atan2(math.sin(a), math.cos(a))


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


class Bot:
    name = "Pocket Comet"

    def __init__(self, hello):
        self.cfg = hello["config"]
        self.arena = Arena(hello["map"]["dir"])
        self.phase = random.Random(hello["seed"]).uniform(-math.pi, math.pi)
        self.heading = self.phase
        self.anchor = None
        self.anchor_time = self.jet_until = 0

    def act(self, obs):
        me, zone, now = obs["self"], obs["zone"], obs["t"]
        pos, stats = me["pos"], me["stats"]
        enemies = sorted((m for m in obs["mechs"] if m["kind"] == "mech"),
                         key=lambda m: distance(pos, m["pos"]))
        nearby = [m for m in enemies if distance(pos, m["pos"]) < 110]
        wounded = me["hp"] < 0.72 * me["max_hp"]
        secondary = me["weapons"][1]
        held = secondary["kind"] if secondary else None
        rank = {None: 0, "gun": 0, "lightning": 1, "laser": 2}
        loot = [p for p in obs["pickups"] if rank.get(p["kind"], 0) > rank.get(held, 0)]
        act = {"tick": obs["tick"]}
        reachable = [p for p in loot if math.dist(pos, p["pos"]) < self.cfg["loot"]["pickup_radius"]]
        if reachable:
            act["take"] = max(reachable, key=lambda p: rank[p["kind"]])["id"]
            if held:
                act["drop"] = 1
        if obs["offer"]:
            order = ["plating", "nanites", "armor", "gun_damage", "laser_damage",
                     "gun_rate", "servos", "gun_mag", "lightning_range", "jets"]
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
        if urgent:
            goal = center
        else:
            opportunities = []
            def consider(item, value):
                p = item["pos"]
                if distance(p, zone["center"]) < zone["radius"] - 5 and abs(p[2]-pos[2]) < 8:
                    opportunities.append((distance(pos,p)-value, p))
            if not nearby:
                for drop in obs["drops"]:
                    if drop["lands_in"] < 5 + distance(pos,drop["pos"])/stats["walk_speed"]:
                        consider(drop, 135 if wounded else 85)
                for crate in obs["crates"]:
                    if me["cubes"] < self.cfg["cubes"]["stat_cap"]:
                        consider(crate, 44)
            for item in loot:
                consider(item, 46 if not secondary else 19)
            for cube in obs["cubes"]:
                consider(cube, 64 if not nearby else 14)
            if opportunities and min(opportunities)[0] < 0:
                goal = min(opportunities)[1]

        wanted = angle(pos, goal)
        best = -math.inf
        heading = wanted
        for index in range(16):
            theta = wanted + index*math.tau/16
            dx, dy = math.cos(theta), math.sin(theta)
            score = math.cos(theta-wanted)*(3.8 if urgent else 1.25)
            score += 0.28*math.cos(theta-self.heading)
            for enemy in nearby:
                bearing = angle(pos, enemy["pos"])
                pressure = max(0, 1-distance(pos, enemy["pos"])/115)
                score -= math.cos(theta-bearing)*pressure*(3.7 if wounded or len(nearby)>1 else 1.8)
            if nearby:
                side = 1 if math.sin(self.phase+now*0.85) >= 0 else -1
                score += side*math.sin(theta-angle(pos,nearby[0]["pos"]))*0.9
            for step, penalty in ((5, 5), (12, 2.8), (23, 1.4)):
                x, y = pos[0]+step*dx, pos[1]+step*dy
                ground = self.arena.ground(x,y)
                if (self.arena.obstructed(x,y,pos[2],stats) or
                        ground-pos[2] > step*math.tan(math.radians(stats["max_slope_deg"]))):
                    score -= penalty
            if score > best:
                best, heading = score, theta
        self.heading = heading
        stop = distance(pos, goal) < min(2.3, max(0.15, radius*0.2)) and (not nearby or radius < 4)
        act["move"] = [0,0] if stop else [math.cos(self.heading), math.sin(self.heading)]
        if self.anchor is None or now-self.anchor_time >= 1.2:
            if self.anchor and distance(pos,self.anchor)<2 and not stop and me["energy"]>28:
                self.jet_until = now + 1.8
            self.anchor, self.anchor_time = pos, now
        jet = now < self.jet_until and not stop
        act["jump"] = jet and me["grounded"]
        act["boost"] = jet or (not stop and (urgent or (bool(nearby) and wounded)) and me["energy"]>32)

        target = min(enemies, key=lambda m: distance(pos,m["pos"])+
                     25*m.get("hp",1)/max(1,m.get("max_hp",1)), default=None)
        if not target and not wounded and not urgent:
            farm = [c for c in obs["crates"] if distance(pos,c["pos"])<50]
            farm += [m for m in obs["mechs"] if m["kind"]=="npc" and distance(pos,m["pos"])<48]
            target = min(farm, key=lambda m: distance(pos,m["pos"]), default=None)
        gun = me["weapons"][0]
        act["reload"] = not target and gun["ammo"] < 0.78*gun["stats"]["mag"]
        if target:
            p = target["pos"]
            d = distance(pos,p)
            velocity = target.get("vel", (0,0,0))
            kind = target.get("kind","crate")
            height = self.cfg[{"crate":"crates","npc":"npc","mech":"mech"}[kind]]["height"]
            height *= 1+self.cfg["cubes"]["size_per_cube"]*target.get("cubes",0)
            laser = held == "laser" and secondary["cooldown"] <= 0 and d < secondary["stats"]["range"]
            lead = 0 if laser else d/gun["stats"]["speed"]
            x, y = p[0]+velocity[0]*lead-pos[0], p[1]+velocity[1]*lead-pos[1]
            yaw = math.atan2(y,x)
            pitch = math.atan2(p[2]+height*0.5-pos[2]-stats["muzzle_height"],math.hypot(x,y))
            act["aim"] = [yaw,max(stats["min_pitch"],min(stats["max_pitch"],pitch))]
            aligned = abs(wrap(yaw-me["aim"][0])) < 0.11 and abs(pitch-me["aim"][1]) < 0.12
            engage = not (wounded and kind=="mech" and d>80 and obs["alive"]>2)
            fire = [aligned and engage and d < gun["stats"]["range"], False]
            if laser and engage:
                fire[1] = secondary.get("charge",0) < secondary["stats"]["charge"]-0.01 or not aligned
            elif held == "lightning":
                fire[1] = d < secondary["stats"]["range"] and abs(wrap(yaw-me["aim"][0])) < math.radians(secondary["stats"]["cone_deg"])
            act["fire"] = fire
        else:
            bearings = [e["bearing"] for e in obs["events"] if e["type"]=="damaged" and e.get("bearing") is not None]
            act["aim"] = [bearings[-1] if bearings else wrap(self.phase+now*1.4), 0]
        return act


if __name__ == "__main__":
    mf.run(Bot)
