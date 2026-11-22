import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { run, type Observation, type BotAction, type Vec2, type Vec3 } from './mechfight.ts'
import { angDiff, clamp, dist2, unit, yawTo } from './vec.ts'
import { Terrain } from './terrain.ts'

type Point = Vec2 | Vec3
const distance = (a: Point, b: Point) => dist2(a[0], a[1], b[0], b[1])
const yaw = (a: Point, b: Point) => yawTo(a[0], a[1], b[0], b[1])
const preference = ['plating','armor','nanites','gun_damage','gun_rate','laser_damage','lightning_range','servos','jets']
const itemValue: Record<string, number> = {gun:0, mortar:0, lightning:2, laser:3}

run(hello => {
  const dir = join(hello.map.dir,'v2')
  const grid = JSON.parse(readFileSync(join(dir,'world.json'),'utf8')).terrain
  const terrain = Terrain.fromBytes(grid,readFileSync(join(dir,grid.file)),grid.scale)
  let goalId = -1, contact = -1, lastSample = -2, jetEnd = -2
  let previous: Point | undefined
  const handedness = hello.seed % 2 ? 1 : -1
  return {name:'Relay', act(o: Observation): BotAction {
    const me=o.self, p=me.pos, st=me.stats, zone=o.zone
    const [gun, aux]=me.weapons
    const rivals=o.mechs.filter(m=>m.kind==='mech').sort((a,b)=>distance(p,a.pos)-distance(p,b.pos))
    const gap=rivals.length ? distance(p,rivals[0].pos) : Infinity
    const a: BotAction = {}
    if(o.offer){
      const ranks=o.offer.choices.map(c=>preference.includes(c.id)?preference.indexOf(c.id):100)
      a.pick=ranks.indexOf(Math.min(...ranks))
    }

    // Compare guaranteed benefits with time until they can be collected.
    // A short commitment bonus prevents oscillation when two items are close.
    const c=zone.next_center, buffer=Math.min(10,zone.next_radius*.25)
    const overtime=(q:Point)=>Math.max(0,distance(p,q)/st.walk_speed+
      Math.max(0,distance(q,c)-zone.next_radius+buffer)/st.walk_speed-(zone.shrink_end-o.t))
    const radius=.73*zone.next_radius
    const phase=yaw(c,p)+handedness*Math.min(1,30/Math.max(1,radius))
    let goal:Point=[c[0]+radius*Math.cos(phase),c[1]+radius*Math.sin(phase)]
    let best=0,selected=-1,stop=2
    const offer=(id:number,q:Vec3,benefit:number,wait:number,within:number)=>{
      const horizontal=distance(p,q)
      const climb=Math.max(0,q[2]-terrain.heightAt(q[0],q[1]))
      const delay=Math.max(wait,horizontal/st.walk_speed)+climb*.25
      if(climb>me.energy*.28 || distance(q,zone.center)>zone.radius-3)return
      const score=benefit/(1+delay+3*overtime(q))*(id===goalId?1.3:1)
      if(score>best){best=score;goal=q;selected=id;stop=within}
    }
    for(const pick of o.pickups){
      const gain=itemValue[pick.kind]-(aux?itemValue[aux.kind]:0)
      if(gain<=0)continue
      offer(pick.id,pick.pos,gain*7,0,1)
      if(distance(p,pick.pos)<hello.config.loot.pickup_radius && Math.abs(p[2]-pick.pos[2])<4){
        a.take=pick.id
        if(aux)a.drop=1
      }
    }
    for(const cube of o.cubes)offer(cube.id,cube.pos,me.cubes<10?10:2,0,1)
    for(const drop of o.drops??[]){
      if(rivals.every(e=>distance(e.pos,drop.pos)>drop.radius+18))
        offer(drop.id,drop.pos,10+30*(1-me.hp/me.max_hp),drop.lands_in+drop.capture_s,drop.radius*.55)
    }
    if(gap>85 && me.cubes<10){
      for(const crate of o.crates)if(distance(p,crate.pos)<58)offer(crate.id,crate.pos,4,1.5,9)
    }
    goalId=selected
    const urgent=!me.in_zone || overtime(p)>0
    if(urgent){goal=c;stop=Math.min(1,zone.next_radius*.1)}
    let heading=yaw(p,goal)
    if(rivals.length && !urgent){
      const e=rivals[0], away=yaw(e.pos,p)
      const reach=aux?.kind==='lightning'?aux.stats.range*.76:120
      const repel=clamp((reach-gap)/25,-.5,3)
      const side=handedness*(Math.sin(o.t*.73)>0?1:-1)
      const vx=Math.cos(heading)+repel*Math.cos(away)-side*Math.sin(away)
      const vy=Math.sin(heading)+repel*Math.sin(away)+side*Math.cos(away)
      heading=Math.atan2(vy,vx)
    }
    const halt=distance(p,goal)<Math.max(.1,stop) && gap>55
    if(o.t-lastSample>=1.2){
      if(previous && !halt && distance(p,previous)<1.75 && me.energy>32)jetEnd=o.t+1.7
      previous=p;lastSample=o.t
    }
    const climb=goal.length===3 && distance(p,goal)<15 && goal[2]!>p[2]+3.9
    const jet=(o.t<jetEnd || climb) && me.energy>8
    a.move=halt&&!climb?[0,0]:unit(Math.cos(heading),Math.sin(heading))
    a.jump=jet&&me.grounded
    a.boost=jet||!halt&&me.grounded&&me.energy>45&&(urgent||gap<68||!aux&&selected>=0)

    const enemies=rivals.filter(e=>distance(p,e.pos)<(aux?.kind==='laser'?200:125))
    const priority=(e:typeof rivals[number])=>distance(p,e.pos)*.5+(e.hp??1000)*.06-(e.id===contact?15:0)
    enemies.sort((a,b)=>priority(a)-priority(b))
    const target=enemies[0]??o.mechs.find(m=>m.kind==='npc'&&distance(p,m.pos)<45)??o.crates.find(k=>k.id===selected)
    if(gun?.ammo!==undefined)a.reload=!target&&gun.ammo<gun.stats.mag*.8
    if(!target){
      a.aim=[me.aim[0]+.42,0]
      return a
    }
    contact=target.id
    const actor='vel' in target?target:undefined
    const height=actor?.kind==='mech'?hello.config.mech.height*(1+Number(hello.config.cubes.size_per_cube)*(actor.cubes??0)):
      actor?hello.config.npc.height:hello.config.crates.height
    const d=distance(p,target.pos)
    const laser=aux?.kind==='laser'&&aux.cooldown<=0&&d<aux.stats.range
    const lead=laser ? 0.05 : d/(gun?.stats.speed??252)
    const x=target.pos[0]+(actor?.vel[0]??0)*lead-p[0],y=target.pos[1]+(actor?.vel[1]??0)*lead-p[1]
    const look=Math.atan2(y,x)
    const pitch=Math.atan2(target.pos[2]+height*.5-p[2]-st.muzzle_height,Math.hypot(x,y))
    a.aim=[look,clamp(pitch,st.min_pitch,st.max_pitch)]
    const error=Math.abs(angDiff(look,me.aim[0]))
    const aligned=error<.04+Math.atan2(2.5,d)&&Math.abs(me.aim[1]-pitch)<.1
    let second=false
    if(aux && laser){
      second=(aux.charge??0)<aux.stats.charge-.01||!aligned
    }else if(aux?.kind==='lightning'){
      second=d<aux.stats.range&&error<aux.stats.cone_deg*Math.PI/180
    }
    a.fire=[!!gun&&aligned&&d<gun.stats.range,second]
    return a
  }}
})
