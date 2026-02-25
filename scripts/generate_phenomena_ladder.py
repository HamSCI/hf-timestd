#!/usr/bin/env python3
"""
Generate "What Science Can You Do?" phenomena ladder figure.

Four hardware tiers on the left, two observable domains (rate vs absolute)
in the body, with phenomena listed in each domain and tier they require.

Usage:
    python3 scripts/generate_phenomena_ladder.py
"""

import os
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch
from matplotlib.lines import Line2D

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), '..', 'docs', 'figures')

# ---------------------------------------------------------------------------
# The four hardware tiers
# ---------------------------------------------------------------------------
TIERS = [
    # (tier_num, label, hw_desc, cost, color, y_center)
    (1, 'Tier 1', 'RX888 alone',                     '~$180',  '#e0e0e0', 0.88),
    (2, 'Tier 2', 'RX888 + GPSDO',                   '~$340', '#c6e9af', 0.63),
    (3, 'Tier 3', 'RX888 + GPSDO\n+ GPS+PPS on LAN', '~$450', '#aad4f5', 0.35),
    (4, 'Tier 4', 'RX888 + GPSDO\n+ PPS in HF stream','~$620','#f5d0a9', 0.10),
]

# What the GPSDO provides vs what absolute time provides
# (domain, tier_min, items)
# domain: 'rate' = needs only GPSDO (frequency stability)
#         'absolute' = needs absolute time-of-day (PPS or HF self-recovery)

RATE_ITEMS = [
    # (tier_min, name, detail, color)
    (2, 'Carrier-phase Doppler',
     '±0.34 Hz range, 99.7% coverage, 24/7',
     '#FF8C00'),
    (2, 'dTEC/dt  (ionospheric TEC rate)',
     '~6 mTECU/min sensitivity, 17K records/day',
     '#28A745'),
    (2, 'Differential dTEC',
     'Cross-freq self-consistency: RMS < 0.03 TECU',
     '#28A745'),
    (2, 'TID / terminator / SID detection',
     'Doppler signatures of ionospheric dynamics & disturbances',
     '#FF8C00'),
    (2, 'Scintillation  (S4, σ_φ)',
     'Tick-to-tick amplitude + phase variance',
     '#9467bd'),
    (2, 'GNSS-anchored absolute dTEC',
     'ZED-F9P VTEC anchor: ±1 TECU → absolute scale',
     '#28A745'),
]

ABSOLUTE_ITEMS = [
    (1, 'Station detection',
     'Template matching: WWV/WWVH/CHU/BPM',
     '#2176FF'),
    (1, 'Coarse propagation mode',
     'WWVH (24 ms) vs WWV (4 ms) vs BPM (39 ms)',
     '#2176FF'),
    (3, 'D_clock  (propagation delay residual)',
     '±1 ms via HF self-recovery or PPS',
     '#2176FF'),
    (3, 'Mode identification  (1F / 2F / E)',
     'Δτ ≈ 5–15 ms between modes',
     '#2176FF'),
    (3, 'Diurnal layer height + storm TEC',
     'hmF2 variation (Δτ 3–8 ms), storm surge (Δτ 1–5 ms)',
     '#FF8C00'),
    (3, 'D_clock discrimination',
     'Delay ordering: WWV < WWVH < BPM on shared channels',
     '#2176FF'),
    (4, 'Group-delay TEC  (1/f² dispersion)',
     'Currently SNR 0.13; needs µs-level timing',
     '#28A745'),
    (4, 'Sub-ms multipath resolution',
     'Phase-domain sub-sample path differences',
     '#2176FF'),
]


def _draw_tier_row(ax, y_center, height, color, tier_num, label, hw, cost):
    """Draw one hardware tier row spanning the full width."""
    ax.axhspan(y_center - height/2, y_center + height/2,
               facecolor=color, alpha=0.35, zorder=0)
    ax.axhline(y_center - height/2, color='#bbb', lw=0.5, zorder=1)


def _draw_item(ax, x, y, name, detail, color, fontsize_name=9.5,
               fontsize_detail=7.5):
    """Draw a single phenomenon item."""
    ax.plot(x - 0.015, y, 'o', color=color, markersize=6, zorder=5,
            markeredgecolor='white', markeredgewidth=0.4)
    ax.text(x, y + 0.006, name, fontsize=fontsize_name, fontweight='bold',
            va='bottom', ha='left', color=color, zorder=6)
    ax.text(x, y - 0.006, detail, fontsize=fontsize_detail,
            va='top', ha='left', color='#555', zorder=6)


