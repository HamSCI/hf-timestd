
import inspect
import hf_timestd.core.core_recorder_v2 as cr
import sys

print(f"Python executable: {sys.executable}")
print(f"Module file: {cr.__file__}")


try:
    src = inspect.getsource(cr.CoreRecorderV2._resolve_channels_and_cleanup)
    if "listen_duration=2.5" in src:
        print("Verdict: NEW CODE (2.5s timeout found)")
    else:
        print(f"Verdict: OLD CODE (timeout not found: {src[:100]}...)")

    if "DEBUG VERSION" in inspect.getsource(cr.CoreRecorderV2.run):
        print("Verdict: DEBUG VERSION FOUND")
    else:
        print("Verdict: DEBUG VERSION NOT FOUND")


except Exception as e:
    print(f"Error inspecting source: {e}")
