
import sysv_ipc
import struct
import time
import datetime

KEY = 0x4e545030
SIZE = 96

try:
    shm = sysv_ipc.SharedMemory(KEY, size=SIZE)
    data = shm.read(SIZE)
    
    # Unpack
    # @ii q i 4x q i 4x iiii II iiiiiiii
    fmt = '@ii q i 4x q i 4x iiii II 8i'
    # Use 8i for dummy dict
    
    unpacked = struct.unpack(fmt, data)
    
    mode = unpacked[0]
    count = unpacked[1]
    clock_sec = unpacked[2]
    clock_usec = unpacked[3]
    recv_sec = unpacked[4]
    recv_usec = unpacked[5]
    leap = unpacked[6]
    precision = unpacked[7]
    nsamples = unpacked[8]
    valid = unpacked[9]
    clock_nsec = unpacked[10]
    recv_nsec = unpacked[11]
    
    print(f"SHM Key: 0x{KEY:x}")
    print(f"Mode: {mode}")
    print(f"Count: {count}")
    print(f"Valid: {valid}")
    print(f"Precision: {precision}")
    print(f"Leap: {leap}")
    
    print(f"Clock Time (Ref): {datetime.datetime.fromtimestamp(clock_sec)}.{clock_usec:06d}")
    print(f"Recv Time (Sys):  {datetime.datetime.fromtimestamp(recv_sec)}.{recv_usec:06d}")
    print(f"NSec: Clock={clock_nsec}, Recv={recv_nsec}")

    now = time.time()
    age = now - recv_sec
    print(f"Age: {age:.2f} seconds")
    
except Exception as e:
    print(f"Error: {e}")
