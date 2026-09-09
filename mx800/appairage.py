"""
Appairage automatique du moniteur.

Objectif : débrancher le Pi d'une salle, le rebrancher dans une autre, et que
ça reprenne sans rien configurer.

Il y a un seul moniteur par salle. Sur le segment on trouve parfois plusieurs
appareils Philips — un ensemble MX800 comporte plusieurs boîtiers — mais un
seul accepte une association Data Export. L'appairage consiste donc à essayer
les candidats une fois et à retenir celui qui répond.

Différence essentielle avec la sonde de l'ancien module, qui a provoqué
environ 4 300 ABORT en six jours : ici l'association est MENÉE À TERME. Le MDS
Create Event est confirmé, l'identité est lue, puis un Release est envoyé et
sa réponse attendue. Le moniteur retrouve un état propre au lieu d'être
abandonné en cours d'établissement.

L'appairage n'a lieu que lorsqu'on ne sait pas à qui parler : au premier
démarrage, ou quand le moniteur appris a disparu du segment. Jamais en boucle.
"""

from __future__ import annotations

import json
import logging
import socket
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from . import adressage
from .protocole import constantes as C, decodage as D, trames as T

log = logging.getLogger('mx800.appairage')

NOM_FICHIER = 'moniteur.json'
DELAI_REPONSE_S = 2.0
DELAI_TOTAL_S = 6.0


@dataclass
class Moniteur:
    """Ce qu'on a appris d'un moniteur, et qui suffit à le reconnaître."""
    mac: str
    ip: str
    bed_label: str = ''
    system_id: str = ''
    modele: str = ''
    mode_operation: int | None = None
    patient_admis: bool = False
    appris_utc: str = ''

    @property
    def salle(self) -> str:
        """
        L'étiquette de lit quand le moniteur en annonce une ; sinon un nom
        dérivé de sa MAC. Jamais rien de configuré à la main : la salle suit
        l'appareil, pas le fichier.
        """
        etiquette = (self.bed_label or '').strip()
        if etiquette:
            return etiquette
        return 'MON-' + self.mac.replace(':', '')[-6:].upper()

    @property
    def identifiant(self) -> str:
        return self.system_id or self.mac


def _sonder(ip: str, mac: str, *, port_local: int = 0,
            port: int = C.PORT_MONITEUR,
            lire_patient: bool = True) -> Moniteur | None:
    """
    Une association complète, menée à terme, puis relâchée proprement.
    Renvoie l'identité du moniteur, ou None s'il ne parle pas le protocole.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(0.3)
    try:
        sock.bind(('', port_local))
    except OSError:
        sock.bind(('', 0))

    trouve: Moniteur | None = None
    associe = False
    invoke = 1
    fin = time.monotonic() + DELAI_TOTAL_S
    try:
        sock.sendto(T.construire_assoc_request(), (ip, port))
        demande_demographiques = False
        while time.monotonic() < fin:
            try:
                donnees, pair = sock.recvfrom(65535)
            except socket.timeout:
                if not associe and time.monotonic() > fin - DELAI_TOTAL_S + DELAI_REPONSE_S:
                    break               # rien en 2 s : ce n'est pas un endpoint Data Export
                continue
            if pair[0] != ip:
                continue
            genre = D.type_message(donnees)

            if genre == 'REFUSE':
                # Il parle le protocole mais refuse — souvent une autre
                # association est active. C'est bien LE moniteur.
                log.info("%s refuse l'association : c'est un endpoint Data Export "
                         "mais il est occupé", ip)
                return Moniteur(mac=mac, ip=ip, appris_utc=_utc())
            if genre == 'ABORT':
                break
            if genre != 'DONNEES':
                continue

            try:
                apdu = D.decoder_apdu(donnees)
            except D.ErreurDecodage:
                continue

            if apdu.command_type == C.CMD_CONFIRMED_EVENT_REPORT:
                try:
                    mds = D.decoder_mds_create(donnees)
                except D.ErreurDecodage:
                    continue
                # Confirmer : sans cela le moniteur ré-émet puis ABORT.
                sock.sendto(T.construire_mds_create_result(
                    mds.invoke_id, mds.managed_object, mds.event_time),
                    (ip, port))
                associe = True
                trouve = Moniteur(mac=mac, ip=ip, bed_label=mds.bed_label or '',
                                  system_id=mds.system_id or '', modele=mds.modele or '',
                                  mode_operation=mds.mode_operation, appris_utc=_utc())
                if not lire_patient:
                    break
                if not demande_demographiques:
                    invoke += 1
                    sock.sendto(T.construire_poll_request(
                        invoke, C.NOM_MOC_PT_DEMOG, C.NOM_ATTR_GRP_PT_DEMOG),
                        (ip, port))
                    demande_demographiques = True
                continue

            if trouve is not None and apdu.command_type == C.CMD_CONFIRMED_ACTION:
                try:
                    resultat = D.decoder_resultat_poll(donnees)
                except D.ErreurDecodage:
                    continue
                if resultat.type_objet != C.NOM_MOC_PT_DEMOG:
                    continue
                attributs = {}
                for objet in resultat.objets:
                    attributs.update(objet.attributs)
                if attributs:
                    trouve.patient_admis = D.decoder_demographiques(attributs).admis
                break
    except OSError as e:
        log.debug("sonde %s : %s", ip, e)
    finally:
        if associe:
            # Relâcher proprement : le moniteur retrouve un état libre.
            try:
                sock.sendto(T.RELEASE_REQUEST, (ip, port))
                sock.settimeout(1.0)
                sock.recvfrom(65535)
            except OSError:
                pass
        sock.close()
    return trouve


def _utc() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _joignable(ip: str) -> bool:
    """Filtre les baux fantômes : dnsmasq est en `infinite`, rien n'est purgé."""
    import subprocess
    try:
        return subprocess.run(['ping', '-c1', '-W1', ip], capture_output=True,
                              timeout=3, check=False).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return True          # dans le doute, on sonde


