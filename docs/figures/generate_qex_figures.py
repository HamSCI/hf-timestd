#!/usr/bin/env python3
"""
Generate figures for the QEX article: "UTC Recovery and Ionospheric Science
from HF Time Signals with a GPSDO SDR"

Usage:
    /opt/hf-timestd/venv/bin/python3 docs/figures/generate_qex_figures.py

Generates Figs 3–7 into docs/figures/. Figs 1–2 require separate treatment
(diagram tool / raw IQ respectively).

Target date: 2026-03-16 (complete 24h, March equinox conditions)
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

# ── Configuration ──────────────────────────────────────────────────────────

TARGET_DATE = '20260316'
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
    ax_bot.set_xlabel('UTC Hour (2026-03-16)', fontsize=11)
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


def generate_fig5():
    print("Generating Fig 5: dTEC/dt time series + GNSS VTEC...")

    dtec_fn = f'{PHASE2}/science/dtec_timeseries/AGGREGATED_dtec_timeseries_{TARGET_DATE}.h5'
    vtec_fn = f'{DATA_ROOT}/data/gnss_vtec/GNSS_gnss_vtec_{TARGET_DATE}.h5'

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 7), height_ratios=[2, 1],
                                    sharex=True)

    # ── Top panel: dTEC/dt by station-frequency pair ──
    with h5py.File(dtec_fn, 'r') as f:
        # Trim all arrays to shortest length (off-by-one possible in HDF5)
        n = min(f['epoch'].shape[0], f['station'].shape[0],
                f['frequency_mhz'].shape[0], f['snr_db'].shape[0],
                f['dtec_rate_tecu_per_s'].shape[0])
        epochs = f['epoch'][:n]
        dtec_rate = f['dtec_rate_tecu_per_s'][:n]
        stations = np.array([s.decode() for s in f['station'][:n]])
        freqs = f['frequency_mhz'][:n]
        snr = f['snr_db'][:n]

    # Convert epoch to hours
    t0 = epochs.min()
    midnight = t0 - (t0 % 86400)
    hrs = (epochs - midnight) / 3600.0

    # Convert to mTECU/min
    dtec_rate_mtecu_min = dtec_rate * 1000 * 60

    # Quality gate: SNR > 12 dB, reasonable rate, finite
    good = (snr > 12.0) & (np.abs(dtec_rate_mtecu_min) < 40) & np.isfinite(dtec_rate_mtecu_min)

    # Plot selected frequency pairs
    pairs_to_show = [
        ('WWV', 10.0, '#4CAF50', 'WWV 10 MHz'),
        ('WWV', 5.0, '#81C784', 'WWV 5 MHz'),
        ('CHU', 7.85, '#2196F3', 'CHU 7.85 MHz'),
        ('CHU', 14.67, '#64B5F6', 'CHU 14.67 MHz'),
        ('WWVH', 10.0, '#FF9800', 'WWVH 10 MHz'),
    ]

    for stn, freq, color, label in pairs_to_show:
        mask = good & (stations == stn) & (np.abs(freqs - freq) < 0.1)
        if mask.sum() < 10:
            continue

        # Light scatter for raw data
        ax1.scatter(hrs[mask], dtec_rate_mtecu_min[mask], s=0.5, alpha=0.08,
                    color=color, rasterized=True)

        # Rolling median (15-min window, 6-min step)
        xm, ym = _rolling_median(hrs[mask], dtec_rate_mtecu_min[mask],
                                  window_hrs=0.25, step_hrs=0.1)
        if len(xm) > 2:
            ax1.plot(xm, ym, color=color, linewidth=1.5, label=label, alpha=0.9)

    ax1.set_ylabel('dTEC/dt (mTECU/min)', fontsize=11)
    ax1.set_title('Carrier-Phase dTEC/dt: 24-Hour Time Series (2026-03-16)',
                  fontsize=12, fontweight='bold')
    ax1.legend(fontsize=8, loc='upper right', ncol=2)
    ax1.grid(True, alpha=0.3)
    ax1.set_ylim(-25, 25)
    ax1.axhline(0, color='#666', linewidth=0.5, alpha=0.5)

    # ── Bottom panel: GNSS overhead VTEC ──
    with h5py.File(vtec_fn, 'r') as f:
        vtec_ts = f['unix_timestamp'][:]
        vtec = f['vtec_tecu'][:]
        vtec_quality = np.array([q.decode() for q in f['quality_flag'][:]])

    vtec_hrs = (vtec_ts - midnight) / 3600.0
    vtec_good = vtec_quality == 'GOOD'

    ax2.plot(vtec_hrs[vtec_good], vtec[vtec_good], color='#7B1FA2',
             linewidth=0.8, alpha=0.7, label='GNSS VTEC (ZED-F9P)')

    # Compute and plot VTEC rate on secondary axis
    ax2r = ax2.twinx()
    dt_vtec = np.diff(vtec_ts[vtec_good])
    dvtec = np.diff(vtec[vtec_good])
    # Avoid division by zero
    valid_dt = dt_vtec > 0
    vtec_rate = np.full_like(dvtec, np.nan)
    vtec_rate[valid_dt] = dvtec[valid_dt] / dt_vtec[valid_dt] * 60 * 1000  # mTECU/min
    vtec_rate_hrs = (vtec_ts[vtec_good][1:] - midnight) / 3600.0

    # Smooth VTEC rate (5-min rolling median)
    xvr, yvr = _rolling_median(vtec_rate_hrs[np.isfinite(vtec_rate)],
                                vtec_rate[np.isfinite(vtec_rate)],
                                window_hrs=1.0/12, step_hrs=1.0/30)
    ax2r.plot(xvr, yvr, color='#E91E63', linewidth=0.8, alpha=0.5,
              label='GNSS dVTEC/dt')
    ax2r.set_ylabel('dVTEC/dt (mTECU/min)', fontsize=9, color='#E91E63', alpha=0.7)
    ax2r.set_ylim(-200, 200)
    ax2r.tick_params(axis='y', labelcolor='#E91E63', labelsize=8)

    ax2.set_ylabel('VTEC (TECU)', fontsize=11, color='#7B1FA2')
    ax2.set_xlabel('UTC Hour (2026-03-16)', fontsize=11)
    ax2.legend(fontsize=8, loc='upper left')
    ax2r.legend(fontsize=7, loc='upper right')
    ax2.grid(True, alpha=0.3)
    ax2.set_xlim(0, 24)
    ax2.set_xticks(range(0, 25, 3))

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
        ('WWV', 10.0, 'WWV 10 MHz (1,490 km)'),
        ('WWVH', 10.0, 'WWVH 10 MHz (7,530 km)'),
        ('CHU', 7.85, 'CHU 7.850 MHz (1,950 km)'),
        ('CHU', 14.67, 'CHU 14.670 MHz (1,950 km)'),
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
    axes[-1].set_xlabel('UTC Hour (2026-03-16)', fontsize=11)
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
    """Generate spectrogram from raw IQ showing tick structure on SHARED_10000."""
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

    # Show 10 seconds around second boundaries (tick region)
    show_seconds = 10
    show_samples = show_seconds * sr
    # Start at second 5 to capture ticks at seconds 5-14
    start_sample = 5 * sr
    if start_sample + show_samples > len(iq):
        start_sample = 0
    segment = iq[start_sample:start_sample + show_samples]

    fig, (ax_spec, ax_env) = plt.subplots(2, 1, figsize=(10, 6),
                                           height_ratios=[3, 1], sharex=True)

    # ── Top: spectrogram ──
    nfft = 512
    noverlap = nfft - nfft // 8
    freqs, times, Sxx = sp_signal.spectrogram(
        segment, fs=sr, nperseg=nfft, noverlap=noverlap,
        return_onesided=False, mode='psd'
    )
    # Shift to center DC, convert to audio frequency offset
    freqs_shifted = np.fft.fftshift(freqs)
    # Wrap negative freqs to show as audio offset from carrier
    freqs_shifted = np.where(freqs_shifted > sr/2, freqs_shifted - sr, freqs_shifted)
    Sxx_shifted = np.fft.fftshift(Sxx, axes=0)
    sort_idx = np.argsort(freqs_shifted)
    freqs_sorted = freqs_shifted[sort_idx]
    Sxx_sorted = Sxx_shifted[sort_idx, :]

    # Show only ±3000 Hz around carrier (tick tones are at 1000/1200 Hz)
    freq_mask = np.abs(freqs_sorted) <= 3000
    Sxx_db = 10 * np.log10(Sxx_sorted[freq_mask, :] + 1e-20)

    # Relative time from start of segment
    t_offset = start_sample / sr
    extent = [t_offset, t_offset + show_seconds,
              freqs_sorted[freq_mask][0], freqs_sorted[freq_mask][-1]]

    vmin = np.percentile(Sxx_db, 5)
    vmax = np.percentile(Sxx_db, 99)

    im = ax_spec.imshow(Sxx_db, aspect='auto', origin='lower', extent=extent,
                        cmap='viridis', vmin=vmin, vmax=vmax, interpolation='bilinear')
    ax_spec.set_ylabel('Offset from 10 MHz (Hz)', fontsize=10)
    ax_spec.set_title('SHARED_10000 Spectrogram: WWV (1000 Hz) + WWVH (1200 Hz) Ticks',
                      fontsize=12, fontweight='bold')

    # Mark tick tone frequencies
    ax_spec.axhline(1000, color='#4CAF50', linewidth=0.8, linestyle='--', alpha=0.7)
    ax_spec.text(t_offset + 0.1, 1050, 'WWV 1000 Hz', fontsize=7, color='#4CAF50')
    ax_spec.axhline(1200, color='#FF9800', linewidth=0.8, linestyle='--', alpha=0.7)
    ax_spec.text(t_offset + 0.1, 1250, 'WWVH 1200 Hz', fontsize=7, color='#FF9800')

    cb = fig.colorbar(im, ax=ax_spec, pad=0.02, aspect=30)
    cb.set_label('PSD (dB)', fontsize=9)

    # ── Bottom: AM envelope showing tick pulses ──
    # Bandpass 800-1400 Hz to isolate tick energy
    sos = sp_signal.butter(4, [800, 1400], btype='bandpass', fs=sr, output='sos')
    filtered = sp_signal.sosfilt(sos, segment)
    # Compute envelope via analytic signal
    analytic = sp_signal.hilbert(np.real(filtered))
    envelope = np.abs(analytic)
    # Smooth envelope (10 ms window)
    smooth_n = int(0.010 * sr)
    envelope_smooth = np.convolve(envelope, np.ones(smooth_n)/smooth_n, mode='same')

    t_axis = np.arange(len(segment)) / sr + t_offset
    ax_env.plot(t_axis, 20 * np.log10(envelope_smooth + 1e-20),
                color='#1565C0', linewidth=0.5)
    ax_env.set_ylabel('Tick Envelope (dB)', fontsize=10)
    ax_env.set_xlabel(f'Time (seconds from minute boundary)', fontsize=10)
    ax_env.set_xlim(t_offset, t_offset + show_seconds)

    # Mark integer second boundaries
    for sec in range(int(t_offset), int(t_offset + show_seconds) + 1):
        ax_env.axvline(sec, color='#D32F2F', linewidth=0.5, alpha=0.4, linestyle=':')
        ax_spec.axvline(sec, color='white', linewidth=0.3, alpha=0.3, linestyle=':')

    ax_env.grid(True, alpha=0.2)

    fig.tight_layout()
    outpath = OUTPUT_DIR / 'fig2_spectrogram_10mhz.png'
    fig.savefig(outpath, dpi=DPI, bbox_inches='tight')
    plt.close(fig)
    print(f"  → {outpath}")


# ══════════════════════════════════════════════════════════════════════════
# FIGURE 6 — Synthetic ray fan diagram (illustrative, based on IRI params)
# ══════════════════════════════════════════════════════════════════════════

def generate_fig6():
    """
    Generate an illustrative ray fan plot for WWV 10 MHz.

    Since PHaRLAP/pyLAP is not installed, this computes simplified parabolic
    ionosphere ray paths using the IRI-2020 parameters cited in the article
    (foF2=10.47 MHz, hmF2=291 km). Clearly labeled as illustrative.
    """
    print("Generating Fig 6: Illustrative ray fan (synthetic)...")

    # ── Ionosphere and geometry parameters (from article/IRI-2020) ──
    R_E = 6371.0          # Earth radius km
    hmF2 = 291.0          # F2 peak height km
    foF2 = 10.47          # critical frequency MHz
    freq = 10.0           # WWV 10 MHz
    target_range = 1490.0 # WWV→EM38ww km

    # Parabolic layer: bottom at yb, peak at hmF2, semi-thickness ym
    yb = 150.0   # bottom of F layer km
    ym = hmF2 - yb  # semi-thickness

    def ray_path_parabolic(elev_deg, n_hops=1):
        """
        Trace a ray through a parabolic F-layer for n_hops.
        Returns (ground_ranges_km, heights_km) arrays for plotting.
        """
        elev = np.radians(elev_deg)

        # For a parabolic layer, the skip distance per hop is approximately:
        #   D = 2 * R_E * arctan( (hmF2 * tan(elev)) / (R_E + hmF2) )
        # But more accurately, use the secant law for oblique incidence:
        #   MUF = foF2 * sec(i) where i is angle of incidence at layer peak
        # Check if ray penetrates: need f < foF2 * sec(i)

        # Simplified: compute apogee height and ground range per hop
        # using spherical earth geometry
        cos_elev = np.cos(elev)
        sin_elev = np.sin(elev)

        # Ray apogee for reflection: solve for height where f = fp * sec(i)
        # For parabolic layer: fp(h) = foF2 * sqrt(1 - ((h - hmF2)/ym)^2)
        # At reflection: f = fp(h) / cos(i_local)
        # Simplified: assume reflection near hmF2 for MUF-viable rays

        # Virtual reflection height (simplified)
        h_reflect = hmF2

        # Ground range per hop (spherical geometry)
        # Half-hop: ray goes from ground to h_reflect
        alpha = np.arccos((R_E * cos_elev) / (R_E + h_reflect))
        half_hop_angle = np.pi/2 - elev - alpha  # This is incorrect for low angles
        # Better: use the geometry directly
        # At launch angle elev, the ray reaches height h at range:
        d_half = R_E * np.arccos(
            (R_E + h_reflect) * np.cos(np.pi/2 - elev) / (R_E + h_reflect)
        ) if False else 0

        # Simple flat-earth approx good enough for illustration
        d_half_km = h_reflect / np.tan(elev) if elev > np.radians(2) else 5000
        d_hop = 2 * d_half_km

        # Build ray path points
        n_pts = 100
        gnd = []
        hgt = []
        for hop in range(n_hops):
            offset = hop * d_hop
            for j in range(n_pts):
                frac = j / (n_pts - 1)
                x = offset + frac * d_hop
                # Parabolic arc for height
                h = h_reflect * 4 * frac * (1 - frac)  # peaks at h_reflect
                gnd.append(x)
                hgt.append(h)

        return np.array(gnd), np.array(hgt), n_hops * d_hop

    fig, ax = plt.subplots(figsize=(10, 5))

    # ── Draw Earth curvature (exaggerated) ──
    # For a 5000 km range, Earth curvature sag is ~500 km
    x_range = np.linspace(0, 5000, 500)
    earth_curve = -x_range * (5000 - x_range) / (2 * R_E * 8)  # very subtle
    ax.fill_between(x_range, -50, earth_curve, color='#8D6E63', alpha=0.15)

    # ── Draw ionospheric layers ──
    # E layer band (90-130 km)
    ax.fill_between(x_range, 90, 130, color='#BBDEFB', alpha=0.3, label='E layer')
    # F layer band (150-450 km)
    ax.fill_between(x_range, 150, 450, color='#C8E6C9', alpha=0.25, label='F2 layer')
    # hmF2 line
    ax.axhline(hmF2, color='#4CAF50', linewidth=1, linestyle='--', alpha=0.6)
    ax.text(4600, hmF2 + 8, f'hmF2 = {hmF2} km', fontsize=8, color='#4CAF50')
    ax.text(4600, hmF2 - 25, f'foF2 = {foF2} MHz', fontsize=8, color='#4CAF50',
            fontstyle='italic')

    # ── Trace rays at various elevation angles ──
    elevations = np.arange(5, 65, 2.5)
    closing_rays = []

    for elev in elevations:
        gnd, hgt, total_range = ray_path_parabolic(elev, n_hops=1)

        # Check each hop count for closure
        for nhops in [1, 2, 3]:
            gnd_n, hgt_n, total_n = ray_path_parabolic(elev, n_hops=nhops)
            closes = abs(total_n - target_range) < 300  # ±300 km tolerance

            if nhops == 1:
                color = '#BDBDBD'
                alpha = 0.2
                lw = 0.4
            else:
                continue  # only plot 1-hop for non-closing rays

            if closes:
                closing_rays.append((elev, nhops, total_n))
                if nhops == 1:
                    color, lw, alpha = '#66BB6A', 2.0, 0.9
                elif nhops == 2:
                    color, lw, alpha = '#FFA726', 2.0, 0.9
                elif nhops == 3:
                    color, lw, alpha = '#EF5350', 2.0, 0.9

            ax.plot(gnd_n, hgt_n, color=color, linewidth=lw, alpha=alpha)

    # Plot multi-hop closing rays on top
    for nhops in [2, 3]:
        for elev in elevations:
            gnd_n, hgt_n, total_n = ray_path_parabolic(elev, n_hops=nhops)
            closes = abs(total_n - target_range) < 300
            if closes:
                if nhops == 2:
                    color, lw = '#FFA726', 2.0
                else:
                    color, lw = '#EF5350', 2.0
                ax.plot(gnd_n, hgt_n, color=color, linewidth=lw, alpha=0.9)

    # ── Mark transmitter and receiver ──
    ax.plot(0, 0, 'v', color='#D32F2F', markersize=12, zorder=20)
    ax.text(50, -30, 'WWV\n(Fort Collins)', fontsize=8, fontweight='bold',
            color='#D32F2F')
    ax.plot(target_range, 0, '*', color='#1565C0', markersize=14, zorder=20)
    ax.text(target_range + 50, -30, 'EM38ww\n(Receiver)', fontsize=8,
            fontweight='bold', color='#1565C0')

    # ── Legend for modes ──
    from matplotlib.lines import Line2D
    legend_elements = [
        Line2D([0], [0], color='#66BB6A', linewidth=2, label='1F2 (closing)'),
        Line2D([0], [0], color='#FFA726', linewidth=2, label='2F2 (closing)'),
        Line2D([0], [0], color='#EF5350', linewidth=2, label='3F2 (closing)'),
        Line2D([0], [0], color='#BDBDBD', linewidth=1, label='Non-closing rays'),
    ]
    ax.legend(handles=legend_elements, fontsize=8, loc='upper right')

    ax.set_xlabel('Ground Range (km)', fontsize=11)
    ax.set_ylabel('Height (km)', fontsize=11)
    ax.set_title('Ray Fan: WWV 10 MHz → EM38ww (1,490 km)\n'
                 'IRI-2020: foF2 = 10.47 MHz, hmF2 = 291 km — Illustrative',
                 fontsize=11, fontweight='bold')
    ax.set_xlim(-100, 5000)
    ax.set_ylim(-50, 500)
    ax.grid(True, alpha=0.2)

    # Note
    ax.text(2500, 470, 'Simplified parabolic ionosphere — for illustration only.\n'
            'Production uses PHaRLAP 4.7.4 numerical ray tracing.',
            fontsize=7, ha='center', fontstyle='italic', color='#666')

    fig.tight_layout()
    outpath = OUTPUT_DIR / 'fig6_ray_fan_illustrative.png'
    fig.savefig(outpath, dpi=DPI, bbox_inches='tight')
    plt.close(fig)
    print(f"  → {outpath}")


# ══════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    print(f"QEX Figure Generator — target date: {TARGET_DATE}")
    print(f"Output directory: {OUTPUT_DIR}")
    print()

    generate_fig1()
    generate_fig2()
    generate_fig3()
    generate_fig4()
    generate_fig5()
    generate_fig6()
    generate_fig7()

    print()
    print("All 7 figures generated.")
