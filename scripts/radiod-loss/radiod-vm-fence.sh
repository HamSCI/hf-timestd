#!/bin/bash
# Fence a VM's non-vCPU qemu threads onto $2, leaving the pinned vCPU threads
# alone, and hold KSM off. Re-applies every $3 seconds.
#   $1 = VMID   $2 = target CPU list   $3 = interval seconds
set -u
VMID=${1:-100}; TARGET=${2:-14-15}; IVAL=${3:-30}
log(){ echo "$(date -u +%FT%TZ) $*"; }
log "fencing VM $VMID non-vCPU threads -> $TARGET every ${IVAL}s"
while true; do
  P=$(qm list 2>/dev/null | awk -v v="$VMID" '$1==v{print $6}')
  if [ -n "${P:-}" ] && [ -d "/proc/$P" ]; then
    n=0
    for d in /proc/$P/task/*/; do
      t=$(basename "$d")
      nm=$(grep '^Name:' "$d/status" 2>/dev/null | cut -f2)
      case "$nm" in "CPU "*) continue;; esac        # leave pinned vCPUs alone
      cur=$(taskset -p "$t" 2>/dev/null | sed 's/.*: //')
      [ -z "$cur" ] && continue
      # only touch threads that can still reach cpu0/cpu1 (mask bits 0,1)
      if [ $(( 16#$cur & 3 )) -ne 0 ]; then
        taskset -pc "$TARGET" "$t" >/dev/null 2>&1 && n=$((n+1))
      fi
    done
    [ "$n" -gt 0 ] && log "re-fenced $n newly-unfenced thread(s)"
  fi
  [ "$(cat /sys/kernel/mm/ksm/run 2>/dev/null)" = "1" ] && { echo 0 > /sys/kernel/mm/ksm/run; log "KSM re-disabled"; }
  sleep "$IVAL"
done
