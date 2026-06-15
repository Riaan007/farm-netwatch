"""Hub side of the on-demand device tunnels.

A LAN operator wants to reach a device on a remote site's LAN (SSH, the device's
web page, RDP, RTSP, any port) without being a VPN client. The site Pi opens a
relay to the device bound to its wg0 address (see app/tunnels.py); the hub here
re-exposes that on a LAN port the operator connects to (PuTTY / browser).

Two-hop: operator -> hub LAN port (HUB_TCP_RANGE, published on wg-easy) --VPN-->
site 10.8.0.x:<site listen port> -> device:port. Pure-Python threading relay (same
primitive as the site), in-memory and on-demand, reaped when idle.
"""
import os
import secrets
import socket
import threading
import time

import requests

import hubconfig

IDLE_TTL = 600
MAX_TTL = 8 * 3600
BUF = 65536
WEB_PORTS = {80, 81, 443, 3000, 8000, 8080, 8081, 8443, 9000}


class TunnelError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def port_range():
    """(lo, hi) inclusive. MUST match the published range on wg-easy in
    docker-compose.yml (HUB_TCP_RANGE keeps code and compose in sync)."""
    spec = os.environ.get("HUB_TCP_RANGE", "8300-8331")
    try:
        lo, hi = (int(x) for x in spec.split("-", 1))
        if lo <= hi:
            return lo, hi
    except (ValueError, TypeError):
        pass
    return 8300, 8331


class Tunnel:
    """Listening socket relaying each accepted connection to dst_ip:dst_port."""

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
        self._socks = set()
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
                dst.shutdown(socket.SHUT_WR)
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


class HubTunnelManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._tuns = {}          # hub tunnel id -> record dict
        self._started = False

    def start(self):
        if self._started:
            return
        self._started = True
        lo, hi = port_range()
        print(f"[tunnels] device-tunnel port range {lo}-{hi} "
              "(must match the published range in docker-compose.yml)", flush=True)
        threading.Thread(target=self._reaper, daemon=True).start()

    def _timeout(self):
        poll = hubconfig.load()["poll"]
        return (poll["timeout_connect_s"], poll["timeout_read_s"])

    def _free_port(self):
        lo, hi = port_range()
        used = {r["hub_port"] for r in self._tuns.values()}
        for p in range(lo, hi + 1):
            if p not in used:
                return p
        return None

    def _close_site(self, vpn_ip, netwatch_port, site_tid):
        try:
            requests.delete(f"http://{vpn_ip}:{netwatch_port}/api/tunnel/{site_tid}",
                            timeout=self._timeout())
        except requests.RequestException:
            pass   # site's own reaper will collect the orphan

    def open(self, site, ip, port, host):
        try:
            port = int(port)
            if not (1 <= port <= 65535):
                raise ValueError
        except (TypeError, ValueError):
            raise TunnelError("Port must be 1-65535")
        vpn_ip = site["vpn_ip"]
        nport = site.get("netwatch_port", 8090)
        # Ask the site to open the device-side relay (it validates ip + binds wg0).
        try:
            r = requests.post(f"http://{vpn_ip}:{nport}/api/tunnel",
                              json={"ip": ip, "port": port}, timeout=self._timeout())
        except requests.RequestException as e:
            raise TunnelError(f"Site unreachable: {e}", 502)
        if r.status_code != 200:
            try:
                msg = r.json().get("error", "site rejected the tunnel")
            except ValueError:
                msg = "site rejected the tunnel"
            raise TunnelError(msg, r.status_code if r.status_code in (400, 409, 429) else 502)
        body = r.json()
        site_tid, site_lp = body["id"], body["listen_port"]
        with self._lock:
            hp = self._free_port()
            if hp is None:
                self._close_site(vpn_ip, nport, site_tid)
                raise TunnelError("Too many active tunnels", 429)
            t = Tunnel("0.0.0.0", hp, vpn_ip, site_lp)
            try:
                t.start()
            except OSError as e:
                self._close_site(vpn_ip, nport, site_tid)
                raise TunnelError(f"Could not open relay: {e}", 500)
            self._tuns[t.id] = {"t": t, "hub_port": hp, "site_id": site["id"],
                                "site_tid": site_tid, "vpn_ip": vpn_ip,
                                "netwatch_port": nport, "ip": ip, "port": port}
        print(f"[tunnels] open {t.id}: :{hp} -> {vpn_ip}:{site_lp} -> {ip}:{port}", flush=True)
        scheme = "https" if port == 443 else ("http" if port in WEB_PORTS else "")
        return {"id": t.id, "host": host, "port": hp, "scheme": scheme,
                "ip": ip, "device_port": port}

    def _info(self, tid, r):
        return {"id": tid, "hub_port": r["hub_port"], "site_id": r["site_id"],
                "ip": r["ip"], "port": r["port"], "conns": r["t"].conns(),
                "created_at": int(r["t"].created_at),
                "scheme": "https" if r["port"] == 443 else
                          ("http" if r["port"] in WEB_PORTS else "")}

    def list(self, site_id=None):
        with self._lock:
            return [self._info(tid, r) for tid, r in self._tuns.items()
                    if site_id is None or r["site_id"] == site_id]

    def close(self, tid):
        with self._lock:
            r = self._tuns.pop(tid, None)
        if not r:
            return False
        r["t"].close()
        self._close_site(r["vpn_ip"], r["netwatch_port"], r["site_tid"])
        print(f"[tunnels] close {tid}", flush=True)
        return True

    def _reaper(self):
        while True:
            time.sleep(30)
            now = time.time()
            doomed = []
            with self._lock:
                for tid, r in list(self._tuns.items()):
                    t = r["t"]
                    if (t.conns() == 0 and now - t.last_active > IDLE_TTL) or \
                       now - t.created_at > MAX_TTL:
                        doomed.append(r)
                        del self._tuns[tid]
            for r in doomed:
                r["t"].close()
                self._close_site(r["vpn_ip"], r["netwatch_port"], r["site_tid"])
                print(f"[tunnels] reaped {r['t'].id} -> {r['ip']}:{r['port']}", flush=True)


manager = HubTunnelManager()
