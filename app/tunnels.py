"""On-demand TCP relays so the hub (and ultimately a LAN operator) can reach a
device on THIS site's LAN.

The WireGuard tunnel is split (only 10.8.0.0/24 is routed), so the hub can reach
this Pi at its 10.8.0.x but not the devices behind it — and farm LANs overlap
across sites, so L3 routing isn't viable. This Pi is the only node that can reach
its own LAN, so it terminates the relay: it listens on its wg0 address
(10.8.0.x:<port>, reachable only over the VPN) and forwards raw TCP to a chosen
device:port. The hub then re-exposes that on a LAN port (see hub/app/tunnels.py).

Pure-Python threading relay (no socat/extra deps), matching the scanner/listener
idiom. Tunnels are in-memory and on-demand: a reaper closes them when idle or past
a hard lifetime, and they all vanish cleanly on restart.
"""
import secrets
import socket
import threading
import time

# Listen ports for relays, bound to the wg0 address (only the VPN reaches them).
PORT_LO, PORT_HI = 10000, 10063
IDLE_TTL = 600            # close after 10 min with no live connections
MAX_TTL = 8 * 3600        # hard lifetime cap
BUF = 65536


class TunnelError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class Tunnel:
    """A listening socket that relays each accepted connection to dst_ip:dst_port."""

    def __init__(self, listen_host, listen_port, dst_ip, dst_port):
        self.id = secrets.token_hex(4)
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.dst_ip = dst_ip
        self.dst_port = int(dst_port)
        self.created_at = time.time()
        self.last_active = time.time()
        self._stop = False
        self._lock = threading.Lock()
        self._socks = set()       # all live client+upstream sockets (2 per connection)
        self._lsock = None

    def start(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.listen_host, self.listen_port))
        s.listen(16)
        s.settimeout(1.0)
        self._lsock = s
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def _accept_loop(self):
        while not self._stop:
            try:
                client, _ = self._lsock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client):
        try:
            upstream = socket.create_connection((self.dst_ip, self.dst_port), timeout=10)
        except OSError:
            client.close()
            return
        with self._lock:
            self._socks.update((client, upstream))
        self.last_active = time.time()
        a = threading.Thread(target=self._pump, args=(client, upstream), daemon=True)
        b = threading.Thread(target=self._pump, args=(upstream, client), daemon=True)
        a.start(); b.start(); a.join(); b.join()
        with self._lock:
            self._socks.discard(client)
            self._socks.discard(upstream)
        for sk in (client, upstream):
            try:
                sk.close()
            except OSError:
                pass

    def _pump(self, src, dst):
        try:
            while not self._stop:
                data = src.recv(BUF)
                if not data:
                    break
                dst.sendall(data)
                self.last_active = time.time()
        except OSError:
            pass
        finally:
            try:
                dst.shutdown(socket.SHUT_WR)   # half-close so SSH/RTSP don't hang
            except OSError:
                pass

    def conns(self):
        with self._lock:
            return len(self._socks) // 2

    def close(self):
        self._stop = True
        try:
            if self._lsock:
                self._lsock.close()
        except OSError:
            pass
        with self._lock:
            socks = list(self._socks)
            self._socks.clear()
        for sk in socks:
            try:
                sk.close()
            except OSError:
                pass


def _known_device(ip):
    """Only relay to IPs the scanner has actually seen — the site is the trust
    boundary for its own LAN; this stops the hub being used as an open relay."""
    from scanner import scanner
    return any(d.get("ip") == ip for d in scanner.get_devices())


def _bind_address():
    """The site's wg0 address (bare 10.8.0.x). Relays bind here so only the VPN
    can reach them; if the tunnel is down we fail closed."""
    import hubvpn
    return hubvpn.wg_address()


class TunnelManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._tuns = {}
        self._started = False

    def start(self):
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._reaper, daemon=True).start()

    def _free_port(self):
        used = {t.listen_port for t in self._tuns.values()}
        for p in range(PORT_LO, PORT_HI + 1):
            if p not in used:
                return p
        return None

    def open(self, ip, port):
        ip = (ip or "").strip()
        try:
            port = int(port)
            if not (1 <= port <= 65535):
                raise ValueError
        except (TypeError, ValueError):
            raise TunnelError("Port must be 1-65535")
        if not _known_device(ip):
            raise TunnelError(f"Unknown device IP: {ip}")
        addr = _bind_address()
        if not addr:
            raise TunnelError("Hub VPN is not up — cannot open a tunnel", 409)
        with self._lock:
            lp = self._free_port()
            if lp is None:
                raise TunnelError("Too many active tunnels", 429)
            t = Tunnel(addr, lp, ip, port)
            try:
                t.start()
            except OSError as e:
                raise TunnelError(f"Could not open relay: {e}", 500)
            self._tuns[t.id] = t
        print(f"[tunnels] open {t.id}: {addr}:{lp} -> {ip}:{port}", flush=True)
        return {"id": t.id, "listen_port": lp, "ip": ip, "port": port}

    def _info(self, t):
        return {"id": t.id, "listen_port": t.listen_port, "ip": t.dst_ip,
                "port": t.dst_port, "created_at": int(t.created_at), "conns": t.conns()}

    def list(self):
        with self._lock:
            return [self._info(t) for t in self._tuns.values()]

    def close(self, tid):
        with self._lock:
            t = self._tuns.pop(tid, None)
        if not t:
            return False
        t.close()
        print(f"[tunnels] close {tid}", flush=True)
        return True

    def _reaper(self):
        while True:
            time.sleep(30)
            now = time.time()
            doomed = []
            with self._lock:
                for tid, t in list(self._tuns.items()):
                    idle = t.conns() == 0 and now - t.last_active > IDLE_TTL
                    if idle or now - t.created_at > MAX_TTL:
                        doomed.append(t)
                        del self._tuns[tid]
            for t in doomed:
                t.close()
                print(f"[tunnels] reaped {t.id} -> {t.dst_ip}:{t.dst_port}", flush=True)


manager = TunnelManager()
