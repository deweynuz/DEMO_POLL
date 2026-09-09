"""
Vérification du volume de stockage.

Le mode de panne visé : le SSD USB n'est pas monté, et les écritures partent
dans le répertoire vide qui sert de point de montage — sur la carte SD. Le
service dit « actif », les données s'accumulent au mauvais endroit, et on s'en
aperçoit quand la carte est pleine.

La parade est un marqueur écrit par install.sh à la racine du volume, contenant
l'UUID du système de fichiers attendu. Au démarrage, le module compare ; en cas
de discordance il REFUSE d'acquérir plutôt que d'écrire ailleurs.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger('mx800.volume')

NOM_MARQUEUR = '.mx800-stockage'


class VolumeInvalide(Exception):
    """Le stockage n'est pas celui attendu. Ne pas écrire."""


@dataclass
class InfoVolume:
    chemin: Path
    uuid: str
    peripherique: str
    point_montage: str
    libre_mo: int
    total_mo: int

    @property
    def libre_pourcent(self) -> float:
        return 100.0 * self.libre_mo / self.total_mo if self.total_mo else 0.0


def uuid_du_chemin(chemin: Path) -> tuple[str, str, str]:
    """Renvoie (uuid, périphérique, point de montage) du volume portant `chemin`."""
    try:
        sortie = subprocess.run(
            ['findmnt', '--target', str(chemin), '--output', 'UUID,SOURCE,TARGET',
             '--noheadings', '--first-only'],
            capture_output=True, text=True, timeout=5, check=False).stdout.split()
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("findmnt indisponible (%s) : UUID non vérifiable", e)
        return '', '', ''
    if len(sortie) >= 3:
        return sortie[0], sortie[1], sortie[2]
    if len(sortie) == 2:                     # UUID absent (tmpfs, overlay…)
        return '', sortie[0], sortie[1]
    return '', '', ''


def decrire(chemin: Path) -> InfoVolume:
    uuid, peripherique, montage = uuid_du_chemin(chemin)
    try:
        usage = shutil.disk_usage(chemin)
        libre, total = usage.free // (1024**2), usage.total // (1024**2)
    except OSError:
        libre = total = 0
    return InfoVolume(chemin=Path(chemin), uuid=uuid, peripherique=peripherique,
                      point_montage=montage, libre_mo=libre, total_mo=total)


def ecrire_marqueur(chemin: Path, *, site: str, salle: str) -> dict:
    """Appelé par install.sh (et par la commande de bascule de stockage)."""
    chemin = Path(chemin)
    chemin.mkdir(parents=True, exist_ok=True)
    info = decrire(chemin)
    contenu = {
        'site': site, 'salle': salle,
        'uuid': info.uuid, 'peripherique': info.peripherique,
        'point_montage': info.point_montage,
        'cree_par': 'mx800',
    }
    (chemin / NOM_MARQUEUR).write_text(
        json.dumps(contenu, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return contenu


def verifier(chemin: Path, *, site: str, salle: str, uuid_attendu: str = '',
             seuil_libre_mo: int = 0) -> InfoVolume:
    """
    Contrôle avant toute écriture. Lève VolumeInvalide plutôt que d'écrire au
    mauvais endroit. Chaque message dit quoi faire.
    """
    chemin = Path(chemin)
    if not chemin.is_dir():
        raise VolumeInvalide(
            f"{chemin} n'existe pas ou n'est pas un répertoire. "
            f"Le volume de données n'est probablement pas monté "
            f"(vérifier : findmnt {chemin}).")

    marqueur = chemin / NOM_MARQUEUR
    if not marqueur.exists():
        raise VolumeInvalide(
            f"marqueur {marqueur} absent. Soit le volume de données n'est pas "
            f"monté et l'on s'apprête à écrire sur la carte SD, soit "
            f"l'installation est incomplète. Relancer install.sh, ou monter le "
            f"volume attendu.")
    try:
        contenu = json.loads(marqueur.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as e:
        raise VolumeInvalide(f"marqueur {marqueur} illisible : {e}") from e

    info = decrire(chemin)

    attendu = uuid_attendu or contenu.get('uuid', '')
    if attendu and info.uuid and info.uuid != attendu:
        raise VolumeInvalide(
            f"le volume monté sur {chemin} porte l'UUID {info.uuid}, "
            f"{attendu} était attendu. Ce n'est pas le bon disque : "
            f"écrire ici mélangerait les données de deux installations.")
    if attendu and not info.uuid:
        log.warning("UUID du volume %s indéterminable : vérification impossible", chemin)

    if contenu.get('salle') and salle and contenu['salle'] != salle:
        # Le Pi est mobile : il change de salle avec son disque. Ce n'est donc
        # PAS une erreur, seulement un fait à consigner. Chaque session porte
        # sa propre salle, les données ne se mélangent pas.
        log.info("Le volume %s portait la salle %r, ce Pi est maintenant en %r. "
                 "Les données restent distinguées par session.",
                 chemin, contenu['salle'], salle)
    if contenu.get('site') and contenu['site'] != site:
        raise VolumeInvalide(
            f"le volume {chemin} appartient au site {contenu['site']!r}, "
            f"ce Pi est configuré pour {site!r}.")

    if not os.access(chemin, os.W_OK):
        raise VolumeInvalide(f"{chemin} n'est pas accessible en écriture")

    if seuil_libre_mo and info.libre_mo < seuil_libre_mo:
        raise VolumeInvalide(
            f"{info.libre_mo} Mo libres sur {chemin}, seuil fixé à "
            f"{seuil_libre_mo} Mo. Arrêt volontaire : mieux vaut ne pas démarrer "
            f"que corrompre une acquisition en cours de route.")

    log.info("Volume vérifié : %s (%s, UUID %s) — %d Mo libres sur %d",
             chemin, info.peripherique or '?', info.uuid or 'n/a',
             info.libre_mo, info.total_mo)
    return info
