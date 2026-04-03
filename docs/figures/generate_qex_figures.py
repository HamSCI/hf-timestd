#!/usr/bin/env python3
"""
Generate figures for the QEX article: "UTC Recovery and Ionospheric Science
from HF Time Signals with a GPSDO SDR"

Usage:
    /opt/hf-timestd/venv/bin/python3 docs/figures/generate_qex_figures.py

Parameters:
    --date YYYYMMDD
        Target UTC date to render figures from.
        Default: 20260317

    --data-root PATH
        Root timestd data directory containing phase2/ and data/.
        Default: /var/lib/timestd

    --output-dir PATH
        Directory to write figure PNG outputs into.
        Default: docs/figures (the directory containing this script)

    --dpi INT
        Raster DPI for output figures.
        Default: 200

    --channels CSV
        Comma-separated list of channel directory names to include in plots that
        iterate CHANNELS (e.g. Fig 3 per-broadcast scatter).
        Default:
            CHU_3330,CHU_7850,CHU_14670,
            SHARED_2500,SHARED_5000,SHARED_10000,SHARED_15000,
            WWV_20000,WWV_25000

    --figs CSV
        Comma-separated list of figure numbers to generate.
        Supported: 1-9
        Default: 1-9

Generates Figs 3–7 into docs/figures/. Figs 1–2 require separate treatment
(diagram tool / raw IQ respectively).

Target date: 2026-03-15 (complete 24h, zero VTEC gaps, March equinox conditions)
"""

import os
import sys
import numpy as np
import h5py
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime, timezone, timedelta
from pathlib import Path
import argparse

# ── Configuration ──────────────────────────────────────────────────────────

TARGET_DATE = '20260317'
DATA_ROOT = '/var/lib/timestd'
PHASE2 = f'{DATA_ROOT}/phase2'
OUTPUT_DIR = Path(__file__).parent
DPI = 200

CHANNELS = [
    'CHU_3330', 'CHU_7850', 'CHU_14670',
    'SHARED_2500', 'SHARED_5000', 'SHARED_10000', 'SHARED_15000',
    'WWV_20000', 'WWV_25000',
]

# Colors by station
STATION_COLORS = {
    'CHU': '#2196F3',    # blue
    'WWV': '#4CAF50',    # green
    'WWVH': '#FF9800',   # orange
    'BPM': '#9C27B0',    # purple
}

FREQ_MARKERS = {
    2.5: 'o', 3.33: 's', 5.0: '^', 7.85: 'D',
    10.0: 'v', 14.67: 'p', 15.0: '>', 20.0: '<', 25.0: 'h',
}


def _parse_args(argv):
    parser = argparse.ArgumentParser(
        prog='generate_qex_figures.py',
        description='Generate figures for the QEX article from hf-timestd outputs.',
    )
    parser.add_argument('--date', default=TARGET_DATE, help='Target UTC date (YYYYMMDD)')
    parser.add_argument('--data-root', default=DATA_ROOT, help='Root timestd data directory')
    parser.add_argument('--output-dir', default=str(OUTPUT_DIR), help='Output directory for figures')
    parser.add_argument('--dpi', type=int, default=DPI, help='Output DPI')
    parser.add_argument(
        '--channels',
        default=','.join(CHANNELS),
        help='Comma-separated channel list (directory names)'
    )
    parser.add_argument(
        '--figs',
        default='1-9',
        help='Comma-separated list of figure numbers (e.g. 3,5,7) or a range (e.g. 3-7)'
    )
    return parser.parse_args(argv)


def _parse_figs(figs_str: str):
    s = figs_str.strip()
    if not s:
        return list(range(1, 10))
    if '-' in s and ',' not in s:
        a, b = s.split('-', 1)
        return list(range(int(a), int(b) + 1))
    return [int(x.strip()) for x in s.split(',') if x.strip()]


# ── Helpers ────────────────────────────────────────────────────────────────

def parse_ts_bytes(ts_array):
    """Parse HDF5 byte-string timestamps to matplotlib date numbers."""
    dates = []
    for ts in ts_array:
        s = ts.decode() if isinstance(ts, bytes) else ts
        # Handle various formats
        s = s.replace('+00:00', '').rstrip('Z')
        try:
            dt = datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
        except ValueError:
            dt = datetime.strptime(s[:19], '%Y-%m-%dT%H:%M:%S').replace(tzinfo=timezone.utc)
        dates.append(dt)
    return dates


def hours_from_midnight(dates):
    """Convert datetime list to fractional hours from midnight UTC."""
    d0 = dates[0].replace(hour=0, minute=0, second=0, microsecond=0)
    return np.array([(d - d0).total_seconds() / 3600.0 for d in dates])


# ══════════════════════════════════════════════════════════════════════════
# FIGURE 3 — 24h D_clock time series, per-broadcast + fusion
# ══════════════════════════════════════════════════════════════════════════

def generate_fig3():
    print("Generating Fig 3: D_clock time series...")

    fig, (ax_top, ax_bot) = plt.subplots(2, 1, figsize=(10, 6.5),
                                          height_ratios=[3, 1], sharex=True)

    # ── Top panel: per-broadcast D_clock (clipped to ±15 ms) ──
    legend_handles = {}
    for ch in CHANNELS:
        fn = f'{PHASE2}/{ch}/clock_offset/{ch}_timing_measurements_{TARGET_DATE}.h5'
        if not os.path.exists(fn):
            continue
        with h5py.File(fn, 'r') as f:
            ts = parse_ts_bytes(f['timestamp_utc'][:])
            hrs = hours_from_midnight(ts)
            d_clock = f['clock_offset_ms'][:]
            stations = [s.decode() for s in f['station'][:]]
            freq = f['frequency_mhz'][0]

        for stn in sorted(set(stations)):
            mask = np.array([s == stn for s in stations])
            color = STATION_COLORS.get(stn, '#888888')
            freq_val = round(freq, 2)

            # Clip to ±15 ms for display (outliers are search-window rail)
            vals = d_clock[mask]
            h = hrs[mask]
            in_range = np.abs(vals) < 15.0

            ax_top.scatter(h[in_range], vals[in_range], s=2, alpha=0.2,
                           color=color, marker='.', rasterized=True)
            if stn not in legend_handles:
                legend_handles[stn] = ax_top.scatter([], [], s=25, color=color,
                                                      label=stn, marker='o')

    # Overlay fused D_clock as bold line
    fusion_fn = f'{PHASE2}/fusion/fusion_fusion_timing_{TARGET_DATE}.h5'
    with h5py.File(fusion_fn, 'r') as f:
        ts = parse_ts_bytes(f['timestamp_utc'][:])
        hrs_fused = hours_from_midnight(ts)
        d_fused = f['d_clock_fused_ms'][:]
        d_l1 = f['d_clock_l1_ms'][:]
        unc = f['uncertainty_ms'][:]
        wwv_mean = f['wwv_mean_ms'][:]
        wwvh_mean = f['wwvh_mean_ms'][:]
        chu_mean = f['chu_mean_ms'][:]

    ax_top.plot(hrs_fused, d_fused, color='#D32F2F', linewidth=1.8,
                label='WLS Fusion', zorder=10)
    ax_top.fill_between(hrs_fused, d_fused - 0.5, d_fused + 0.5,
                        color='#D32F2F', alpha=0.12, zorder=5,
                        label='±0.5 ms (1σ)')

    ax_top.set_ylabel('D_clock (ms)', fontsize=11)
    ax_top.set_title('24-Hour Clock Offset: Per-Broadcast Measurements and WLS Fusion',
                     fontsize=12, fontweight='bold')
    ax_top.set_ylim(-12, 12)
    ax_top.legend(fontsize=8, loc='upper right', ncol=3, markerscale=1.5)
    ax_top.grid(True, alpha=0.3)
    ax_top.axhline(0, color='#666', linewidth=0.5, linestyle='-', alpha=0.5)

    # ── Bottom panel: fusion detail (±2 ms) ──
    ax_bot.plot(hrs_fused, d_fused, color='#D32F2F', linewidth=1.2, label='Fused')
    ax_bot.fill_between(hrs_fused, d_fused - unc, d_fused + unc,
                        color='#D32F2F', alpha=0.1, label='±u_fused')

    # Per-station means
    valid_wwv = ~np.isnan(wwv_mean)
    valid_wwvh = ~np.isnan(wwvh_mean)
    valid_chu = ~np.isnan(chu_mean)
    ax_bot.plot(np.array(hrs_fused)[valid_wwv], wwv_mean[valid_wwv],
                color='#4CAF50', linewidth=0.6, alpha=0.6, label='WWV mean')
    ax_bot.plot(np.array(hrs_fused)[valid_wwvh], wwvh_mean[valid_wwvh],
                color='#FF9800', linewidth=0.6, alpha=0.6, label='WWVH mean')
    ax_bot.plot(np.array(hrs_fused)[valid_chu], chu_mean[valid_chu],
                color='#2196F3', linewidth=0.6, alpha=0.6, label='CHU mean')

    ax_bot.set_ylabel('D_clock (ms)', fontsize=10)
    ax_bot.set_xlabel(f'UTC Hour ({TARGET_DATE[:4]}-{TARGET_DATE[4:6]}-{TARGET_DATE[6:]})', fontsize=11)
    ax_bot.set_ylim(-3, 3)
    ax_bot.set_xlim(0, 24)
    ax_bot.set_xticks(range(0, 25, 3))
    ax_bot.axhline(0, color='#666', linewidth=0.5, linestyle='-', alpha=0.5)
    ax_bot.legend(fontsize=7, loc='upper right', ncol=5)
    ax_bot.grid(True, alpha=0.3)
    ax_bot.set_title('Fusion Detail (expanded scale)', fontsize=9, fontstyle='italic')

    fig.tight_layout()
    outpath = OUTPUT_DIR / 'fig3_dclock_24h.png'
    fig.savefig(outpath, dpi=DPI, bbox_inches='tight')
    plt.close(fig)
    print(f"  → {outpath}")


# ══════════════════════════════════════════════════════════════════════════
# FIGURE 4 — Uncertainty budget waterfall (ISO GUM style)
# ══════════════════════════════════════════════════════════════════════════

