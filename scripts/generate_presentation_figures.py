#!/usr/bin/env python3
"""
Generate evidence figures for HamSCI 2026 presentation.

Reads live HDF5 data from /var/lib/timestd/phase2/ and produces publication-quality
matplotlib figures. Run from repo root:

    python3 scripts/generate_presentation_figures.py [--date YYYYMMDD] [--outdir docs/figures]

Figures produced:
  1. Metrological ladder (bar chart of timing accuracy tiers)
  2. D_clock time series (24h, per-channel)
  3. Fusion D_clock distribution (histogram vs GPS)
  4. dTEC rate time series (multi-station overlay)
  5. Differential dTEC validation (RMS by station pair)
  6. Doppler diurnal signature (24h, CHU 7.85 MHz)
  7. All-arrivals mode timeline (time-of-flight vs time)
  8. Detection performance summary (tick counts + SNR per channel)
"""

import argparse
import glob
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import h5py
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np

# ---------- Configuration ----------

PHASE2 = Path('/var/lib/timestd/phase2')
SCIENCE = PHASE2 / 'science'

CHANNELS_TICK_TIMING = [
    ('CHU_3330',    'CHU 3.33'),
    ('CHU_7850',    'CHU 7.85'),
    ('CHU_14670',   'CHU 14.67'),
    ('SHARED_2500', 'Shared 2.5'),
    ('SHARED_5000', 'Shared 5.0'),
    ('SHARED_10000','Shared 10.0'),
    ('SHARED_15000','Shared 15.0'),
    ('WWV_20000',   'WWV 20.0'),
    ('WWV_25000',   'WWV 25.0'),
]

STATION_COLORS = {
    'CHU':  '#1f77b4',
    'WWV':  '#ff7f0e',
    'WWVH': '#2ca02c',
    'BPM':  '#d62728',
}

CHANNEL_COLORS = {
    'CHU_3330':    '#1f77b4',
    'CHU_7850':    '#4a90d9',
    'CHU_14670':   '#7fb3e0',
    'SHARED_2500': '#9467bd',
    'SHARED_5000': '#c5b0d5',
    'SHARED_10000':'#8c564b',
    'SHARED_15000':'#c49c94',
    'WWV_20000':   '#ff7f0e',
    'WWV_25000':   '#ffbb78',
}


def safe_float(arr):
    """Return array with NaN/Inf → NaN."""
    out = np.array(arr, dtype=float)
    out[~np.isfinite(out)] = np.nan
    return out


def epoch_to_datetime(epoch_arr):
    """Convert array of Unix epochs to matplotlib-friendly datetime64."""
    return np.array([datetime.fromtimestamp(e, tz=timezone.utc) for e in epoch_arr])


def find_h5(directory, date_str):
    """Find HDF5 file(s) matching a date in a directory."""
    pattern = str(directory / f'*{date_str}*.h5')
    return sorted(glob.glob(pattern))


# ---------- Figure generators ----------

def fig1_metrological_ladder(outdir):
    """Bar chart of timing accuracy tiers."""
    tiers = [
        ('Unsync PC',      100.0,  '#d62728'),
        ('Internet NTP',     0.6,  '#ff7f0e'),
        ('LAN NTP',          1.2,  '#ffbb78'),
        ('GPS+PPS',          0.006, '#2ca02c'),
        ('HF TSL1\n(geometric)', 1.1, '#9467bd'),
        ('HF TSL2\n(ionospheric)', 0.055, '#1f77b4'),
    ]

    fig, ax = plt.subplots(figsize=(10, 5))
    names = [t[0] for t in tiers]
    offsets = [t[1] for t in tiers]
    colors = [t[2] for t in tiers]

    bars = ax.barh(names, offsets, color=colors, edgecolor='white', linewidth=0.5)
    ax.set_xscale('log')
    ax.set_xlabel('Timing Offset from GPS (ms)', fontsize=12)
    ax.set_title('Metrological Ladder — Timing Authority Comparison', fontsize=14)
    ax.invert_yaxis()

    for bar, offset in zip(bars, offsets):
        if offset >= 1:
            ax.text(offset * 1.3, bar.get_y() + bar.get_height()/2,
                    f'{offset:.0f} ms', va='center', fontsize=10)
        else:
            ax.text(offset * 1.5, bar.get_y() + bar.get_height()/2,
                    f'{offset:.3f} ms', va='center', fontsize=10)

    ax.axvline(x=0.001, color='green', linestyle='--', alpha=0.4, label='GPS reference')
    plt.tight_layout()
    path = outdir / 'fig1_metrological_ladder.png'
    fig.savefig(path, dpi=150, bbox_inches='tight')
    plt.close(fig)
    print(f'  [1] {path}')


