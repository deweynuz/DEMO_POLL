"""
Fichier d'état lisible de l'extérieur, et watchdog systemd.

Deux mécanismes distincts, à ne pas confondre :

  status.json  informe un humain ou un script : « est-ce que ça enregistre,
               là, maintenant ? » sans ouvrir la base.

  sd_notify    informe systemd. **WATCHDOG=1 n'est émis que depuis le chemin de
               données** — une ligne effectivement écrite, ou un keep-alive
               confirmé en veille assumée. Si plus rien ne s'écrit alors qu'on
               se croit en acquisition, systemd cesse de recevoir le signal et
               redémarre le service.

C'est la réponse structurelle au défaut du module précédent : il avait déjà un
Restart=always, qui n'a jamais rien redémarré parce que le processus ne mourait
pas. Ici, l'absence de données EST la panne, et elle est visible de systemd.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger('mx800.etat')


def _maintenant_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


# ─────────────────────────────────────────────────────────────────────────────
# sd_notify — protocole systemd, sans dépendance
# ─────────────────────────────────────────────────────────────────────────────

class NotificateurSystemd:
    """
    Implémente sd_notify(3) directement : une socket UNIX datagramme vers
    $NOTIFY_SOCKET. Évite une dépendance pour trois lignes de protocole.
    Inerte hors systemd, ce qui permet de lancer le module à la main.
    """

    def __init__(self):
        adresse = os.environ.get('NOTIFY_SOCKET', '')
        self.actif = bool(adresse)
        self._sock = None
        if not self.actif:
            log.debug("NOTIFY_SOCKET absent : notifications systemd désactivées")
            return
        if adresse.startswith('@'):          # espace de noms abstrait
            adresse = '\0' + adresse[1:]
        self._adresse = adresse
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC)

    def _envoyer(self, message: str):
        if not self.actif:
            return
        try:
            self._sock.sendto(message.encode('utf-8'), self._adresse)
        except OSError as e:
            log.warning("notification systemd impossible (%s) : %s", message, e)

    def pret(self):
        self._envoyer('READY=1')

    def battement(self):
        """WATCHDOG=1. À n'appeler QUE depuis le chemin de données."""
        self._envoyer('WATCHDOG=1')

    def statut(self, texte: str):
        self._envoyer(f'STATUS={texte}')

    def arret(self, texte: str = ''):
        self._envoyer('STOPPING=1' + (f'\nSTATUS={texte}' if texte else ''))

    @staticmethod
    def intervalle_watchdog_s() -> float | None:
        """WatchdogSec transmis par systemd, en secondes. None si absent."""
        micro = os.environ.get('WATCHDOG_USEC')
        if not micro:
            return None
        try:
            return int(micro) / 1_000_000
        except ValueError:
            return None


# ─────────────────────────────────────────────────────────────────────────────
# status.json
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Compteurs:
    lignes_ecrites: int = 0
    lignes_derniere_minute: int = 0
    associations: int = 0
    aborts: int = 0
    refus: int = 0
    timeouts: int = 0
    lacunes: int = 0
    resultats_ondes: int = 0
    ondes_manquantes: int = 0
    erreurs_decodage: int = 0
    reassociations_forcees: int = 0


@dataclass
class Etat:
    """Ce qu'on peut savoir du service sans ouvrir la base."""
    site: str = ''
    salle: str = ''
    version: str = ''
    etat: str = 'DECONNECTE'
    productif: bool = False
    depuis_s: float = 0.0
    moniteur_ip: str = ''
    moniteur_bed_label: str = ''
    moniteur_conforme: bool | None = None
    courbes_negociees: bool = False
    intervention: str | None = None
    derniere_ligne_utc: str | None = None
    silence_donnees_s: float | None = None
    debit_lignes_par_min: float = 0.0
    horloge_source: str = 'inconnue'
    horloge_synchronisee: bool = False
    ecart_horloge_moniteur_s: float | None = None
    disque_libre_mo: int = 0
    alerte: str | None = None
    demarre_utc: str = field(default_factory=_maintenant_utc)
    maj_utc: str = field(default_factory=_maintenant_utc)
    compteurs: Compteurs = field(default_factory=Compteurs)


