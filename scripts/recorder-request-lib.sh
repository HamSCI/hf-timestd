# scripts/recorder-request-lib.sh -- shared by pipeline-watchdog.sh and
# hpps-watchdog.sh.  Source it; do not run it.
#
# request_recorder_restart REPORTER CAUSE EVIDENCE
#
# Files a restart request with sigmond-recorder-watchdog, then kicks it.
# Format: sigmond lib/sigmond/recorder_watchdog.py, file_request.  The
# requested_utc must be a JSON number; the watchdog drops a string.
#
# The caller supplies log_warn and log_error, and may set DRY_RUN=true.
# Environment: REQUEST_DIR (default /run/sigmond/recorder-restart),
# REQUEST_DIR_OWNER_UID (default 0; tests override), and
# REQUEST_DIR_INSTALL_OWNER (tests set it empty to skip install's chown).

REQUEST_DIR="${REQUEST_DIR:-/run/sigmond/recorder-restart}"
# Required owner of REQUEST_DIR (tests override; root in production).
REQUEST_DIR_OWNER_UID="${REQUEST_DIR_OWNER_UID:-0}"

request_recorder_restart() {
    local reporter="$1" cause="$2" evidence="$3"
    if [[ "${DRY_RUN:-false}" == "true" ]]; then
        log_warn "[DRY-RUN] Would request recorder restart ($cause): $evidence"
        echo "[DRY-RUN] Would request recorder restart ($cause): $evidence"
        return 0
    fi
    local esc="${evidence//\\/\\\\}"
    esc="${esc//\"/\\\"}"
    local path="$REQUEST_DIR/$reporter.json" tmp
    # The callers run as root.  Write only into a directory that root owns
    # and nobody else can write: a planted symlink or a writable directory
    # would turn the write below into a root-privileged one.
    if [[ -e "$REQUEST_DIR" || -L "$REQUEST_DIR" ]]; then
        local owner perms
        owner=$(stat -c %u "$REQUEST_DIR" 2>/dev/null)
        perms=$(stat -c %a "$REQUEST_DIR" 2>/dev/null)
        if [[ -L "$REQUEST_DIR" || ! -d "$REQUEST_DIR" \
              || "$owner" != "$REQUEST_DIR_OWNER_UID" \
              || -z "$perms" || $(( 8#$perms & 8#022 )) -ne 0 ]]; then
            log_error "refusing to write a request: $REQUEST_DIR is a symlink, not a directory, not owned by uid $REQUEST_DIR_OWNER_UID, or group/world-writable"
            echo "refusing to write a request: $REQUEST_DIR is unsafe"
            return 1
        fi
    else
        mkdir -p "$(dirname "$REQUEST_DIR")" 2>/dev/null
        install -d -m 0755 ${REQUEST_DIR_INSTALL_OWNER--o root -g root} "$REQUEST_DIR" \
            || { log_error "cannot create $REQUEST_DIR"; return 1; }
    fi
    tmp=$(mktemp "$REQUEST_DIR/.$reporter.XXXXXX") \
        || { log_error "cannot create a temp file in $REQUEST_DIR"; return 1; }
    if ! { printf '{"schema": 1, "reporter": "%s", "cause": "%s", "requested_utc": %s, "evidence": "%s", "stopped_recorder": false}\n' \
            "$reporter" "$cause" "$(date +%s.%N)" "$esc" > "$tmp" && mv -f "$tmp" "$path"; }; then
        rm -f "$tmp"
        log_error "cannot write $path"
        return 1
    fi
    log_warn "Requested recorder restart ($cause): $evidence"
    systemctl start --no-block sigmond-recorder-watchdog.service 2>/dev/null || true
}
