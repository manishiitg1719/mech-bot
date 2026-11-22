#!/usr/bin/env python3
"""Survival-first Mech Royale bot (mechfight/1).

Strategy: placement is everything. Stay inside the (next) zone, keep
buildings between us and enemies, keep distance, face threats (rear hits
do x1.5), return fire only when it is worth it, grab weapons/cubes when safe.
"""
import json
import math
import os
import select
import sys
import time

PI = math.pi


def log(*a):
    try:
        print(*a, file=sys.stderr, flush=True)
    except Exception:
        pass


def send(obj):
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def g(d, *keys, default=None):
    for k in keys:
        if d is None:
            return default
        if isinstance(d, dict):
            d = d.get(k)
        elif isinstance(d, (list, tuple)) and isinstance(k, int) and -len(d) <= k < len(d):
            d = d[k]
        else:
            return default
    return default if d is None else d


def num(x, default=0.0):
    try:
        if x is None:
            return default
        return float(x)
    except Exception:
        return default


def vec3(p):
    try:
        return (float(p[0]), float(p[1]), float(p[2]) if len(p) > 2 else 0.0)
    except Exception:
        return (0.0, 0.0, 0.0)


def wrap(a):
    while a > PI:
        a -= 2 * PI
    while a < -PI:
        a += 2 * PI
    return a


