"""
Résolution de l'adresse du moniteur.

L'identité d'un moniteur n'est pas son adresse : c'est sa MAC, et l'étiquette
de lit qu'il annonce dans son MDS Create Event. Le Pi étant serveur DHCP/BOOTP
du segment, il connaît la correspondance MAC -> IP et peut la résoudre à chaque
tentative. Le moniteur change d'adresse : on le suit. Un autre moniteur se
branche : on refuse.

Ce module NE SONDE JAMAIS le réseau. L'ancien module envoyait un Association
Request à chacun des quatre moniteurs du segment toutes les deux minutes, ce
qui provoquait environ 4 300 ABORT en six jours sur du matériel clinique. Ici,
on lit des tables locales, rien de plus.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

log = logging.getLogger('mx800.adressage')

BAUX_DNSMASQ = Path('/var/lib/misc/dnsmasq.leases')
OUI_PHILIPS = '00:09:fb'


class AdresseIntrouvable(Exception):
    """Le moniteur n'est pas dans les tables locales. Réessayer plus tard."""


def baux(chemin: Path = BAUX_DNSMASQ) -> dict[str, str]:
    """Lit les baux dnsmasq -> {mac: ip}. Format : <expiration> <mac> <ip> ..."""
    resultat = {}
    try:
        for ligne in Path(chemin).read_text(encoding='utf-8').splitlines():
            morceaux = ligne.split()
            if len(morceaux) >= 3:
                resultat[morceaux[1].lower()] = morceaux[2]
    except OSError as e:
        log.debug("baux dnsmasq illisibles (%s) : %s", chemin, e)
    return resultat


def voisinage(interface: str = 'eth0') -> dict[str, str]:
    """Table ARP -> {mac: ip}, pour les moniteurs à adresse statique."""
    resultat = {}
    try:
        sortie = subprocess.run(['ip', '-4', 'neigh', 'show', 'dev', interface],
                                capture_output=True, text=True, timeout=5,
                                check=False).stdout
    except (OSError, subprocess.SubprocessError) as e:
        log.debug("table ARP illisible : %s", e)
        return resultat
    for ligne in sortie.splitlines():
        morceaux = ligne.split()
        if 'lladdr' in morceaux:
            resultat[morceaux[morceaux.index('lladdr') + 1].lower()] = morceaux[0]
    return resultat


def resoudre(*, mac: str = '', ip: str = '', interface: str = 'eth0',
             chemin_baux: Path = BAUX_DNSMASQ,
             consulter_arp: bool = True) -> str:
    """
    Renvoie l'adresse à contacter. Une IP explicitement configurée l'emporte.

    `consulter_arp` permet de s'en tenir au fichier de baux. Les tests le
    mettent à False : sans cela ils retombent sur la table ARP de la machine
    et émettent du trafic vers de vrais moniteurs cliniques.
    """
    if ip:
        return ip
    if not mac:
        raise AdresseIntrouvable("ni mac ni ip en configuration")
    mac = mac.lower()

    table = baux(chemin_baux)
    if mac in table:
        return table[mac]
    if consulter_arp:
        table = voisinage(interface)
        if mac in table:
            log.info("%s résolue par la table ARP (absente des baux dnsmasq)", mac)
            return table[mac]

    connues = sorted(set(baux(chemin_baux))
                     | (set(voisinage(interface)) if consulter_arp else set()))
    philips = [m for m in connues if m.startswith(OUI_PHILIPS)]
    raise AdresseIntrouvable(
        f"MAC {mac} absente des baux dnsmasq et de la table ARP. "
        f"Moniteurs Philips visibles : {', '.join(philips) or 'aucun'}. "
        f"Le moniteur est-il allumé et raccordé à {interface} ?")


def inventaire(interface: str = 'eth0',
               chemin_baux: Path = BAUX_DNSMASQ,
               consulter_arp: bool = True) -> list[tuple[str, str, str]]:
    """
    [(mac, ip, origine)] des moniteurs Philips visibles. Sert à l'outil de
    réglage et au message d'erreur ci-dessus — jamais à choisir tout seul.
    """
    vus: dict[str, tuple[str, str]] = {}
    for mac, adresse in baux(chemin_baux).items():
        if mac.startswith(OUI_PHILIPS):
            vus[mac] = (adresse, 'bail dnsmasq')
    if consulter_arp:
        for mac, adresse in voisinage(interface).items():
            if mac.startswith(OUI_PHILIPS) and mac not in vus:
                vus[mac] = (adresse, 'table ARP')
    return sorted((mac, a, o) for mac, (a, o) in vus.items())
