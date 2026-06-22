#!/usr/bin/env python3
"""
make_figures.py — illustrative PHaRLAP-style propagation figures for
PHARLAP_RAYTRACING.md.

IMPORTANT — these are ILLUSTRATIVE / SCHEMATIC figures, not live PHaRLAP
output.  PHaRLAP 4.7.4 is the licence-restricted DST binary and is not
installed on every host.  These plots are produced from a simplified
Chapman-layer + spherical-geometry + secant-law model so that the
documentation can show the *form* of the 2-D and 3-D ray-trace products
PHaRLAP generates, and the geometry of the actual hf-tec Alaska→Missouri
paths.  The real figures are produced by pylap.raytrace_2d /
pylap.raytrace_3d driven by an IRI-2020 (2-D) or WAM-IPE (3-D) grid; see
the doc body for the exact calls.

Geometry is real (great-circle distances/bearings of the Hysell Alaska
network to EM38ww); the ionosphere and ray refraction are a teaching model.

Run:  python3 make_figures.py
Out:  fig1_raytrace_2d.png, fig2_propagation_window.png, fig3_raytrace_3d.png
"""
from __future__ import annotations
import math
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch
from mpl_toolkits.mplot3d import proj3d

R = 6371.0  # Earth radius, km
C_KM_S = 299792.458

# --- Sites (from hf-tec data/stations.toml + EM38ww receiver) -------------
SITES = {
    "Poker Flat": (65.1175, -147.4319),
    "Gakona":     (62.3892, -145.1358),
    "Palmer":     (61.5656, -149.2517),
}
RX = (38.9375, -92.125)   # EM38ww, mid-Missouri (AC0G)
RX_NAME = "EM38ww (mid-MO)"


def haversine(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


def bearing(lat1, lon1, lat2, lon2):
    dl = math.radians(lon2 - lon1)
    p1, p2 = math.radians(lat1), math.radians(lat2)
    return (math.degrees(math.atan2(
        math.sin(dl) * math.cos(p2),
        math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl))) % 360.0)


def gc_point(lat1, lon1, brg, dist_km):
    d = dist_km / R
    p1 = math.radians(lat1)
    l1 = math.radians(lon1)
    b = math.radians(brg)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(b))
    l2 = l1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(p1),
                         math.cos(d) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), math.degrees(l2)


# print geometry for the doc
print("Path geometry to", RX_NAME, RX)
for name, (la, lo) in SITES.items():
    print(f"  {name:11s} d={haversine(la,lo,*RX):7.1f} km  brg={bearing(la,lo,*RX):5.1f} deg")


