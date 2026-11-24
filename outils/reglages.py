"""
Réglages modifiables après l'installation : nom de la salle, réseau moniteur.

Ces commandes modifient la configuration du Pi, pas les données. Elles ne
font rien pendant une intervention sans qu'on le leur demande explicitement :
redémarrer le service en plein enregistrement y laisse une lacune.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
import time
import tomllib
from pathlib import Path

from mx800 import config as K

RACINE = Path(__file__).resolve().parent.parent
SERVICE = 'mx800.service'
NOM_SALLE = re.compile(r'^[\w][\w .-]{0,31}$')


class ErreurReglage(Exception):
    """Échec dont le message dit quoi faire."""


def modifier_toml(texte: str, valeurs: dict[tuple[str, str], str]) -> str:
    """
    Remplace des valeurs de type chaîne dans un TOML en conservant tout le
    reste — commentaires compris, qui sont la documentation du fichier.
    `valeurs` : {(section, clé): valeur}. Une clé absente est ajoutée en tête
    de sa section ; une section absente est une erreur.
    """
    lignes = texte.splitlines()
    restantes = dict(valeurs)
    entetes: dict[str, int] = {}
    section = ''
    for n, ligne in enumerate(lignes):
        m = re.match(r'\s*\[([^\]]+)\]\s*(#.*)?$', ligne)
        if m:
            section = m.group(1).strip()
            entetes[section] = n
            continue
        m = re.match(r'(\s*)([A-Za-z0-9_]+)(\s*=\s*)', ligne)
        if m and (section, m.group(2)) in restantes:
            valeur = restantes.pop((section, m.group(2)))
            lignes[n] = f'{m.group(1)}{m.group(2)}{m.group(3)}"{valeur}"'
    for (section, cle), valeur in sorted(restantes.items(),
                                         key=lambda e: -entetes.get(e[0][0], -1)):
        if section not in entetes:
            raise ErreurReglage(f"section [{section}] absente de la configuration")
        lignes.insert(entetes[section] + 1, f'{cle} = "{valeur}"')
    return '\n'.join(lignes) + '\n'


def ecrire_config(chemin: Path, texte: str):
    """
    Vérifie le nouveau texte AVANT de remplacer le fichier : une configuration
    invalide empêcherait le service de redémarrer. L'ancienne est sauvegardée.
    """
    with tempfile.NamedTemporaryFile('w', suffix='.toml', delete=False,
                                     encoding='utf-8') as f:
        f.write(texte)
        provisoire = Path(f.name)
    try:
        tomllib.loads(texte)
        K.charger(provisoire)
        sauvegarde = f"{chemin}.bak.{time.strftime('%Y%m%d%H%M%S')}"
        try:
            Path(sauvegarde).write_text(chemin.read_text(encoding='utf-8'), encoding='utf-8')
            chemin.write_text(texte, encoding='utf-8')
        except PermissionError:
            # /etc/mx800 appartient à root : on passe par sudo, en écrasant le
            # contenu pour garder le propriétaire et les droits du fichier.
            for commande in (['cp', '-p', str(chemin), sauvegarde],
                             ['cp', '--no-preserve=mode,ownership', str(provisoire), str(chemin)]):
                r = subprocess.run(['sudo', *commande], capture_output=True, text=True, check=False)
                if r.returncode != 0:
                    raise ErreurReglage(f"écriture de {chemin} impossible : {r.stderr.strip()}")
    except (tomllib.TOMLDecodeError, K.ErreurConfiguration) as e:
        raise ErreurReglage(f"la configuration modifiée serait invalide, rien n'est changé : {e}")
    finally:
        provisoire.unlink(missing_ok=True)


def redemarrer_service() -> bool:
    return subprocess.run(['sudo', 'systemctl', 'restart', SERVICE],
                          capture_output=True, check=False).returncode == 0


def regler_salle(chemin_config: Path, nom: str) -> str:
    """
    nom = '' : la salle redevient celle qu'annonce le moniteur.
    Sinon le nom est IMPOSÉ pour étiqueter les données. L'étiquette du
    moniteur n'est plus vérifiée : un nom de salle est une étiquette, pas un
    verrou — c'est `mac` qui épingle un appareil.
    """
    if nom and not NOM_SALLE.match(nom):
        raise ErreurReglage("nom de salle invalide : 32 caractères au plus, lettres, "
                            "chiffres, espace, point, tiret")
    texte = chemin_config.read_text(encoding='utf-8')
    nouveau = modifier_toml(texte, {('site', 'salle'): nom, ('moniteur', 'bed_label'): ''})
    ecrire_config(chemin_config, nouveau)
    return nouveau


# ─── réseau ─────────────────────────────────────────────────────────────────

def _sortie(*commande: str) -> str:
    try:
        return subprocess.run(list(commande), capture_output=True, text=True,
                              timeout=15, check=False).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ''


def etat_reseau(chemin_baux: Path = Path('/var/lib/misc/dnsmasq.leases')) -> dict:
    """Ce qu'il faut voir pour savoir si un moniteur peut obtenir une adresse."""
    try:
        cable = Path('/sys/class/net/eth0/carrier').read_text().strip() == '1'
    except OSError:
        cable = False
    adresses = [m for m in _sortie('ip', '-4', '-brief', 'addr', 'show', 'eth0').split()
                if '/' in m]
    baux = []
    try:
        for ligne in chemin_baux.read_text().splitlines():
            champs = ligne.split()
            if len(champs) >= 3 and champs[1].lower().startswith('00:09:fb'):
                baux.append((champs[1], champs[2]))
    except OSError:
        pass
    return {
        'cable': cable,
        'eth0': adresses,
        'dnsmasq': _sortie('systemctl', 'is-active', 'dnsmasq') == 'active',
        'baux_philips': baux,
    }


def configurer_reseau() -> int:
    """eth0 en adresse fixe et dnsmasq — le même script que l'installation."""
    return subprocess.run(['bash', str(RACINE / 'deploiement' / 'reseau.sh')],
                          check=False).returncode
