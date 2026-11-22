"""Turn budget: one pass over the current game objects and 12 movement headings."""
import math
import mechfight as mf


def dist(a, b):
    return math.hypot(a[0]-b[0], a[1]-b[1])


def bearing(a, b):
    return math.atan2(b[1]-a[1], b[0]-a[0])


def delta(a, b):
    return math.remainder(a-b, math.tau)


class Bot:
    name = "Dividend"
    gear = {"gun": 0, "mortar": 0, "laser": 2, "lightning": 1}

    def __init__(self, hello):
        self.config = hello["config"]
        self.phase = (hello["seed"] % 360) * math.pi / 180
        self.shelter_until = -1
        self.bookmark = None
        self.last_sample = -1
        self.last_heading = self.phase
        self.jet_until = -1
        self.loot = {}
        self.last_target = None

    def act(self, obs):
        me, z, t = obs["self"], obs["zone"], obs["t"]
        pos, stats = me["pos"], me["stats"]
        gun, aux = me["weapons"]
        rank = self.gear.get(aux["kind"], 0) if aux else 0
        health = me["hp"]/me["max_hp"]
        rivals = sorted((m for m in obs["mechs"] if m["kind"] == "mech"),
                        key=lambda m: dist(pos, m["pos"]))
        closest = dist(pos,rivals[0]["pos"]) if rivals else math.inf
        if health < .73 and any(e["type"] == "kill" for e in obs["events"]):
            self.shelter_until = t + stats["regen_delay"] + 2
        recovering = health < .9 and t < self.shelter_until and closest > 58
        speed = stats["walk_speed"]
        # Price destinations in seconds of travel, including the walk back
        # into the future circle. A pickup loses value as the shrink approaches.
        def journey(p):
            travel = dist(pos,p)/speed
            inward = max(0, dist(p,z["next_center"])-z["next_radius"]+4)/speed
            late = max(0, t+travel+inward+3-z["shrink_end"])
            return travel + late*5 + max(0,dist(p,z["center"])-z["radius"])/speed*4

        action = {"tick":obs["tick"]}
        if obs["offer"]:
            order = ["plating","nanites","armor","gun_damage","lightning_efficiency",
                     "gun_rate","laser_damage","lightning_range","servos","gun_mag","reactor"]
            options = obs["offer"]["choices"]
            action["pick"] = min(range(len(options)),key=lambda i:
                                 order.index(options[i]["id"]) if options[i]["id"] in order else 100)
        visible = {item["id"] for item in obs["pickups"]}
        for item in obs["pickups"]:
            self.loot[item["id"]] = (item,t)
        choices = []
        for ident,(item,seen) in list(self.loot.items()):
            p = item["pos"]
            if t-seen > 14 or (ident not in visible and dist(pos,p)<9):
                del self.loot[ident]
                continue
            gain = self.gear.get(item["kind"],0)-rank
            if gain <= 0 or abs(p[2]-pos[2])>16:
                continue
            choices.append((23+4*gain-journey(p)-.17*(t-seen), p, 1))
            if ident in visible and math.dist(pos,p)<self.config["loot"]["pickup_radius"]:
                action["take"] = ident
                if aux:
                    action["drop"] = 1
        for cube in obs["cubes"]:
            if abs(cube["pos"][2]-pos[2])<5:
                choices.append(((12 if me["cubes"]<self.config["cubes"]["stat_cap"] else 4)
                                -journey(cube["pos"]),cube["pos"],1))
        for drop in obs["drops"]:
            p = drop["pos"]
            if all(dist(e["pos"],p)>drop["radius"]+10 for e in rivals):
                value = 10+18*(1-health)-max(journey(p),drop["lands_in"])-drop["capture_s"]
                choices.append((value,p,drop["radius"]*.55))
        farm = None
        if closest > 75 and not recovering and me["cubes"]<self.config["cubes"]["stat_cap"]:
            farm = min(obs["crates"],key=lambda c:journey(c["pos"]),default=None)
            if farm and abs(farm["pos"][2]-pos[2])<6:
                choices.append((8-journey(farm["pos"]),farm["pos"],9))
        urgent = (not me["in_zone"] or
                  t + max(0,dist(pos,z["next_center"])-z["next_radius"])/speed*1.6+4 > z["shrink_end"]
                  and dist(pos,z["next_center"])>max(0,z["next_radius"]-5))
        orbit = self.phase + t*.022
        r = .62*z["next_radius"]
        goal = (z["next_center"][0]+r*math.cos(orbit),z["next_center"][1]+r*math.sin(orbit))
        stop = min(2,z["next_radius"]*.1)
        if choices:
            value,p,rad = max(choices,key=lambda c:c[0])
            if value > 0:
                goal,stop = p,rad
        if urgent:
            goal,stop = z["next_center"],min(1,z["next_radius"]*.1)
        target = min(rivals,key=lambda e: dist(pos,e["pos"])+.035*e.get("hp",1000)
                     -10*(e["id"]==self.last_target),default=None)
        if (target is None or closest>100) and farm and dist(pos,farm["pos"])<55 and not recovering:
            target = farm
        if not target and not recovering:
            target = next((m for m in obs["mechs"] if m["kind"]=="npc" and dist(pos,m["pos"])<48),None)
        aim = bearing(pos,goal)
        action["reload"] = target is None and gun["ammo"] < gun["stats"]["mag"]*.8
        if target:
            self.last_target = target["id"]
            p, v = target["pos"], target.get("vel",(0,0,0))
            kind = target.get("kind","crate")
            scale = 1 + self.config["cubes"]["size_per_cube"]*target.get("cubes",0)
            body = self.config[{"mech":"mech","npc":"npc","crate":"crates"}[kind]]
            d = dist(pos,p)
            laser = aux and aux["kind"]=="laser" and aux["cooldown"]<=0
            lead = .05 if laser else d/gun["stats"]["speed"]
            dx,dy = p[0]+v[0]*lead-pos[0], p[1]+v[1]*lead-pos[1]
            aim = math.atan2(dy,dx)
            pitch = math.atan2(p[2]+body["height"]*scale*.5-pos[2]-stats["muzzle_height"],math.hypot(dx,dy))
            pitch = min(stats["max_pitch"],max(stats["min_pitch"],pitch))
            error = abs(delta(aim,me["aim"][0]))
            ready = error < .055+math.atan2(body["radius"]*scale,d) and abs(me["aim"][1]-pitch)<.13
            shoot = not recovering or (kind=="mech" and target.get("hp",999)<190)
            action["aim"] = [aim,pitch]
            alt = False
            if aux and shoot:
                if aux["kind"]=="lightning":
                    alt = d<aux["stats"]["range"] and error<math.radians(aux["stats"]["cone_deg"])
                elif laser:
                    alt = d<aux["stats"]["range"] and (aux.get("charge",0)<aux["stats"]["charge"]-.01 or not ready)
            action["fire"] = [ready and shoot and d<gun["stats"]["range"], bool(alt)]
        else:
            action["aim"] = [delta(aim+.85*math.sin(t*1.4),0),0]
        wanted = bearing(pos,goal)
        scores = []
        for i in range(12):
            h = wanted + i*math.tau/12
            score = (4 if urgent or (rank == 0 and choices) else 1.5)*math.cos(h-wanted)+.35*math.cos(h-self.last_heading)
            for rival in rivals[:3]:
                d = dist(pos,rival["pos"])
                gap = 34 if aux and aux["kind"] == "lightning" and health > .65 else 144
                if d < gap+18:
                    a = bearing(pos,rival["pos"])
                    score -= min(4,(gap-d)/18)*math.cos(h-a)
                    score += math.sin(h-a)*math.cos(self.phase+int(t/2)*math.pi)*.85
            scores.append((score,h))
        _,self.last_heading = max(scores)
        moving = dist(pos,goal)>max(.1,stop) or closest<45 and not urgent
        if t-self.last_sample>=1:
            if moving and self.bookmark and dist(pos,self.bookmark)<1.8 and me["energy"]>35:
                self.jet_until = t+1.4
            self.last_sample,self.bookmark = t,pos
        jet = moving and t<self.jet_until
        action.update(move=[math.cos(self.last_heading),math.sin(self.last_heading)] if moving else [0,0],
                      boost=bool(jet or moving and (urgent or closest<95 or (not rank and choices and me["energy"]>65)) and me["energy"]>40),
                      jump=bool(jet and me["grounded"]))
        return action


if __name__ == "__main__":
    mf.run(Bot)