class RapporteurEtat:
    """
    Tient l'état à jour et l'écrit atomiquement.

    L'écriture passe par un fichier temporaire puis os.replace() : un lecteur
    ne verra jamais un JSON tronqué, même si le service est tué en plein vol.
    """

    def __init__(self, chemin: Path, *, site: str = '', salle: str = '',
                 version: str = '', notificateur: NotificateurSystemd | None = None,
                 horloge=time.monotonic, chemin_donnees: Path | None = None):
        self.chemin = Path(chemin)
        # L'espace libre rapporté est celui du VOLUME DE DONNÉES, pas celui du
        # répertoire où vit status.json : ce dernier est sous /run, un tmpfs de
        # quelques centaines de mégaoctets. Mesurer le mauvais système de
        # fichiers ferait surveiller une valeur sans rapport avec le risque.
        self.chemin_donnees = Path(chemin_donnees) if chemin_donnees else self.chemin.parent
        self.horloge = horloge
        self.notificateur = notificateur or NotificateurSystemd()
        self.etat = Etat(site=site, salle=salle, version=version)
        self._t_derniere_ligne: float | None = None
        self._instants_lignes: list[float] = []
        self.chemin.parent.mkdir(parents=True, exist_ok=True)

    # ── chemin de données ───────────────────────────────────────────────────

    def signaler_donnees(self, lignes: int = 1):
        """
        LE point d'entrée du chemin de données. Appelé après une écriture
        réellement validée en base — pas après un poll émis, pas après un
        paquet reçu. C'est ce qui alimente le watchdog systemd.
        """
        maintenant = self.horloge()
        self._t_derniere_ligne = maintenant
        self.etat.compteurs.lignes_ecrites += lignes
        self.etat.derniere_ligne_utc = _maintenant_utc()
        self._instants_lignes.extend([maintenant] * lignes)
        limite = maintenant - 60.0
        self._instants_lignes = [t for t in self._instants_lignes if t >= limite]
        self.etat.compteurs.lignes_derniere_minute = len(self._instants_lignes)
        self.etat.debit_lignes_par_min = float(len(self._instants_lignes))
        self.notificateur.battement()

    def signaler_vie_sans_donnees(self):
        """
        Alimente le watchdog en veille ASSUMÉE : associé, aucune intervention en
        cours, donc aucune ligne à écrire. La distinction est portée par l'état,
        jamais par une exception dans le code.
        """
        self.notificateur.battement()

    def silence_donnees_s(self) -> float | None:
        if self._t_derniere_ligne is None:
            return None
        return self.horloge() - self._t_derniere_ligne

    # ── mise à jour et écriture ─────────────────────────────────────────────

    def mettre_a_jour(self, **champs):
        for cle, valeur in champs.items():
            if not hasattr(self.etat, cle):
                raise AttributeError(f"champ d'état inconnu : {cle}")
            setattr(self.etat, cle, valeur)

    def ecrire(self):
        self.etat.maj_utc = _maintenant_utc()
        self.etat.silence_donnees_s = (
            round(s, 1) if (s := self.silence_donnees_s()) is not None else None)
        try:
            usage = shutil.disk_usage(self.chemin_donnees)
            self.etat.disque_libre_mo = usage.free // (1024 * 1024)
        except OSError:
            self.etat.disque_libre_mo = -1

        contenu = json.dumps(asdict(self.etat), ensure_ascii=False, indent=2,
                             sort_keys=False)
        try:
            with tempfile.NamedTemporaryFile('w', encoding='utf-8', delete=False,
                                             dir=self.chemin.parent,
                                             prefix='.status-') as f:
                f.write(contenu + '\n')
                f.flush()
                os.fsync(f.fileno())
                provisoire = f.name
            os.replace(provisoire, self.chemin)
        except OSError as e:
            log.error("écriture de %s impossible : %s", self.chemin, e)

        self.notificateur.statut(self.resume_court())

    def resume_court(self) -> str:
        e = self.etat
        if e.alerte:
            return f"ALERTE {e.alerte}"
        if e.productif:
            return (f"{e.etat} — {e.compteurs.lignes_derniere_minute} lignes/min, "
                    f"intervention {e.intervention or '-'}")
        return f"{e.etat} depuis {e.depuis_s:.0f}s"
