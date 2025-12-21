
import socket

def get_host_ip():
    """
    Detect the IP usage of the main network interface.
    This works by creating a dummy UDP socket and 'connecting' to a public DNS.
    It doesn't actually send packets.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # unexpected address 10.255.255.255 to not trigger any route?
        # Standard trick: connect to Google DNS
        s.connect(('8.8.8.8', 1))
        IP = s.getsockname()[0]
    except Exception:
        IP = '127.0.0.1'
    finally:
        s.close()
    return IP

if __name__ == '__main__':
    print(get_host_ip())
