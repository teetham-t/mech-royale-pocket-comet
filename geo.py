"""Map model: surface, obstacles, canopy, nav grid (4 m) with exposure. Pure stdlib."""
import gzip, json, math, os, heapq, time
from array import array

DET = bool(os.environ.get('MECH_DET'))
DIRS = [(1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1)]
DLEN = [1.0, 1.4142, 1.0, 1.4142, 1.0, 1.4142, 1.0, 1.4142]


class Map:
    def __init__(self, path=None):
        path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'map.bin.gz')
        raw = gzip.open(path, 'rb').read()
        n = int.from_bytes(raw[:4], 'little'); h = json.loads(raw[4:4 + n]); o = 4 + n
        self.nx, self.ny = h['nx'], h['ny']; self.minX, self.minY = h['minX'], h['minY']
        self.NX, self.NY, self.S, self.c0 = h['NX'], h['NY'], h['S'], h['c0']
        N = self.nx * self.ny; M = self.NX * self.NY

        def take_h(o):
            a = array('h'); a.frombytes(raw[o:o + 2 * N]); return a, o + 2 * N
        self.surf, o = take_h(o)
        self.obase, o = take_h(o)
        self.otop, o = take_h(o)
        self.canopy = raw[o:o + N]; o += N
        self.walk = raw[o:o + M]; o += M
        self.edges = raw[o:o + M]; o += M
        self.E = raw[o:o + M]; o += M
        self.walk1 = raw[o:o + N]; o += N
        # good hiding cells sorted by exposure
        cells = [(self.E[k], k) for k in range(M) if self.walk[k]]
        cells.sort()
        self.cells_sorted = cells

    # ---------- raster helpers ----------
    def vidx(self, x, y):
        i = int(round(x - self.minX)); j = int(round(y - self.minY))
        if i < 0 or j < 0 or i >= self.nx or j >= self.ny:
            return -1
        return j * self.nx + i

    def ground(self, x, y):
        k = self.vidx(x, y)
        return self.surf[k] * 0.01 if k >= 0 else 0.0

    def walkable_pt(self, x, y):
        k = self.vidx(x, y)
        return k >= 0 and self.walk1[k] == 1

    def los(self, a, b, step=1.0, skip=1.5):
        """True if the segment a->b is not blocked by terrain or buildings (trees ignored)."""
        dx = b[0] - a[0]; dy = b[1] - a[1]; dz = b[2] - a[2]
        L = math.sqrt(dx * dx + dy * dy + dz * dz)
        if L < 2 * skip:
            return True
        n = int((L - 2 * skip) / step) + 1
        surf = self.surf; ob = self.obase; ot = self.otop; nx = self.nx; ny = self.ny
        mx = self.minX; my = self.minY
        for s in range(n + 1):
            f = (skip + s * step) / L
            if f > 1.0 - skip / L:
                f = 1.0 - skip / L
            x = a[0] + dx * f; y = a[1] + dy * f; z = (a[2] + dz * f) * 100.0
            i = int(x - mx + 0.5); j = int(y - my + 0.5)
            if i < 0 or j < 0 or i >= nx or j >= ny:
                return False
            k = j * nx + i
            if surf[k] > z + 30:
                return False
            if ob[k] < z < ot[k]:
                return False
        return True

    def sees(self, eye, tgt):
        """Approximate vision: LOS plus canopy (only beyond 45 m)."""
        if not self.los(eye, tgt):
            return False
        dx = tgt[0] - eye[0]; dy = tgt[1] - eye[1]
        L = math.hypot(dx, dy)
        if L <= 45:
            return True
        n = int(L / 2.0); c = 0.0
        for s in range(1, n):
            f = s * 2.0 / L
            k = self.vidx(eye[0] + dx * f, eye[1] + dy * f)
            if k >= 0 and self.canopy[k]:
                c += 2.0
                if c > 4.0:
                    return False
        return True

    def open_dist(self, x, y, z, yaw, maxd=200.0, step=3.0):
        ca = math.cos(yaw); sa = math.sin(yaw)
        d = step
        while d <= maxd:
            k = self.vidx(x + ca * d, y + sa * d)
            if k < 0:
                return d
            zc = z * 100
            if self.surf[k] > zc or self.obase[k] < zc < self.otop[k]:
                return d
            d += step
        return maxd

    def walk_line(self, a, b):
        """Straight walk feasible on the 1 m dilated raster."""
        dx = b[0] - a[0]; dy = b[1] - a[1]
        L = math.hypot(dx, dy)
        n = int(L) + 1
        for s in range(n + 1):
            f = s / n
            k = self.vidx(a[0] + dx * f, a[1] + dy * f)
            if k < 0 or not self.walk1[k]:
                return False
        return True

    # ---------- nav grid ----------
    def cell_of(self, x, y):
        ci = int(round((x - self.minX - self.c0) / self.S)); cj = int(round((y - self.minY - self.c0) / self.S))
        ci = min(max(ci, 0), self.NX - 1); cj = min(max(cj, 0), self.NY - 1)
        return cj * self.NX + ci

    def cell_xy(self, c):
        cj, ci = divmod(c, self.NX)
        return (self.minX + self.c0 + self.S * ci, self.minY + self.c0 + self.S * cj)

    def nearest_walk(self, x, y, maxr=12):
        c = self.cell_of(x, y)
        if self.walk[c]:
            return c
        cj, ci = divmod(c, self.NX)
        best = None; bd = 1e9
        for r in range(1, maxr + 1):
            for dj in range(-r, r + 1):
                for di in (-r, r) if abs(dj) != r else range(-r, r + 1):
                    a = cj + dj; b = ci + di
                    if 0 <= a < self.NY and 0 <= b < self.NX:
                        k = a * self.NX + b
                        if self.walk[k]:
                            px, py = self.cell_xy(k); d = (px - x) ** 2 + (py - y) ** 2
                            if d < bd:
                                bd = d; best = k
            if best is not None:
                return best
        return None