def fig2_dclock_timeseries(outdir, date_str):
    """Per-channel D_clock over 24 hours."""
    fig, ax = plt.subplots(figsize=(14, 6))
    n_plotted = 0

    for ch_id, ch_label in CHANNELS_TICK_TIMING:
        files = find_h5(PHASE2 / ch_id / 'tick_timing', date_str)
        if not files:
            continue
        try:
            with h5py.File(files[0], 'r', locking=False) as f:
                if 'd_clock_ms' not in f or 'minute_boundary_utc' not in f:
                    continue
                dc = safe_float(f['d_clock_ms'][:])
                mb = f['minute_boundary_utc'][:].astype(float)
                valid = np.isfinite(dc) & (mb > 0)
                if np.sum(valid) < 10:
                    continue
                times = epoch_to_datetime(mb[valid])
                ax.scatter(times, dc[valid], s=2, alpha=0.5,
                          color=CHANNEL_COLORS.get(ch_id, 'gray'),
                          label=f'{ch_label} ({np.sum(valid)})')
                n_plotted += 1
        except Exception as e:
            print(f'    skip {ch_id}: {e}')

    if n_plotted == 0:
        print('  [2] No tick_timing data found')
        plt.close(fig)
        return

    ax.set_xlabel('UTC', fontsize=12)
    ax.set_ylabel('D_clock (ms)', fontsize=12)
    ax.set_title(f'Tick Timing — D_clock by Channel ({date_str})', fontsize=14)
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
    ax.legend(fontsize=8, ncol=3, loc='upper right', markerscale=3)
    ax.set_ylim(-15, 15)
    ax.axhline(0, color='gray', linestyle='--', alpha=0.3)
    plt.tight_layout()
    path = outdir / 'fig2_dclock_timeseries.png'
    fig.savefig(path, dpi=150, bbox_inches='tight')
    plt.close(fig)
    print(f'  [2] {path}')


def fig3_fusion_dclock_histogram(outdir, date_str):
    """Histogram of fusion D_clock residuals."""
    # Fusion files are in phase2/fusion/global_physics_YYYYMMDD.h5
    # with utc_offset_ms as the D_clock field
    files = find_h5(PHASE2 / 'fusion', date_str)
    if not files:
        files = find_h5(SCIENCE / 'd_clock', date_str)
    if not files:
        print('  [3] No fusion d_clock data found')
        return

    dc = None
    for fpath in files:
        try:
            with h5py.File(fpath, 'r', locking=False) as f:
                for key in ['utc_offset_ms', 'd_clock_ms', 'fusion_d_clock_ms']:
                    if key in f:
                        dc = safe_float(f[key][:])
                        break
            if dc is not None:
                break
        except Exception:
            continue

    if dc is None:
        print('  [3] No D_clock field found in fusion files')
        return

    valid = np.isfinite(dc)
    dc = dc[valid]

    if len(dc) < 10:
        print(f'  [3] Only {len(dc)} valid records')
        return

    fig, ax = plt.subplots(figsize=(10, 5))
    bins = np.linspace(-10, 10, 81)
    ax.hist(dc, bins=bins, color='#1f77b4', edgecolor='white', linewidth=0.3, alpha=0.8)
    ax.axvline(np.median(dc), color='red', linestyle='--', label=f'Median: {np.median(dc):.1f} ms')
    ax.axvline(0, color='green', linestyle='--', alpha=0.5, label='GPS reference')

    within_2 = np.sum(np.abs(dc) < 2) / len(dc) * 100
    within_5 = np.sum(np.abs(dc) < 5) / len(dc) * 100
    ax.set_xlabel('D_clock residual (ms)', fontsize=12)
    ax.set_ylabel('Count', fontsize=12)
    ax.set_title(f'Fusion D_clock Distribution ({date_str}) — n={len(dc)}\n'
                 f'{within_2:.0f}% within ±2 ms, {within_5:.0f}% within ±5 ms',
                 fontsize=13)
    ax.legend(fontsize=11)
    plt.tight_layout()
    path = outdir / 'fig3_fusion_dclock_histogram.png'
    fig.savefig(path, dpi=150, bbox_inches='tight')
    plt.close(fig)
    print(f'  [3] {path}')


