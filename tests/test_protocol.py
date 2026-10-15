import json
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import unittest
import zlib

from emulator.cli import png_from_ppm
from emulator.runtime import SerialBroker
from emulator.protocol import qmp


class ProtocolTests(unittest.TestCase):
    def test_guest_echo_does_not_match_markers_and_exit_code_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            host, guest = socket.socketpair()
            broker = SerialBroker(host, Path(temp))
            reader = threading.Thread(target=broker.read)
            reader.start()
            def shell():
                line = guest.recv(65536)
                guest.sendall(line)  # terminal echo, including marker fragments
                # The VM need not have the guest's BusyBox at /sbin/busybox.
                program = line.decode().strip().replace("/sbin/busybox printf", "printf")
                result = subprocess.run(["sh", "-c", program], capture_output=True)
                guest.sendall(result.stdout)
            worker = threading.Thread(target=shell)
            worker.start()
            reply = broker.execute("printf 'hello\\n'; exit 7", 5)
            self.assertEqual(reply, {"stdout": "hello\n", "exit_code": 7})
            worker.join()
            guest.close()
            reader.join()
            host.close()

    def test_timeout_is_explicit(self):
        with tempfile.TemporaryDirectory() as temp:
            host, guest = socket.socketpair()
            broker = SerialBroker(host, Path(temp))
            reply = broker.execute("sleep 99", 0.01)
            self.assertEqual(reply["exit_code"], 124)
            self.assertIn("timed out", reply["error"])
            guest.close(); host.close()

    def test_qmp_skips_events_and_propagates_errors(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "qmp.sock")
            with socket.socket(socket.AF_UNIX) as server:
                server.bind(path); server.listen()
                def respond():
                    client, _ = server.accept()
                    with client, client.makefile("rwb") as stream:
                        stream.write(b'{"QMP":{}}\n'); stream.flush()
                        json.loads(stream.readline())
                        stream.write(b'{"event":"TEST"}\n{"return":{}}\n'); stream.flush()
                        json.loads(stream.readline())
                        stream.write(b'{"error":{"desc":"deliberate failure"}}\n'); stream.flush()
                worker = threading.Thread(target=respond); worker.start()
                with self.assertRaisesRegex(RuntimeError, "deliberate failure"):
                    qmp(path, "screendump")
                worker.join()

    def test_ppm_preserves_whitespace_valued_first_pixel(self):
        with tempfile.TemporaryDirectory() as temp:
            source, target = Path(temp) / "a.ppm", Path(temp) / "a.png"
            pixels = b"\n\r\x20\xff\0\x80"
            source.write_bytes(b"P6\n# comment\n2 1\n255\n" + pixels)
            png_from_ppm(source, target)
            png = target.read_bytes()
            self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
            offset = png.index(b"IDAT")
            size = int.from_bytes(png[offset-4:offset], "big")
            self.assertEqual(zlib.decompress(png[offset+4:offset+4+size]), b"\0" + pixels)


if __name__ == "__main__":
    unittest.main()
