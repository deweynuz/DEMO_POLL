"""
Chargement et validation de la configuration.

Principe : le module REFUSE de démarrer sur une configuration douteuse plutôt
que d'appliquer des valeurs par défaut silencieuses. Le module précédent faisait
l'inverse — `monitor_ip` absent devenait "auto", ce qui déclenchait une sonde
permanente des quatre moniteurs du segment. Une clé inconnue est une erreur :
c'est presque toujours une faute de frappe, et l'ignorer fait croire à un
réglage qui n'a jamais été pris en compte.
"""

from __future__ import annotations

import difflib
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .protocole import ondes as CAT

CHEMIN_DEFAUT = Path('/etc/mx800/config.toml')

_MAC = re.compile(r'^([0-9a-f]{2}:){5}[0-9a-f]{2}$')
_IPV4 = re.compile(r'^(\d{1,3}\.){3}\d{1,3}$')


class ErreurConfiguration(Exception):
    """Configuration invalide. Le service doit s'arrêter, pas continuer."""


@dataclass
class Site:
    nom: str
    #: Vide = la salle est celle qu'annonce le moniteur. Ne renseigner que
    #: pour forcer un nom, quand l'étiquette de lit est fantaisiste.
    salle: str = ''


@dataclass
class Moniteur:
    #: Vide = le moniteur est appris automatiquement. Ne renseigner que pour
    #: épingler un appareil précis.
    mac: str = ''
    bed_label: str = ''
    ip: str = ''
    verifier_bed_label: bool = True
    #: Apprendre le moniteur quand aucun n'est configuré, et le réapprendre
    #: quand celui qui est mémorisé a disparu du segment.
    appairage_auto: bool = True


@dataclass
class Acquisition:
    periode_numerics_s: float = 1.0
    periode_demographiques_s: float = 30.0
    courbes: bool = False
    ondes: list[str] = field(default_factory=list)
    mtu: int = 1364
    #: ouvrir/fermer une intervention sur admission et sortie du patient
    intervention_auto: bool = True
    #: refuser d'enregistrer quand le moniteur est en mode démonstration
    refuser_mode_demo: bool = True


@dataclass
class Surveillance:
    silence_donnees_s: float = 60.0
    echecs_avant_alerte: int = 3
    seuil_disque_mo: int = 2000
    backoff_min_s: float = 1.0
    backoff_max_s: float = 60.0


@dataclass
class Stockage:
    chemin: Path = Path('/var/lib/mx800')
    uuid: str = ''
    marqueur: str = '.mx800-stockage'


@dataclass
class Configuration:
    site: Site
    moniteur: Moniteur = field(default_factory=Moniteur)
    acquisition: Acquisition = field(default_factory=Acquisition)
    surveillance: Surveillance = field(default_factory=Surveillance)
    stockage: Stockage = field(default_factory=Stockage)
    niveau_journal: str = 'INFO'
    chemin_etat: Path = Path('/run/mx800/status.json')

    @property
    def physio_ids_ondes(self) -> list[int]:
        return [CAT.onde(nom).physio_id for nom in self.acquisition.ondes]

    @property
    def base(self) -> Path:
        return self.stockage.chemin / 'mx800.db'

    @property
    def base_identites(self) -> Path:
        """Base nominative, séparée et à permissions restreintes."""
        return self.stockage.chemin / 'identites.db'

    @property
    def courbes_dir(self) -> Path:
        return self.stockage.chemin / 'courbes'

    @property
    def exports_dir(self) -> Path:
        return self.stockage.chemin / 'exports'


# ─────────────────────────────────────────────────────────────────────────────
# Validation
# ─────────────────────────────────────────────────────────────────────────────

_SECTIONS = {
    'site':         {'nom', 'salle'},
    'moniteur':     {'mac', 'bed_label', 'ip', 'verifier_bed_label',
                     'appairage_auto'},
    'acquisition':  {'periode_numerics_s', 'periode_demographiques_s', 'courbes',
                     'ondes', 'mtu', 'intervention_auto', 'refuser_mode_demo'},
    'surveillance': {'silence_donnees_s', 'echecs_avant_alerte', 'seuil_disque_mo',
                     'backoff_min_s', 'backoff_max_s'},
    'stockage':     {'chemin', 'uuid', 'marqueur'},
    'journal':      {'niveau'},
    'etat':         {'chemin'},
}


def _verifier_cles(brut: dict) -> list[str]:
    problemes = []
    for section in brut:
        if section not in _SECTIONS:
            problemes.append(f"section inconnue : [{section}]")
            continue
        if not isinstance(brut[section], dict):
            problemes.append(f"[{section}] devrait être une section")
            continue
        for cle in brut[section]:
            if cle not in _SECTIONS[section]:
                proches = difflib.get_close_matches(cle, _SECTIONS[section], n=1, cutoff=0.6)
                indice = f" (vouliez-vous dire {proches[0]} ?)" if proches else ""
                problemes.append(f"clé inconnue : [{section}] {cle}{indice}")
    return problemes