def fig4_dtec_rate_timeseries(outdir, date_str):
    """Multi-station dTEC rate overlay."""
    dtec_files = find_h5(SCIENCE / 'dtec', date_str)
    if not dtec_files:
        print('  [4] No dTEC data found')
        return

    fig, ax = plt.subplots(figsize=(14, 6))

    try:
        with h5py.File(dtec_files[0], 'r', locking=False) as f:
            rates = safe_float(f['dtec_rate_tecu_per_s'][:])
            stations_raw = f['station'][:] if 'station' in f else None
            mb = f['minute_boundary_utc'][:].astype(float) if 'minute_boundary_utc' in f else None
            ts_raw = f['timestamp_utc'][:] if 'timestamp_utc' in f else None

            if mb is not None:
                times_epoch = mb
            elif ts_raw is not None:
                # Parse ISO strings
                times_epoch = np.zeros(len(ts_raw))
                for i, t in enumerate(ts_raw):
                    s = t.decode() if isinstance(t, bytes) else str(t)
                    try:
                        times_epoch[i] = datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp()
                    except:
                        times_epoch[i] = 0
            else:
                print('  [4] No time field found')
                plt.close(fig)
                return

            if stations_raw is not None:
                stations = [s.decode() if isinstance(s, bytes) else str(s) for s in stations_raw]
            else:
                stations = ['ALL'] * len(rates)

            for stn in sorted(set(stations)):
                mask = np.array([s == stn for s in stations]) & np.isfinite(rates) & (times_epoch > 0)
                if np.sum(mask) < 10:
                    continue
                times = epoch_to_datetime(times_epoch[mask])
                ax.scatter(times, rates[mask] * 1000,  # Convert to mTECU/s
                          s=2, alpha=0.4,
                          color=STATION_COLORS.get(stn, 'gray'),
                          label=f'{stn} ({np.sum(mask)})')
    except Exception as e:
        print(f'  [4] Error: {e}')
        plt.close(fig)
        return

    ax.set_xlabel('UTC', fontsize=12)
    ax.set_ylabel('dTEC rate (mTECU/s)', fontsize=12)
    ax.set_title(f'Carrier-Phase dTEC Rate ({date_str})', fontsize=14)
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
    ax.legend(fontsize=10, ncol=4, loc='upper right', markerscale=3)
    ax.set_ylim(-3, 3)
    ax.axhline(0, color='gray', linestyle='--', alpha=0.3)
    plt.tight_layout()
    path = outdir / 'fig4_dtec_rate_timeseries.png'
    fig.savefig(path, dpi=150, bbox_inches='tight')
    plt.close(fig)
    print(f'  [4] {path}')


