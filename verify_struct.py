
import ctypes
import struct

# Define the C struct equivalent using ctypes
class SHMTime(ctypes.Structure):
    _fields_ = [
        ("mode", ctypes.c_int),                 # 0
        ("count", ctypes.c_int),                # 4
        ("clockTimeStampSec", ctypes.c_long),   # 8  (time_t is long on 64-bit Linux)
        ("clockTimeStampUSec", ctypes.c_int),   # 16
        # PADDING 4 BYTES HERE (20-23)
        ("receiveTimeStampSec", ctypes.c_long), # 24
        ("receiveTimeStampUSec", ctypes.c_int), # 32
        # NO PADDING SHOULD BE HERE
        ("leap", ctypes.c_int),                 # 36
        ("precision", ctypes.c_int),            # 40
        ("nsamples", ctypes.c_int),             # 44
        ("valid", ctypes.c_int),                # 48
        ("clockTimeStampNSec", ctypes.c_uint),  # 52
        ("receiveTimeStampNSec", ctypes.c_uint),# 56
        ("dummy", ctypes.c_int * 8)             # 60
    ]

print(f"Size of SHMTime: {ctypes.sizeof(SHMTime)}")
print(f"Offset of mode: {SHMTime.mode.offset}")
print(f"Offset of count: {SHMTime.count.offset}")
print(f"Offset of clockTimeStampSec: {SHMTime.clockTimeStampSec.offset}")
print(f"Offset of clockTimeStampUSec: {SHMTime.clockTimeStampUSec.offset}")
print(f"Offset of receiveTimeStampSec: {SHMTime.receiveTimeStampSec.offset}")
print(f"Offset of receiveTimeStampUSec: {SHMTime.receiveTimeStampUSec.offset}")
print(f"Offset of leap: {SHMTime.leap.offset} (Expected 36)")
print(f"Offset of precision: {SHMTime.precision.offset} (Expected 40)")
print(f"Offset of nsamples: {SHMTime.nsamples.offset} (Expected 44)")
print(f"Offset of valid: {SHMTime.valid.offset} (Expected 48)")

print("-" * 20)
print("Testing struct pack format:")
# Proposed fix format (now the current format)
# @ii q i 4x q i iiii II iiiiiiii 4x
current_fmt = '@ii q i 4x q i iiii II iiiiiiii 4x'
try:
    packed = struct.pack(current_fmt, 
        0, 0, 0, 0, 
        0, 0, 
        0, 0, 0, 0, 
        0, 0, 0, 0, 0, 0, 0, 0, 0, 0
    ) 
    print(f"Current format size: {struct.calcsize(current_fmt)}")
    # Where does leap start? 
    # ii (8) + q (8) + i (4) + 4x (4) + q (8) + i (4) + 4x (4)
    # 8 + 8 + 4 + 4 + 8 + 4 + 4 = 40
    print(f"Current format places 'leap' at offset: {struct.calcsize('@ii q i 4x q i 4x')}")
except Exception as e:
    print(f"Pack error: {e}")

print("-" * 20)
# Proposed fix format
# @ii q i 4x q i iiii II iiiiiiii
# Removing the second 4x
proposed_fmt = '@ii q i 4x q i iiii II iiiiiiii'
print(f"Proposed format size: {struct.calcsize(proposed_fmt)}")
print(f"Proposed format places 'leap' at offset: {struct.calcsize('@ii q i 4x q i')}")