def generate_fig4():
    print("Generating Fig 4: Uncertainty budget waterfall...")

    components = [
        ('u_rtp\n(GPS+PPS)', 0.050),
        ('u_detection\n(matched filter)', 0.200),
        ('u_prop (geo)\n(geometric)', 5.000),
        ('u_prop (IRI)\n(IRI-2020)', 1.500),
        ('u_prop (VTEC)\n(GNSS VTEC)', 0.300),
        ('u_fused\n(17-broadcast WLS)', 0.500),
    ]

    labels = [c[0] for c in components]
    values = [c[1] for c in components]

    fig, ax = plt.subplots(figsize=(9, 4.5))

    colors = ['#4CAF50', '#4CAF50', '#F44336', '#FF9800', '#2196F3', '#D32F2F']
    bars = ax.bar(range(len(values)), values, color=colors, edgecolor='white',
                  linewidth=1.5, width=0.65)

    # Value labels on bars
    for i, (bar, val) in enumerate(zip(bars, values)):
        if val >= 1.0:
            ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.15,
                    f'{val:.1f} ms', ha='center', va='bottom', fontsize=9,
                    fontweight='bold')
        else:
            ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.15,
                    f'{val*1000:.0f} µs' if val < 0.1 else f'{val:.1f} ms',
                    ha='center', va='bottom', fontsize=9, fontweight='bold')

    # Arrows showing reduction
    for i in (3, 4):
        prev = values[i-1]
        cur = values[i]
        ax.annotate('', xy=(i, cur + 0.08), xytext=(i-1, prev + 0.08),
                    arrowprops=dict(arrowstyle='->', color='#333', lw=1.5))
        reduction = prev / cur
        mid_x = (i - 1 + i) / 2
        mid_y = (prev + cur) / 2 + 0.3
        ax.text(mid_x, mid_y, f'{reduction:.0f}×', ha='center', fontsize=8,
                color='#333', fontstyle='italic')

    ax.set_xticks(range(len(labels)))
    ax.set_xticklabels(labels, fontsize=8)
    ax.set_ylabel('Uncertainty (ms)', fontsize=11)
    ax.set_title('Uncertainty Budget (ISO GUM): From RTP Timestamp to Fused D_clock',
                 fontsize=12, fontweight='bold')
    ax.set_ylim(0, 6.5)
    ax.grid(axis='y', alpha=0.3)

    # Annotations
    ax.axhline(y=0.5, color='#D32F2F', linestyle='--', alpha=0.5, linewidth=1)
    ax.text(5.4, 0.55, '±0.5 ms target', fontsize=8, color='#D32F2F', alpha=0.8)

    fig.tight_layout()
    outpath = OUTPUT_DIR / 'fig4_uncertainty_budget.png'
    fig.savefig(outpath, dpi=DPI, bbox_inches='tight')
    plt.close(fig)
    print(f"  → {outpath}")


# ══════════════════════════════════════════════════════════════════════════
# FIGURE 5 — dTEC/dt 24h with GNSS VTEC overlay
# ══════════════════════════════════════════════════════════════════════════

def _rolling_median(x, y, window_hrs=0.25, step_hrs=0.1):
    """Compute rolling median of y vs x (hours), returning smoothed x, y arrays."""
    x_out, y_out = [], []
    t = 0.0
    while t < 24.0:
        mask = (x >= t - window_hrs/2) & (x < t + window_hrs/2)
        if mask.sum() >= 3:
            x_out.append(t)
            y_out.append(np.median(y[mask]))
        t += step_hrs
    return np.array(x_out), np.array(y_out)


def _solar_zenith_angle(lat_deg, lon_deg, utc_hours, day_of_year):
    """Compute solar zenith angle (degrees) for a location over a 24h period.

    Parameters
    ----------
    lat_deg, lon_deg : float
        Observer latitude/longitude in degrees (west negative).
    utc_hours : array-like
        Fractional UTC hours (0–24).
    day_of_year : int
        Day of year (1–366).

    Returns
    -------
    sza : ndarray
        Solar zenith angle in degrees for each utc_hours entry.
    """
    import math
    lat_r = math.radians(lat_deg)
    # Solar declination (Spencer formula, approximate)
    gamma = 2.0 * math.pi * (day_of_year - 1) / 365.0
    decl = (0.006918 - 0.399912 * math.cos(gamma) + 0.070257 * math.sin(gamma)
            - 0.006758 * math.cos(2*gamma) + 0.000907 * math.sin(2*gamma)
            - 0.002697 * math.cos(3*gamma) + 0.00148 * math.sin(3*gamma))

    utc_h = np.asarray(utc_hours, dtype=np.float64)
    # Hour angle: 0 at solar noon (local noon = 12 - lon/15 UTC)
    hour_angle = np.radians(15.0 * (utc_h - 12.0) + lon_deg)  # negative lon → west

    cos_sza = (math.sin(lat_r) * math.sin(decl) +
               math.cos(lat_r) * math.cos(decl) * np.cos(hour_angle))
    cos_sza = np.clip(cos_sza, -1.0, 1.0)
    return np.degrees(np.arccos(cos_sza))


def generate_fig5():
    print("Generating Fig 5: CHU 14.67 dTEC/dt + solar zenith angle + GNSS VTEC...")

    date_label = f'{TARGET_DATE[:4]}-{TARGET_DATE[4:6]}-{TARGET_DATE[6:]}'
    dtec_fn = f'{PHASE2}/science/dtec_timeseries/AGGREGATED_dtec_timeseries_{TARGET_DATE}.h5'
    vtec_fn = f'{DATA_ROOT}/data/gnss_vtec/GNSS_gnss_vtec_{TARGET_DATE}.h5'

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 6.5), height_ratios=[2, 1],
                                    sharex=True)

    # ── Top panel: CHU 14.67 MHz dTEC/dt only ──
    with h5py.File(dtec_fn, 'r') as f:
        n = min(f['epoch'].shape[0], f['station'].shape[0],
                f['frequency_mhz'].shape[0], f['snr_db'].shape[0],
                f['dtec_rate_tecu_per_s'].shape[0])
        epochs = f['epoch'][:n]
        dtec_rate = f['dtec_rate_tecu_per_s'][:n]
        stations = np.array([s.decode() for s in f['station'][:n]])
        freqs = f['frequency_mhz'][:n]
        snr = f['snr_db'][:n]

    t0 = epochs.min()
    midnight = t0 - (t0 % 86400)
    hrs = (epochs - midnight) / 3600.0
    dtec_rate_mtecu_min = dtec_rate * 1000 * 60

    # Quality gate
    good = (snr > 12.0) & (np.abs(dtec_rate_mtecu_min) < 40) & np.isfinite(dtec_rate_mtecu_min)

    # Single trace: CHU 14.67 MHz
    mask = good & (stations == 'CHU') & (np.abs(freqs - 14.67) < 0.1)
    if mask.sum() >= 10:
        ax1.scatter(hrs[mask], dtec_rate_mtecu_min[mask], s=1.0, alpha=0.12,
                    color='#2196F3', rasterized=True, zorder=2)
        xm, ym = _rolling_median(hrs[mask], dtec_rate_mtecu_min[mask],
                                  window_hrs=0.25, step_hrs=0.1)
        if len(xm) > 2:
            ax1.plot(xm, ym, color='#1565C0', linewidth=2.0,
                     label='CHU 14.67 MHz dTEC/dt', alpha=0.95, zorder=4)

    ax1.set_ylabel('dTEC/dt (mTECU/min)', fontsize=11)
    # Auto-scale symmetric around 0 so no values clip
    dtec_max = np.percentile(np.abs(dtec_rate_mtecu_min[mask]), 99.5) if mask.sum() > 10 else 25
    dtec_lim = max(dtec_max * 1.15, 10)  # at least ±10, with 15% padding
    ax1.set_ylim(-dtec_lim, dtec_lim)
    ax1.axhline(0, color='#666', linewidth=0.5, alpha=0.5)
    ax1.grid(True, alpha=0.3)

    # Solar zenith angle overlay on twin axis
    # Midpoint of CHU→EM38ww path
    mid_lat = (45.2958 + 38.918461) / 2.0  # ~42.1°N
    mid_lon = (-75.7533 + -92.127974) / 2.0  # ~-83.9°W
    doy = datetime(int(TARGET_DATE[:4]), int(TARGET_DATE[4:6]),
                   int(TARGET_DATE[6:])).timetuple().tm_yday
    sza_hours = np.linspace(0, 24, 1441)
    sza = _solar_zenith_angle(mid_lat, mid_lon, sza_hours, doy)

    ax1r = ax1.twinx()
    ax1r.plot(sza_hours, sza, color='#FF6F00', linewidth=1.5, alpha=0.7,
              linestyle='--', label='Solar Zenith Angle', zorder=3)
    ax1r.axhline(90, color='#FF6F00', linewidth=0.5, alpha=0.3, linestyle=':')
    ax1r.set_ylabel('Solar Zenith Angle (°)', fontsize=10, color='#FF6F00')
    # SZA axis symmetric around 90° so SZA=90° aligns with dTEC=0
    sza_half = max(90.0 - sza.min(), sza.max() - 90.0, 40.0)
    ax1r.set_ylim(90.0 + sza_half, 90.0 - sza_half)  # inverted: noon at top
    ax1r.tick_params(axis='y', labelcolor='#FF6F00', labelsize=8)

    # Shade night regions (SZA > 90°)
    night = sza > 90
    for i in range(len(sza_hours) - 1):
        if night[i]:
            ax1.axvspan(sza_hours[i], sza_hours[i+1], color='#263238',
                        alpha=0.06, zorder=0)

    ax1.set_title(f'Carrier-Phase dTEC/dt: CHU 14.670 MHz ({date_label})',
                  fontsize=12, fontweight='bold')
    # Combined legend
    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax1r.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labels1 + labels2, fontsize=8, loc='upper left')

    # ── Bottom panel: GNSS overhead VTEC ──
    with h5py.File(vtec_fn, 'r') as f:
        vtec_ts = f['unix_timestamp'][:]
        vtec = f['vtec_tecu'][:]
        vtec_quality = np.array([q.decode() for q in f['quality_flag'][:]])

    vtec_hrs = (vtec_ts - midnight) / 3600.0
    vtec_usable = (vtec_quality == 'GOOD') | (vtec_quality == 'MARGINAL')

    ax2.plot(vtec_hrs[vtec_usable], vtec[vtec_usable], color='#7B1FA2',
             linewidth=0.8, alpha=0.7, label='GNSS VTEC (ZED-F9P)')
    ax2.set_ylabel('VTEC (TECU)', fontsize=11, color='#7B1FA2')
    ax2.set_xlabel(f'UTC Hour ({date_label})', fontsize=11)
    ax2.legend(fontsize=8, loc='upper left')
    ax2.grid(True, alpha=0.3)
    ax2.set_xlim(0, 24)
    ax2.set_xticks(range(0, 25, 3))

    # Shade night on VTEC panel too
    for i in range(len(sza_hours) - 1):
        if night[i]:
            ax2.axvspan(sza_hours[i], sza_hours[i+1], color='#263238',
                        alpha=0.06, zorder=0)

    fig.tight_layout()
    outpath = OUTPUT_DIR / 'fig5_dtec_24h.png'
    fig.savefig(outpath, dpi=DPI, bbox_inches='tight')
    plt.close(fig)
    print(f"  → {outpath}")


# ══════════════════════════════════════════════════════════════════════════
# FIGURE 7 — Mode probability stacked bars (24h, 4 panels)
# ══════════════════════════════════════════════════════════════════════════