def fig5_differential_dtec(outdir, date_str):
    """Differential dTEC RMS by station pair."""
    diff_files = find_h5(SCIENCE / 'dtec_diff', date_str)
    if not diff_files:
        print('  [5] No differential dTEC data found')
        return

    try:
        with h5py.File(diff_files[0], 'r', locking=False) as f:
            keys = list(f.keys())
            rms_key = None
            for candidate in ['rms_tecu', 'rms_diff_tecu', 'rms']:
                if candidate in f:
                    rms_key = candidate
                    break
            if 'station' not in f or rms_key is None:
                print(f'  [5] Missing fields. Available: {keys[:10]}')
                plt.close(fig)
                return

            stations = [s.decode() if isinstance(s, bytes) else str(s) for s in f['station'][:]]
            rms = safe_float(f[rms_key][:])

            # If there are freq pair fields
            freq1 = safe_float(f['freq1_mhz'][:]) if 'freq1_mhz' in f else None
            freq2 = safe_float(f['freq2_mhz'][:]) if 'freq2_mhz' in f else None
    except Exception as e:
        print(f'  [5] Error: {e}')
        return

    fig, ax = plt.subplots(figsize=(10, 5))

    # Group by station
    station_rms = {}
    for i, stn in enumerate(stations):
        if not np.isfinite(rms[i]):
            continue
        if stn not in station_rms:
            station_rms[stn] = []
        station_rms[stn].append(rms[i])

    if not station_rms:
        print('  [5] No valid differential dTEC data')
        plt.close(fig)
        return

    stn_names = sorted(station_rms.keys())
    positions = range(len(stn_names))
    bp_data = [station_rms[s] for s in stn_names]
    bp_colors = [STATION_COLORS.get(s, 'gray') for s in stn_names]

    bp = ax.boxplot(bp_data, positions=list(positions), patch_artist=True,
                    widths=0.6, showfliers=True)
    for patch, color in zip(bp['boxes'], bp_colors):
        patch.set_facecolor(color)
        patch.set_alpha(0.6)

    ax.set_xticks(list(positions))
    ax.set_xticklabels(stn_names, fontsize=12)
    ax.set_ylabel('RMS (TECU)', fontsize=12)
    ax.set_title(f'Differential dTEC — Multi-Frequency Consistency ({date_str})', fontsize=14)
    ax.axhline(0.03, color='red', linestyle='--', alpha=0.5, label='0.03 TECU threshold')
    ax.legend(fontsize=10)
    plt.tight_layout()
    path = outdir / 'fig5_differential_dtec.png'
    fig.savefig(path, dpi=150, bbox_inches='tight')
    plt.close(fig)
    print(f'  [5] {path}')


def fig6_doppler_diurnal(outdir, date_str):
    """24-hour Doppler time series for CHU 7.85 MHz."""
    files = find_h5(PHASE2 / 'CHU_7850' / 'tick_timing', date_str)
    if not files:
        print('  [6] No CHU_7850 tick_timing data found')
        return

    fig, ax = plt.subplots(figsize=(14, 5))

    try:
        with h5py.File(files[0], 'r', locking=False) as f:
            if 'doppler_hz' not in f or 'minute_boundary_utc' not in f:
                print('  [6] Missing doppler_hz or minute_boundary_utc')
                plt.close(fig)
                return
            doppler = safe_float(f['doppler_hz'][:])
            mb = f['minute_boundary_utc'][:].astype(float)
            valid = np.isfinite(doppler) & (mb > 0)
            times = epoch_to_datetime(mb[valid])
            ax.plot(times, doppler[valid], '.', markersize=3, color='#1f77b4', alpha=0.6)
    except Exception as e:
        print(f'  [6] Error: {e}')
        plt.close(fig)
        return

    ax.set_xlabel('UTC', fontsize=12)
    ax.set_ylabel('Doppler shift (Hz)', fontsize=12)
    ax.set_title(f'CHU 7.85 MHz — Doppler Diurnal Signature ({date_str})', fontsize=14)
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
    ax.axhline(0, color='gray', linestyle='--', alpha=0.3)
    plt.tight_layout()
    path = outdir / 'fig6_doppler_diurnal.png'
    fig.savefig(path, dpi=150, bbox_inches='tight')
    plt.close(fig)
    print(f'  [6] {path}')