def appairer(*, interface: str = 'eth0',
             chemin_baux: Path = adressage.BAUX_DNSMASQ,
             lire_patient: bool = True,
             port: int = C.PORT_MONITEUR,
             consulter_arp: bool = True) -> list[Moniteur]:
    """
    Sonde les appareils Philips visibles et renvoie ceux qui parlent Data
    Export. Un par salle en pratique ; la liste permet de le vérifier.
    """
    candidats = adressage.inventaire(interface, chemin_baux, consulter_arp)
    if not candidats:
        log.warning("aucun appareil Philips visible sur %s", interface)
        return []

    vivants = [(mac, ip) for mac, ip, _ in candidats if _joignable(ip)]
    fantomes = len(candidats) - len(vivants)
    log.info("Appairage : %d appareil(s) Philips visible(s), %d joignable(s)%s",
             len(candidats), len(vivants),
             f", {fantomes} bail(s) périmé(s) ignoré(s)" if fantomes else "")

    trouves = []
    for mac, ip in vivants:
        moniteur = _sonder(ip, mac, lire_patient=lire_patient, port=port)
        if moniteur is None:
            log.info("  %s (%s) ne répond pas au Data Export", ip, mac)
            continue
        log.info("  %s (%s) : lit %r, %s%s", ip, mac, moniteur.salle,
                 moniteur.modele or 'modèle inconnu',
                 ", patient admis" if moniteur.patient_admis else "")
        trouves.append(moniteur)
    return trouves


def choisir(moniteurs: list[Moniteur]) -> Moniteur | None:
    """
    Un seul moniteur par salle : le cas normal est une liste à un élément.
    Si plusieurs répondent — configuration inattendue — on préfère celui qui a
    un patient admis, et on refuse s'il y en a plusieurs plutôt que de deviner
    entre deux patients.
    """
    if not moniteurs:
        return None
    if len(moniteurs) == 1:
        return moniteurs[0]

    avec_patient = [m for m in moniteurs if m.patient_admis]
    if len(avec_patient) == 1:
        log.warning("%d moniteurs répondent au Data Export ; un seul a un patient "
                    "admis (%s), il est retenu.", len(moniteurs), avec_patient[0].salle)
        return avec_patient[0]
    log.error("%d moniteurs répondent au Data Export (%s) et %d ont un patient. "
              "Impossible de choisir sans risquer d'enregistrer le mauvais lit. "
              "Fixer `mac` dans la configuration pour trancher.",
              len(moniteurs), ', '.join(m.salle for m in moniteurs), len(avec_patient))
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Mémorisation
# ─────────────────────────────────────────────────────────────────────────────

def charger(repertoire: Path) -> Moniteur | None:
    chemin = Path(repertoire) / NOM_FICHIER
    if not chemin.exists():
        return None
    try:
        return Moniteur(**json.loads(chemin.read_text(encoding='utf-8')))
    except (OSError, json.JSONDecodeError, TypeError) as e:
        log.warning("%s illisible (%s) : nouvel appairage", chemin, e)
        return None


def memoriser(repertoire: Path, moniteur: Moniteur):
    chemin = Path(repertoire) / NOM_FICHIER
    chemin.write_text(json.dumps(asdict(moniteur), ensure_ascii=False, indent=2) + '\n',
                      encoding='utf-8')
    log.info("Moniteur mémorisé dans %s : %s (%s), salle %r",
             chemin, moniteur.mac, moniteur.ip, moniteur.salle)


def oublier(repertoire: Path):
    chemin = Path(repertoire) / NOM_FICHIER
    if chemin.exists():
        chemin.unlink()
        log.warning("Appairage oublié : %s supprimé", chemin)
