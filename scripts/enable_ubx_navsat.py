#!/usr/bin/env python3
"""
Enable UBX-NAV-SAT on ZED-F9P

Sends UBX configuration commands to enable NAV-SAT message output.

Usage:
    python3 enable_ubx_navsat.py --host 192.168.0.202 --port 2000
"""

import socket
import struct
import time
import argparse


def calculate_checksum(msg_class: int, msg_id: int, payload: bytes) -> tuple:
    """Calculate UBX checksum."""
    data = bytes([msg_class, msg_id]) + struct.pack('<H', len(payload)) + payload
    ck_a = 0
    ck_b = 0
    for byte in data:
        ck_a = (ck_a + byte) & 0xFF
        ck_b = (ck_b + ck_a) & 0xFF
    return ck_a, ck_b


def create_ubx_message(msg_class: int, msg_id: int, payload: bytes) -> bytes:
    """Create complete UBX message with header and checksum."""
    ck_a, ck_b = calculate_checksum(msg_class, msg_id, payload)
    
    msg = bytes([0xB5, 0x62])  # Sync chars
    msg += bytes([msg_class, msg_id])
    msg += struct.pack('<H', len(payload))
    msg += payload
    msg += bytes([ck_a, ck_b])
    
    return msg


def enable_nav_sat(sock: socket.socket):
    """
    Enable UBX-NAV-SAT message.
    
    Sends CFG-MSG command to enable NAV-SAT on current port.
    """
    # CFG-MSG (0x06 0x01) - Configure message rate
    # Payload:
    #   msgClass: 0x01 (NAV)
    #   msgID: 0x35 (SAT)
    #   rate: 1 (output every measurement epoch)
    
    payload = bytes([
        0x01,  # msgClass (NAV)
        0x35,  # msgID (SAT)
        1      # rate (1 = every epoch)
    ])
    
    msg = create_ubx_message(0x06, 0x01, payload)
    
    print("Sending CFG-MSG to enable UBX-NAV-SAT...")
    print(f"  Command: {msg.hex()}")
    
    sock.sendall(msg)
    time.sleep(0.5)
    
    # Read ACK
    try:
        response = sock.recv(1024)
        if len(response) >= 10:
            if response[0] == 0xB5 and response[1] == 0x62:
                if response[2] == 0x05 and response[3] == 0x01:
                    print("  ✅ ACK received - NAV-SAT enabled!")
                    return True
                elif response[2] == 0x05 and response[3] == 0x00:
                    print("  ❌ NAK received - command rejected")
                    return False
    except socket.timeout:
        print("  ⚠️  No ACK received (timeout)")
    
    return False


def poll_nav_sat(sock: socket.socket):
    """Poll for NAV-SAT message."""
    # UBX-NAV-SAT poll (empty payload)
    msg = create_ubx_message(0x01, 0x35, b'')
    
    print("\nPolling for NAV-SAT message...")
    sock.sendall(msg)
    time.sleep(1.0)
    
    # Try to read response
    try:
        data = sock.recv(4096)
        print(f"  Received {len(data)} bytes")
        
        # Look for NAV-SAT response (0xB5 0x62 0x01 0x35)
        for i in range(len(data) - 4):
            if (data[i] == 0xB5 and data[i+1] == 0x62 and 
                data[i+2] == 0x01 and data[i+3] == 0x35):
                print(f"  ✅ Found NAV-SAT at offset {i}")
                length = struct.unpack('<H', data[i+4:i+6])[0]
                print(f"  Message length: {length} bytes")
                return True
        
        print("  ❌ No NAV-SAT message found in response")
        return False
    
    except socket.timeout:
        print("  ⚠️  No response (timeout)")
        return False


def main():
    parser = argparse.ArgumentParser(description='Enable UBX-NAV-SAT on ZED-F9P')
    parser.add_argument('--host', default='192.168.0.202', help='GPS receiver IP')
    parser.add_argument('--port', type=int, default=2000, help='GPS receiver port')
    
    args = parser.parse_args()
    
    print("ZED-F9P UBX-NAV-SAT Configuration")
    print("=" * 50)
    print(f"Target: {args.host}:{args.port}\n")
    
    # Connect
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(5.0)
    
    try:
        print(f"Connecting to {args.host}:{args.port}...")
        sock.connect((args.host, args.port))
        print("Connected!\n")
        
        # Enable NAV-SAT
        if enable_nav_sat(sock):
            # Poll to verify
            time.sleep(1)
            if poll_nav_sat(sock):
                print("\n✅ SUCCESS: UBX-NAV-SAT is now enabled!")
                print("\nYou can now run:")
                print("  python3 scripts/zedf9p_tec_client.py")
            else:
                print("\n⚠️  NAV-SAT enabled but not receiving data yet")
                print("Try running zedf9p_tec_client.py in a few seconds")
        else:
            print("\n❌ Failed to enable NAV-SAT")
            print("\nTroubleshooting:")
            print("  1. Check if receiver supports UBX protocol")
            print("  2. Try configuring via u-center software")
            print("  3. Check receiver firmware version")
    
    except Exception as e:
        print(f"\n❌ Error: {e}")
        return 1
    
    finally:
        sock.close()
    
    return 0


if __name__ == '__main__':
    exit(main())