def fig7_all_arrivals(outdir, date_str):
    """Time-of-flight vs time showing multiple propagation modes."""
    files = find_h5(PHASE2 / 'CHU_7850' / 'all_arrivals', date_str)
    if not files:
        print('  [7] No CHU_7850 all_arrivals data found')
        return

    fig, ax = plt.subplots(figsize=(14, 6))

    try:
        with h5py.File(files[0], 'r', locking=False) as f:
            if 'arrival_ms' not in f or 'minute_boundary_utc' not in f:
                print(f'  [7] Fields: {list(f.keys())[:10]}')
                plt.close(fig)
                return
            arrival = safe_float(f['arrival_ms'][:])
            mb = f['minute_boundary_utc'][:].astype(float)
            snr = safe_float(f['snr_db'][:]) if 'snr_db' in f else np.ones(len(arrival)) * 10

            valid = np.isfinite(arrival) & (mb > 0) & np.isfinite(snr)
            times = epoch_to_datetime(mb[valid])
            arr_valid = arrival[valid]
            snr_valid = snr[valid]

            # Color by SNR
            sc = ax.scatter(times, arr_valid, s=1, c=snr_valid, cmap='viridis',
                           alpha=0.4, vmin=0, vmax=30)
            plt.colorbar(sc, ax=ax, label='SNR (dB)', shrink=0.8)
    except Exception as e:
        print(f'  [7] Error: {e}')
        plt.close(fig)
        return

    ax.set_xlabel('UTC', fontsize=12)
    ax.set_ylabel('Arrival time (ms)', fontsize=12)
    ax.set_title(f'CHU 7.85 MHz — All Arrivals / Mode Timeline ({date_str})\n'
                 f'{np.sum(valid)} arrivals', fontsize=14)
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
    plt.tight_layout()
    path = outdir / 'fig7_all_arrivals.png'
    fig.savefig(path, dpi=150, bbox_inches='tight')
    plt.close(fig)
    print(f'  [7] {path}')


# Template duration per station (ms) — used to normalize correlation SNR
# to a signal-level metric comparable across stations with different tick lengths.
# CHU's 300ms template has ~18 dB more processing gain than WWV's 5ms template.
STATION_TICK_DURATION_MS = {
    'CHU': 300.0,
    'WWV': 5.0,
    'WWVH': 5.0,
    'BPM': 10.0,
}

# Map channel ID prefix to primary station for processing-gain normalization
CHANNEL_PRIMARY_STATION = {
    'CHU_3330':     'CHU',
    'CHU_7850':     'CHU',
    'CHU_14670':    'CHU',
    'SHARED_2500':  'WWV',   # Use WWV as reference for shared channels
    'SHARED_5000':  'WWV',
    'SHARED_10000': 'WWV',
    'SHARED_15000': 'WWV',
    'WWV_20000':    'WWV',
    'WWV_25000':    'WWV',
}


def _processing_gain_db(station: str) -> float:
    """Matched-filter processing gain in dB for a station's tick template.

    Processing gain = 10*log10(N_samples), where N_samples = duration_ms * sample_rate/1000.
    At 24 kHz sample rate: CHU=7200 samples (38.6 dB), WWV=120 (20.8 dB).
    """
    dur_ms = STATION_TICK_DURATION_MS.get(station, 5.0)
    n_samples = dur_ms * 24.0  # 24 kHz sample rate
    return 10.0 * np.log10(max(n_samples, 1.0))