def generate_fig7():
    print("Generating Fig 7: Mode probability stacked bars...")

    prop_fn = f'{PHASE2}/science/propagation_stats/REANALYSIS_propagation_stats_{TARGET_DATE}.h5'

    with h5py.File(prop_fn, 'r') as f:
        stations = np.array([s.decode() for s in f['station'][:]])
        freqs = f['frequency_mhz'][:]
        period_start = np.array([s.decode() for s in f['period_start'][:]])
        p_1e = f['mode_1e_probability'][:]
        p_1f = f['mode_1f_probability'][:]
        p_2f = f['mode_2f_probability'][:]
        p_3f = f['mode_3f_probability'][:]
        p_unk = f['mode_unknown_probability'][:]
        n_obs = f['n_observations'][:]
        quality = np.array([q.decode() for q in f['quality_flag'][:]])

    # Parse hours
    hours = []
    for ps in period_start:
        ps = ps.rstrip('Z')
        try:
            dt = datetime.fromisoformat(ps).replace(tzinfo=timezone.utc)
        except ValueError:
            dt = datetime.strptime(ps[:19], '%Y-%m-%dT%H:%M:%S').replace(tzinfo=timezone.utc)
        hours.append(dt.hour)
    hours = np.array(hours)

    # Four panels
    panels = [
        ('WWV', 10.0, 'WWV 10 MHz (1,119 km)'),
        ('WWVH', 10.0, 'WWVH 10 MHz (6,600 km)'),
        ('CHU', 7.85, 'CHU 7.850 MHz (1,522 km)'),
        ('CHU', 14.67, 'CHU 14.670 MHz (1,522 km)'),
    ]

    mode_colors = {
        '1E': '#42A5F5',    # light blue
        '1F': '#66BB6A',    # green
        '2F': '#FFA726',    # orange
        '3F': '#EF5350',    # red
        'UNK': '#BDBDBD',   # grey
    }

    fig, axes = plt.subplots(4, 1, figsize=(10, 10), sharex=True)

    for idx, (stn, freq, title) in enumerate(panels):
        ax = axes[idx]
        mask = (stations == stn) & (np.abs(freqs - freq) < 0.1)

        if mask.sum() == 0:
            ax.text(12, 0.5, 'No data', ha='center', va='center', fontsize=12,
                    color='#999')
            ax.set_title(title, fontsize=10, fontweight='bold')
            ax.set_ylim(0, 1)
            continue

        h = hours[mask]
        sort_idx = np.argsort(h)
        h = h[sort_idx]

        data = {
            '1E': p_1e[mask][sort_idx],
            '1F': p_1f[mask][sort_idx],
            '2F': p_2f[mask][sort_idx],
            '3F': p_3f[mask][sort_idx],
            'UNK': p_unk[mask][sort_idx],
        }

        bottom = np.zeros(len(h))
        for mode_name in ['1E', '1F', '2F', '3F', 'UNK']:
            vals = data[mode_name]
            ax.bar(h, vals, bottom=bottom, width=0.8,
                   color=mode_colors[mode_name], edgecolor='white',
                   linewidth=0.3, label=mode_name if idx == 0 else '')
            bottom += vals

        ax.set_title(title, fontsize=10, fontweight='bold')
        ax.set_ylim(0, 1.05)
        ax.set_ylabel('Probability')
        ax.grid(axis='y', alpha=0.2)

        # Add observation count as text
        obs = n_obs[mask][sort_idx]
        for i, (hour_val, n) in enumerate(zip(h, obs)):
            if n > 0:
                ax.text(hour_val, 1.01, str(n), ha='center', va='bottom',
                        fontsize=5, color='#666')

    axes[0].legend(fontsize=9, loc='upper left', ncol=5,
                   bbox_to_anchor=(0.0, 1.25))
    axes[-1].set_xlabel(f'UTC Hour ({TARGET_DATE[:4]}-{TARGET_DATE[4:6]}-{TARGET_DATE[6:]})', fontsize=11)
    axes[-1].set_xlim(-0.5, 23.5)
    axes[-1].set_xticks(range(0, 24, 2))

    fig.suptitle('Propagation Mode Probability by Hour', fontsize=13,
                 fontweight='bold', y=1.01)
    fig.tight_layout()
    outpath = OUTPUT_DIR / 'fig7_mode_probability.png'
    fig.savefig(outpath, dpi=DPI, bbox_inches='tight')
    plt.close(fig)
    print(f"  → {outpath}")


# ══════════════════════════════════════════════════════════════════════════
# FIGURE 1 — System block diagram + station map
# ══════════════════════════════════════════════════════════════════════════

def generate_fig1():
    print("Generating Fig 1: Station map + system block diagram...")

    fig = plt.figure(figsize=(12, 5))

    # ── Left panel: Great-circle paths on a simple map ──
    ax_map = fig.add_axes([0.02, 0.05, 0.45, 0.88])

    # Station coordinates
    stations = {
        'EM38ww\n(Receiver)': (38.9, -92.1),
        'WWV\n(Fort Collins)': (40.68, -105.04),
        'WWVH\n(Kauai)': (21.99, -159.76),
        'CHU\n(Ottawa)': (45.30, -75.75),
        'BPM\n(Pucheng)': (34.95, 109.54),
    }

    colors = {
        'WWV\n(Fort Collins)': '#4CAF50',
        'WWVH\n(Kauai)': '#FF9800',
        'CHU\n(Ottawa)': '#2196F3',
        'BPM\n(Pucheng)': '#9C27B0',
    }

    # Simple equirectangular projection
    ax_map.set_xlim(-180, 140)
    ax_map.set_ylim(10, 60)

    # Draw coastline approximation (simple rectangle outlines)
    # North America rough outline
    na_lons = [-130, -125, -120, -117, -115, -110, -105, -100, -95, -90, -85, -80, -75, -70, -65, -60]
    na_lats_s = [48, 42, 35, 33, 32, 30, 28, 26, 25, 25, 25, 28, 35, 43, 44, 47]
    na_lats_n = [60, 55, 50, 48, 48, 48, 48, 49, 49, 49, 49, 49, 49, 48, 50, 52]
    ax_map.fill_between(na_lons, na_lats_s, na_lats_n, color='#E8F5E9', alpha=0.5)

    # Plot receiver
    rx_lat, rx_lon = stations['EM38ww\n(Receiver)']
    ax_map.plot(rx_lon, rx_lat, '*', color='#D32F2F', markersize=15, zorder=10)
    ax_map.annotate('EM38ww\n(Receiver)', (rx_lon, rx_lat),
                    textcoords='offset points', xytext=(5, -15),
                    fontsize=7, fontweight='bold', color='#D32F2F')

    # Plot stations and great-circle paths
    for name, (lat, lon) in stations.items():
        if 'Receiver' in name:
            continue
        color = colors[name]
        ax_map.plot(lon, lat, 'o', color=color, markersize=8, zorder=10)
        ax_map.annotate(name, (lon, lat), textcoords='offset points',
                        xytext=(5, 5), fontsize=7, fontweight='bold', color=color)

        # Simple straight line (equirectangular approx of GC path)
        # Handle BPM wrap-around
        if lon > 0:  # BPM - go westward
            ax_map.plot([rx_lon, -180], [rx_lat, (rx_lat + lat)/2],
                        '--', color=color, alpha=0.4, linewidth=1.5)
            ax_map.plot([140, lon], [(rx_lat + lat)/2, lat],
                        '--', color=color, alpha=0.4, linewidth=1.5)
        else:
            ax_map.plot([rx_lon, lon], [rx_lat, lat],
                        '-', color=color, alpha=0.5, linewidth=1.5)

    ax_map.set_xlabel('Longitude', fontsize=9)
    ax_map.set_ylabel('Latitude', fontsize=9)
    ax_map.set_title('HF Time-Signal Paths to EM38ww', fontsize=10, fontweight='bold')
    ax_map.grid(True, alpha=0.2)

    # ── Right panel: Pipeline block diagram ──
    ax_block = fig.add_axes([0.52, 0.05, 0.46, 0.88])
    ax_block.set_xlim(0, 10)
    ax_block.set_ylim(0, 10)
    ax_block.axis('off')

    # Boxes for pipeline stages
    boxes = [
        (1.5, 9.0, 'Antenna + RX888\n(GPSDO clock)', '#FFCDD2'),
        (1.5, 7.8, 'ka9q-radio (radiod)\nRTP multicast IQ', '#FFCDD2'),
        (1.5, 6.6, 'Core Recorder\n(.bin.zst archive)', '#FFF9C4'),
        (1.5, 5.4, 'Metrology Engine\n(TickEdgeDetector)', '#C8E6C9'),
        (1.5, 4.2, 'L2 Calibration\n(iono correction)', '#C8E6C9'),
        (1.5, 3.0, 'Fusion\n(Kalman + WLS)', '#C8E6C9'),
        (1.5, 1.8, 'Chrony SHM\n(TSL1 / TSL2)', '#BBDEFB'),
        (6.5, 5.4, 'Physics Service\n(dTEC/dt)', '#E1BEE7'),
        (6.5, 4.2, 'GNSS VTEC\n(ZED-F9P)', '#E1BEE7'),
        (6.5, 3.0, 'Mode ID\n(PHaRLAP raytrace)', '#E1BEE7'),
    ]

    for x, y, text, color in boxes:
        rect = plt.Rectangle((x - 1.3, y - 0.45), 2.6, 0.8,
                              facecolor=color, edgecolor='#333',
                              linewidth=1, zorder=5)
        ax_block.add_patch(rect)
        ax_block.text(x, y, text, ha='center', va='center', fontsize=6.5,
                      fontweight='bold', zorder=10)

    # Arrows (main pipeline)
    arrow_props = dict(arrowstyle='->', color='#333', lw=1.5)
    for i in range(len(boxes) - 4):
        ax_block.annotate('', xy=(1.5, boxes[i+1][1] + 0.35),
                          xytext=(1.5, boxes[i][1] - 0.45),
                          arrowprops=arrow_props)

    # Arrow from Metrology to Physics
    ax_block.annotate('', xy=(5.2, 5.4), xytext=(2.8, 5.4),
                      arrowprops=dict(arrowstyle='->', color='#9C27B0', lw=1.2))
    # Arrow from GNSS to L2 Cal
    ax_block.annotate('', xy=(2.8, 4.35), xytext=(5.2, 4.2),
                      arrowprops=dict(arrowstyle='->', color='#9C27B0', lw=1.2,
                                      linestyle='dashed'))

    ax_block.set_title('Software Pipeline (8 Services)', fontsize=10,
                       fontweight='bold')

    # Legend
    legend_items = [
        ('#FFCDD2', 'RF / Hardware'),
        ('#C8E6C9', 'Timing Pipeline'),
        ('#E1BEE7', 'Physics Products'),
        ('#BBDEFB', 'System Clock'),
    ]
    for i, (color, label) in enumerate(legend_items):
        ax_block.add_patch(plt.Rectangle((6.5, 8.8 - i*0.5, ), 0.3, 0.3,
                                          facecolor=color, edgecolor='#333'))
        ax_block.text(7.0, 8.95 - i*0.5, label, fontsize=7, va='center')

    fig.suptitle('Figure 1: System Overview — Hardware, Paths, and Software Pipeline',
                 fontsize=11, fontweight='bold', y=0.98)

    outpath = OUTPUT_DIR / 'fig1_system_overview.png'
    fig.savefig(outpath, dpi=DPI, bbox_inches='tight')
    plt.close(fig)
    print(f"  → {outpath}")


# ══════════════════════════════════════════════════════════════════════════
# FIGURE 2 — 10 MHz spectrogram showing WWV+WWVH ticks
# ══════════════════════════════════════════════════════════════════════════