class AStar:
    """Resumable A* on the nav grid; cost weighted by exposure."""

    def __init__(self, m, start, goal, expw=1.5, avoid=None):
        self.m = m; self.start = start; self.goal = goal; self.expw = expw
        self.avoid = avoid or []  # list of (x, y, r, penalty)
        self.g = {start: 0.0}; self.par = {start: -1}
        gx, gy = m.cell_xy(goal); self.gx, self.gy = gx, gy
        self.open = [(self.h(start), 0.0, start)]
        self.done = False; self.path = None; self.closed = set()

    def h(self, c):
        x, y = self.m.cell_xy(c)
        dx = abs(x - self.gx); dy = abs(y - self.gy)
        return (max(dx, dy) + 0.4142 * min(dx, dy))

    def step(self, budget_s):
        if DET:
            return self.step_n(int(budget_s * 40000))
        return self.step_n(10 ** 9, time.perf_counter() + budget_s)

    def step_n(self, max_it, t_end=None):
        m = self.m; NX = m.NX; S = m.S; E = m.E; edges = m.edges
        op = self.open; g = self.g; par = self.par; closed = self.closed
        it = 0
        while op:
            it += 1
            if it > max_it or ((it & 63) == 0 and t_end is not None and time.perf_counter() > t_end):
                return False
            f, gc, c = heapq.heappop(op)
            if c in closed:
                continue
            closed.add(c)
            if c == self.goal:
                p = []
                while c != -1:
                    p.append(c); c = par[c]
                p.reverse(); self.path = p; self.done = True
                return True
            e = edges[c]
            if not e:
                continue
            for k in range(8):
                if e >> k & 1:
                    dx, dy = DIRS[k]
                    n = c + dy * NX + dx
                    if n in closed:
                        continue
                    w = DLEN[k] * S * (1.0 + self.expw * E[n] / 254.0)
                    if self.avoid:
                        x, y = m.cell_xy(n)
                        for (ax, ay, ar, ap) in self.avoid:
                            if (x - ax) ** 2 + (y - ay) ** 2 < ar * ar:
                                w += ap
                    ng = gc + w
                    if ng < g.get(n, 1e18):
                        g[n] = ng; par[n] = c
                        heapq.heappush(op, (ng + self.h(n), ng, n))
        self.done = True; self.path = None
        return True