def fig8_detection_summary(outdir, date_str):
    """Tick detection performance summary per channel."""
    channels = []
    ch_ids_found = []
    records = []
    median_edges = []
    median_snr = []

    for ch_id, ch_label in CHANNELS_TICK_TIMING:
        files = find_h5(PHASE2 / ch_id / 'tick_timing', date_str)
        if not files:
            continue
        try:
            with h5py.File(files[0], 'r', locking=False) as f:
                n = len(f[list(f.keys())[0]])
                edges = safe_float(f['n_edges'][:]) if 'n_edges' in f else None
                snr = safe_float(f['mean_snr_db'][:]) if 'mean_snr_db' in f else None

                channels.append(ch_label)
                ch_ids_found.append(ch_id)
                records.append(n)
                median_edges.append(float(np.nanmedian(edges)) if edges is not None else 0)
                median_snr.append(float(np.nanmedian(snr)) if snr is not None else 0)
        except Exception as e:
            print(f'    skip {ch_id}: {e}')

    if not channels:
        print('  [8] No tick_timing data found')
        return

    # Normalize SNR: subtract processing gain to get signal-level SNR/ms
    # This makes CHU (300ms template) comparable to WWV (5ms template)
    normalized_snr = []
    for i, ch_id in enumerate(ch_ids_found):
        station = CHANNEL_PRIMARY_STATION.get(ch_id, 'WWV')
        gain = _processing_gain_db(station)
        normalized_snr.append(median_snr[i] - gain)

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 5))

    cmap = plt.cm.tab10
    bar_colors = [cmap(i / len(channels)) for i in range(len(channels))]

    ax1.barh(channels, records, color=bar_colors, edgecolor='white')
    ax1.set_xlabel('Records/day', fontsize=11)
    ax1.set_title('Tick Timing Records per Channel', fontsize=13)
    for i, v in enumerate(records):
        ax1.text(v + 50, i, str(v), va='center', fontsize=9)

    # Normalized SNR per channel (processing gain removed)
    ax2.barh(channels, normalized_snr, color=bar_colors, edgecolor='white')
    ax2.set_xlabel('Signal-Level SNR (dB)', fontsize=11)
    ax2.set_title('Median Signal SNR per Channel\n'
                  '(correlation SNR − processing gain)', fontsize=12)
    for i, v in enumerate(normalized_snr):
        ax2.text(v + 0.3, i, f'{v:.1f}', va='center', fontsize=9)

    plt.suptitle(f'Detection Performance Summary ({date_str})', fontsize=14, y=1.02)
    plt.tight_layout()
    path = outdir / 'fig8_detection_summary.png'
    fig.savefig(path, dpi=150, bbox_inches='tight')
    plt.close(fig)
    print(f'  [8] {path}')