# ---------------------------------------------------------------- map / LOS
class World:
    CELL = 25.0

    def __init__(self):
        self.prisms = []  # (xs, ys, base, top, bbox)
        self.grid = {}
        self.ok = False

    def load(self, mapdir, files):
        path = None
        cands = []
        if isinstance(files, dict):
            cands += [str(v) for v in files.values() if isinstance(v, str)]
        elif isinstance(files, list):
            cands += [str(v) for v in files if isinstance(v, str)]
        cands.append("v2/world.json")
        for c in cands:
            if c.endswith("world.json"):
                p = c if os.path.isabs(c) else os.path.join(mapdir or "/arena", c)
                if os.path.exists(p):
                    path = p
                    break
        if path is None:
            for root in [mapdir or "/arena", "/arena"]:
                p = os.path.join(root, "v2", "world.json")
                if os.path.exists(p):
                    path = p
                    break
        if path is None:
            log("world.json not found")
            return
        with open(path) as f:
            w = json.load(f)
        prisms = w.get("prisms") or []
        if isinstance(prisms, dict):
            prisms = prisms.get("items") or prisms.get("list") or list(prisms.values())
        for pr in prisms:
            try:
                if not isinstance(pr, dict):
                    continue
                ring = pr.get("ring") or pr.get("points") or []
                if ring and isinstance(ring[0], (list, tuple)):
                    flat = []
                    for q in ring:
                        flat += [q[0], q[1]]
                    ring = flat
                if len(ring) < 6:
                    continue
                xs = [float(v) for v in ring[0::2]]
                ys = [float(v) for v in ring[1::2]]
                base = num(pr.get("base"), 0.0)
                top = num(pr.get("top"), base + 10.0)
                if top - base < 1.5:
                    continue
                bb = (min(xs), min(ys), max(xs), max(ys))
                idx = len(self.prisms)
                self.prisms.append((xs, ys, base, top, bb))
                c = self.CELL
                for gx in range(int(math.floor(bb[0] / c)), int(math.floor(bb[2] / c)) + 1):
                    for gy in range(int(math.floor(bb[1] / c)), int(math.floor(bb[3] / c)) + 1):
                        self.grid.setdefault((gx, gy), []).append(idx)
            except Exception:
                continue
        self.ok = len(self.prisms) > 0
        log("loaded prisms", len(self.prisms))
        try:
            import array
            t = w.get("terrain") or {}
            tp = os.path.join(os.path.dirname(path), t.get("file", "terrain.i16"))
            a = array.array("h")
            with open(tp, "rb") as f:
                a.frombytes(f.read())
            if sys.byteorder != "little":
                a.byteswap()
            self.tnx = int(t.get("nx", 1070))
            self.tny = int(t.get("ny", 724))
            self.tminx = float(t.get("minX", -513))
            self.tminy = float(t.get("minY", -334))
            self.tstep = float(t.get("step", 1.0))
            self.tscale = float(t.get("scale", 0.01))
            if len(a) >= self.tnx * self.tny:
                self.terr = a
                log("terrain ok")
        except Exception as e:
            log("terrain fail", e)

    terr = None

    def ground(self, x, y):
        a = self.terr
        if a is None:
            return None
        i = int((x - self.tminx) / self.tstep + 0.5)
        j = int((y - self.tminy) / self.tstep + 0.5)
        if i < 0 or j < 0 or i >= self.tnx or j >= self.tny:
            return None
        return a[j * self.tnx + i] * self.tscale

    def terrain_blocked(self, a, b):
        if self.terr is None:
            return False
        d = math.hypot(b[0] - a[0], b[1] - a[1])
        n = int(d / 5.0)
        for k in range(1, n):
            s = k / n
            x = a[0] + (b[0] - a[0]) * s
            y = a[1] + (b[1] - a[1]) * s
            gz = self.ground(x, y)
            if gz is not None and gz > a[2] + (b[2] - a[2]) * s + 0.3:
                return True
        return False

    def _cells_along(self, ax, ay, bx, by):
        c = self.CELL
        d = math.hypot(bx - ax, by - ay)
        n = max(1, int(d / 8.0))
        seen = set()
        out = []
        for i in range(n + 1):
            s = i / n
            x = ax + (bx - ax) * s
            y = ay + (by - ay) * s
            k = (int(math.floor(x / c)), int(math.floor(y / c)))
            if k not in seen:
                seen.add(k)
                for idx in self.grid.get(k, ()):
                    out.append(idx)
        return set(out)

    def blocked(self, a, b):
        """True if a building or terrain blocks the segment a->b (3D points)."""
        if self.terrain_blocked(a, b):
            return True
        if not self.ok:
            return False
        ax, ay, az = a
        bx, by, bz = b
        dx = bx - ax
        dy = by - ay
        for idx in self._cells_along(ax, ay, bx, by):
            xs, ys, base, top, bb = self.prisms[idx]
            if max(ax, bx) < bb[0] or min(ax, bx) > bb[2] or max(ay, by) < bb[1] or min(ay, by) > bb[3]:
                continue
            if min(az, bz) > top:
                continue
            n = len(xs)
            for i in range(n):
                x1, y1 = xs[i], ys[i]
                x2, y2 = xs[(i + 1) % n], ys[(i + 1) % n]
                ex = x2 - x1
                ey = y2 - y1
                den = dx * ey - dy * ex
                if abs(den) < 1e-9:
                    continue
                s = ((x1 - ax) * ey - (y1 - ay) * ex) / den
                u = ((x1 - ax) * dy - (y1 - ay) * dx) / den
                if 0.0 < s < 1.0 and 0.0 <= u <= 1.0:
                    z = az + (bz - az) * s
                    if base - 0.5 <= z <= top:
                        return True
        return False

    def inside(self, x, y, z=0.0):
        if not self.ok:
            return False
        c = self.CELL
        for idx in self.grid.get((int(math.floor(x / c)), int(math.floor(y / c))), ()):
            xs, ys, base, top, bb = self.prisms[idx]
            if x < bb[0] - 3 or x > bb[2] + 3 or y < bb[1] - 3 or y > bb[3] + 3:
                continue
            if z > top - 1 or top - base < 3:
                continue
            n = len(xs)
            ins = False
            j = n - 1
            for i in range(n):
                if (ys[i] > y) != (ys[j] > y):
                    xc = xs[i] + (y - ys[i]) * (xs[j] - xs[i]) / (ys[j] - ys[i] + 1e-12)
                    if x < xc:
                        ins = not ins
                j = i
            if ins:
                return True
        return False


