// Height field from terrain.i16 (int16 little-endian centimetres, vertex grid at 1 m).
export type GridSpec = { minX: number; minY: number; step: number; nx: number; ny: number }

export class Terrain {
  spec: GridSpec
  ground: Float32Array
  constructor(spec: GridSpec, ground: Float32Array) {
    this.spec = spec
    this.ground = ground
  }

  static fromBytes(spec: GridSpec, bytes: Uint8Array, scale = 0.01): Terrain {
    const n = spec.nx * spec.ny
    const g = new Float32Array(n)
    const dv = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength)
    const m = Math.min(n, bytes.byteLength >> 1)
    for (let k = 0; k < m; k++) g[k] = dv.getInt16(k * 2, true) * scale
    return new Terrain(spec, g)
  }

  static flat(spec: GridSpec): Terrain {
    return new Terrain(spec, new Float32Array(spec.nx * spec.ny))
  }

  /** Bilinear ground height at a world point (clamped to the grid). */
  heightAt(x: number, y: number): number {
    const { minX, minY, step, nx, ny } = this.spec
    let fx = (x - minX) / step
    let fy = (y - minY) / step
    if (fx < 0) fx = 0
    if (fy < 0) fy = 0
    if (fx > nx - 1.001) fx = nx - 1.001
    if (fy > ny - 1.001) fy = ny - 1.001
    const i = Math.floor(fx)
    const j = Math.floor(fy)
    const tx = fx - i
    const ty = fy - j
    const g = this.ground
    const k = j * nx + i
    const a = g[k] * (1 - tx) + g[k + 1] * tx
    const b = g[k + nx] * (1 - tx) + g[k + nx + 1] * tx
    return a * (1 - ty) + b * ty
  }
}
