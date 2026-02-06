#!/bin/bash
# setup-cpu-affinity.sh - Configure CPU affinity for radiod and hf-timestd
#
# This script:
# 1. Detects L3 cache topology from sysfs (portable across machines)
# 2. Selects the best L3 cache group for radiod (keeps FFT data cache-local)
# 3. Creates a systemd drop-in that RESETS any base-unit affinity before setting ours
# 4. Installs a systemd .path unit to re-apply after ka9q-radio reinstalls
#
# WHY THIS MATTERS:
#   radiod runs 40+ threads doing USB DMA and FFTs. If threads span multiple
#   L3 cache domains, FFT working sets bounce across the interconnect, adding
#   latency and wasting memory bandwidth. Pinning to one L3 group keeps all
#   shared data in a single last-level cache.
#
# RESILIENCE TO ka9q-radio REINSTALLS:
#   The base radiod@.service file is managed by ka9q-radio (wsprdaemon) and
#   may be overwritten at any time with a different CPUAffinity=. Our drop-in
#   uses "CPUAffinity=" (empty) to RESET the base value before setting ours.
#   A systemd .path unit watches for changes to the base service file and
#   triggers a daemon-reload so our drop-in takes effect immediately.
#
# Usage: sudo ./setup-cpu-affinity.sh [radiod-instance-name]
# Example: sudo ./setup-cpu-affinity.sh ac0g-bee1-rx888

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# ============================================================================
# DETECT L3 CACHE TOPOLOGY
# ============================================================================
# Read L3 shared_cpu_list from sysfs to find cache groups.
# Each unique shared_cpu_list represents one L3 cache domain.
# On machines without L3 (or without sysfs), fall back to upper-half heuristic.

