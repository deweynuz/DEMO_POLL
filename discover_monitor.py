#!/usr/bin/env python3
"""
discover_monitor.py — Découverte automatique du moniteur Philips sur eth0

Principe :
  1. Collecte les IP candidates (baux dnsmasq + table ARP + IP configurée)
  2. Ne garde que les MAC Philips Patient Monitoring (OUI 00:09:FB)
  3. Envoie un Association Request à chaque candidat
  4. Retourne la première IP qui parle le protocole Data Export

Usage autonome (test) :
  python3 discover_monitor.py
  python3 discover_monitor.py --debug

Usage depuis mx800_capture.py :
  from discover_monitor import discover_monitor
  ip = discover_monitor(fallback='192.168.100.31')
"""

import socket
import struct
import subprocess
import argparse
import logging
import ipaddress
import os

log = logging.getLogger('discover')

# OUI Philips Patient Monitoring
PHILIPS_OUI = ('00:09:fb',)

LEASES_FILE     = '/var/lib/misc/dnsmasq.leases'
MX800_DATA_PORT = 24105
PROBE_TIMEOUT   = 1.5    # secondes d'attente par candidat
PROBE_ROUNDS    = 2      # nombre de passes sur la liste des candidats


# ─────────────────────────────────────────────────────────────────────────────
# Collecte des candidats
# ─────────────────────────────────────────────────────────────────────────────

def _is_philips(mac: str) -> bool:
    return mac.lower().startswith(PHILIPS_OUI)


def candidates_from_leases() -> list[tuple[str, str]]:
    """Lit /var/lib/misc/dnsmasq.leases → [(ip, mac), ...] Philips uniquement."""
    found = []
    if not os.path.exists(LEASES_FILE):
        return found
    try:
        with open(LEASES_FILE) as f:
            for line in f:
                parts = line.split()
                # format : <expiry> <mac> <ip> <hostname> <client-id>
                if len(parts) >= 3 and _is_philips(parts[1]):
                    found.append((parts[2], parts[1].lower()))
    except Exception as e:
        log.debug(f"Lecture baux impossible : {e}")
    return found


def candidates_from_arp(iface: str = 'eth0') -> list[tuple[str, str]]:
    """Lit la table de voisinage (ip neigh) → moniteurs à IP statique inclus."""
    found = []
    try:
        out = subprocess.run(
            ['ip', '-4', 'neigh', 'show', 'dev', iface],
            capture_output=True, text=True, timeout=5
        ).stdout
        for line in out.splitlines():
            parts = line.split()
            if 'lladdr' in parts:
                ip  = parts[0]
                mac = parts[parts.index('lladdr') + 1].lower()
                if _is_philips(mac):
                    found.append((ip, mac))
    except Exception as e:
        log.debug(f"Lecture table ARP impossible : {e}")
    return found


def prime_arp_cache(iface: str = 'eth0') -> None:
    """
    Peuple la table ARP en pingant le broadcast du sous-réseau.
    Réveille les moniteurs à IP statique absents des baux DHCP.
    """
    try:
        out = subprocess.run(
            ['ip', '-4', '-brief', 'addr', 'show', iface],
            capture_output=True, text=True, timeout=5
        ).stdout.split()
        cidr = next((p for p in out if '/' in p), None)
        if not cidr:
            return
        net = ipaddress.ip_interface(cidr).network
        subprocess.run(
            ['ping', '-c', '2', '-W', '1', '-b', str(net.broadcast_address)],
            capture_output=True, timeout=8
        )
        log.debug(f"Broadcast ping émis sur {net}")
    except Exception as e:
        log.debug(f"Ping broadcast impossible : {e}")


def collect_candidates(iface: str = 'eth0', fallback: str | None = None) -> list[str]:
    """Retourne la liste ordonnée et dédoublonnée des IP à sonder."""
    # Les baux dnsmasq sont la source la plus fiable et la moins coûteuse
    pairs = candidates_from_leases()

    # Table ARP existante (moniteurs à IP statique déjà connus)
    pairs += candidates_from_arp(iface)

    # En dernier recours seulement : ping broadcast pour réveiller le voisinage.
    # Coûteux et polluant pour la table ARP, donc évité si on a déjà un candidat.
    if not pairs:
        log.debug("Aucun candidat connu — ping broadcast de découverte")
        prime_arp_cache(iface)
        pairs = candidates_from_arp(iface)

    ips: list[str] = []
    # L'IP de repli est testée en premier : c'est la plus probable
    if fallback and fallback not in ('auto', ''):
        ips.append(fallback)
    for ip, mac in pairs:
        if ip not in ips:
            ips.append(ip)
            log.debug(f"Candidat : {ip} (MAC {mac})")
    return ips


# ─────────────────────────────────────────────────────────────────────────────
# Sonde protocolaire
# ─────────────────────────────────────────────────────────────────────────────