# ==========================================================================
# FIGURE 1 — 2-D ray trace (curved earth), Poker Flat -> EM38ww, 3.4 MHz, night
# ==========================================================================
def fig1():
    tx_name = "Poker Flat"
    la, lo = SITES[tx_name]
    D = haversine(la, lo, *RX)             # ground range km
    foF2 = 3.0                             # MHz, night
    freq = 3.4                             # MHz carrier
    hv = 300.0                             # virtual reflection height km

    fig, ax = plt.subplots(figsize=(11, 6.2))
    alpha_tot = D / R

    def to_xy(ground, height):
        a = ground / R - alpha_tot / 2.0
        r = R + height
        return r * math.sin(a), r * math.cos(a)

    # Earth surface arc
    gs = np.linspace(0, D, 400)
    ex, ey = zip(*[to_xy(g, 0) for g in gs])
    ax.plot(ex, ey, color="#5b3a1a", lw=2.2, zorder=5)
    ax.fill(list(ex) + [to_xy(D, -250)[0], to_xy(0, -250)[0]],
            list(ey) + [to_xy(D, -250)[1], to_xy(0, -250)[1]],
            color="#d9c3a3", zorder=0)

    # Ionospheric F2 layer shading (Chapman-ish band 200-380 km)
    for h0, h1, al in [(200, 250, 0.10), (250, 300, 0.20), (300, 340, 0.14), (340, 380, 0.07)]:
        xs, ys = [], []
        for g in gs:
            x, y = to_xy(g, h0); xs.append(x); ys.append(y)
        for g in gs[::-1]:
            x, y = to_xy(g, h1); xs.append(x); ys.append(y)
        ax.fill(xs, ys, color="#1f6f8b", alpha=al, zorder=1, lw=0)
    # E layer hint
    for g in gs[::8]:
        pass
    ax.plot(*zip(*[to_xy(g, 300) for g in gs]), color="#1f6f8b", lw=0.8, ls="--", alpha=0.5, zorder=2)

    # --- ray fan: reflect if cos(i) <= foF2/freq, else penetrate ----------
    def incidence_cos(beta_deg):
        b = math.radians(beta_deg)
        sin_i = (R * math.cos(b)) / (R + hv)
        return math.sqrt(max(0.0, 1 - sin_i ** 2))

    crit = foF2 / freq
    fan = np.arange(8, 86, 6.0)
    for beta in fan:
        reflects = incidence_cos(beta) <= crit
        if reflects:
            # one hop ground range for this launch elevation at height hv
            b = math.radians(beta)
            # solve half-hop central angle from elevation (spherical)
            # ground range of a single hop:
            psi = math.acos(min(1.0, (R / (R + hv)) * math.cos(b))) - b
            d_hop = 2 * R * psi
            g0 = 0.0
            xs, ys = [], []
            nh = 0
            while g0 < D - 50 and nh < 6:
                tt = np.linspace(0, 1, 60)
                for t in tt:
                    z = hv * 4 * t * (1 - t)
                    g = g0 + t * d_hop
                    if g > D:
                        break
                    x, y = to_xy(min(g, D), z)
                    xs.append(x); ys.append(y)
                g0 += d_hop
                nh += 1
            ax.plot(xs, ys, color="#888", lw=0.7, alpha=0.55, zorder=3)
        else:
            # penetrating ray: straight out at elevation beta
            b = math.radians(beta)
            xs, ys = [], []
            for s in np.linspace(0, 700, 40):
                # approx straight in local frame
                g = s * math.cos(b)
                z = s * math.sin(b)
                x, y = to_xy(g, z)
                xs.append(x); ys.append(y)
            ax.plot(xs, ys, color="#c0392b", lw=0.7, alpha=0.5, ls=(0, (4, 2)), zorder=3)

    # --- closing modes: 2-hop and 3-hop reaching RX -----------------------
    def draw_mode(nhops, color, label):
        d_hop = D / nhops
        # virtual-height group path
        phi = d_hop / (2 * R)
        chord = math.sqrt(R ** 2 + (R + hv) ** 2 - 2 * R * (R + hv) * math.cos(phi))
        grp_path = nhops * 2 * chord
        delay_ms = grp_path / C_KM_S * 1000.0
        # launch elevation
        b = math.atan2(math.cos(phi) - R / (R + hv), math.sin(phi))
        elev = math.degrees(b)
        xs, ys = [], []
        for k in range(nhops):
            tt = np.linspace(0, 1, 80)
            for t in tt:
                z = hv * 4 * t * (1 - t)
                g = (k + t) * d_hop
                x, y = to_xy(g, z)
                xs.append(x); ys.append(y)
        ax.plot(xs, ys, color=color, lw=2.4, zorder=6,
                label=f"{label}: {nhops}-hop F2, elev {elev:.0f}°, "
                      f"group delay {delay_ms:.1f} ms")
        # apogee markers
        for k in range(nhops):
            x, y = to_xy((k + 0.5) * d_hop, hv)
            ax.plot([x], [y], "o", color=color, ms=5, zorder=7)

    draw_mode(2, "#e67e22", "Mode A")
    draw_mode(3, "#2e86de", "Mode B")

    # TX / RX markers
    x0, y0 = to_xy(0, 0); x1, y1 = to_xy(D, 0)
    ax.plot([x0], [y0], "^", color="k", ms=11, zorder=8)
    ax.plot([x1], [y1], "v", color="k", ms=11, zorder=8)
    ax.annotate(f"{tx_name}\nTX 3.4 MHz", (x0, y0), textcoords="offset points",
                xytext=(-6, 14), ha="right", fontsize=9, fontweight="bold")
    ax.annotate(f"{RX_NAME}\nRX", (x1, y1), textcoords="offset points",
                xytext=(6, 14), ha="left", fontsize=9, fontweight="bold")

    ax.set_title("Fig. 1 — 2-D ray trace (curved Earth): Poker Flat → EM38ww, "
                 "3.4 MHz, night ionosphere\n(illustrative; foF2≈3.0 MHz, "
                 f"hmF2≈300 km; ground range {D:.0f} km)", fontsize=11)
    ax.text(0.012, 0.02,
            "grey = reflecting ray fan   red dashed = penetrating (above MUF for steep angles)\n"
            "F2 layer shaded; dots = apogee.  Group delay is the timing observable PHaRLAP refines.",
            transform=ax.transAxes, fontsize=8, color="#333", va="bottom")
    ax.set_aspect("equal")
    ax.axis("off")
    ax.legend(loc="upper center", fontsize=9, framealpha=0.92, ncol=1)
    # tight view
    allx = [x0, x1] + list(ex)
    ax.set_xlim(min(allx) * 1.001 - 50, max(allx) * 1.001 + 50)
    fig.tight_layout()
    fig.savefig("fig1_raytrace_2d.png", dpi=130)
    plt.close(fig)
    print("wrote fig1_raytrace_2d.png")