# ---------------------------------------------------------------- bot
class Bot:
    def __init__(self):
        self.world = World()
        self.cfg = {}
        self.slot = 0
        self.target = None
        self.hist = []  # (t, x, y)
        self.unstick_until = -1
        self.unstick_dir = 1
        self.threat_marks = []  # (t_expire, x, y, weight)
        self.last_damage_t = -99
        self.laser_hold = False
        self.strafe = 1
        self.strafe_t = 0
        self.gun_speed = 252.0
        self.mortar_speed = 72.0
        self.gravity = 22.0
        self.last_aim = [0.0, 0.0]
        self.pk_mem = {}

    # ---------------- hello
    def hello(self, msg):
        self.cfg = msg.get("config") or {}
        self.slot = msg.get("slot", 0)
        w = self.cfg.get("weapons") or {}
        self.gun_speed = num(g(w, "gun", "speed"), 252.0)
        self.mortar_speed = num(g(w, "mortar", "speed"), 72.0)
        self.gravity = num(g(w, "mortar", "gravity"), 22.0)
        m = msg.get("map") or {}
        try:
            self.world.load(m.get("dir") or "/arena", m.get("files"))
        except Exception as e:
            log("map load failed", e)

    # ---------------- helpers
    def aim_at(self, me, tp, tv, speed):
        mx, my, mz = me
        tx, ty, tz = tp
        t = 0.0
        for _ in range(2):
            px = tx + tv[0] * t
            py = ty + tv[1] * t
            d = math.hypot(px - mx, py - my)
            t = d / max(speed, 1.0)
        px = tx + tv[0] * t
        py = ty + tv[1] * t
        pz = tz + tv[2] * t * 0.3
        dx, dy, dz = px - mx, py - my, pz - mz
        return [math.atan2(dy, dx), math.atan2(dz, math.hypot(dx, dy))]

    def mortar_aim(self, me, tp, tv):
        v = self.mortar_speed
        gr = self.gravity
        mx, my, mz = me
        t = 1.5
        best = None
        for _ in range(3):
            px = tp[0] + tv[0] * t
            py = tp[1] + tv[1] * t
            d = math.hypot(px - mx, py - my)
            h = tp[2] - mz
            disc = v ** 4 - gr * (gr * d * d + 2 * h * v * v)
            if disc < 0 or d < 1:
                return None
            # low arc (faster, better vs moving target)
            th = math.atan2(v * v - math.sqrt(disc), gr * d)
            t = d / max(v * math.cos(th), 1.0)
            best = [math.atan2(py - my, px - mx), th]
        return best

    # ---------------- main decision
    def act(self, o):
        self.t_start = time.perf_counter()
        tick = o.get("tick")
        t = num(o.get("t"))
        me = o.get("self") or {}
        pos = vec3(me.get("pos") or [0, 0, 0])
        vel = vec3(me.get("vel") or [0, 0, 0])
        hp = num(me.get("hp"), 1400)
        max_hp = max(1.0, num(me.get("max_hp"), 1400))
        energy = num(me.get("energy"), 100)
        my_id = me.get("id", self.slot)
        hpf = hp / max_hp
        stats = me.get("stats") or {}
        muzzle_h = num(stats.get("muzzle_height"), 7.0)
        eye_h = num(stats.get("eye_height"), 8.0)
        muzzle = (pos[0], pos[1], pos[2] + muzzle_h)

        act = {"tick": tick}

        # ---- upgrade offer
        offer = o.get("offer")
        if isinstance(offer, dict) and offer.get("choices"):
            act["pick"] = self.choose_upgrade(offer["choices"])

        # ---- events: damage bearing -> threat marks
        for ev in o.get("events") or []:
            if not isinstance(ev, dict):
                continue
            if ev.get("type") == "damaged":
                self.last_damage_t = t
                b = ev.get("bearing")
                if b is not None and ev.get("weapon") not in ("zone",):
                    b = num(b)
                    self.threat_marks.append((t + 3.0, pos[0] + 90 * math.cos(b), pos[1] + 90 * math.sin(b), 1.0))
        for s in o.get("sounds") or []:
            if not isinstance(s, dict):
                continue
            kind = s.get("kind")
            if kind in ("gun", "laser", "laser_charge", "lightning", "mortar", "step", "boost", "jet", "reload"):
                dist = {"near": 40.0, "mid": 110.0}.get(s.get("dist"))
                if dist is None:
                    continue
                b = num(s.get("bearing"))
                self.threat_marks.append((t + 2.0, pos[0] + dist * math.cos(b), pos[1] + dist * math.sin(b), 0.4))
        self.threat_marks = [m for m in self.threat_marks if m[0] > t][-20:]

        # ---- enemies
        enemies = []
        npcs = []
        for m in o.get("mechs") or []:
            if not isinstance(m, dict) or m.get("id") == my_id:
                continue
            mp = vec3(m.get("pos") or [0, 0, 0])
            mv = vec3(m.get("vel") or [0, 0, 0])
            d = math.hypot(mp[0] - pos[0], mp[1] - pos[1])
            rec = (d, m, mp, mv)
            if m.get("kind") == "npc" or num(m.get("id"), 0) >= 100:
                npcs.append(rec)
            else:
                enemies.append(rec)
        enemies.sort(key=lambda r: r[0])
        npcs.sort(key=lambda r: r[0])

        # ---- zone
        z = o.get("zone") or {}
        zc = z.get("center") or [41.3, -72.8]
        zr = num(z.get("radius"), 560)
        nc = z.get("next_center") or zc
        nr = num(z.get("next_radius"), zr)
        shrink_start = num(z.get("shrink_start"), 1e9)
        zc = (num(zc[0]), num(zc[1]))
        nc = (num(nc[0]), num(nc[1]))
        time_to_shrink = shrink_start - t
        # target circle: next circle once shrink is near, else current with margin
        if time_to_shrink < 35 or nr < zr - 1:
            tc, trr = nc, nr
        else:
            tc, trr = zc, zr
        margin = min(30.0, max(4.0, trr * 0.22))

        # ---- weapons
        weapons = me.get("weapons") or [None, None]
        free_slot = None
        kinds = {}
        for i, w in enumerate(weapons[:2]):
            if w is None:
                if free_slot is None:
                    free_slot = i
            elif isinstance(w, dict):
                kinds[w.get("kind")] = (i, w)

        # ---- choose movement target (sampled candidate scoring)
        threats = []
        for ei, (d, m, mp, mv) in enumerate(enemies[:6]):
            ehp = num(m.get("hp"), 1400)
            wgt = 1.0 + 0.3 * min(10, num(m.get("cubes"), 0)) / 10.0
            ew = m.get("weapons") or []
            if "laser" in ew:
                wgt *= 1.8
            if m.get("charging"):
                wgt *= 1.5
            threats.append((mp, wgt * (0.7 + 0.6 * ehp / 1400.0), ei < 3, "laser" in ew))
        for d, m, mp, mv in npcs[:3]:
            if d < 80:
                threats.append((mp, 0.25, True, False))
        for (te, x, y, w) in self.threat_marks:
            threats.append(((x, y, pos[2]), w * 0.5, False, False))

        late = zr < 80
        expose_w = 60.0 * (1.6 - hpf)
        dist_w = 1.2 * (1.5 - hpf)

        # objective: weapons / cubes when safe
        close_threat = enemies[0][0] if enemies else 999.0
        n_vis = sum(1 for e in enemies if e[0] < 200)
        aggressive = late or hpf > 0.55 or (n_vis <= 1 and hpf > 0.3)
        ideal = 45.0
        if n_vis == 1 and hpf > 0.5:
            ideal = 30.0  # lone hunter: close in where the gun hits reliably
        if "lightning" in kinds:
            ideal = 32.0
        elif "laser" in kinds:
            ideal = 90.0
        goal = None
        # remember pickups seen
        vis_ids = set()
        for p in o.get("pickups") or []:
            try:
                self.pk_mem[p["id"]] = (p.get("kind"), vec3(p.get("pos")), t)
                vis_ids.add(p["id"])
            except Exception:
                pass
        for pid in list(self.pk_mem):
            k_, pp_, ts_ = self.pk_mem[pid]
            if pid not in vis_ids and (math.dist(pp_, pos) < 35 or t - ts_ > 90):
                del self.pk_mem[pid]
        PREF = {"laser": 3, "lightning": 2, "mortar": 1}
        slot1 = weapons[1] if len(weapons) > 1 else None
        cur1 = PREF.get(slot1.get("kind"), 0) if isinstance(slot1, dict) else -1
        if close_threat > 40:
            best = None
            for pid, (pk, pp, ts_) in self.pk_mem.items():
                pv = PREF.get(pk, 0)
                if pk in kinds or pv <= max(cur1, 0) and free_slot is None:
                    continue
                if pv == 0:
                    continue
                d = math.dist(pp, pos)
                lim = 220 if free_slot is not None else 90
                if d > lim:
                    continue
                if math.hypot(pp[0] - tc[0], pp[1] - tc[1]) > trr - 5 and math.hypot(pp[0] - zc[0], pp[1] - zc[1]) > zr - 10:
                    continue
                sc = d - 40 * pv
                if best is None or sc < best[0]:
                    best = (sc, pp, pid, d)
            if best:
                goal = best[1]
                pp = best[1]
                if abs(pp[2] - pos[2]) < 3.9 and math.hypot(pp[0] - pos[0], pp[1] - pos[1]) < 3.9:
                    act["take"] = best[2]
                    if free_slot is None:
                        act["drop"] = 1
            if goal is None:
                best = None
                for c in o.get("cubes") or []:
                    try:
                        cp = vec3(c.get("pos"))
                        d = math.dist(cp, pos)
                        if d < 80 and (best is None or d < best[0]):
                            best = (d, cp)
                    except Exception:
                        pass
                if best:
                    goal = best[1]
        if goal is not None:
            # don't leave the zone for loot
            if math.hypot(goal[0] - tc[0], goal[1] - tc[1]) > trr - margin * 0.5 and math.hypot(goal[0] - zc[0], goal[1] - zc[1]) > zr - 8:
                goal = None

        cands = [(pos[0], pos[1])]
        for r in (14.0, 30.0, 55.0):
            for k in range(16):
                a = k * PI / 8 + (0.2 if r == 30.0 else 0.0)
                cands.append((pos[0] + r * math.cos(a), pos[1] + r * math.sin(a)))
        if self.target is not None:
            cands.append(self.target)
        if goal is not None:
            cands.append((goal[0], goal[1]))

        best_s = None
        best_p = None
        dmz = math.hypot(pos[0] - tc[0], pos[1] - tc[1])
        for (cx, cy) in cands:
            s = 0.0
            dz_ = math.hypot(cx - tc[0], cy - tc[1])
            over = dz_ - (trr - margin)
            if over > 0:
                s += over * (3.0 if dmz > trr - margin else 2.0)
            dcur = math.hypot(cx - zc[0], cy - zc[1]) - (zr - 6)
            if dcur > 0:
                s += dcur * 8.0
            s += 0.02 * dz_
            gz = self.world.ground(cx, cy)
            cz = pos[2] if gz is None or abs(gz - pos[2]) > 15 else gz
            if gz is not None:
                hd = math.hypot(cx - pos[0], cy - pos[1])
                if hd > 1 and gz - pos[2] > 0.9 * hd:
                    s += 120.0
            if self.world.inside(cx, cy, cz):
                s += 500.0
            cp3 = (cx, cy, cz + 4.5)
            exposed = 0
            nthr = 0
            for (tp, w, vis, haslaser) in threats:
                d = math.hypot(cx - tp[0], cy - tp[1])
                if aggressive and vis:
                    nthr += 1
                    if nthr <= 2:
                        s += w * 0.5 * abs(d - ideal)
                    if d < 18:
                        s += w * (18 - d) * 4
                else:
                    s += w * dist_w * max(0.0, 160.0 - d)
                    if d < 30:
                        s += w * (30 - d) * 3
                if vis and d < 200:
                    if (time.perf_counter() - self.t_start) > 0.022 or not self.world.blocked((tp[0], tp[1], tp[2] + 8.0), cp3):
                        exposed += 1
                        if not aggressive:
                            s += w * expose_w
                        elif haslaser and "laser" not in kinds:
                            s += w * 30.0
                        elif d > 110:
                            s += w * 15.0
            if aggressive and nthr:
                s += 35.0 * max(0, exposed - 1)
                if exposed == 0:
                    s += 6.0
            dm = math.hypot(cx - pos[0], cy - pos[1])
            s += 0.15 * dm
            if goal is not None and abs(cx - goal[0]) < 0.01 and abs(cy - goal[1]) < 0.01:
                s -= 60.0
            if self.target is not None and abs(cx - self.target[0]) < 0.01 and abs(cy - self.target[1]) < 0.01:
                s -= 8.0
            if best_s is None or s < best_s:
                best_s, best_p = s, (cx, cy)
        self.target = best_p
        mvx = best_p[0] - pos[0]
        mvy = best_p[1] - pos[1]
        dlen = math.hypot(mvx, mvy)
        if dlen > 2.0:
            move = [mvx / dlen, mvy / dlen]
        else:
            move = [0.0, 0.0]

        # strafe when engaged
        if enemies and enemies[0][0] < 120:
            if t > self.strafe_t:
                self.strafe = -self.strafe
                self.strafe_t = t + 0.8 + (hash((tick, self.slot)) % 7) * 0.1
            ex, ey = enemies[0][2][0] - pos[0], enemies[0][2][1] - pos[1]
            el = math.hypot(ex, ey) or 1
            px, py = -ey / el * self.strafe, ex / el * self.strafe
            move = [move[0] * 0.8 + px * 0.6, move[1] * 0.8 + py * 0.6]
            ml = math.hypot(*move)
            if ml > 1:
                move = [move[0] / ml, move[1] / ml]

        # dodge charging lasers aimed at us
        for d, m, mp, mv in enemies[:4]:
            if m.get("charging") and d < 280:
                ea = m.get("aim") or [0, 0]
                to_me = math.atan2(pos[1] - mp[1], pos[0] - mp[0])
                if abs(wrap(num(ea[0]) - to_me)) < 0.25:
                    ex, ey = pos[0] - mp[0], pos[1] - mp[1]
                    el = math.hypot(ex, ey) or 1
                    sgn = 1 if (tick // 10) % 2 == 0 else -1
                    move = [-ey / el * sgn, ex / el * sgn]
                    if energy > 15:
                        act["boost"] = True
                    break

        # unstick
        self.hist.append((t, pos[0], pos[1]))
        self.hist = [h for h in self.hist if t - h[0] <= 1.2]
        jump = False
        if t < self.unstick_until:
            ang = math.atan2(move[1], move[0]) + self.unstick_dir * PI / 2
            move = [math.cos(ang), math.sin(ang)]
            jump = True
        elif math.hypot(*move) > 0.5 and len(self.hist) > 8:
            h0 = self.hist[0]
            if t - h0[0] > 0.9 and math.hypot(pos[0] - h0[1], pos[1] - h0[2]) < 1.5:
                self.unstick_until = t + 0.8
                self.unstick_dir = -self.unstick_dir
                jump = True
        act["move"] = [round(move[0], 4), round(move[1], 4)]
        if jump and me.get("grounded", True):
            act["jump"] = True

        # boost when outside / near zone edge, or fleeing hurt
        outside = not me.get("in_zone", True)
        if (outside or dmz > trr - 2 and time_to_shrink < 10) and energy > 20:
            act["boost"] = True
        elif hpf < 0.5 and close_threat < 70 and energy > 40:
            act["boost"] = True

        # ---- aim & fire
        fire = [False, False]
        aim = None
        tgt = None
        bsc = None
        for d, m, mp, mv in enemies[:6]:
            if d > 230:
                continue
            c3 = (mp[0], mp[1], mp[2] + 4.5)
            if not self.world.blocked(muzzle, c3):
                sc = num(m.get("hp"), 1400) + 5.0 * d
                if bsc is None or sc < bsc:
                    bsc = sc
                    tgt = (d, m, mp, mv, c3)
        engage = False
        if tgt is not None:
            d = tgt[0]
            # fight back when close, healthy, or late game / being shot
            engage = True
            # if hurt and can break LOS, prefer regen (no firing) unless very close
            if not aggressive and d > 70 and (t - self.last_damage_t) > 1.5:
                engage = False
        # npc drones shooting at us
        if tgt is None and npcs and (npcs[0][0] < 45 or (not enemies and hpf > 0.5 and npcs[0][0] < 110)):
            d, m, mp, mv = npcs[0]
            c3 = (mp[0], mp[1], mp[2] + 1.8)
            if not self.world.blocked(muzzle, c3):
                tgt = (d, m, mp, mv, c3)
                engage = True

        laser_slot = kinds.get("laser", (None, None))[0]
        laser_w = kinds.get("laser", (None, None))[1]
        if tgt is not None:
            d, m, mp, mv, c3 = tgt
            aim = self.aim_at(muzzle, c3, mv, self.gun_speed)
            if engage:
                # choose weapon
                used = False
                if "lightning" in kinds and d < 42:
                    i, w = kinds["lightning"]
                    if num(w.get("cooldown")) <= 0.05 and energy > 5:
                        fire[i] = True
                        used = True
                if not used and laser_slot is not None and d < 270:
                    charge = num(laser_w.get("charge"))
                    cd = num(laser_w.get("cooldown"))
                    aim = self.aim_at(muzzle, c3, mv, 1e9)
                    if cd <= 0.05:
                        cur = me.get("aim") or aim
                        al = abs(wrap(num(cur[0]) - aim[0])) < max(0.02, math.atan2(2.0, max(d, 1.0))) and abs(num(cur[1]) - aim[1]) < 0.06
                        if charge >= 0.7 and al:
                            fire[laser_slot] = False  # release
                            self.laser_hold = False
                        else:
                            fire[laser_slot] = True
                            self.laser_hold = True
                        used = d > 40
                if "mortar" in kinds and 30 < d < 225:
                    i, w = kinds["mortar"]
                    if num(w.get("cooldown")) <= 0.05 and num(w.get("ammo"), 1) != 0:
                        tube = (pos[0], pos[1], pos[2] + muzzle_h * 9.5 / 7.0)
                        ma = self.mortar_aim(tube, (mp[0], mp[1], mp[2]), mv)
                        if ma is not None and not used:
                            aim = ma
                            fire[i] = True
                            used = True
                if not used:
                    gun = kinds.get("gun")
                    if gun is not None and (d < 120 or (d < 170 and num(m.get("hp"), 1400) < 400)):
                        i, w = gun
                        ammo = num(w.get("ammo"), 1)
                        if ammo > 0 and num(w.get("reload")) <= 0:
                            # only fire if torso is roughly on target
                            cur = me.get("aim") or aim
                            tol = max(0.03, 1.6 * math.atan2(2.5, max(d, 1.0)))
                            if abs(wrap(num(cur[0]) - aim[0])) < tol:
                                fire[i] = True
        elif laser_slot is not None and self.laser_hold:
            # lost target while charging: keep holding briefly (auto-fire later) - release
            fire[laser_slot] = False
            self.laser_hold = False

        if aim is None:
            # face the most likely threat (front armor), else movement direction
            look = None
            if enemies:
                ep = enemies[0][2]
                look = (ep[0], ep[1], ep[2] + 4.5)
            elif self.threat_marks:
                m_ = max(self.threat_marks, key=lambda q: q[3])
                look = (m_[1], m_[2], pos[2] + 6)
            else:
                # shoot crates when safe (cubes = +HP)
                cr_best = None
                if close_threat > 120 and "gun" in kinds:
                    for c in o.get("crates") or []:
                        try:
                            cp = vec3(c.get("pos"))
                            dd = math.dist(cp, pos)
                            if dd < 70 and (cr_best is None or dd < cr_best[0]):
                                cr_best = (dd, (cp[0], cp[1], cp[2] + 1.0))
                        except Exception:
                            pass
                if cr_best and not self.world.blocked(muzzle, cr_best[1]):
                    aim = self.aim_at(muzzle, cr_best[1], (0, 0, 0), self.gun_speed)
                    i, w = kinds["gun"]
                    cur = me.get("aim") or aim
                    if num(w.get("ammo"), 1) > 0 and abs(wrap(num(cur[0]) - aim[0])) < 0.08:
                        fire[i] = True
                elif math.hypot(*move) > 0.3:
                    look = (pos[0] + move[0] * 50, pos[1] + move[1] * 50, pos[2] + eye_h)
                else:
                    # scan slowly
                    yaw = self.last_aim[0] + 0.25
                    aim = [wrap(yaw), 0.0]
            if aim is None and look is not None:
                dx, dy, dz = look[0] - muzzle[0], look[1] - muzzle[1], look[2] - muzzle[2]
                aim = [math.atan2(dy, dx), math.atan2(dz, math.hypot(dx, dy) + 1e-6)]

        aim = [round(wrap(aim[0]), 4), round(max(-1.2, min(1.2, aim[1])), 4)]
        self.last_aim = aim
        act["aim"] = aim
        act["fire"] = fire

        # reload gun when empty or when safe and low
        gun = kinds.get("gun")
        if gun is not None:
            w = gun[1]
            ammo = num(w.get("ammo"), 12)
            mag = num(g(w, "stats", "mag"), 12)
            if ammo <= 0 or (ammo < mag * 0.5 and close_threat > 100):
                if num(w.get("reload")) <= 0:
                    act["reload"] = True
        return act

    def choose_upgrade(self, choices):
        prefs = [("armor", 10), ("hp", 9), ("health", 9), ("regen", 8), ("heal", 8), ("shield", 8),
                 ("speed", 6), ("move", 5), ("energy", 4), ("vision", 4), ("damage", 3), ("reload", 3)]
        best_i, best_s = 0, -1
        for i, c in enumerate(choices):
            txt = (str(c.get("id", "")) + " " + str(c.get("name", "")) + " " + str(c.get("desc", ""))).lower()
            s = 0
            for k, v in prefs:
                if k in txt:
                    s = max(s, v)
            if s > best_s:
                best_i, best_s = i, s
        return best_i


def main():
    bot = Bot()
    fd = sys.stdin.fileno()
    buf = b""
    eof = False
    while not eof:
        r, _, _ = select.select([fd], [], [], 1.0)
        if not r:
            continue
        chunk = os.read(fd, 1 << 20)
        if not chunk:
            eof = True
        buf += chunk
        # drain anything else already available
        while True:
            r, _, _ = select.select([fd], [], [], 0)
            if not r:
                break
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                eof = True
                break
            buf += chunk
        lines = buf.split(b"\n")
        buf = lines.pop()
        latest_obs = None
        for ln in lines:
            ln = ln.strip()
            if not ln:
                continue
            try:
                msg = json.loads(ln)
            except Exception:
                continue
            if not isinstance(msg, dict):
                continue
            typ = msg.get("type")
            if typ == "hello":
                try:
                    bot.hello(msg)
                except Exception as e:
                    log("hello err", e)
                send({"type": "ready", "name": "Ustaad Survivor"})
            elif typ == "obs":
                latest_obs = msg
            elif typ == "end":
                log("end", msg.get("place"), msg.get("kills"))
                return
        if latest_obs is not None:
            try:
                a = bot.act(latest_obs)
            except Exception as e:
                log("act err", repr(e))
                a = {"tick": latest_obs.get("tick"), "move": [0, 0]}
            send(a)


if __name__ == "__main__":
    main()
