# shellcheck shell=bash
# hf-timestd's chrony refclock declaration, installed and converged.
#
# Sourced by scripts/install.sh (Phase 6).  Kept in its own file so a test
# can run it against a scratch chrony tree (tests/test_fuse_retired.py):
# the installer as a whole wants root and a real host.
#
# The FUSE refclock (SHM 1) RETIRED on 2026-10-22 (review-fusion-kalman-
# 20261022.md section 15, ruling 5).  Fusion fed it the calibrated D_clock,
# which the calibration pulls to zero, so FUSE told chrony "the host is
# right" whatever the host did.  Fusion no longer writes SHM 1, and this
# function removes every FUSE refclock line it finds -- in chrony.conf and
# in conf.d -- rather than leaving an old one behind.  An old line without
# `noselect` would otherwise survive every reinstall and could vote.
#
# HPPS (SHM 2, T6) stays exactly as shipped.
#
# chronyd reads refclocks only at start.  A removed line leaves chronyd's
# running FUSE source in place, unfed and `noselect`, until chrony next
# restarts; chrony is shared infrastructure, so this function never
# restarts it.

_tcr_info() { if declare -F log_info >/dev/null; then log_info "$@"; else echo "[INFO] $*"; fi; }
_tcr_warn() { if declare -F log_warn >/dev/null; then log_warn "$@"; else echo "[WARN] $*" >&2; fi; }

# A live (uncommented) refclock line naming refid FUSE.
TCR_FUSE_RE='^[[:space:]]*refclock[[:space:]].*refid[[:space:]]+FUSE([[:space:]]|$)'

# _tcr_strip_fuse FILE: remove FUSE refclock lines from FILE, keeping a
# backup beside it.  The backup's name does not end in .conf, so chrony's
# confdir never loads it.
_tcr_strip_fuse() {
    local f="$1" stamp
    [[ -f "$f" ]] || return 0
    grep -qE "$TCR_FUSE_RE" "$f" || return 0
    stamp=$(date -u +%Y%m%dT%H%M%SZ)
    cp -p "$f" "$f.bak-fuse-retired-$stamp"
    sed -i -E "/$TCR_FUSE_RE/d" "$f"
    _tcr_warn "Removed the retired FUSE refclock from $f (backup: $f.bak-fuse-retired-$stamp)." \
              "chronyd keeps its running FUSE source, unfed, until chrony next restarts."
}

# timestd_install_chrony_refclocks CHRONY_CONF CONF_D SRC_CONF
#   CHRONY_CONF  chrony's main file (/etc/chrony/chrony.conf)
#   CONF_D       its drop-in directory (/etc/chrony/conf.d); may not exist
#   SRC_CONF     the shipped declaration (config/chrony-timestd-refclocks.conf)
timestd_install_chrony_refclocks() {
    local chrony_conf="$1" conf_d="$2" src="$3" f n_hpps

    # 1. FUSE retired: no live FUSE line may stay in chrony.conf, on either
    #    branch below.  Before 2026-10-22 a host without conf.d skipped any
    #    chrony.conf that already named FUSE and so kept whatever line it had.
    _tcr_strip_fuse "$chrony_conf"

    if [[ -d "$conf_d" ]]; then
        install -m 0644 "$src" "$conf_d/timestd-refclocks.conf"
        # The drop-in is installed under a name that DIFFERS from the source
        # file.  Older installs placed it under the SOURCE name, and chrony
        # includes conf.d/*.conf -- so leaving that copy behind defines every
        # refclock TWICE.  The duplicates contend for the same SHM unit
        # (chrony clears `valid` on read, so whichever instance polls first
        # consumes the sample) and sit at reach 0 forever.  hf-timestd#17.
        if [[ -e "$conf_d/chrony-timestd-refclocks.conf" ]]; then
            mv -f "$conf_d/chrony-timestd-refclocks.conf" \
                  "$conf_d/chrony-timestd-refclocks.conf.legacy-disabled"
            _tcr_warn "Disabled legacy duplicate refclock drop-in (chrony-timestd-refclocks.conf)"
        fi
        # 2. Any other drop-in that still declares FUSE loses the line too.
        for f in "$conf_d"/*.conf; do
            [[ -e "$f" ]] || continue
            _tcr_strip_fuse "$f"
        done
        # 3. Converge to exactly one HPPS definition, or say so.  A silent
        #    duplicate is what made this cost a session to find.
        # `|| true`: install.sh runs under `set -euo pipefail`, and grep
        # exits 1 when no file matches -- the very case worth warning about.
        n_hpps=$({ grep -lE '^[[:space:]]*refclock[[:space:]].*refid[[:space:]]+HPPS' \
                   "$conf_d"/*.conf 2>/dev/null || true; } | wc -l)
        if [[ "$n_hpps" -ne 1 ]]; then
            _tcr_warn "chrony conf.d defines refid HPPS in $n_hpps files (expected 1) — chrony will create duplicate, permanently unreachable sources"
        fi
        _tcr_info "Chrony HPPS refclock installed (conf.d); FUSE retired"
    elif ! grep -qE '^[[:space:]]*refclock[[:space:]].*refid[[:space:]]+HPPS' "$chrony_conf" 2>/dev/null \
         && ! grep -qF "include $src" "$chrony_conf" 2>/dev/null; then
        echo "include $src" >> "$chrony_conf"
        _tcr_info "Chrony HPPS refclock included in $chrony_conf"
    fi
}