def _assoc_request() -> bytes:
    """Association Request minimal (identique à mx800_capture.py)."""
    session_data = bytes([
        0x05, 0x08, 0x13, 0x01, 0x00, 0x16, 0x01, 0x02,
        0x80, 0x00, 0x14, 0x02, 0x00, 0x02
    ])
    pres_header = bytes([
        0xC1, 0x00, 0x31, 0x80, 0xA0, 0x80, 0x80, 0x01,
        0x01, 0x00, 0x00, 0xA2, 0x80, 0xA0, 0x03, 0x00,
        0x00, 0x01, 0xA4, 0x80, 0x30, 0x80, 0x02, 0x01,
        0x01, 0x06, 0x04, 0x52, 0x01, 0x00, 0x01, 0x30,
        0x80, 0x06, 0x02, 0x51, 0x01, 0x00, 0x00, 0x00,
        0x00, 0x30, 0x80, 0x02, 0x01, 0x02, 0x06, 0x0C,
        0x2A, 0x86, 0x48, 0xCE, 0x14, 0x02, 0x01, 0x00,
        0x00, 0x00, 0x01, 0x01, 0x30, 0x80, 0x06, 0x0C,
        0x2A, 0x86, 0x48, 0xCE, 0x14, 0x02, 0x01, 0x00,
        0x00, 0x00, 0x02, 0x01, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x61, 0x80, 0x30, 0x80, 0x02, 0x01,
        0x01, 0xA0, 0x80, 0x60, 0x80, 0xA1, 0x80, 0x06,
        0x0C, 0x2A, 0x86, 0x48, 0xCE, 0x14, 0x02, 0x01,
        0x00, 0x00, 0x00, 0x03, 0x01, 0x00, 0x00, 0xBE,
        0x80, 0x28, 0x80, 0x06, 0x0C, 0x2A, 0x86, 0x48,
        0xCE, 0x14, 0x02, 0x01, 0x00, 0x00, 0x00, 0x01,
        0x01, 0x02, 0x01, 0x02, 0x81
    ])
    user_data = bytes([
        0x48,
        0x80, 0x00, 0x00, 0x00,
        0x40, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00,
        0x80, 0x00, 0x00, 0x00,
        0x20, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00,
        0x00, 0x01, 0x00, 0x2C,
        0x00, 0x01, 0x00, 0x28,
        0x80, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x09, 0xC4,
        0x00, 0x00, 0x03, 0xE8,
        0x00, 0x00, 0x03, 0xE8,
        0xFF, 0xFF, 0xFF, 0xFF,
        0x60, 0x00, 0x00, 0x00,
        0x00, 0x01, 0x00, 0x0C,
        0xF0, 0x01, 0x00, 0x08,
        0x80, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00,
    ])
    inner = pres_header[2:] + user_data + bytes(16)
    pres  = bytes([pres_header[0], len(inner)]) + inner
    body  = session_data + pres
    return bytes([0x0D, len(body)]) + body


RELEASE_REQ = bytes([
    0x09, 0x18,
    0xC1, 0x16, 0x61, 0x80, 0x30, 0x80,
    0x02, 0x01, 0x01, 0xA0, 0x80, 0x62,
    0x80, 0x80, 0x01, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
])


def probe(ip: str, timeout: float = PROBE_TIMEOUT) -> str | None:
    """
    Envoie un Association Request à `ip`.
    Retourne 'ACCEPT', 'BUSY' (refus = parle le protocole mais occupé), ou None.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.bind(('', 0))
        sock.sendto(_assoc_request(), (ip, MX800_DATA_PORT))
        while True:
            data, addr = sock.recvfrom(4096)
            if addr[0] != ip or not data:
                continue
            if data[0] == 0x0E:      # Association Response
                sock.sendto(RELEASE_REQ, (ip, MX800_DATA_PORT))
                return 'ACCEPT'
            if data[0] == 0x0C:      # Refuse : endpoint Data Export, occupé
                return 'BUSY'
            if data[0] == 0x19:      # Abort
                return 'BUSY'
    except socket.timeout:
        return None
    except OSError as e:
        log.debug(f"{ip} : {e}")
        return None
    finally:
        sock.close()


# ─────────────────────────────────────────────────────────────────────────────
# API publique
# ─────────────────────────────────────────────────────────────────────────────

def discover_monitor(iface: str = 'eth0', fallback: str | None = None) -> str | None:
    """
    Découvre l'IP du moniteur Philips. Retourne l'IP ou None.
    `fallback` est sondé en premier s'il est fourni.
    """
    ips = collect_candidates(iface, fallback)
    if not ips:
        log.warning("Aucun candidat Philips trouvé sur %s", iface)
        return None

    log.info("Candidats : %s", ', '.join(ips))

    busy = None
    for _ in range(PROBE_ROUNDS):
        for ip in ips:
            result = probe(ip)
            if result == 'ACCEPT':
                log.info("Moniteur découvert : %s", ip)
                return ip
            if result == 'BUSY' and busy is None:
                busy = ip

    if busy:
        log.info("Moniteur découvert (occupé) : %s", busy)
        return busy

    log.warning("Aucun candidat n'a répondu au Data Export")
    return None


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description='Découverte moniteur Philips')
    ap.add_argument('--iface',    default='eth0')
    ap.add_argument('--fallback', default=None,
                    help='IP testée en priorité (ex. 192.168.100.31)')
    ap.add_argument('--debug',    action='store_true')
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format='%(asctime)s %(levelname)s %(message)s'
    )

    ip = discover_monitor(args.iface, args.fallback)
    if ip:
        print(f"\nIP moniteur : {ip}")
    else:
        print("\nAucun moniteur trouvé.")
        raise SystemExit(1)
