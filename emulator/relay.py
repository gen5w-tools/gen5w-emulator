"""One optional VNC TCP endpoint; guest containers remain networkless."""
import socket
import threading
import sys


def forward(a, b):
    try:
        while data := a.recv(65536):
            b.sendall(data)
    except OSError:
        pass
    finally:
        try:
            b.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def connect(client, path):
    with client, socket.socket(socket.AF_UNIX) as upstream:
        try:
            upstream.connect(path)
            thread = threading.Thread(target=forward, args=(client, upstream))
            thread.start()
            forward(upstream, client)
            thread.join()
        except OSError:
            pass


with socket.socket() as listener:
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # Loopback: the relay is a localhost-only convenience for one user.
    listener.bind(("127.0.0.1", 5900))
    listener.listen(8)
    while True:
        client, _ = listener.accept()
        threading.Thread(target=connect, args=(client, sys.argv[1]), daemon=True).start()
