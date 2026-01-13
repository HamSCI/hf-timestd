# Diagnostics for Recorder

## Issues

1. **Health Check Timeout**: Fixed logically (extension mismatch), but likely will fail due to stale data.
2. **Stale Data**: Last file mod time is 14:55 UTC (current time 23:25 UTC). Files are ~8.5 hours old.
3. **Logs**: Show "RTP-to-Unix reference LOCKED", "Core recorder running".

## Potential Causes

- **Timestamp Drift**: If system time jumped, recorder might be waiting for future packets?
- **RTP Stream**: Packets arriving but discarded?
- **Writer Thread**: Crashed silent?

## Next Steps

1. Check logs since restart.
2. Check `radiod` status (is it sending *current* timestamps?).
3. Check `verify_pipeline.sh` again to confirm if files are appearing now.
