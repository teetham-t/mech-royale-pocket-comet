// Small vector and angle helpers. Angles in radians, yaw from +x counter-clockwise.
export type V2 = [number, number]
export type V3 = [number, number, number]

export const TAU = Math.PI * 2

/** Wrap an angle to (-PI, PI]. */
export function wrap(a: number): number {
  a = a % TAU
  if (a <= -Math.PI) a += TAU
  else if (a > Math.PI) a -= TAU
  return a
}

export const angDiff = (a: number, b: number) => wrap(a - b)
export const yawTo = (ax: number, ay: number, bx: number, by: number) => Math.atan2(by - ay, bx - ax)
export const dist2 = (ax: number, ay: number, bx: number, by: number) => Math.hypot(bx - ax, by - ay)
export const clamp = (v: number, lo: number, hi: number) => (v < lo ? lo : v > hi ? hi : v)
export const finite = (v: number, d = 0) => (Number.isFinite(v) ? v : d)

/** Direction vector for [yaw, pitch] (protocol section 5). */
export function dirOf(yaw: number, pitch: number): V3 {
  const c = Math.cos(pitch)
  return [c * Math.cos(yaw), c * Math.sin(yaw), Math.sin(pitch)]
}

/** Unit 2D vector, or [0,0] for a zero vector. */
export function unit(x: number, y: number): V2 {
  const l = Math.hypot(x, y)
  return l > 1e-9 ? [x / l, y / l] : [0, 0]
}
