"""
Point d'entrée du service.

Assemble configuration, stockage, état, page d'état et boucle d'acquisition,
puis tourne jusqu'à l'arrêt. C'est ce que lance systemd.

Deux principes :

  - toute exception non gérée fait ÉCHOUER le service bruyamment (code de
    sortie non nul, journal en CRITICAL) plutôt que de le laisser tourner à
    vide. C'est l'inverse du module précédent, qui restait « actif » ;
  - le volume de stockage est vérifié AVANT d'ouvrir quoi que ce soit : mieux
    vaut refuser de démarrer que d'écrire au mauvais endroit.
"""

from __future__ import annotations

import argparse
import logging
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import config as K
from .acquisition import Acquisition
from .controle import FileCommandes, ServeurEtat
from .etat import NotificateurSystemd, RapporteurEtat
from .stockage import volume as V
from .stockage.base import Base

log = logging.getLogger('mx800')

VERSION = '0.1.0'


def _version_git() -> str:
    try:
        return subprocess.run(['git', 'rev-parse', '--short', 'HEAD'],
                              cwd=Path(__file__).resolve().parent.parent,
                              capture_output=True, text=True, timeout=5,
                              check=False).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ''


def _journaliser(niveau: str):
    logging.basicConfig(
        level=getattr(logging, niveau, logging.INFO),
        format='%(asctime)s %(levelname)-8s %(name)s: %(message)s',
        stream=sys.stderr)


def executer(chemin_config: Path, *, sans_page: bool = False,
             port_page: int = 8080, adresse_page: str = '0.0.0.0',
             duree_max: float | None = None) -> int:
    cfg = K.charger(chemin_config)
    _journaliser(cfg.niveau_journal)
    log.info("mx800 %s (%s) — site %s, salle %s", VERSION, _version_git() or 'hors git',
             cfg.site.nom, cfg.site.salle)

    # Le volume d'abord : si le SSD n'est pas monté, on écrirait sur la carte SD.
    V.verifier(cfg.stockage.chemin, site=cfg.site.nom, salle=cfg.site.salle,
               uuid_attendu=cfg.stockage.uuid,
               seuil_libre_mo=cfg.surveillance.seuil_disque_mo)
    cfg.courbes_dir.mkdir(parents=True, exist_ok=True)
    cfg.exports_dir.mkdir(parents=True, exist_ok=True)

    notificateur = NotificateurSystemd()
    intervalle = notificateur.intervalle_watchdog_s()
    if intervalle:
        log.info("Watchdog systemd : %.0f s. Alimenté par le chemin de données, ou "
                 "par les tentatives d'association sans moniteur — si plus rien ne "
                 "s'écrit ni ne se tente, systemd redémarre.", intervalle)

    base = Base(cfg.base, version_module=VERSION, git_commit=_version_git())
    rapporteur = RapporteurEtat(cfg.chemin_etat, site=cfg.site.nom,
                                salle=cfg.site.salle, version=VERSION,
                                notificateur=notificateur,
                                chemin_donnees=cfg.stockage.chemin)
    commandes = FileCommandes()
    acquisition = Acquisition(cfg, base, rapporteur, commandes=commandes)

    serveur = None
    if not sans_page:
        from dataclasses import asdict
        serveur = ServeurEtat(commandes,
                              lire_etat=lambda: asdict(rapporteur.etat),
                              resume=rapporteur.resume_court,
                              adresse=adresse_page, port=port_page)
        serveur.demarrer()

    arret = {'demande': False}

    def sur_signal(numero, _cadre):
        log.info("Signal %s reçu — arrêt propre", signal.Signals(numero).name)
        arret['demande'] = True

    signal.signal(signal.SIGTERM, sur_signal)
    signal.signal(signal.SIGINT, sur_signal)

    acquisition.ouvrir_socket()
    notificateur.pret()
    fin = None if duree_max is None else time.monotonic() + duree_max
    derniere_ecriture_etat = 0.0
    try:
        while not arret['demande'] and (fin is None or time.monotonic() < fin):
            acquisition.tick()
            maintenant = time.monotonic()
            if maintenant - derniere_ecriture_etat >= 1.0:
                derniere_ecriture_etat = maintenant
                rapporteur.mettre_a_jour(
                    etat=str(acquisition.machine.etat),
                    productif=acquisition.machine.productif,
                    depuis_s=round(acquisition.machine.depuis_s, 1))
                rapporteur.ecrire()
    finally:
        notificateur.arret("arrêt en cours")
        if serveur:
            serveur.arreter()
        acquisition.fermer('arrêt du service')
        base.fermer()
        rapporteur.mettre_a_jour(etat='ARRETE', productif=False)
        rapporteur.ecrire()
        log.info("Service arrêté proprement.")
    return 0


def principal(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Service d'acquisition MX800")
    ap.add_argument('--config', type=Path, default=K.CHEMIN_DEFAUT)
    ap.add_argument('--sans-page', action='store_true',
                    help="ne pas démarrer la page d'état locale")
    ap.add_argument('--port-page', type=int, default=8080)
    ap.add_argument('--adresse-page', default='0.0.0.0')
    ap.add_argument('--duree-max', type=float,
                    help="s'arrêter après N secondes (mise au point)")
    args = ap.parse_args(argv)
    try:
        return executer(args.config, sans_page=args.sans_page,
                        port_page=args.port_page, adresse_page=args.adresse_page,
                        duree_max=args.duree_max)
    except K.ErreurConfiguration as e:
        _journaliser('INFO')
        log.critical("%s", e)
        return 2
    except V.VolumeInvalide as e:
        _journaliser('INFO')
        log.critical("stockage inutilisable : %s", e)
        return 3
    except Exception:                                    # noqa: BLE001
        _journaliser('INFO')
        log.critical("erreur non gérée — le service s'arrête plutôt que de "
                     "tourner à vide", exc_info=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(principal())