def generate_fig2():
    """Generate spectrogram from raw IQ showing tick structure on SHARED_10000.

    Shows ~5 seconds of the 10 MHz shared channel with:
      - Top: time-frequency spectrogram (±3 kHz around carrier)
      - Bottom: 800–1400 Hz bandpass AM envelope (tick pulse energy)
    Both panels share a common time axis starting at 0, with vertical
    highlight bars at each UTC second boundary showing where ticks occur.
    """
    import zstandard
    import json as _json
    from scipy import signal as sp_signal

    print("Generating Fig 2: 10 MHz spectrogram from raw IQ...")

    # Find a recent raw IQ file for SHARED_10000
    raw_dir = f'{DATA_ROOT}/raw_buffer/SHARED_10000'
    candidates = []
    for daydir in sorted(os.listdir(raw_dir), reverse=True):
        daypath = os.path.join(raw_dir, daydir)
        if not os.path.isdir(daypath):
            continue
        jsons = sorted([f for f in os.listdir(daypath) if f.endswith('.json')])
        for jf in jsons[-10:]:  # check last 10 files of most recent day
            jpath = os.path.join(daypath, jf)
            bpath = jpath.replace('.json', '.bin.zst')
            if os.path.exists(bpath):
                candidates.append((jpath, bpath))
        if candidates:
            break

    if not candidates:
        print("  ✗ No raw IQ data found for SHARED_10000")
        return

    # Pick a file from the middle of the available set (avoid edge effects)
    jpath, bpath = candidates[len(candidates)//2]

    with open(jpath) as f:
        meta = _json.load(f)

    sr = meta['sample_rate']
    dctx = zstandard.ZstdDecompressor()
    with open(bpath, 'rb') as f:
        raw = dctx.decompress(f.read())
    iq = np.frombuffer(raw, dtype=np.complex64)

    # Show 5 seconds — enough for 5 tick pulses, not so many that detail is lost
    show_seconds = 5
    show_samples = show_seconds * sr
    # Start at a second boundary (second 10) to align ticks cleanly
    start_sample = 10 * sr
    if start_sample + show_samples > len(iq):
        start_sample = 0
    segment = iq[start_sample:start_sample + show_samples]

    # Use GridSpec so both panels share the same x-extent.
    # A narrow column on the right holds the colorbar, keeping plot widths equal.
    from matplotlib.gridspec import GridSpec
    fig = plt.figure(figsize=(10, 5.5))
    gs = GridSpec(2, 2, figure=fig, height_ratios=[3, 1],
                  width_ratios=[1, 0.03], hspace=0.08, wspace=0.04)
    ax_spec = fig.add_subplot(gs[0, 0])
    ax_cb   = fig.add_subplot(gs[0, 1])   # colorbar axis
    ax_env  = fig.add_subplot(gs[1, 0], sharex=ax_spec)
    gs[1, 1].set_visible = False           # empty cell

    # ── Top: spectrogram ──
    nfft = 512
    noverlap = nfft - nfft // 8
    freqs, times, Sxx = sp_signal.spectrogram(
        segment, fs=sr, nperseg=nfft, noverlap=noverlap,
        return_onesided=False, mode='psd'
    )
    # Shift to center DC, convert to audio frequency offset
    freqs_shifted = np.fft.fftshift(freqs)
    freqs_shifted = np.where(freqs_shifted > sr/2, freqs_shifted - sr, freqs_shifted)
    Sxx_shifted = np.fft.fftshift(Sxx, axes=0)
    sort_idx = np.argsort(freqs_shifted)
    freqs_sorted = freqs_shifted[sort_idx]
    Sxx_sorted = Sxx_shifted[sort_idx, :]

    # Show only ±3000 Hz around carrier (tick tones are at 1000/1200 Hz)
    freq_mask = np.abs(freqs_sorted) <= 3000
    Sxx_db = 10 * np.log10(Sxx_sorted[freq_mask, :] + 1e-20)

    # Relative time axis: 0 to show_seconds
    extent = [0, show_seconds,
              freqs_sorted[freq_mask][0], freqs_sorted[freq_mask][-1]]

    vmin = np.percentile(Sxx_db, 5)
    vmax = np.percentile(Sxx_db, 99)

    im = ax_spec.imshow(Sxx_db, aspect='auto', origin='lower', extent=extent,
                        cmap='viridis', vmin=vmin, vmax=vmax, interpolation='bilinear')
    ax_spec.set_ylabel('Offset from 10 MHz (Hz)', fontsize=10)
    ax_spec.set_title('SHARED_10000: WWV + WWVH Tick Structure (5-Second Window)',
                      fontsize=12, fontweight='bold')

    # Mark tick tone frequencies with labels on right side for clarity
    ax_spec.axhline(1000, color='#4CAF50', linewidth=0.8, linestyle='--', alpha=0.7)
    ax_spec.axhline(1200, color='#FF9800', linewidth=0.8, linestyle='--', alpha=0.7)
    ax_spec.axhline(-1000, color='#4CAF50', linewidth=0.8, linestyle='--', alpha=0.4)
    ax_spec.axhline(-1200, color='#FF9800', linewidth=0.8, linestyle='--', alpha=0.4)
    # Labels at right edge
    ax_spec.text(show_seconds - 0.05, 1060, 'WWV 1000 Hz', fontsize=7,
                 color='#4CAF50', ha='right', fontweight='bold')
    ax_spec.text(show_seconds - 0.05, 1260, 'WWVH 1200 Hz', fontsize=7,
                 color='#FF9800', ha='right', fontweight='bold')

    cb = fig.colorbar(im, cax=ax_cb)
    cb.set_label('PSD (dB)', fontsize=9)

    # ── Bottom: per-tone envelope showing WWV and WWVH ticks separately ──
    # Narrow bandpass around each tick frequency, then Hilbert envelope.
    # WWV tick: 5 cycles of 1000 Hz (5 ms) at the UTC second boundary.
    # WWVH tick: 6 cycles of 1200 Hz (5 ms), arriving ~25 ms later.
    # 100 Hz bandwidth cleanly separates the two (200 Hz apart).
    t_axis = np.arange(len(segment)) / sr
    smooth_n = max(int(0.002 * sr), 1)  # 2 ms smoothing

    for lo, hi, color, label in [(950, 1050, '#4CAF50', 'WWV 1000 Hz'),
                                  (1150, 1250, '#FF9800', 'WWVH 1200 Hz')]:
        sos = sp_signal.butter(4, [lo, hi], btype='bandpass', fs=sr, output='sos')
        filtered = sp_signal.sosfilt(sos, segment)
        envelope = np.abs(sp_signal.hilbert(np.real(filtered)))
        envelope = np.convolve(envelope, np.ones(smooth_n)/smooth_n, mode='same')
        env_db = 20 * np.log10(envelope + 1e-20)
        ax_env.plot(t_axis, env_db, color=color, linewidth=0.7,
                    label=label, alpha=0.85)

    ax_env.set_ylabel('Tone Power (dB)', fontsize=10)
    ax_env.set_xlabel('Time (seconds)', fontsize=10)
    ax_env.set_xlim(0, show_seconds)
    ax_env.legend(fontsize=8, loc='lower right', ncol=2)

    # ── Second-boundary markers on both panels ──
    for sec in range(show_seconds + 1):
        ax_spec.axvline(sec, color='white', linewidth=0.5, alpha=0.4, linestyle='-')
        ax_env.axvline(sec, color='#D32F2F', linewidth=0.6, alpha=0.4, linestyle=':')

    ax_env.grid(True, alpha=0.2)

    # Hide the empty bottom-right cell (colorbar column, envelope row)
    ax_empty = fig.add_subplot(gs[1, 1])
    ax_empty.axis('off')

    outpath = OUTPUT_DIR / 'fig2_spectrogram_10mhz.png'
    fig.savefig(outpath, dpi=DPI, bbox_inches='tight')
    plt.close(fig)
    print(f"  → {outpath}")


# ══════════════════════════════════════════════════════════════════════════
# FIGURE 6 — Synthetic ray fan diagram (illustrative, based on IRI params)
# ══════════════════════════════════════════════════════════════════════════

def _try_pylap_raytrace():
    """Attempt to import pylap and trace a ray fan for WWV 10 MHz.

    Returns (paths, rays, foF2, hmF2, target_range_km) or None if pylap unavailable.
    """
    # Auto-detect PHaRLAP reference data if not already set
    if 'DIR_MODELS_REF_DAT' not in os.environ:
        for candidate in ['/opt/pharlap_4.7.4/dat', '/opt/pharlap/dat']:
            if os.path.isdir(candidate):
                os.environ['DIR_MODELS_REF_DAT'] = candidate
                break
    try:
        import pylap.raytrace_2d as rt_mod
        import pylap.iri2016 as iri_mod
    except (ImportError, OSError):
        return None

    # WWV → EM38ww geometry (exact coordinates from timestd-config.toml)
    tx_lat, tx_lon = 40.6773, -105.0421
    rx_lat, rx_lon = 38.918461, -92.127974

    # Great-circle bearing and distance
    import math
    dlon_r = math.radians(rx_lon - tx_lon)
    lat1_r, lat2_r = math.radians(tx_lat), math.radians(rx_lat)
    bearing = math.degrees(math.atan2(
        math.sin(dlon_r) * math.cos(lat2_r),
        math.cos(lat1_r) * math.sin(lat2_r) -
        math.sin(lat1_r) * math.cos(lat2_r) * math.cos(dlon_r)
    )) % 360.0

    dlat = math.radians(rx_lat - tx_lat)
    dlon = math.radians(rx_lon - tx_lon)
    a_gc = (math.sin(dlat/2)**2 +
            math.cos(math.radians(tx_lat)) *
            math.cos(math.radians(rx_lat)) *
            math.sin(dlon/2)**2)
    target_km = 6371.0 * 2 * math.asin(math.sqrt(a_gc))

    # Build spatially varying IRI grid — target date 18:00 UTC (local noon)
    ut = [int(TARGET_DATE[:4]), int(TARGET_DATE[4:6]), int(TARGET_DATE[6:]), 18, 0]

    # Great-circle bearing for sampling IRI along the path
    dlon_b = math.radians(rx_lon - tx_lon)
    lat1_b, lat2_b = math.radians(tx_lat), math.radians(rx_lat)
    brg = math.degrees(math.atan2(
        math.sin(dlon_b) * math.cos(lat2_b),
        math.cos(lat1_b) * math.sin(lat2_b) -
        math.sin(lat1_b) * math.cos(lat2_b) * math.cos(dlon_b)
    )) % 360.0

    def _gc_pt(lat1, lon1, bearing, dist_km):
        R = 6371.0; d = dist_km / R
        la1 = math.radians(lat1); lo1 = math.radians(lon1); br = math.radians(bearing)
        la2 = math.asin(math.sin(la1)*math.cos(d) + math.cos(la1)*math.sin(d)*math.cos(br))
        lo2 = lo1 + math.atan2(math.sin(br)*math.sin(d)*math.cos(la1),
                                math.cos(d) - math.sin(la1)*math.sin(la2))
        return math.degrees(la2), math.degrees(lo2)

    n_heights = 200
    range_inc_km = 50.0
    grid_max_km = max(10000.0, target_km * 2)
    n_ranges = int(grid_max_km / range_inc_km) + 1

    # Sample IRI at multiple points along the path (auto: 1 per 500 km, min 5)
    n_iri = max(5, min(25, int(target_km / 500.0) + 1))
    sample_dists = np.linspace(0.0, grid_max_km, n_iri)
    sample_profiles = []
    sample_foF2 = []
    sample_hmF2 = []
    for sd in sample_dists:
        slat, slon = _gc_pt(tx_lat, tx_lon, brg, sd)
        outf, oarr = iri_mod.iri2016(slat, slon, 100.0, ut, 60.0, 3.0, n_heights, {})
        ne = np.maximum(outf[0, :], 0.0) / 1e6
        sample_profiles.append(ne)
        nmF2 = max(float(oarr[0]), 0.0)
        sample_foF2.append(8.98 * math.sqrt(nmF2) / 1e6)
        sample_hmF2.append(float(oarr[1]))

    # Interpolate onto every range column
    range_km = np.arange(n_ranges) * range_inc_km
    profiles_arr = np.column_stack(sample_profiles)
    sample_km = np.array(sample_dists)
    iono_en = np.zeros((n_heights, n_ranges), dtype=np.float64)
    for h in range(n_heights):
        iono_en[h, :] = np.interp(range_km, sample_km, profiles_arr[h, :])

    mid_idx = n_iri // 2
    foF2 = sample_foF2[mid_idx]
    hmF2 = sample_hmF2[mid_idx]
    print(f"    IRI grid: {n_iri} samples, foF2 {min(sample_foF2):.2f}–{max(sample_foF2):.2f} MHz, "
          f"hmF2 {min(sample_hmF2):.0f}–{max(sample_hmF2):.0f} km")

    zeros = np.zeros_like(iono_en)
    irreg = np.zeros((4, n_ranges), dtype=np.float64)

    # Trace fan of rays: 2–60° in 0.5° steps
    elevs = np.arange(2.0, 60.5, 0.5)
    freqs = np.full(len(elevs), 10.0)
    tol = [1e-7, 0.01, 10.0]

    rays, paths, states = rt_mod.raytrace_2d(
        tx_lat, tx_lon, elevs, bearing, freqs, 3,
        tol, 0, iono_en, zeros, zeros, 60.0, 3.0, 50.0, irreg
    )
    ht_start, ht_inc = 60.0, 3.0
    height_km = ht_start + np.arange(n_heights) * ht_inc

    return dict(
        paths=paths, rays=rays, foF2=foF2, hmF2=hmF2,
        target_km=target_km, elevs=elevs,
        iono_en=iono_en, height_km=height_km, range_km=range_km,
        sample_foF2=sample_foF2, sample_hmF2=sample_hmF2,
        sample_dists=sample_dists,
    )


def generate_fig6():
    """
    Generate ray fan plot for WWV 10 MHz using PHaRLAP numerical ray tracing
    (if available) or a simplified parabolic approximation as fallback.

    Two-panel layout:
      Left:  Ray paths overlaid on IRI-2020 electron density pcolormesh
      Right: Midpoint Ne(h) profile with E/F layer annotation
    Closing modes annotated with elevation range, group delay, virtual height,
    and apogee from the full PHaRLAP per-hop output.
    """
    result = _try_pylap_raytrace()
    if result is None:
        print("  ✗ pylap not available — skipping Fig 6 (needs PHaRLAP)")
        return

    paths = result['paths']
    rays = result['rays']
    foF2 = result['foF2']
    hmF2 = result['hmF2']
    target_km = result['target_km']
    elevs = result['elevs']
    iono_en = result['iono_en']
    height_km = result['height_km']
    range_km = result['range_km']
    sample_foF2 = result['sample_foF2']
    sample_hmF2 = result['sample_hmF2']

    print(f"Generating Fig 6: PHaRLAP ray fan (foF2={foF2:.2f}, hmF2={hmF2:.1f})...")

    from matplotlib.lines import Line2D
    from matplotlib.colors import LogNorm
    import matplotlib.gridspec as gridspec

    fig = plt.figure(figsize=(13, 6.5))
    gs = gridspec.GridSpec(1, 2, width_ratios=[5, 1.2], wspace=0.06, figure=fig)
    ax = fig.add_subplot(gs[0])
    ax_ne = fig.add_subplot(gs[1], sharey=ax)

    # ── Display limits ──
    x_max = max(target_km * 1.8, 1800.0)
    y_min, y_max = 50.0, 500.0

    # ── Electron density background (pcolormesh) ──
    # Clip grid to display range
    r_mask = range_km <= x_max
    h_mask = (height_km >= y_min) & (height_km <= y_max)
    ne_display = iono_en[np.ix_(h_mask, r_mask)]
    ne_display = np.where(ne_display > 0, ne_display, np.nan)

    ne_vmin = max(np.nanmin(ne_display[ne_display > 0]) if np.any(ne_display > 0) else 1e2, 1e2)
    ne_vmax = np.nanmax(ne_display) if np.any(~np.isnan(ne_display)) else 1e6
    pcm = ax.pcolormesh(
        range_km[r_mask], height_km[h_mask], ne_display,
        norm=LogNorm(vmin=ne_vmin, vmax=ne_vmax),
        cmap='YlGnBu', shading='auto', alpha=0.7, zorder=1, rasterized=True,
    )

    # Thin colorbar at top-right inside the main panel
    cax = ax.inset_axes([0.72, 0.88, 0.25, 0.03])
    cb = fig.colorbar(pcm, cax=cax, orientation='horizontal')
    cb.set_label('Ne (cm⁻³)', fontsize=7, labelpad=2)
    cb.ax.tick_params(labelsize=6)

    # ── hmF2 dashed line ──
    ax.axhline(hmF2, color='#2E7D32', linewidth=0.8, linestyle='--', alpha=0.6, zorder=2)

    # ── Classify rays: collect rich per-mode info from PHaRLAP output ──
    tolerance_km = 200.0
    mode_colors = {1: '#1B5E20', 2: '#E65100', 3: '#B71C1C'}
    # Each entry: (elev, nhops, ground_range, group_delay_ms,
    #              virtual_height, apogee, phase_path_km, absorption_dB,
    #              doppler_hz, tec_path, ray_idx)
    closing_info = []

    for i, (path, ray) in enumerate(zip(paths, rays)):
        gnd = np.asarray(path.get('ground_range', []))
        hgt = np.asarray(path.get('height', []))
        if gnd.size < 2:
            continue

        labels = np.asarray(ray.get('ray_label', []))
        gnd_cumul = np.asarray(ray.get('ground_range', []))
        grp_cumul = np.asarray(ray.get('group_range', []))
        phase_cumul = np.asarray(ray.get('phase_path', []))
        apogee_arr = np.asarray(ray.get('apogee', []))
        virt_h_arr = np.asarray(ray.get('virtual_height', []))
        absorp_arr = np.asarray(ray.get('total_absorption', []))
        doppler_arr = np.asarray(ray.get('Doppler_shift', []))
        closes = False
        close_nhops = 0

        for k in range(len(labels)):
            if int(labels[k]) != 1:
                continue
            if abs(float(gnd_cumul[k]) - target_km) < tolerance_km:
                closes = True
                close_nhops = k + 1
                grp_km = float(grp_cumul[k]) if k < len(grp_cumul) else 0
                delay_ms = grp_km / 299792.458 * 1000.0
                ph_km = float(phase_cumul[k]) if k < len(phase_cumul) else 0
                apg = float(apogee_arr[k]) if k < len(apogee_arr) else 0
                vh = float(virt_h_arr[k]) if k < len(virt_h_arr) else 0
                ab = float(absorp_arr[k]) if k < len(absorp_arr) else 0
                dop = float(doppler_arr[k]) if k < len(doppler_arr) else 0
                closing_info.append((
                    float(elevs[i]), close_nhops, float(gnd_cumul[k]),
                    delay_ms, vh, apg, ph_km, ab, dop, i,
                ))
                break

        if closes:
            color = mode_colors.get(close_nhops, '#555')
            ax.plot(gnd, hgt, color=color, linewidth=1.6, alpha=0.9, zorder=5)
        else:
            ax.plot(gnd, hgt, color='#9E9E9E', linewidth=0.25, alpha=0.12, zorder=3)

    # ── Highlight one representative closing ray per mode with thicker line ──
    for nhops in sorted(mode_colors.keys()):
        matches = [c for c in closing_info if c[1] == nhops]
        if not matches:
            continue
        matches.sort(key=lambda x: x[0])
        rep = matches[len(matches) // 2]
        rep_idx = rep[9]
        rep_path = paths[rep_idx]
        gnd = np.asarray(rep_path.get('ground_range', []))
        hgt = np.asarray(rep_path.get('height', []))
        ax.plot(gnd, hgt, color=mode_colors[nhops], linewidth=2.8, alpha=1.0,
                zorder=6, solid_capstyle='round')

    # ── Mark transmitter and receiver ──
    ax.plot(0, y_min, 'v', color='#D32F2F', markersize=14, zorder=20,
            markeredgecolor='white', markeredgewidth=0.8)
    ax.annotate('WWV\nFort Collins', xy=(0, y_min), xytext=(80, y_min + 55),
                fontsize=10, fontweight='bold', color='#D32F2F',
                arrowprops=dict(arrowstyle='->', color='#D32F2F', lw=1.2),
                bbox=dict(boxstyle='round,pad=0.3', facecolor='white',
                          edgecolor='#D32F2F', alpha=0.9),
                zorder=25)
    ax.plot(target_km, y_min, '*', color='#1565C0', markersize=16, zorder=20,
            markeredgecolor='white', markeredgewidth=0.8)
    ax.annotate('EM38ww\nReceiver', xy=(target_km, y_min),
                xytext=(target_km - 250, y_min + 55),
                fontsize=10, fontweight='bold', color='#1565C0',
                arrowprops=dict(arrowstyle='->', color='#1565C0', lw=1.2),
                bbox=dict(boxstyle='round,pad=0.3', facecolor='white',
                          edgecolor='#1565C0', alpha=0.9),
                zorder=25)

    # ── Per-mode summary table (top-left inset) ──
    table_lines = []
    for nhops in sorted(mode_colors.keys()):
        matches = [c for c in closing_info if c[1] == nhops]
        if not matches:
            continue
        el_lo = min(m[0] for m in matches)
        el_hi = max(m[0] for m in matches)
        dl_lo = min(m[3] for m in matches)
        dl_hi = max(m[3] for m in matches)
        apg_lo = min(m[5] for m in matches)
        apg_hi = max(m[5] for m in matches)
        vh_med = sorted(m[4] for m in matches)[len(matches) // 2]
        n_rays = len(matches)
        table_lines.append(
            f'{nhops}F2  elev {el_lo:4.1f}–{el_hi:4.1f}°  '
            f'τ_g {dl_lo:5.2f}–{dl_hi:5.2f} ms  '
            f'apogee {apg_lo:.0f}–{apg_hi:.0f} km  '
            f'h′ {vh_med:.0f} km  '
            f'({n_rays} rays)'
        )
    if table_lines:
        table_text = '\n'.join(table_lines)
        ax.text(0.02, 0.97, table_text, transform=ax.transAxes,
                fontsize=7, fontfamily='monospace', verticalalignment='top',
                bbox=dict(boxstyle='round,pad=0.4', facecolor='white',
                          edgecolor='#BDBDBD', alpha=0.92),
                zorder=25)

    # ── Legend ──
    legend_elements = []
    for nhops, lbl in [(1, '1F2'), (2, '2F2'), (3, '3F2')]:
        if any(c[1] == nhops for c in closing_info):
            legend_elements.append(
                Line2D([0], [0], color=mode_colors[nhops], linewidth=2.5, label=f'{lbl} (closing)'))
    legend_elements.append(
        Line2D([0], [0], color='#9E9E9E', linewidth=0.8, alpha=0.4, label='Non-closing'))
    ax.legend(handles=legend_elements, fontsize=7.5, loc='upper right',
              framealpha=0.9, edgecolor='#CCC')

    ax.set_xlabel('Ground Range (km)', fontsize=10)
    ax.set_ylabel('Height (km)', fontsize=10)
    date_label = f'{TARGET_DATE[:4]}-{TARGET_DATE[4:6]}-{TARGET_DATE[6:]}'
    ax.set_title(
        f'PHaRLAP 4.7.4 Numerical Ray Trace — WWV 10 MHz → EM38ww '
        f'({target_km:.0f} km)\n'
        f'IRI-2020 Ne(h) grid: foF2 = {foF2:.2f} MHz, hmF2 = {hmF2:.0f} km — '
        f'{date_label} 18:00 UTC',
        fontsize=10, fontweight='bold')
    ax.set_xlim(-50, x_max)
    ax.set_ylim(y_min, y_max)
    ax.tick_params(labelsize=9)

    # ══════════════════════════════════════════════════════════════════════
    # Right panel: midpoint Ne(h) profile
    # ══════════════════════════════════════════════════════════════════════
    mid_col = iono_en.shape[1] // 2
    ne_mid = iono_en[:, mid_col]
    ne_mid_plot = np.where(ne_mid > 0, ne_mid, np.nan)
    ax_ne.plot(ne_mid_plot, height_km, color='#0D47A1', linewidth=1.2)
    ax_ne.fill_betweenx(height_km, 0, ne_mid_plot, alpha=0.15, color='#42A5F5')
    ax_ne.set_xscale('log')
    ax_ne.set_xlabel('Ne (cm⁻³)', fontsize=9)
    ax_ne.set_title('Midpoint\nNe(h)', fontsize=9, fontweight='bold')
    ax_ne.tick_params(labelsize=7, labelleft=False)
    ax_ne.set_xlim(left=1e2)
    ax_ne.grid(True, alpha=0.15, which='both')

    # Mark foF2/hmF2 on Ne profile
    ax_ne.axhline(hmF2, color='#2E7D32', linewidth=0.8, linestyle='--', alpha=0.6)
    ax_ne.annotate(f'hmF2\n{hmF2:.0f} km', xy=(ax_ne.get_xlim()[1] * 0.3, hmF2),
                   fontsize=6.5, color='#2E7D32', fontweight='bold',
                   verticalalignment='bottom')

    # Find and mark E-layer peak (90–150 km)
    e_mask = (height_km >= 90) & (height_km <= 150)
    if np.any(e_mask) and np.any(ne_mid[e_mask] > 0):
        e_idx = np.argmax(ne_mid[e_mask])
        e_height = height_km[e_mask][e_idx]
        ax_ne.annotate(f'E\n{e_height:.0f} km', xy=(ne_mid[e_mask][e_idx], e_height),
                       fontsize=6.5, color='#1565C0', fontweight='bold',
                       verticalalignment='bottom',
                       xytext=(ne_mid[e_mask][e_idx] * 3, e_height + 10),
                       arrowprops=dict(arrowstyle='->', color='#1565C0', lw=0.6))

    # ── Save ──
    fig.subplots_adjust(left=0.06, right=0.97, top=0.88, bottom=0.10)
    outpath = OUTPUT_DIR / 'fig6_ray_fan_pharlap.png'
    fig.savefig(outpath, dpi=DPI, bbox_inches='tight')
    plt.close(fig)

    n_closing = len(closing_info)
    modes_found = sorted(set(c[1] for c in closing_info))
    modes_str = ', '.join(f'{n}F2' for n in modes_found)
    print(f"  → {outpath}")
    print(f"  {len(elevs)} rays traced, {n_closing} closing — modes: {modes_str}")
    for nhops in sorted(mode_colors.keys()):
        matches = [c for c in closing_info if c[1] == nhops]
        if matches:
            elevs_m = [m[0] for m in matches]
            delays = [m[3] for m in matches]
            apogees = [m[5] for m in matches]
            print(f"    {nhops}F2: elev {min(elevs_m):.1f}–{max(elevs_m):.1f}°, "
                  f"delay {min(delays):.2f}–{max(delays):.2f} ms, "
                  f"apogee {min(apogees):.0f}–{max(apogees):.0f} km")


# ══════════════════════════════════════════════════════════════════════════
# FIGURE 8 — Measured ToA vs propagation mode (24h, multi-channel)
# ══════════════════════════════════════════════════════════════════════════

def generate_fig8():
    """Show real correlation between measured arrival time and mode assignment.

    For each of 4 representative channels, scatter-plot the raw arrival time
    (propagation delay) over 24 hours, colored by the assigned propagation mode.
    This demonstrates that mode discrimination is driven by measured timing.
    """
    print("Generating Fig 8: Measured ToA vs mode probability...")

    date_label = f'{TARGET_DATE[:4]}-{TARGET_DATE[4:6]}-{TARGET_DATE[6:]}'

    # Receiver coordinates
    rx_lat, rx_lon = 38.918461, -92.127974
    # TX coordinates and path midpoints for SZA
    tx_coords = {
        'WWV':  (40.6773, -105.0421),
        'WWVH': (21.9886, -159.7601),
        'CHU':  (45.2958, -75.7533),
    }
    doy = datetime(int(TARGET_DATE[:4]), int(TARGET_DATE[4:6]),
                   int(TARGET_DATE[6:])).timetuple().tm_yday
    sza_hours = np.linspace(0, 24, 1441)

    panels = [
        ('SHARED_10000', 'WWV', 10.0, 'WWV 10 MHz (1,119 km)'),
        ('SHARED_10000', 'WWVH', 10.0, 'WWVH 10 MHz (6,600 km)'),
        ('CHU_7850', 'CHU', 7.85, 'CHU 7.850 MHz (1,522 km)'),
        ('CHU_14670', 'CHU', 14.67, 'CHU 14.670 MHz (1,522 km)'),
    ]

    mode_colors = {
        '1E': '#42A5F5',   # light blue
        '1F': '#66BB6A',   # green
        '1F2': '#66BB6A',
        '2F': '#FFA726',   # orange
        '2F2': '#FFA726',
        '3F': '#EF5350',   # red
        '3F2': '#EF5350',
        'UNKNOWN': '#BDBDBD',
    }

    fig, axes = plt.subplots(4, 1, figsize=(10, 10), sharex=True)

    for idx, (channel, target_station, target_freq, title) in enumerate(panels):
        ax = axes[idx]

        fn = f'{PHASE2}/{channel}/clock_offset/{channel}_timing_measurements_{TARGET_DATE}.h5'
        if not os.path.exists(fn):
            ax.text(12, 0.5, 'No data', ha='center', va='center', fontsize=12,
                    color='#999', transform=ax.get_xaxis_transform())
            ax.set_title(title, fontsize=10, fontweight='bold')
            continue

        with h5py.File(fn, 'r') as f:
            ts_utc = parse_ts_bytes(f['timestamp_utc'][:])
            stations = np.array([s.decode() for s in f['station'][:]])
            prop_delay = f['propagation_delay_ms'][:]
            raw_toa = f['raw_arrival_time_ms'][:]
            modes = np.array([m.decode().strip() for m in f['propagation_mode'][:]])
            snr = f['snr_db'][:]
            freqs = f['frequency_mhz'][:]
            confidence = f['confidence'][:]

        hrs = hours_from_midnight(ts_utc)

        # Filter: correct station, reasonable SNR, tone detected
        mask = ((stations == target_station) &
                (np.abs(freqs - target_freq) < 0.1) &
                (snr > 8.0) &
                np.isfinite(prop_delay))

        if mask.sum() < 10:
            ax.text(12, 0.5, f'Insufficient data ({mask.sum()} pts)',
                    ha='center', va='center', fontsize=10, color='#999',
                    transform=ax.get_xaxis_transform())
            ax.set_title(title, fontsize=10, fontweight='bold')
            continue

        h = np.array(hrs)[mask]
        delay = prop_delay[mask]
        m = modes[mask]

        # Scatter by mode
        plotted_labels = set()
        for mode_name in ['1E', '1F', '2F', '3F', 'UNKNOWN']:
            # Match mode prefixes (e.g. '1F2' matches '1F')
            if mode_name == 'UNKNOWN':
                mode_mask = np.array([
                    mm.upper() in ('UNKNOWN', '', 'FALLBACK', 'TICK', 'FSK',
                                   'CHU_FSK', 'TEC_VALIDATED', 'TEC_CORRECTED',
                                   'TEC_UNREALISTIC', 'TEC_POOR_FIT')
                    or mm.upper().endswith('+GNSS_TEC')
                    or mm.upper().endswith('+GNSS_VALIDATED')
                    for mm in m
                ])
            else:
                mode_mask = np.array([mm.upper().startswith(mode_name) for mm in m])

            if mode_mask.sum() == 0:
                continue

            color = mode_colors.get(mode_name, '#888')
            label = mode_name if mode_name not in plotted_labels else None
            ax.scatter(h[mode_mask], delay[mode_mask], s=3, alpha=0.3,
                       color=color, label=label, rasterized=True, zorder=3)
            plotted_labels.add(mode_name)

        ax.set_title(title, fontsize=10, fontweight='bold')
        ax.set_ylabel('Prop. Delay (ms)', fontsize=9)
        ax.grid(True, alpha=0.2)

        # Solar zenith angle overlay on twin axis
        tx = tx_coords[target_station]
        mid_lat = (tx[0] + rx_lat) / 2.0
        mid_lon = (tx[1] + rx_lon) / 2.0
        sza = _solar_zenith_angle(mid_lat, mid_lon, sza_hours, doy)

        axr = ax.twinx()
        axr.plot(sza_hours, sza, color='#FF6F00', linewidth=1.2, alpha=0.5,
                 linestyle='--', zorder=1)
        axr.axhline(90, color='#FF6F00', linewidth=0.5, alpha=0.3, linestyle=':')
        axr.set_ylim(140, 20)  # inverted: noon (low SZA) at top
        if idx == 0:
            axr.set_ylabel('SZA (°)', fontsize=8, color='#FF6F00')
        axr.tick_params(axis='y', labelcolor='#FF6F00', labelsize=7)

        # Shade night regions (SZA > 90°)
        night = sza > 90
        for j in range(len(sza_hours) - 1):
            if night[j]:
                ax.axvspan(sza_hours[j], sza_hours[j+1], color='#263238',
                           alpha=0.06, zorder=0)

        # Legend combines scatter + SZA
        from matplotlib.lines import Line2D
        sza_handle = Line2D([0], [0], color='#FF6F00', linewidth=1.2,
                            linestyle='--', alpha=0.5, label='SZA')
        handles, labels = ax.get_legend_handles_labels()
        ax.legend(handles=handles + [sza_handle],
                  labels=labels + ['SZA'],
                  fontsize=7, loc='upper right', ncol=6, markerscale=3)

        # Annotate tick count
        ax.text(0.01, 0.95, f'n={mask.sum():,}', fontsize=7,
                transform=ax.transAxes, va='top', color='#666')

    axes[-1].set_xlabel(f'UTC Hour ({date_label})', fontsize=11)
    axes[-1].set_xlim(0, 24)
    axes[-1].set_xticks(range(0, 25, 3))

    fig.suptitle('Measured Propagation Delay by Assigned Mode (24-Hour)',
                 fontsize=13, fontweight='bold', y=0.995)
    fig.tight_layout()
    outpath = OUTPUT_DIR / 'fig8_toa_mode_correlation.png'
    fig.savefig(outpath, dpi=DPI, bbox_inches='tight')
    plt.close(fig)
    print(f"  → {outpath}")


# ══════════════════════════════════════════════════════════════════════════
# FIGURE 9 — 3D globe: ray arcs from all four stations to EM38ww
# ══════════════════════════════════════════════════════════════════════════

def _raytrace_station_quick(tx_lat, tx_lon, rx_lat, rx_lon, freq_mhz):
    """Run a quick PHaRLAP ray trace for one station at one frequency.

    Returns list of dicts with keys: nhops, elev, group_delay_ms, apogee_km,
    virtual_height_km, phase_path_km, ground_range_km.
    Returns None if PHaRLAP unavailable.
    """
    if 'DIR_MODELS_REF_DAT' not in os.environ:
        for candidate in ['/opt/pharlap_4.7.4/dat', '/opt/pharlap/dat']:
            if os.path.isdir(candidate):
                os.environ['DIR_MODELS_REF_DAT'] = candidate
                break
    try:
        import pylap.raytrace_2d as rt_mod
        import pylap.iri2016 as iri_mod
    except (ImportError, OSError):
        return None

    import math

    # Great-circle bearing and distance
    dlon_r = math.radians(rx_lon - tx_lon)
    lat1_r, lat2_r = math.radians(tx_lat), math.radians(rx_lat)
    bearing = math.degrees(math.atan2(
        math.sin(dlon_r) * math.cos(lat2_r),
        math.cos(lat1_r) * math.sin(lat2_r) -
        math.sin(lat1_r) * math.cos(lat2_r) * math.cos(dlon_r)
    )) % 360.0

    dlat = math.radians(rx_lat - tx_lat)
    dlon = math.radians(rx_lon - tx_lon)
    a_gc = (math.sin(dlat / 2) ** 2 +
            math.cos(math.radians(tx_lat)) *
            math.cos(math.radians(rx_lat)) *
            math.sin(dlon / 2) ** 2)
    target_km = 6371.0 * 2 * math.asin(math.sqrt(a_gc))

    # Simple single-midpoint IRI grid (fast, sufficient for mode finding)
    ut = [int(TARGET_DATE[:4]), int(TARGET_DATE[4:6]), int(TARGET_DATE[6:]), 18, 0]
    mid_lat = (tx_lat + rx_lat) / 2.0
    mid_lon = (tx_lon + rx_lon) / 2.0
    # Normalize longitude
    if abs(tx_lon - rx_lon) > 180:
        mid_lon = ((tx_lon + rx_lon + 360) / 2.0) % 360 - 180

    n_heights = 200
    ht_start, ht_inc = 60.0, 3.0
    range_inc_km = 50.0
    max_hops = max(3, int(target_km / 2000) + 1)
    grid_max_km = max(target_km * 2, 5000.0)
    n_ranges = int(grid_max_km / range_inc_km) + 1

    try:
        outf, oarr = iri_mod.iri2016(mid_lat, mid_lon, 100.0, ut,
                                      ht_start, ht_inc, n_heights, {})
    except Exception:
        return None

    ne = np.maximum(outf[0, :], 0.0) / 1e6
    iono_en = np.tile(ne, (n_ranges, 1)).T  # (n_heights, n_ranges)
    zeros = np.zeros_like(iono_en)
    irreg = np.zeros((4, n_ranges), dtype=np.float64)

    nmF2 = max(float(oarr[0]), 0.0)
    foF2 = 8.98 * math.sqrt(nmF2) / 1e6
    hmF2 = float(oarr[1])

    # Trace a fan of rays to find closing modes
    elevs = np.arange(2.0, 80.5, 1.0)
    freqs = np.full(len(elevs), freq_mhz)
    tol = [1e-7, 0.01, 10.0]

    try:
        rays, paths, states = rt_mod.raytrace_2d(
            tx_lat, tx_lon, elevs, bearing, freqs, max_hops,
            tol, 0, iono_en, zeros, zeros, ht_start, ht_inc,
            range_inc_km, irreg)
    except Exception:
        return None

    tolerance_km = max(200.0, target_km * 0.05)
    closing = []
    for i, (path, ray) in enumerate(zip(paths, rays)):
        labels = np.asarray(ray.get('ray_label', []))
        gnd_cumul = np.asarray(ray.get('ground_range', []))
        grp_cumul = np.asarray(ray.get('group_range', []))
        apogee_arr = np.asarray(ray.get('apogee', []))
        virt_h_arr = np.asarray(ray.get('virtual_height', []))
        for k in range(len(labels)):
            if int(labels[k]) != 1:
                continue
            if abs(float(gnd_cumul[k]) - target_km) < tolerance_km:
                nhops = k + 1
                grp_km = float(grp_cumul[k]) if k < len(grp_cumul) else 0
                closing.append(dict(
                    nhops=nhops,
                    elev=float(elevs[i]),
                    group_delay_ms=grp_km / 299792.458 * 1000.0,
                    apogee_km=float(apogee_arr[k]) if k < len(apogee_arr) else hmF2,
                    virtual_height_km=float(virt_h_arr[k]) if k < len(virt_h_arr) else hmF2,
                    ground_range_km=float(gnd_cumul[k]),
                    foF2=foF2,
                    hmF2=hmF2,
                ))
                break

    return closing if closing else None


def _iri_midpoint_params(tx_lat, tx_lon, rx_lat, rx_lon):
    """Get IRI midpoint foF2 and hmF2 for a path (no PHaRLAP needed)."""
    if 'DIR_MODELS_REF_DAT' not in os.environ:
        for candidate in ['/opt/pharlap_4.7.4/dat', '/opt/pharlap/dat']:
            if os.path.isdir(candidate):
                os.environ['DIR_MODELS_REF_DAT'] = candidate
                break
    try:
        import pylap.iri2016 as iri_mod
    except (ImportError, OSError):
        return None

    import math
    ut = [int(TARGET_DATE[:4]), int(TARGET_DATE[4:6]), int(TARGET_DATE[6:]), 18, 0]
    mid_lat = (tx_lat + rx_lat) / 2.0
    mid_lon = (tx_lon + rx_lon) / 2.0
    if abs(tx_lon - rx_lon) > 180:
        mid_lon = ((tx_lon + rx_lon + 360) / 2.0) % 360 - 180
    try:
        outf, oarr = iri_mod.iri2016(mid_lat, mid_lon, 100.0, ut, 60.0, 3.0, 200, {})
    except Exception:
        return None
    nmF2 = max(float(oarr[0]), 0.0)
    foF2 = 8.98 * math.sqrt(nmF2) / 1e6
    hmF2 = float(oarr[1])
    return dict(foF2=foF2, hmF2=hmF2)


def generate_fig9():
    """3D globe showing ray propagation from all four stations to EM38ww.

    If PHaRLAP is available, runs a quick ray trace per station to find actual
    closing modes with correct apogee heights, group delays, and virtual heights.
    Falls back to IRI-derived parabolic arcs with estimated hop counts.

    Shows: multi-mode arcs per station, ground bounce points, per-path
    annotation table with distance/delay/foF2/hmF2/modes.
    """
    import math
    try:
        from mpl_toolkits.mplot3d import Axes3D  # noqa: F401
    except (ImportError, ModuleNotFoundError):
        print("  ✗ mpl_toolkits.mplot3d not usable (matplotlib version conflict) — skipping Fig 9")
        return

    print("Generating Fig 9: 3D globe ray arcs (all four stations)...")

    R_E = 6371.0
    RX_LAT, RX_LON = 38.918461, -92.127974

    # Station definitions: name, lat, lon, color, representative freq MHz, detected
    STATIONS = [
        ('CHU',  45.2958,  -75.7533, '#2196F3', 7.85,  True),
        ('WWV',  40.6773, -105.0421, '#4CAF50', 10.0,  True),
        ('WWVH', 21.9886, -159.7601, '#FF9800', 10.0,  True),
        ('BPM',  34.9500,  109.5400, '#9C27B0', 10.0,  False),
    ]

    def _gc_distance(lat1, lon1, lat2, lon2):
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        a = (math.sin(dlat / 2) ** 2 +
             math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
             math.sin(dlon / 2) ** 2)
        return R_E * 2 * math.asin(math.sqrt(a))

    def _gc_points(lat1, lon1, lat2, lon2, n=300):
        la1, lo1 = math.radians(lat1), math.radians(lon1)
        la2, lo2 = math.radians(lat2), math.radians(lon2)
        d = 2 * math.asin(math.sqrt(
            math.sin((la2 - la1) / 2) ** 2 +
            math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2))
        if d < 1e-9:
            return np.array([lat1]), np.array([lon1])
        lats, lons = [], []
        for i in range(n):
            f = i / (n - 1)
            A = math.sin((1 - f) * d) / math.sin(d)
            B = math.sin(f * d) / math.sin(d)
            x = A * math.cos(la1) * math.cos(lo1) + B * math.cos(la2) * math.cos(lo2)
            y = A * math.cos(la1) * math.sin(lo1) + B * math.cos(la2) * math.sin(lo2)
            z = A * math.sin(la1) + B * math.sin(la2)
            lats.append(math.degrees(math.atan2(z, math.sqrt(x ** 2 + y ** 2))))
            lons.append(math.degrees(math.atan2(y, x)))
        return np.array(lats), np.array(lons)

    def _hop_heights(n_pts, nhops, apogee=280.0):
        h = np.zeros(n_pts)
        for k in range(nhops):
            s = k * n_pts // nhops
            e = (k + 1) * n_pts // nhops if k < nhops - 1 else n_pts
            t = np.linspace(0, 1, e - s)
            h[s:e] = apogee * 4 * t * (1 - t)
        return h

    def _xyz(lat_deg, lon_deg, alt_km=0.0):
        r = R_E + alt_km
        la = np.radians(np.asarray(lat_deg, dtype=np.float64))
        lo = np.radians(np.asarray(lon_deg, dtype=np.float64))
        return r * np.cos(la) * np.cos(lo), r * np.cos(la) * np.sin(lo), r * np.sin(la)

    # ── Attempt PHaRLAP ray trace per station ──
    station_results = {}  # name -> list of closing mode dicts or None
    use_pharlap = False
    for stn, tx_lat, tx_lon, color, freq, detected in STATIONS:
        dist_km = _gc_distance(tx_lat, tx_lon, RX_LAT, RX_LON)
        rt = _raytrace_station_quick(tx_lat, tx_lon, RX_LAT, RX_LON, freq)
        if rt is not None:
            use_pharlap = True
            station_results[stn] = rt
            modes_str = ', '.join(sorted(set(f"{m['nhops']}F" for m in rt)))
            print(f"    {stn}: {dist_km:.0f} km, {len(rt)} closing rays, modes: {modes_str}")
        else:
            # Fallback: IRI midpoint for hmF2 + geometric hop estimate
            iri = _iri_midpoint_params(tx_lat, tx_lon, RX_LAT, RX_LON)
            hmF2 = iri['hmF2'] if iri else 280.0
            foF2 = iri['foF2'] if iri else 8.0
            # Approximate skip distance for F2 mode
            skip_km = 2 * math.sqrt(2 * R_E * hmF2 + hmF2 ** 2)
            nhops = max(1, round(dist_km / skip_km))
            delay_ms = dist_km / 299792.458 * 1000.0 * 1.05  # ~5% iono excess
            station_results[stn] = [dict(
                nhops=nhops, elev=0.0, group_delay_ms=delay_ms,
                apogee_km=hmF2, virtual_height_km=hmF2,
                ground_range_km=dist_km, foF2=foF2, hmF2=hmF2,
            )]
            print(f"    {stn}: {dist_km:.0f} km, IRI fallback, ~{nhops}F, "
                  f"hmF2={hmF2:.0f} km, foF2={foF2:.1f} MHz")

    # ── Build 3D figure ──
    from matplotlib.lines import Line2D

    fig = plt.figure(figsize=(13, 9))
    ax = fig.add_subplot(111, projection='3d')

    # ── Earth sphere ──
    u = np.linspace(0, 2 * np.pi, 80)
    v = np.linspace(0, np.pi, 40)
    ax.plot_surface(
        R_E * np.outer(np.cos(u), np.sin(v)),
        R_E * np.outer(np.sin(u), np.sin(v)),
        R_E * np.outer(np.ones(80), np.cos(v)),
        color='#B3E5FC', alpha=0.20, linewidth=0, zorder=1)

    # Latitude circles every 30°
    theta = np.linspace(0, 2 * np.pi, 360)
    for lat_deg in range(-60, 90, 30):
        la_r = math.radians(lat_deg)
        r_circle = R_E * math.cos(la_r)
        ax.plot(r_circle * np.cos(theta), r_circle * np.sin(theta),
                np.full(360, R_E * math.sin(la_r)),
                color='#90A4AE', linewidth=0.3, alpha=0.20)
    # Longitude lines every 30°
    lat_line = np.linspace(-np.pi / 2, np.pi / 2, 180)
    for lo_deg in range(-180, 180, 30):
        lo_r = math.radians(lo_deg)
        ax.plot(R_E * np.cos(lat_line) * math.cos(lo_r),
                R_E * np.cos(lat_line) * math.sin(lo_r),
                R_E * np.sin(lat_line),
                color='#90A4AE', linewidth=0.3, alpha=0.20)

    # ── Ionospheric F2 shell ──
    f2_r = R_E + 280
    ax.plot_surface(
        f2_r * np.outer(np.cos(u), np.sin(v)),
        f2_r * np.outer(np.sin(u), np.sin(v)),
        f2_r * np.outer(np.ones(80), np.cos(v)),
        color='#C8E6C9', alpha=0.06, linewidth=0, zorder=2)

    # ── Ray arcs per station ──
    mode_lw = {1: 2.8, 2: 2.0, 3: 1.5, 4: 1.2, 5: 1.0}
    mode_ls = {1: '-', 2: '-', 3: '-', 4: '--', 5: '--'}

    for stn, tx_lat, tx_lon, color, freq, detected in STATIONS:
        results = station_results.get(stn, [])
        if not results:
            continue

        # Group by nhops, pick median-elevation representative per mode
        modes_seen = {}
        for m in results:
            nh = m['nhops']
            if nh not in modes_seen:
                modes_seen[nh] = []
            modes_seen[nh].append(m)

        dist_km = _gc_distance(tx_lat, tx_lon, RX_LAT, RX_LON)
        lats, lons = _gc_points(tx_lat, tx_lon, RX_LAT, RX_LON, n=400)

        for nhops, mode_rays in sorted(modes_seen.items()):
            # Pick the median-elevation ray as representative
            mode_rays.sort(key=lambda x: x['elev'])
            rep = mode_rays[len(mode_rays) // 2]
            apogee = rep['apogee_km']

            hts = _hop_heights(len(lats), nhops, apogee=apogee)
            xs, ys, zs = _xyz(lats, lons, hts)

            lw = mode_lw.get(nhops, 1.0)
            ls = mode_ls.get(nhops, '--')
            alpha = 0.9 if detected else 0.4

            ax.plot(xs, ys, zs, color=color, linewidth=lw,
                    linestyle=ls, alpha=alpha, zorder=10)

            # Ground bounce points (where arc touches ground between hops)
            if nhops > 1:
                for bk in range(1, nhops):
                    frac = bk / nhops
                    bi = int(frac * (len(lats) - 1))
                    bx, by, bz = _xyz(lats[bi], lons[bi], 0)
                    ax.scatter([bx], [by], [bz], color=color, s=20,
                               marker='d', alpha=alpha * 0.7, zorder=15,
                               depthshade=False, edgecolors='white',
                               linewidths=0.3)

        # Transmitter marker
        tx_x, tx_y, tx_z = _xyz(tx_lat, tx_lon, 20)
        ax.scatter([tx_x], [tx_y], [tx_z], color=color, s=70,
                   marker='o', zorder=20, depthshade=False,
                   edgecolors='white', linewidths=0.5)

    # ── Receiver marker ──
    rx_x, rx_y, rx_z = _xyz(RX_LAT, RX_LON, 20)
    ax.scatter([rx_x], [rx_y], [rx_z], color='#D32F2F', s=150,
               marker='*', zorder=25, depthshade=False,
               edgecolors='white', linewidths=0.5)

    # View angle centred on North America to show all paths
    ax.view_init(elev=25, azim=-95)
    lim = R_E * 1.55
    ax.set_xlim(-lim, lim)
    ax.set_ylim(-lim, lim)
    ax.set_zlim(-lim, lim)
    ax.set_axis_off()

    # ── Legend (manual handles) ──
    legend_handles = []
    for stn, tx_lat, tx_lon, color, freq, detected in STATIONS:
        results = station_results.get(stn, [])
        dist_km = _gc_distance(tx_lat, tx_lon, RX_LAT, RX_LON)
        modes = sorted(set(m['nhops'] for m in results)) if results else []
        modes_str = '/'.join(f'{n}F' for n in modes)
        delays = [m['group_delay_ms'] for m in results] if results else [0]
        delay_str = f'{min(delays):.1f}–{max(delays):.1f}' if len(delays) > 1 and max(delays) - min(delays) > 0.1 else f'{delays[0]:.1f}'
        det_str = '' if detected else ' (undetected)'
        lbl = f'{stn}  {dist_km:.0f} km  {modes_str}  τ={delay_str} ms{det_str}'
        ls = '-' if detected else '--'
        legend_handles.append(
            Line2D([0], [0], color=color, linewidth=2, linestyle=ls, label=lbl))
    legend_handles.append(
        Line2D([0], [0], color='#D32F2F', marker='*', linestyle='None',
               markersize=10, label='EM38ww (Receiver)'))
    ax.legend(handles=legend_handles, fontsize=7.5, loc='upper left',
              bbox_to_anchor=(-0.02, 0.95), framealpha=0.85,
              edgecolor='#CCC', handlelength=2.5)

    # ── Per-path annotation table (lower right) ──
    table_lines = ['Mode    Distance   τ_group    hmF2   foF2']
    table_lines.append('─' * 48)
    for stn, tx_lat, tx_lon, color, freq, detected in STATIONS:
        results = station_results.get(stn, [])
        dist_km = _gc_distance(tx_lat, tx_lon, RX_LAT, RX_LON)
        modes_seen = {}
        for m in results:
            nh = m['nhops']
            if nh not in modes_seen:
                modes_seen[nh] = m
        for nh in sorted(modes_seen):
            m = modes_seen[nh]
            det = '' if detected else '*'
            table_lines.append(
                f'{stn}{det:1s} {nh}F  {dist_km:7.0f} km  '
                f'{m["group_delay_ms"]:6.2f} ms  '
                f'{m["hmF2"]:5.0f} km  '
                f'{m["foF2"]:5.1f}'
            )
    table_text = '\n'.join(table_lines)
    fig.text(0.62, 0.04, table_text, fontsize=6.5, fontfamily='monospace',
             verticalalignment='bottom',
             bbox=dict(boxstyle='round,pad=0.4', facecolor='white',
                       edgecolor='#BDBDBD', alpha=0.90))

    src_str = 'PHaRLAP 4.7.4 + IRI-2020' if use_pharlap else 'IRI-2020 (geometric)'
    date_label = f'{TARGET_DATE[:4]}-{TARGET_DATE[4:6]}-{TARGET_DATE[6:]}'
    ax.set_title(
        f'HF Propagation Paths → EM38ww (38.9°N 92.1°W)\n'
        f'{src_str} — {date_label} 18:00 UTC',
        fontsize=11, fontweight='bold')

    outpath = OUTPUT_DIR / 'fig9_3d_globe_arcs.png'
    fig.savefig(outpath, dpi=DPI, bbox_inches='tight')
    plt.close(fig)
    print(f"  → {outpath}")


# ══════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    args = _parse_args(sys.argv[1:])

    TARGET_DATE = args.date
    DATA_ROOT = args.data_root
    PHASE2 = f'{DATA_ROOT}/phase2'
    OUTPUT_DIR = Path(args.output_dir)
    DPI = int(args.dpi)
    CHANNELS = [c.strip() for c in args.channels.split(',') if c.strip()]
    figs = _parse_figs(args.figs)

    fig_map = {
        1: generate_fig1,
        2: generate_fig2,
        3: generate_fig3,
        4: generate_fig4,
        5: generate_fig5,
        6: generate_fig6,
        7: generate_fig7,
        8: generate_fig8,
        9: generate_fig9,
    }

    print(f"QEX Figure Generator — target date: {TARGET_DATE}")
    print(f"Data root: {DATA_ROOT}")
    print(f"Output directory: {OUTPUT_DIR}")
    print(f"DPI: {DPI}")
    print(f"Channels: {', '.join(CHANNELS)}")
    print(f"Figures: {', '.join(str(x) for x in figs)}")
    print()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    generated = 0
    for n in figs:
        fn = fig_map.get(n)
        if fn is None:
            raise SystemExit(f"Unsupported figure number: {n}")
        fn()
        generated += 1

    print()
    print(f"Generated {generated} figure(s).")