def valider(brut: dict) -> Configuration:
    """Transforme un dict TOML en Configuration, ou lève avec TOUS les problèmes."""
    problemes = _verifier_cles(brut)

    site = brut.get('site', {})
    if not site.get('nom'):
        problemes.append("[site] nom est obligatoire (ex. « HEGP »)")
    mon = brut.get('moniteur', {})
    mac, ip = (mon.get('mac') or '').lower(), mon.get('ip') or ''
    if not mac and not ip and not mon.get('appairage_auto', True):
        problemes.append("[moniteur] appairage_auto = false exige mac ou ip. "
                         "Sinon le Pi ne saurait à qui parler.")
    if mac and not _MAC.match(mac):
        problemes.append(f"[moniteur] mac invalide : {mac!r} (format 00:09:fb:xx:xx:xx)")
    if ip and (not _IPV4.match(ip) or any(int(o) > 255 for o in ip.split('.'))):
        problemes.append(f"[moniteur] ip invalide : {ip!r}")

    acq = brut.get('acquisition', {})
    periode = acq.get('periode_numerics_s', 1.0)
    if not isinstance(periode, (int, float)) or not (0.5 <= periode <= 60):
        problemes.append(f"[acquisition] periode_numerics_s={periode} hors [0.5, 60] ; "
                         "le moniteur traite au plus un poll par objet et par "
                         "seconde (PIPG p. 56)")
    mtu = acq.get('mtu', 1364)
    if not isinstance(mtu, int) or not (300 <= mtu <= 1364):
        problemes.append(f"[acquisition] mtu={mtu} hors [300, 1364] (PIPG p. 71)")

    liste = acq.get('ondes', [])
    if not isinstance(liste, list):
        problemes.append("[acquisition] ondes doit être une liste de noms")
        liste = []
    physio = []
    for nom in liste:
        o = CAT.onde(nom)
        if o is None:
            problemes.append(f"[acquisition] onde inconnue : {nom!r} — "
                             "voir mx800/protocole/ondes.py pour les 55 noms valides")
        else:
            physio.append(o.physio_id)
    problemes += [f"[acquisition] {p}" for p in CAT.verifier_selection(physio)]
    if acq.get('courbes') and not liste:
        problemes.append("[acquisition] courbes = true mais aucune onde listée : "
                         "le moniteur enverrait sa liste par défaut, qui peut saturer "
                         "la liaison (PIPG p. 63)")
    if liste and not acq.get('courbes'):
        problemes.append("[acquisition] des ondes sont listées mais courbes = false : "
                         "elles ne seraient jamais acquises")
    if acq.get('courbes') and mtu < 500:
        problemes.append(f"[acquisition] mtu={mtu} insuffisant pour 256 ms de courbe "
                         "en un message : 500 minimum (PIPG p. 71)")

    sur = brut.get('surveillance', {})
    silence = sur.get('silence_donnees_s', 60.0)
    if not isinstance(silence, (int, float)) or silence < 5:
        problemes.append(f"[surveillance] silence_donnees_s={silence} : au moins 5 s")
    if silence < periode * 3:
        problemes.append(f"[surveillance] silence_donnees_s={silence} est inférieur à "
                         f"trois périodes de poll ({periode * 3:.1f} s) : "
                         "le watchdog déclencherait sur du fonctionnement normal")

    sto = brut.get('stockage', {})
    chemin = sto.get('chemin', '/var/lib/mx800')
    if not str(chemin).startswith('/'):
        problemes.append(f"[stockage] chemin doit être absolu : {chemin!r}")

    niveau = brut.get('journal', {}).get('niveau', 'INFO')
    if niveau not in ('DEBUG', 'INFO', 'WARNING', 'ERROR'):
        problemes.append(f"[journal] niveau invalide : {niveau!r}")

    if problemes:
        raise ErreurConfiguration(
            "configuration invalide :\n" + '\n'.join(f"  - {p}" for p in problemes))

    return Configuration(
        site=Site(nom=site['nom'], salle=site.get('salle', '') or ''),
        moniteur=Moniteur(mac=mac, bed_label=mon.get('bed_label', ''), ip=ip,
                          verifier_bed_label=mon.get('verifier_bed_label', True),
                          appairage_auto=bool(mon.get('appairage_auto', True))),
        acquisition=Acquisition(
            periode_numerics_s=float(periode),
            periode_demographiques_s=float(acq.get('periode_demographiques_s', 30.0)),
            courbes=bool(acq.get('courbes', False)),
            ondes=list(liste), mtu=mtu,
            intervention_auto=bool(acq.get('intervention_auto', True)),
            refuser_mode_demo=bool(acq.get('refuser_mode_demo', True))),
        surveillance=Surveillance(
            silence_donnees_s=float(silence),
            echecs_avant_alerte=int(sur.get('echecs_avant_alerte', 3)),
            seuil_disque_mo=int(sur.get('seuil_disque_mo', 2000)),
            backoff_min_s=float(sur.get('backoff_min_s', 1.0)),
            backoff_max_s=float(sur.get('backoff_max_s', 60.0))),
        stockage=Stockage(chemin=Path(chemin), uuid=sto.get('uuid', ''),
                          marqueur=sto.get('marqueur', '.mx800-stockage')),
        niveau_journal=niveau,
        chemin_etat=Path(brut.get('etat', {}).get('chemin', '/run/mx800/status.json')),
    )


def charger(chemin: Path | str = CHEMIN_DEFAUT) -> Configuration:
    chemin = Path(chemin)
    if not chemin.exists():
        raise ErreurConfiguration(f"configuration introuvable : {chemin}")
    try:
        brut = tomllib.loads(chemin.read_text(encoding='utf-8'))
    except tomllib.TOMLDecodeError as e:
        raise ErreurConfiguration(f"{chemin} : TOML illisible — {e}") from e
    return valider(brut)
