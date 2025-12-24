#!/usr/bin/env python3
"""
Configure ZED-F9P to enable UBX-NAV-SAT via USB

This script enables UBX-NAV-SAT messages on UART1 (GPIO pins)
while connected via USB for configuration.
"""

from serial import Serial
from pyubx2 import UBXMessage, SET
import time

# Find USB serial port
# Common: /dev/ttyACM0 on Linux, COM3 on Windows
USB_PORT = '/dev/ttyACM0'
BAUD_RATE = 38400

print("ZED-F9P Configuration via USB")
print("=" * 50)
print(f"Port: {USB_PORT}")
print(f"Baud: {BAUD_RATE}\n")

try:
    # Connect to ZED-F9P via USB
    print("Connecting to ZED-F9P...")
    ser = Serial(USB_PORT, BAUD_RATE, timeout=5)
    print("Connected!\n")
    
    # 1. Enable UBX-NAV-SAT on UART1 (your GPIO connection)
    print("1. Enabling UBX-NAV-SAT on UART1...")
    msg = UBXMessage('CFG', 'CFG-MSG', SET,
        msgClass=0x01,    # NAV
        msgID=0x35,       # SAT
        rateDDC=0,        # I2C: off
        rateUART1=1,      # UART1 (GPIO): every epoch
        rateUART2=0,      # UART2: off
        rateUSB=0,        # USB: off (don't flood USB)
        rateSPI=0         # SPI: off
    )
    ser.write(msg.serialize())
    time.sleep(0.5)
    
    # Read ACK/NAK
    response = ser.read(100)
    if b'\xb5\x62\x05\x01' in response:
        print("   ✅ ACK received - NAV-SAT enabled on UART1")
    elif b'\xb5\x62\x05\x00' in response:
        print("   ❌ NAK received - command rejected")
    else:
        print("   ⚠️  No ACK/NAK received")
    
    # 2. Also enable on USB for testing (optional)
    print("\n2. Enabling UBX-NAV-SAT on USB (for testing)...")
    msg = UBXMessage('CFG', 'CFG-MSG', SET,
        msgClass=0x01,
        msgID=0x35,
        rateDDC=0,
        rateUART1=1,      # Keep UART1 enabled
        rateUART2=0,
        rateUSB=1,        # Also enable on USB
        rateSPI=0
    )
    ser.write(msg.serialize())
    time.sleep(0.5)
    
    response = ser.read(100)
    if b'\xb5\x62\x05\x01' in response:
        print("   ✅ ACK received - NAV-SAT also on USB")
    
    # 3. Save configuration to flash (persists after power cycle)
    print("\n3. Saving configuration to flash...")
    # Use raw UBX command for CFG-CFG (simpler than pyubx2 API)
    # Format: B5 62 06 09 0D 00 00 00 00 00 FF FF 00 00 00 00 00 00 17
    # clearMask=0x00000000, saveMask=0x0000FFFF, loadMask=0x00000000, deviceMask=0x17
    cfg_cfg_raw = bytes([
        0xB5, 0x62,  # Sync
        0x06, 0x09,  # CFG-CFG
        0x0D, 0x00,  # Length = 13
        0x00, 0x00, 0x00, 0x00,  # clearMask (4 bytes)
        0xFF, 0xFF, 0x00, 0x00,  # saveMask (4 bytes) - save all
        0x00, 0x00, 0x00, 0x00,  # loadMask (4 bytes)
        0x17,                     # deviceMask (1 byte) - BBR, Flash, EEPROM
        0x8E, 0x75               # Checksum (calculated)
    ])
    ser.write(cfg_cfg_raw)
    time.sleep(1.0)
    
    response = ser.read(100)
    if b'\xb5\x62\x05\x01' in response:
        print("   ✅ Configuration saved to flash!")
    else:
        print("   ⚠️  Save status unknown")
    
    print("\n" + "=" * 50)
    print("✅ Configuration complete!")
    print("\nNext steps:")
    print("  1. Disconnect USB")
    print("  2. ZED-F9P will continue outputting UBX on UART1")
    print("  3. Run: python3 scripts/zedf9p_tec_client.py")
    print("  4. Your TCP stream should now have UBX-NAV-SAT data")
    
    ser.close()

except FileNotFoundError:
    print(f"\n❌ Error: Serial port {USB_PORT} not found")
    print("\nTry:")
    print("  ls /dev/ttyACM* /dev/ttyUSB*")
    print("  or check Device Manager on Windows")

except PermissionError:
    print(f"\n❌ Error: Permission denied for {USB_PORT}")
    print("\nTry:")
    print(f"  sudo chmod 666 {USB_PORT}")
    print(f"  or add user to dialout group: sudo usermod -a -G dialout $USER")

except Exception as e:
    print(f"\n❌ Error: {e}")
    import traceback
    traceback.print_exc()