detect_l3_groups() {
    local groups=()
    local seen=()

    for cpu_dir in /sys/devices/system/cpu/cpu[0-9]*/; do
        local l3_file="${cpu_dir}cache/index3/shared_cpu_list"
        if [ -f "$l3_file" ]; then
            local group
            group=$(cat "$l3_file")
            # Deduplicate
            local already_seen=false
            for s in "${seen[@]}"; do
                if [ "$s" = "$group" ]; then
                    already_seen=true
                    break
                fi
            done
            if ! $already_seen; then
                seen+=("$group")
                groups+=("$group")
            fi
        fi
    done

    if [ ${#groups[@]} -eq 0 ]; then
        echo ""
        return
    fi

    # Return groups as newline-separated list
    printf '%s\n' "${groups[@]}"
}

# Convert a CPU range like "8-15" or "0-7" to a space-separated list
expand_cpu_range() {
    local range="$1"
    local result=""
    # Handle comma-separated ranges like "0-3,8-11"
    IFS=',' read -ra parts <<< "$range"
    for part in "${parts[@]}"; do
        if [[ "$part" == *-* ]]; then
            local start=${part%-*}
            local end=${part#*-}
            for ((i=start; i<=end; i++)); do
                result="$result $i"
            done
        else
            result="$result $part"
        fi
    done
    echo "$result" | xargs  # trim whitespace
}

# Count CPUs in a range string
count_cpus_in_range() {
    local expanded
    expanded=$(expand_cpu_range "$1")
    echo "$expanded" | wc -w
}

# ============================================================================
# MAIN
# ============================================================================

# Get radiod instance name from argument or detect it
RADIOD_INSTANCE="${1:-}"

if [ -z "$RADIOD_INSTANCE" ]; then
    # Try to detect running radiod instance
    RADIOD_INSTANCE=$(systemctl list-units --type=service --state=running | grep 'radiod@' | sed 's/.*radiod@\([^.]*\).*/\1/' | head -1)
    if [ -z "$RADIOD_INSTANCE" ]; then
        echo -e "${YELLOW}No running radiod instance detected.${NC}"
        echo "Usage: $0 <radiod-instance-name>"
        echo "Example: $0 ac0g-bee1-rx888"
        exit 1
    fi
    echo -e "${GREEN}Detected radiod instance: ${RADIOD_INSTANCE}${NC}"
fi

NPROC=$(nproc)
echo -e "${GREEN}Detected ${NPROC} CPU cores${NC}"

# Detect L3 cache topology
L3_GROUPS=$(detect_l3_groups)

if [ -n "$L3_GROUPS" ]; then
    N_L3=$(echo "$L3_GROUPS" | wc -l)
    echo -e "${GREEN}Detected ${N_L3} L3 cache domain(s):${NC}"
    echo "$L3_GROUPS" | while read -r g; do
        local_count=$(count_cpus_in_range "$g")
        echo "  L3 group: CPUs $g ($local_count cores)"
    done

    if [ "$N_L3" -ge 2 ]; then
        # Multiple L3 domains: pick the LAST one (highest-numbered cores).
        # Rationale: kernel scheduler and short-lived tasks prefer low-numbered
        # cores, so the upper group has less contention for radiod.
        SELECTED_GROUP=$(echo "$L3_GROUPS" | tail -1)
    else
        # Single L3 domain: use all cores (no cache boundary to worry about)
        SELECTED_GROUP=$(echo "$L3_GROUPS" | head -1)
    fi

    RADIOD_CORES=$(expand_cpu_range "$SELECTED_GROUP")
    echo -e "${GREEN}Selected L3 group for radiod: CPUs ${SELECTED_GROUP}${NC}"
else
    # No L3 info in sysfs: fall back to upper-half heuristic
    echo -e "${YELLOW}No L3 cache info in sysfs, falling back to upper-half heuristic${NC}"
    HALF_CORES=$((NPROC / 2))
    RADIOD_CORES=$(seq -s ' ' $HALF_CORES $((NPROC - 1)))
    SELECTED_GROUP="${HALF_CORES}-$((NPROC - 1))"
fi

# ============================================================================
# CREATE SYSTEMD DROP-IN
# ============================================================================
# CRITICAL: The "CPUAffinity=" (empty) line RESETS any affinity set by the
# base radiod@.service unit. Without this, systemd OR's our mask with the
# base unit's mask, causing radiod to span multiple L3 domains.
# This makes us resilient to ka9q-radio reinstalls that change the base unit.

DROPIN_DIR="/etc/systemd/system/radiod@${RADIOD_INSTANCE}.service.d"
echo "Creating drop-in directory: ${DROPIN_DIR}"
mkdir -p "$DROPIN_DIR"

DROPIN_FILE="${DROPIN_DIR}/cpu-affinity.conf"
cat > "$DROPIN_FILE" << EOF
# Auto-generated by hf-timestd setup-cpu-affinity.sh
# Pins radiod to a single L3 cache domain for cache-local FFT processing.
# The empty CPUAffinity= line RESETS any affinity from the base unit file,
# so this works regardless of what ka9q-radio sets in radiod@.service.
#
# L3 topology detected: $(echo "$L3_GROUPS" | tr '\n' ' ')
# Selected group: ${SELECTED_GROUP}
# Generated: $(date -Iseconds)
# Machine: $(hostname) ($(nproc) cores)

[Service]
# Reset any CPUAffinity from the base radiod@.service unit
CPUAffinity=
# Pin to L3 cache group: CPUs ${SELECTED_GROUP}
CPUAffinity=${RADIOD_CORES}

# High priority for real-time audio processing
Nice=-15
CPUSchedulingPolicy=fifo
CPUSchedulingPriority=80
EOF

echo -e "${GREEN}Created ${DROPIN_FILE}${NC}"

# ============================================================================
# INSTALL SYSTEMD .PATH WATCHER
# ============================================================================
# Watches for changes to the base radiod@.service file (ka9q-radio reinstalls)
# and triggers a daemon-reload so our drop-in override takes effect immediately.

WATCHER_PATH_FILE="/etc/systemd/system/timestd-radiod-affinity.path"
WATCHER_SERVICE_FILE="/etc/systemd/system/timestd-radiod-affinity.service"

cat > "$WATCHER_PATH_FILE" << 'EOF'
# Watches for ka9q-radio reinstalls that overwrite radiod@.service.
# When the file changes, triggers a daemon-reload so our cpu-affinity
# drop-in override takes effect on the next radiod restart.
# Installed by: hf-timestd setup-cpu-affinity.sh

[Unit]
Description=Watch radiod@.service for ka9q-radio reinstalls

[Path]
PathModified=/etc/systemd/system/radiod@.service
Unit=timestd-radiod-affinity.service

[Install]
WantedBy=multi-user.target
EOF

cat > "$WATCHER_SERVICE_FILE" << EOF
# Triggered when radiod@.service is modified (ka9q-radio reinstall).
# Re-runs daemon-reload and logs a notice.
# Installed by: hf-timestd setup-cpu-affinity.sh

[Unit]
Description=Re-apply radiod CPU affinity after ka9q-radio reinstall

[Service]
Type=oneshot
ExecStart=/bin/systemctl daemon-reload
ExecStart=/bin/bash -c 'echo "radiod@.service was modified (ka9q-radio reinstall?). Ran daemon-reload. hf-timestd cpu-affinity drop-in will apply on next radiod restart." | systemd-cat -t timestd-affinity -p notice'
EOF

echo -e "${GREEN}Created watcher: ${WATCHER_PATH_FILE}${NC}"

# Reload and enable the watcher
systemctl daemon-reload
systemctl enable --now timestd-radiod-affinity.path 2>/dev/null || true

# ============================================================================
# UPDATE TEMPLATE DROP-IN
# ============================================================================
# Also update the template file in the repo for reference
TEMPLATE_FILE="$(dirname "$0")/../systemd/radiod-cpu-affinity.conf"
if [ -d "$(dirname "$TEMPLATE_FILE")" ]; then
    cat > "$TEMPLATE_FILE" << EOF
# Drop-in configuration for radiod CPU affinity
# Install to: /etc/systemd/system/radiod@INSTANCE.service.d/cpu-affinity.conf
#
# This configuration pins radiod to a single L3 cache domain to:
# 1. Keep FFT working sets in one last-level cache (no cross-domain bouncing)
# 2. Isolate real-time USB/FFT processing from other system tasks
# 3. Allow Linux scheduler to distribute 40+ radiod threads efficiently
#
# The empty CPUAffinity= line RESETS any affinity from the base unit,
# making this resilient to ka9q-radio reinstalls.
#
# Generated by: scripts/setup-cpu-affinity.sh
# Actual core assignment is machine-specific (detected from sysfs L3 topology).

[Service]
# Reset any CPUAffinity from the base radiod@.service unit
CPUAffinity=
# Pin to detected L3 cache group (example: upper half of 16-core system)
CPUAffinity=${RADIOD_CORES}

# Ensure high priority for real-time audio processing
Nice=-15
CPUSchedulingPolicy=fifo
CPUSchedulingPriority=80
EOF
    echo -e "${GREEN}Updated template: ${TEMPLATE_FILE}${NC}"
fi

# ============================================================================
# SUMMARY
# ============================================================================
echo ""
echo -e "${GREEN}CPU affinity configured successfully!${NC}"
echo ""
echo "  radiod pinned to: CPUs ${SELECTED_GROUP} (single L3 cache domain)"
echo "  Drop-in: ${DROPIN_FILE}"
echo "  Watcher: ${WATCHER_PATH_FILE} (re-applies after ka9q-radio reinstalls)"
echo ""
echo "To apply now, restart radiod:"
echo "  sudo systemctl restart radiod@${RADIOD_INSTANCE}"
echo ""
echo "To verify after restart:"
echo "  taskset -cp \$(pgrep -x radiod)"
echo ""
echo "hf-timestd services are configured with Nice=5 to yield to radiod."
