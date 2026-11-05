"""Device tunnels (app/tunnels.py on the site, hub/app/tunnels.py on the hub):
a relayed session must survive quiet spells on either side and must end when
its tunnel is closed.

Found 2026-11-05 at Tri-Lectro: WinBox to the MikroTik kept dropping through
the hub. The relay's 10 s CONNECT timeout stayed on the device socket as a read
timeout (a device quiet for 10 s was cut), and on the site the accepted socket
inherited the process-wide socket.setdefaulttimeout(3) from the scanner's DNS
check (an operator quiet for 3 s half-closed the device side).

The same file tests both relays (each is `tunnels` on /app). Everything runs on
127.0.0.1, so no network is needed:
  docker run --rm --network none -e PYTHONDONTWRITEBYTECODE=1 \\
    -v "$PWD/app:/app:ro" -v "$PWD/tests:/tests:ro" \\
    --entrypoint python farm-netwatch:netcfg /tests/test_tunnels.py -v
  docker run --rm --network none -e PYTHONDONTWRITEBYTECODE=1 \\
    -v "$PWD/hub/app:/app:ro" -v "$PWD/tests:/tests:ro" \\
    --entrypoint python farm-netwatch-hub:local /tests/test_tunnels.py -v
"""
import inspect
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

os.environ.setdefault("NETWATCH_DATA", tempfile.mkdtemp())
os.environ.setdefault("HUB_DATA", tempfile.mkdtemp())
sys.path.insert(0, os.environ.get("TUNNELS_APP", "/app"))

import tunnels    # noqa: E402

QUIET = 1.5       # seconds of silence; several times the timeouts patched below


def read_until(sock, marker, timeout):
    """Read until `marker`, EOF or `timeout`; return what arrived."""
    sock.settimeout(timeout)
    data = b""
    end = time.time() + timeout
    try:
        while marker not in data and time.time() < end:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
    except (socket.timeout, ConnectionResetError):
        pass
    return data


class RelayTest(unittest.TestCase):
    def device(self, script):
        """A one-connection 'device' on 127.0.0.1; script(conn, log) runs on it."""
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        srv.settimeout(10)
        log = []

        def run():
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            conn.settimeout(10)
            try:
                script(conn, log)
            except OSError as e:
                log.append(f"device error: {e}")
            finally:
                conn.close()
                srv.close()

        th = threading.Thread(target=run, daemon=True)
        th.start()
        self.addCleanup(th.join, 5)
        return srv.getsockname()[1], log

    def tunnel(self, dst_port):
        kw = {}
        if "allowed" in inspect.signature(tunnels.Tunnel).parameters:   # the hub's
            kw["allowed"] = ["127.0.0.1"]
        t = tunnels.Tunnel("127.0.0.1", 0, "127.0.0.1", dst_port, **kw)
        t.start()
        self.addCleanup(t.close)
        return t, t._lsock.getsockname()[1]

    def operator(self, port):
        c = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.addCleanup(c.close)
        return c

    def test_quiet_device_is_not_cut(self):
        """WinBox/SSH: the device sends nothing for a while; the relay must wait."""
        def script(conn, log):
            conn.sendall(b"hello\n")
            time.sleep(QUIET)
            conn.sendall(b"still here\n")

        with mock.patch.object(tunnels, "CONNECT_TIMEOUT", 0.3, create=True):
            port, _ = self.device(script)
            _, lp = self.tunnel(port)
            got = read_until(self.operator(lp), b"still here\n", QUIET + 5)
        self.assertIn(b"still here", got)

    def test_quiet_operator_is_not_cut(self):
        """The site's scanner sets socket.setdefaulttimeout(3) for the whole process.
        The relay's accepted socket must not inherit it: an operator quiet for that
        long half-closed the device side, and the device hung up."""
        def script(conn, log):
            data = b""
            while b"\n" not in data:
                chunk = conn.recv(100)
                if not chunk:
                    log.append("EOF before the operator spoke")
                    return
                data += chunk
            log.append(data)
            conn.sendall(b"pong\n")

        old = socket.getdefaulttimeout()
        socket.setdefaulttimeout(0.3)
        try:
            port, log = self.device(script)
            _, lp = self.tunnel(port)
            c = self.operator(lp)
            time.sleep(QUIET)
            c.sendall(b"ping\n")
            got = read_until(c, b"pong\n", 5)
        finally:
            socket.setdefaulttimeout(old)
        self.assertEqual(log, [b"ping\n"])
        self.assertIn(b"pong", got)

    def test_close_ends_a_live_session(self):
        """Closing a tunnel (operator, reaper, MAX_TTL) must end a session that is
        open but quiet. With no read timeout left, close() alone would leave the
        pump threads blocked in recv() on Linux and the session running."""
        def script(conn, log):
            conn.sendall(b"hello\n")
            try:
                while conn.recv(100):
                    pass
            except OSError:
                pass

        port, _ = self.device(script)
        t, lp = self.tunnel(port)
        c = self.operator(lp)
        self.assertIn(b"hello", read_until(c, b"hello\n", 5))
        t0 = time.time()
        t.close()
        c.settimeout(3)
        try:
            rest = c.recv(100)
        except ConnectionResetError:
            rest = b""
        except socket.timeout:
            self.fail("the session was still open 3 s after its tunnel was closed")
        self.assertEqual(rest, b"")
        self.assertLess(time.time() - t0, 2.5)

    def test_relay_sockets_have_keepalive_and_no_timeout(self):
        """With no read timeout, TCP keepalive is what notices a vanished peer."""
        hold = threading.Event()
        self.addCleanup(hold.set)
        port, _ = self.device(lambda conn, log: hold.wait(5))
        t, lp = self.tunnel(port)
        self.operator(lp)
        end = time.time() + 3
        while t.conns() == 0 and time.time() < end:
            time.sleep(0.05)
        socks = list(t._socks)
        self.assertEqual(len(socks), 2)
        for sk in socks:
            self.assertTrue(sk.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE))
            self.assertIsNone(sk.gettimeout())


if __name__ == "__main__":
    unittest.main()