# ==========================================================================
# FIGURE 2 — diurnal propagation window (MUF/LUF) for the path, 2.9 & 3.4 MHz
# ==========================================================================
def fig2():
    tx_name = "Gakona"
    la, lo = SITES[tx_name]
    D = haversine(la, lo, *RX)
    # path-midpoint local-time model
    hours = np.linspace(0, 24, 481)

    # foF2 diurnal (winter mid/high lat, moderate solar): night ~3, day ~7
    def foF2(t):
        return 5.0 + 2.0 * np.cos((t - 14) / 24 * 2 * np.pi)  # peak ~14 LT

    # obliquity (secant) factor for ~2-hop path
    sec_factor = 2.9
    muf = foF2(hours) * sec_factor

    # LUF from D-layer absorption: high at midday, low at night
    sza_term = np.clip(np.cos((hours - 12) / 24 * 2 * np.pi), 0, None)
    luf = 1.4 + 7.2 * sza_term ** 1.3   # MHz

    fig, ax = plt.subplots(figsize=(11, 5.6))
    # usable band
    ax.fill_between(hours, luf, muf, where=(muf > luf), color="#2ecc71",
                    alpha=0.25, label="usable band (LUF–MUF)")
    ax.plot(hours, muf, color="#2e86de", lw=2.2, label="MUF (F2, ~2-hop)")
    ax.plot(hours, luf, color="#c0392b", lw=2.2, label="LUF (D-layer absorption)")

    for f, c in [(2.9, "#8e44ad"), (3.4, "#d35400")]:
        ax.axhline(f, color=c, lw=2.0, ls="--")
        ax.text(0.2, f + 0.12, f"{f} MHz carrier", color=c, fontsize=9, fontweight="bold")

    # shade local night (optimum) — approx where both carriers are inside band
    usable_34 = (3.4 < muf) & (3.4 > luf)
    ax.fill_between(hours, 0, 21, where=usable_34, color="#34495e", alpha=0.07,
                    label="both carriers propagating")

    ax.set_xlim(0, 24)
    ax.set_ylim(0, 21)
    ax.set_xticks(range(0, 25, 2))
    ax.set_xlabel("Local solar time at path midpoint (h)")
    ax.set_ylabel("Frequency (MHz)")
    ax.set_title(f"Fig. 2 — Diurnal propagation window: {tx_name} → EM38ww "
                 f"({D:.0f} km)\nOptimum copy of the 2.9 / 3.4 MHz beacons is "
                 "local night (illustrative model)", fontsize=11)
    ax.grid(alpha=0.25)
    ax.legend(loc="upper right", fontsize=8.5, framealpha=0.93)
    fig.tight_layout()
    fig.savefig("fig2_propagation_window.png", dpi=130)
    plt.close(fig)
    print("wrote fig2_propagation_window.png")


# ==========================================================================
# FIGURE 3 — 3-D ray trace over the globe: all three sites -> EM38ww
# ==========================================================================
class Arrow3D(FancyArrowPatch):
    def __init__(self, xs, ys, zs, *args, **kw):
        super().__init__((0, 0), (0, 0), *args, **kw)
        self._v = (xs, ys, zs)

    def do_3d_projection(self, renderer=None):
        xs, ys, zs = self._v
        xp, yp, zp = proj3d.proj_transform(xs, ys, zs, self.axes.M)
        self.set_positions((xp[0], yp[0]), (xp[1], yp[1]))
        return float(np.min(zp))


def lla_to_xyz(lat, lon, h=0.0, scale_h=18.0):
    r = (R + h * scale_h) / R   # exaggerate height for visibility
    la, lo = math.radians(lat), math.radians(lon)
    return (r * math.cos(la) * math.cos(lo),
            r * math.cos(la) * math.sin(lo),
            r * math.sin(la))