def fig9_shared_channel_discrimination(outdir, date_str):
    """D_clock by station on SHARED 10 MHz + test signal comparison."""
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(16, 6),
                                    gridspec_kw={'width_ratios': [2, 1]})

    # Left panel: D_clock scatter by station on SHARED_10000
    files = find_h5(PHASE2 / 'SHARED_10000' / 'tick_timing', date_str)
    if not files:
        print('  [9] No SHARED_10000 tick_timing data found')
        plt.close(fig)
        return

    try:
        with h5py.File(files[0], 'r', locking=False) as f:
            dc = safe_float(f['d_clock_ms'][:])
            mb = f['minute_boundary_utc'][:].astype(float)
            stations = [s.decode() if isinstance(s, bytes) else str(s)
                        for s in f['station'][:]]

            for stn in ['WWV', 'WWVH', 'BPM']:
                mask = (np.array([s == stn for s in stations])
                        & np.isfinite(dc) & (mb > 0))
                if np.sum(mask) < 10:
                    continue
                times = epoch_to_datetime(mb[mask])
                med = np.median(dc[mask])
                ax1.scatter(times, dc[mask], s=2, alpha=0.4,
                           color=STATION_COLORS.get(stn, 'gray'),
                           label=f'{stn} (n={np.sum(mask)}, '
                                 f'med={med:+.1f} ms)')
                ax1.axhline(med, color=STATION_COLORS.get(stn, 'gray'),
                           linestyle='--', alpha=0.5)
    except Exception as e:
        print(f'  [9] Error reading tick_timing: {e}')
        plt.close(fig)
        return

    ax1.set_xlabel('UTC', fontsize=12)
    ax1.set_ylabel('D_clock (ms)', fontsize=12)
    ax1.set_title(f'SHARED 10 MHz — Station Separation ({date_str})',
                  fontsize=13)
    ax1.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
    ax1.legend(fontsize=9, markerscale=3, loc='upper right')
    ax1.set_ylim(-15, 15)
    ax1.axhline(0, color='gray', linestyle=':', alpha=0.3)

    # Right panel: Test signal tone power comparison
    ts_files = find_h5(PHASE2 / 'SHARED_10000' / 'test_signal', date_str)
    if ts_files:
        try:
            with h5py.File(ts_files[0], 'r', locking=False) as f:
                stations_ts = [s.decode() if isinstance(s, bytes) else str(s)
                               for s in f['station'][:]]
                freqs_khz = [2, 3, 4, 5]
                field_names = [f'tone_power_{f}khz_db' for f in freqs_khz]

                for stn, color in [('WWV', STATION_COLORS['WWV']),
                                   ('WWVH', STATION_COLORS['WWVH'])]:
                    mask = np.array([s == stn for s in stations_ts])
                    means = []
                    stds = []
                    for fn in field_names:
                        if fn in f:
                            vals = safe_float(f[fn][:])[mask]
                            valid = vals[np.isfinite(vals)]
                            means.append(np.mean(valid) if len(valid) > 0
                                         else np.nan)
                            stds.append(np.std(valid) if len(valid) > 0
                                        else 0)
                        else:
                            means.append(np.nan)
                            stds.append(0)

                    x = np.arange(len(freqs_khz))
                    offset = -0.15 if stn == 'WWV' else 0.15
                    ax2.bar(x + offset, means, 0.3, yerr=stds,
                           color=color, alpha=0.7, label=stn,
                           capsize=3, edgecolor='white')

                ax2.set_xticks(range(len(freqs_khz)))
                ax2.set_xticklabels([f'{f} kHz' for f in freqs_khz],
                                    fontsize=10)
                ax2.set_ylabel('Tone Power (dB)', fontsize=12)
                ax2.set_title('Test Signal Tone Power\n'
                             '(WWV min 8 vs WWVH min 44)', fontsize=13)
                ax2.legend(fontsize=10)
        except Exception as e:
            print(f'  [9] Error reading test_signal: {e}')
            ax2.text(0.5, 0.5, 'No test signal data',
                    transform=ax2.transAxes, ha='center')
    else:
        ax2.text(0.5, 0.5, 'No test signal data',
                transform=ax2.transAxes, ha='center')

    plt.tight_layout()
    path = outdir / 'fig9_shared_channel_discrimination.png'
    fig.savefig(path, dpi=150, bbox_inches='tight')
    plt.close(fig)
    print(f'  [9] {path}')


# ---------- Main ----------

def main():
    parser = argparse.ArgumentParser(description='Generate HamSCI 2026 presentation figures')
    parser.add_argument('--date', default=None,
                        help='Date in YYYYMMDD format (default: yesterday)')
    parser.add_argument('--outdir', default='docs/figures',
                        help='Output directory for figures')
    args = parser.parse_args()

    if args.date:
        date_str = args.date
    else:
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        date_str = yesterday.strftime('%Y%m%d')

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    print(f'Generating figures for {date_str} → {outdir}/')
    print()

    fig1_metrological_ladder(outdir)
    fig2_dclock_timeseries(outdir, date_str)
    fig3_fusion_dclock_histogram(outdir, date_str)
    fig4_dtec_rate_timeseries(outdir, date_str)
    fig5_differential_dtec(outdir, date_str)
    fig6_doppler_diurnal(outdir, date_str)
    fig7_all_arrivals(outdir, date_str)
    fig8_detection_summary(outdir, date_str)
    fig9_shared_channel_discrimination(outdir, date_str)

    print()
    print('Done.')


if __name__ == '__main__':
    main()