def generate_figure():
    fig, ax = plt.subplots(figsize=(18, 13))
    ax.set_xlim(-0.02, 1.02)
    ax.set_ylim(-0.05, 1.08)
    ax.set_aspect('auto')

    # ---- Tier band dimensions ----
    tier_height = 0.22
    tier_gap = 0.03
    # Tiers from bottom (4) to top (1)
    tier_y = {}
    for t_num, label, hw, cost, color, y_c in TIERS:
        tier_y[t_num] = y_c
        _draw_tier_row(ax, y_c, tier_height, color, t_num, label, hw, cost)

    # Top boundary line
    ax.axhline(TIERS[0][5] + tier_height/2, color='#bbb', lw=0.5, zorder=1)

    # ---- Hardware tier labels (left column, x < 0.18) ----
    for t_num, label, hw, cost, color, y_c in TIERS:
        ax.text(0.005, y_c + 0.05, f'{label}',
                fontsize=14, fontweight='bold', va='center', ha='left',
                color='#222', zorder=6)
        ax.text(0.005, y_c + 0.005, hw,
                fontsize=10, va='center', ha='left', color='#444', zorder=6)
        ax.text(0.005, y_c - 0.04, cost,
                fontsize=9, va='center', ha='left', color='#777',
                fontstyle='italic', zorder=6)

    # ---- Column headers (above the tier bands) ----
    col_rate_x = 0.20
    col_abs_x = 0.60
    header_y = 1.04

    ax.text(col_rate_x + 0.15, header_y,
            'RATE  DOMAIN\n(needs only GPSDO — frequency stability)',
            fontsize=12, fontweight='bold', va='top', ha='center',
            color='#2a7f2a',
            bbox=dict(boxstyle='round,pad=0.4', facecolor='#e8f5e9',
                      edgecolor='#66bb6a', linewidth=1.2, alpha=0.9),
            zorder=8)

    ax.text(col_abs_x + 0.15, header_y,
            'ABSOLUTE  TIME  DOMAIN\n(needs UTC — PPS or HF self-recovery)',
            fontsize=12, fontweight='bold', va='top', ha='center',
            color='#1565c0',
            bbox=dict(boxstyle='round,pad=0.4', facecolor='#e3f2fd',
                      edgecolor='#42a5f5', linewidth=1.2, alpha=0.9),
            zorder=8)

    # ---- Vertical divider between columns ----
    ax.axvline(0.57, color='#ccc', lw=1.5, ls='--', zorder=1)

    # ---- Vertical divider between HW and phenomena ----
    ax.axvline(0.175, color='#aaa', lw=1.0, zorder=1)

    # ---- Place rate-domain items ----
    # All rate items require Tier 2.  Place them within Tiers 2+3 bands
    # with a bracket indicating they're available from Tier 2 onward.
    rate_y_top = tier_y[2] + tier_height/2 - 0.025
    rate_y_bot = tier_y[3] - tier_height/2 + 0.025
    n_rate = len(RATE_ITEMS)
    rate_spacing = (rate_y_top - rate_y_bot) / max(n_rate - 1, 1)

    for i, (t_min, name, detail, color) in enumerate(RATE_ITEMS):
        y = rate_y_top - i * rate_spacing
        _draw_item(ax, col_rate_x, y, name, detail, color)

    # Available-from-Tier-2 bracket (on the right side of the rate column)
    bx = 0.545
    ax.plot([bx, bx], [rate_y_bot - 0.01, rate_y_top + 0.02],
            color='#2a7f2a', lw=2, solid_capstyle='round', zorder=4)
    ax.plot([bx - 0.008, bx], [rate_y_top + 0.02, rate_y_top + 0.02],
            color='#2a7f2a', lw=2, solid_capstyle='round', zorder=4)
    ax.plot([bx - 0.008, bx], [rate_y_bot - 0.01, rate_y_bot - 0.01],
            color='#2a7f2a', lw=2, solid_capstyle='round', zorder=4)
    ax.text(bx + 0.004, (rate_y_top + rate_y_bot) / 2,
            'All available\nfrom Tier 2',
            fontsize=7.5, fontweight='bold', color='#2a7f2a',
            va='center', ha='left', rotation=0, zorder=6)

    # ---- Place absolute-time-domain items ----
    # Items are placed in the tier band they require
    # Group by tier
    abs_by_tier = {}
    for t_min, name, detail, color in ABSOLUTE_ITEMS:
        abs_by_tier.setdefault(t_min, []).append((name, detail, color))

    for t_num, items in abs_by_tier.items():
        y_c = tier_y[t_num]
        n = len(items)
        y_top = y_c + tier_height/2 - 0.025
        y_bot = y_c - tier_height/2 + 0.025
        spacing = (y_top - y_bot) / max(n - 1, 1) if n > 1 else 0
        for i, (name, detail, color) in enumerate(items):
            y = y_top - i * spacing
            _draw_item(ax, col_abs_x, y, name, detail, color)

    # ---- "HF self-recovery" bridge arrow from Tier 2 to Tier 3 ----
    bridge_x = 0.155
    ax.annotate(
        '',
        xy=(bridge_x, tier_y[3] + tier_height/2 - 0.01),
        xytext=(bridge_x, tier_y[2] - tier_height/2 + 0.01),
        arrowprops=dict(arrowstyle='->', color='#1565c0', lw=2.5,
                        connectionstyle='arc3,rad=0.2'))
    ax.text(bridge_x - 0.005, (tier_y[2] + tier_y[3]) / 2,
            'HF self-\nrecovery\n(this talk)',
            fontsize=8.5, fontweight='bold', color='#1565c0',
            va='center', ha='right', rotation=0,
            bbox=dict(boxstyle='round,pad=0.3', facecolor='white',
                      alpha=0.95, edgecolor='#1565c0', linewidth=1.0),
            zorder=7)

    # ---- Tier 2 "THE BIG UNLOCK" callout (in Tier 1 empty space) ----
    ax.text(col_rate_x + 0.15, tier_y[1],
            '★  The GPSDO upgrade that unlocks ionospheric science',
            fontsize=11, fontweight='bold', color='#1b5e20',
            va='center', ha='center',
            bbox=dict(boxstyle='round,pad=0.4', facecolor='#e8f5e9',
                      edgecolor='#66bb6a', linewidth=1.5, alpha=0.9),
            zorder=7)
    ax.annotate('', xy=(col_rate_x + 0.15, tier_y[2] + tier_height/2 + 0.005),
                xytext=(col_rate_x + 0.15, tier_y[1] - 0.04),
                arrowprops=dict(arrowstyle='->', color='#1b5e20', lw=2),
                zorder=7)

    # ---- Category legend at bottom ----
    cat_items = [
        ('#2176FF', 'Propagation geometry'),
        ('#28A745', 'Electron content (TEC)'),
        ('#FF8C00', 'Ionospheric dynamics'),
        ('#DC3545', 'Space weather events'),
        ('#9467bd', 'Scintillation / turbulence'),
    ]
    legend_elements = [
        Line2D([0], [0], marker='o', color='w',
               markerfacecolor=c, markersize=9, label=lbl)
        for c, lbl in cat_items
    ]
    ax.legend(handles=legend_elements, loc='lower center',
              bbox_to_anchor=(0.5, -0.06), ncol=5, fontsize=9,
              framealpha=0.95, edgecolor='gray', columnspacing=1.2)

    # ---- Title ----
    ax.set_title(
        'With an RX888 and a GPSDO, What Science Can You Do?',
        fontsize=17, fontweight='bold', pad=15)

    # ---- Bottom annotation ----
    fig.text(0.5, 0.015,
             'Rate-domain observables (left) need only a stable sample clock  '
             '·  Absolute-time observables (right) need UTC  '
             '·  HF self-recovery bridges Tier 2 → Tier 3',
             ha='center', fontsize=10, fontstyle='italic', color='#555')

    # ---- Clean up axes ----
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)

    plt.tight_layout(rect=[0, 0.05, 1, 1])
    out = os.path.join(OUTPUT_DIR, 'fig15_phenomena_ladder.png')
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    fig.savefig(out, dpi=150, bbox_inches='tight')
    plt.close(fig)
    print(f"Saved: {out}")


if __name__ == '__main__':
    generate_figure()