def fig3():
    fig = plt.figure(figsize=(10.5, 8.6))
    ax = fig.add_subplot(111, projection="3d")
    allpts = []  # collect for tight framing

    # globe surface patch (N. America / N. Pacific, around the paths)
    u = np.radians(np.linspace(-160, -80, 70))
    v = np.radians(np.linspace(33, 70, 50))
    uu, vv = np.meshgrid(u, v)
    xs = np.cos(vv) * np.cos(uu)
    ys = np.cos(vv) * np.sin(uu)
    zs = np.sin(vv)
    ax.plot_surface(xs, ys, zs, color="#bcd6ee", alpha=0.45,
                    linewidth=0, antialiased=True, zorder=0)
    # graticule
    for lon in range(-160, -79, 10):
        la = np.linspace(33, 70, 30)
        pts = np.array([lla_to_xyz(a, lon) for a in la])
        ax.plot(*pts.T, color="#8aa", lw=0.5, alpha=0.55)
    for lat in range(35, 71, 5):
        lo = np.linspace(-160, -80, 40)
        pts = np.array([lla_to_xyz(lat, a) for a in lo])
        ax.plot(*pts.T, color="#8aa", lw=0.5, alpha=0.55)

    hv = 300.0
    colors = {"Poker Flat": "#e67e22", "Gakona": "#2e86de", "Palmer": "#27ae60"}

    rx_xyz = lla_to_xyz(*RX, 0)
    allpts.append(rx_xyz)
    for name, (la, lo) in SITES.items():
        D = haversine(la, lo, *RX)
        brg = bearing(la, lo, *RX)
        nhops = max(2, int(round(D / 2000.0)))   # ~2000 km per hop
        d_hop = D / nhops
        col = colors[name]
        # build 3-D multi-hop path with a small off-great-circle bow
        path = []
        for k in range(nhops):
            for t in np.linspace(0, 1, 40):
                frac = (k + t) / nhops
                g = (k + t) * d_hop
                h = hv * 4 * t * (1 - t)
                # lateral deviation (3-D refraction illustration): up to ~120 km
                dev = 120.0 * math.sin(math.pi * frac) * (1 if name == "Gakona" else 0.4)
                plat, plon = gc_point(la, lo, brg, g)
                # offset perpendicular to path for the bow
                plat2, plon2 = gc_point(plat, plon, (brg + 90) % 360, dev)
                path.append(lla_to_xyz(plat2, plon2, h))
        path = np.array(path)
        allpts.extend(path.tolist())
        ax.plot(*path.T, color=col, lw=2.6, zorder=6, label=f"{name} ({D:.0f} km, {nhops} hops)")
        # ground track (great circle on the surface) for geographic anchoring
        gt = np.array([lla_to_xyz(*gc_point(la, lo, brg, g), 0.0)
                       for g in np.linspace(0, D, 60)])
        ax.plot(*gt.T, color=col, lw=1.0, ls=":", alpha=0.8, zorder=4)
        # apogee markers
        for k in range(nhops):
            plat, plon = gc_point(la, lo, brg, (k + 0.5) * d_hop)
            ax.scatter(*lla_to_xyz(plat, plon, hv), color=col, s=22, zorder=7)
        # TX marker
        ax.scatter(*lla_to_xyz(la, lo, 0), color=col, marker="^", s=70,
                   edgecolor="k", zorder=8)
        ax.text(*lla_to_xyz(la, lo, 0), f"  {name}", fontsize=8.5, fontweight="bold")

    ax.scatter(*rx_xyz, color="k", marker="v", s=90, zorder=9)
    ax.text(*rx_xyz, f"  {RX_NAME}", fontsize=9, fontweight="bold")

    ax.set_title("Fig. 3 — 3-D ray trace over the globe: Alaska hf-tec network → EM38ww\n"
                 "multi-hop F2 paths; lateral bow illustrates 3-D off-great-circle\n"
                 "refraction PHaRLAP resolves (heights exaggerated ×18; illustrative)",
                 fontsize=11)
    ax.legend(loc="upper left", fontsize=9)
    # tight framing around the data
    P = np.array(allpts)
    cx, cy, cz = P.mean(0)
    rad = np.max(np.linalg.norm(P - [cx, cy, cz], axis=1)) * 1.08
    ax.set_xlim(cx - rad, cx + rad)
    ax.set_ylim(cy - rad, cy + rad)
    ax.set_zlim(cz - rad, cz + rad)
    ax.set_box_aspect((1, 1, 1))
    ax.view_init(elev=28, azim=-78)
    try:
        ax.set_proj_type("persp", focal_length=0.5)  # closer/zoomed perspective
    except Exception:
        pass
    ax.set_axis_off()
    fig.subplots_adjust(left=-0.05, right=1.05, bottom=-0.02, top=0.93)
    fig.savefig("fig3_raytrace_3d.png", dpi=130)
    plt.close(fig)
    print("wrote fig3_raytrace_3d.png")


if __name__ == "__main__":
    fig1()
    fig2()
    fig3()
