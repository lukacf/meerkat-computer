"""Static Incus-exec helper. Its stdin is private; it never prints input or errors."""
import http.client
import socket
import sys


def main():
    if len(sys.argv) not in (2, 3):
        return 2
    path = sys.argv[1]
    if len(sys.argv) == 2:
        endpoint = "/v1/secure-input"
    elif len(sys.argv) == 3 and sys.argv[2] == "fork_control":
        endpoint = "/v1/fork-control"
    else:
        return 2
    payload = sys.stdin.buffer.read(65537)
    if len(payload) > 65536:
        return 2

    class LocalConnection(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(self.timeout)
            self.sock.connect(path)

    connection = LocalConnection("localhost", timeout=180 if endpoint == "/v1/fork-control" else 30)
    try:
        connection.request("POST", endpoint, body=payload,
                           headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        receipt = response.read(1024 * 1024 + 1)
        if len(receipt) > 1024 * 1024:
            return 2
        sys.stdout.buffer.write(receipt)
        return 0
    except Exception:  # noqa: BLE001 - no secret-bearing diagnostics cross the pipe
        return 2
    finally:
        connection.close()


if __name__ == "__main__":
    sys.exit(main())
