#!/usr/bin/env python3
import struct
import sysv_ipc
import time
import math

SHM_KEY = 0x4e545030
SHM_SIZE = 96

def get_shm():
    try:
        return sysv_ipc.SharedMemory(SHM_KEY, flags=0, size=SHM_SIZE)
    except Exception as e:
        print(f"Error connecting: {e}")
        return None

def feed_forever():
    shm = get_shm()
    if not shm:
        return

    print("Feeding Chrony SHM every 1s...")
    count = 0
    
    while True:
        now = time.time()
        # Create a perfect zero-offset sample
        # Ref time = System time
        ref_time = now
        sys_time = now
        
        r_sec = int(ref_time)
        r_usec = int((ref_time - r_sec) * 1e6)
        r_nsec = int((ref_time - r_sec) * 1e9)
        
        s_sec = int(sys_time)
        s_usec = int((sys_time - s_sec) * 1e6)
        s_nsec = int((sys_time - s_sec) * 1e9)
        
        # Mode 0: valid 0 -> write -> valid 1
        pack_fmt = '@ii q i 4x q i 4x iiii II iiiiiiii'
        
        # 1. Set valid=0
        # We can just write the whole struct with valid=0, then valid=1
        # Or just write once with valid=1 if we don't care about race for this test
        
        # count increments
        count += 1
        
        # Data with valid=1
        data = struct.pack(
            pack_fmt,
            0, count,           # mode=0, count
            r_sec, r_usec,      # clock (ref)
            s_sec, s_usec,      # recv (sys)
            0, -10, 1, 1,       # leap, prec, nsamples=1, valid=1
            r_nsec, s_nsec,     # nsecs
            0,0,0,0,0,0,0,0
        )
        
        shm.write(data, 0)
        print(f"Wrote update {count}: t={now:.3f}")
        
        # Check if cleared
        time.sleep(0.5)
        rb = shm.read(96, 0)
        u = struct.unpack(pack_fmt, rb)
        print(f"  Readback: nsamples={u[8]} valid={u[9]}")
        
        time.sleep(0.5)

if __name__ == "__main__":
    feed_forever()
