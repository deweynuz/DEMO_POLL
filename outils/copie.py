"""
Copie des interventions terminées vers une clé USB, et purge du Pi.

Le Pi enregistre sur son propre disque (ext4, robuste aux coupures). Pour
sortir les données, on branche une clé lisible partout (exFAT), on lance
`mx800 copier`, on la retire. La clé n'est jamais branchée pendant
l'acquisition : une coupure de courant ne peut donc pas l'abîmer.

Ce que reçoit la clé, par intervention, dans mx800/<SITE>/<CODE>/ :

    <CODE>.csv            mesures valides, une colonne par paramètre
    <CODE>.db             extrait SQLite COMPLET : mesures (valides ou non,
                          avec leur état), lacunes, sessions, événements
    <CODE>_<date>.h5      courbes
    empreintes.sha256     empreintes des fichiers (format sha256sum)

`--supprimer` ne retire du Pi que ce qui a été RELU sur la clé et trouvé
identique. La règle : rien n'est effacé sur la foi d'une écriture, seulement
sur la foi d'une relecture. Quand la commande monte elle-même la clé, la
relecture se fait après démontage et remontage — relire juste après avoir
écrit ne prouve rien, le noyau répond depuis sa mémoire.

Ne sont jamais effacés : la ligne de l'intervention, ses lacunes, ses
sessions, et la base nominative. Seules partent les mesures et les courbes.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from mx800 import config as K
from mx800.stockage import base as B

FORMATS_CLE = ('exfat', 'vfat', 'ntfs')
POINT_MONTAGE = Path('/mnt/mx800-cle')
DOSSIER_RACINE = 'mx800'
MANIFESTE = 'empreintes.sha256'
LIMITE_FAT32 = 4 * 1024**3 - 1
LOT_PURGE = 5000

LISEZMOI = """\
Données d'acquisition MX800 — copie produite par « mx800 copier ».

Un dossier par intervention, nommé par son code de recherche :

  <CODE>.csv          mesures valides, une ligne par instant, une colonne par
                      paramètre. Horodatage en UTC. S'ouvre dans Excel, R, Python.
  <CODE>.db           extrait SQLite complet : toutes les mesures (y compris les
                      invalides, avec leur état), les lacunes d'acquisition, les
                      sessions et les événements. S'ouvre avec DB Browser for
                      SQLite, ou sqlite3.
  <CODE>_<date>.h5    courbes (HDF5), échantillons bruts et calibration.
                      S'ouvre avec HDFView, h5py (Python) ou MATLAB.
  empreintes.sha256   empreintes SHA-256 des fichiers ci-dessus.

identites.csv, s'il est présent, relie les codes de recherche aux patients :
ce sont des données de santé nominatives.
"""


class ErreurCopie(Exception):
    """Échec dont le message dit quoi faire."""


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def sha256_fichier(chemin: Path) -> str:
    h = hashlib.sha256()
    with open(chemin, 'rb') as f:
        for bloc in iter(lambda: f.read(1024 * 1024), b''):
            h.update(bloc)
    return h.hexdigest()


def nom_sur(nom: str) -> str:
    """Nom de fichier acceptable sous Windows."""
    return ''.join('_' if c in '<>:"/\\|?*' or ord(c) < 32 else c for c in nom).rstrip(' .')


def _synchroniser(chemin: Path):
    with open(chemin, 'rb') as f:
        os.fsync(f.fileno())


# ─── export ─────────────────────────────────────────────────────────────────

def ecrire_csv_large(conn: sqlite3.Connection, intervention_id: int, destination: Path,
                     *, invalides: bool = False) -> tuple[int, int]:
    """
    CSV au format LARGE : une colonne par paramètre. Renvoie (instants,
    paramètres). Le format long de la base est un choix de stockage, pas
    d'exploitation.
    """
    condition = "" if invalides else " AND valide = 1"
    noms: set[str] = set()
    par_instant: dict[str, dict[str, float]] = {}
    for ts, physio, parametre, valeur in conn.execute(
            f"SELECT ts_utc, physio_id, parametre, valeur FROM mesures "
            f"WHERE intervention_id = ?{condition} ORDER BY ts_utc", (intervention_id,)):
        nom = parametre or f"PHYSIO_{physio:04X}"
        noms.add(nom)
        par_instant.setdefault(ts, {})[nom] = valeur
    colonnes = sorted(noms)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with open(destination, 'w', newline='', encoding='utf-8') as f:
        ecrivain = csv.writer(f)
        ecrivain.writerow(['ts_utc'] + colonnes)
        for instant in sorted(par_instant):
            valeurs = par_instant[instant]
            ecrivain.writerow([instant] + [valeurs.get(c, '') for c in colonnes])
    return len(par_instant), len(colonnes)


#: Tables de l'extrait et leur filtre, exprimé sur la base source « src ».
_EXTRAIT = (
    ('interventions',    "id = :i"),
    ('mesures',          "intervention_id = :i"),
    ('lacunes',          "intervention_id = :i"),
    ('fichiers_courbes', "intervention_id = :i"),
    ('sessions',         "id IN (SELECT session_id FROM src.mesures WHERE intervention_id = :i "
                         "UNION SELECT session_id FROM src.lacunes WHERE intervention_id = :i "
                         "UNION SELECT session_id FROM src.fichiers_courbes WHERE intervention_id = :i)"),
)


def ecrire_extrait(base_source: Path, intervention_id: int, destination: Path) -> dict[str, int]:
    """
    Extrait SQLite autonome d'une intervention, au même schéma que la base.
    Renvoie le nombre de lignes par table — c'est ce qui sera recompté à la
    relecture, puis comparé à la base du Pi avant toute suppression.
    """
    destination = Path(destination)
    if destination.exists():
        destination.unlink()
    conn = sqlite3.connect(f'file:{destination}?mode=rwc', uri=True, isolation_level=None)
    try:
        # Un seul fichier, sans -wal ni -shm : la clé sera lue ailleurs.
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.executescript("BEGIN;" + B.SCHEMA_COMPLET +
                           f"PRAGMA user_version={B.VERSION_SCHEMA};COMMIT;")
        conn.execute("ATTACH DATABASE ? AS src", (f'file:{base_source}?mode=ro',))
        comptes = {}
        conn.execute("BEGIN")
        for table, filtre in _EXTRAIT:
            colonnes = ','.join(r[1] for r in conn.execute(f"PRAGMA main.table_info({table})"))
            conn.execute(f"INSERT INTO main.{table} ({colonnes}) "
                         f"SELECT {colonnes} FROM src.{table} WHERE {filtre}",
                         {'i': intervention_id})
        colonnes = ','.join(r[1] for r in conn.execute("PRAGMA main.table_info(evenements)"))
        conn.execute(f"INSERT INTO main.evenements ({colonnes}) SELECT {colonnes} "
                     "FROM src.evenements WHERE session_id IN (SELECT id FROM main.sessions)")
        conn.execute("COMMIT")
        for table, _ in _EXTRAIT:
            comptes[table] = conn.execute(f"SELECT count(*) FROM main.{table}").fetchone()[0]
        conn.execute("DETACH DATABASE src")
    finally:
        conn.close()
    return comptes


def compter_extrait(chemin: Path) -> dict[str, int]:
    """Relit un extrait : intégrité SQLite, puis nombre de lignes par table."""
    conn = sqlite3.connect(f'file:{chemin}?mode=ro', uri=True)
    try:
        verdict = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if verdict != 'ok':
            raise ErreurCopie(f"{chemin.name} : extrait SQLite corrompu ({verdict})")
        return {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                for t, _ in _EXTRAIT}
    finally:
        conn.close()


# ─── copie ──────────────────────────────────────────────────────────────────

@dataclass
class Copie:
    """Ce qui a été posé sur la clé pour une intervention."""
    code: str
    intervention_id: int
    dossier: Path                       # relatif à la racine de la clé
    empreintes: dict[str, str] = field(default_factory=dict)   # nom -> sha256
    comptes: dict[str, int] = field(default_factory=dict)      # table -> lignes
    courbes_source: list[Path] = field(default_factory=list)
    octets: int = 0
    deja_presente: bool = False
    verifiee: bool = False
    erreur: str = ''


def interventions_terminees(conn: sqlite3.Connection, codes: list[str] | None = None):
    lignes = conn.execute("SELECT * FROM interventions WHERE fin_utc IS NOT NULL "
                          "ORDER BY id").fetchall()
    if codes:
        lignes = [l for l in lignes if l['code_recherche'] in codes]
    return lignes


def _courbes_de(conn, cfg: K.Configuration, interv) -> list[Path]:
    """Fichiers de courbes : ceux inscrits en base, plus ceux présents sur disque."""
    chemins = {Path(r[0]) for r in conn.execute(
        "SELECT chemin FROM fichiers_courbes WHERE intervention_id=?", (interv['id'],))}
    if cfg.courbes_dir.is_dir():
        chemins |= set(cfg.courbes_dir.glob(f"{interv['code_recherche']}_*.h5"))
    return sorted(c for c in chemins if c.is_file())


def _lire_manifeste(dossier: Path) -> dict[str, str]:
    empreintes = {}
    for ligne in (dossier / MANIFESTE).read_text(encoding='utf-8').splitlines():
        if '  ' in ligne:
            somme, nom = ligne.split('  ', 1)
            empreintes[nom.strip()] = somme.strip()
    return empreintes


def copier_intervention(cfg: K.Configuration, conn: sqlite3.Connection, interv,
                        racine: Path, *, fat32: bool = False) -> Copie:
    """
    Pose une intervention sur la clé. N'efface rien, ne vérifie rien : la
    vérification est une étape séparée, faite après remontage.
    """
    code = interv['code_recherche']
    relatif = Path(DOSSIER_RACINE) / nom_sur(interv['site']) / nom_sur(code)
    dossier = racine / relatif
    copie = Copie(code=code, intervention_id=interv['id'], dossier=relatif,
                  courbes_source=_courbes_de(conn, cfg, interv))

    # Déjà sur cette clé, avec le même nombre de mesures : on ne réécrit pas.
    nom_extrait = f"{nom_sur(code)}.db"
    if (dossier / MANIFESTE).is_file() and (dossier / nom_extrait).is_file():
        try:
            anciens = compter_extrait(dossier / nom_extrait)
            sur_pi = conn.execute("SELECT count(*) FROM mesures WHERE intervention_id=?",
                                  (interv['id'],)).fetchone()[0]
            manifeste = _lire_manifeste(dossier)
            # sur_pi == 0 : déjà purgée du Pi, la clé fait foi.
            # Les fichiers sont relus : une copie abîmée doit être refaite, pas
            # déclarée présente puis refusée indéfiniment à la vérification.
            intacte = all((dossier / nom).is_file() and sha256_fichier(dossier / nom) == somme
                          for nom, somme in manifeste.items())
            if intacte and sur_pi in (0, anciens['mesures']) and all(
                    nom_sur(c.name) in manifeste for c in copie.courbes_source):
                copie.empreintes, copie.comptes = manifeste, anciens
                copie.deja_presente = True
                return copie
        except (ErreurCopie, sqlite3.Error, OSError):
            pass                                  # copie douteuse : on la refait

    for source in copie.courbes_source:
        if fat32 and source.stat().st_size > LIMITE_FAT32:
            copie.erreur = (f"{source.name} dépasse 4 Go, la limite d'une clé FAT32 : "
                            f"utiliser une clé exFAT")
            return copie

    dossier.mkdir(parents=True, exist_ok=True)
    try:
        ecrire_csv_large(conn, interv['id'], dossier / f"{nom_sur(code)}.csv")
        copie.comptes = ecrire_extrait(cfg.base, interv['id'], dossier / nom_extrait)
        for nom in (f"{nom_sur(code)}.csv", nom_extrait):
            _synchroniser(dossier / nom)
            copie.empreintes[nom] = sha256_fichier(dossier / nom)
        for source in copie.courbes_source:
            # L'empreinte vient de la SOURCE : c'est elle que la relecture de la
            # clé devra retrouver.
            copie.empreintes[nom_sur(source.name)] = sha256_fichier(source)
            shutil.copyfile(source, dossier / nom_sur(source.name))
            _synchroniser(dossier / nom_sur(source.name))
        (dossier / MANIFESTE).write_text(
            ''.join(f"{somme}  {nom}\n" for nom, somme in sorted(copie.empreintes.items())),
            encoding='utf-8')
        _synchroniser(dossier / MANIFESTE)
        copie.octets = sum((dossier / n).stat().st_size for n in copie.empreintes)
    except (OSError, sqlite3.Error) as e:
        copie.erreur = f"écriture impossible : {e}"
    return copie


def verifier_copie(cfg: K.Configuration, conn: sqlite3.Connection, copie: Copie,
                   racine: Path) -> bool:
    """
    Relit la clé : chaque fichier doit avoir l'empreinte attendue, l'extrait
    doit être intègre et contenir autant de mesures que la base du Pi, et les
    courbes encore présentes sur le Pi doivent être identiques à leur copie.
    """
    dossier = racine / copie.dossier
    try:
        attendues = _lire_manifeste(dossier)
        if not attendues or attendues != copie.empreintes:
            raise ErreurCopie("manifeste absent ou différent de ce qui a été écrit")
        for nom, somme in attendues.items():
            if not (dossier / nom).is_file():
                raise ErreurCopie(f"{nom} manque sur la clé")
            if sha256_fichier(dossier / nom) != somme:
                raise ErreurCopie(f"{nom} : contenu différent après relecture")
        comptes = compter_extrait(dossier / f"{nom_sur(copie.code)}.db")
        sur_pi = conn.execute("SELECT count(*) FROM mesures WHERE intervention_id=?",
                              (copie.intervention_id,)).fetchone()[0]
        if sur_pi and comptes['mesures'] != sur_pi:
            raise ErreurCopie(f"{comptes['mesures']} mesures sur la clé, {sur_pi} sur le Pi")
        for source in copie.courbes_source:
            if attendues.get(nom_sur(source.name)) != sha256_fichier(source):
                raise ErreurCopie(f"{source.name} : la copie diffère du fichier du Pi")
    except (ErreurCopie, OSError, sqlite3.Error) as e:
        copie.erreur = str(e)
        return False
    copie.verifiee = True
    return True


def ecrire_identites(cfg: K.Configuration, copies: list[Copie], racine: Path, site: str) -> int:
    """Correspondance code de recherche <-> patient, pour les interventions copiées."""
    if not cfg.base_identites.exists():
        return 0
    destination = racine / DOSSIER_RACINE / nom_sur(site) / 'identites.csv'
    champs = ['code_recherche', 'patient_id', 'nom', 'prenom', 'sexe', 'type_patient',
              'admis_utc']
    lignes: dict[str, dict] = {}
    if destination.is_file():
        with open(destination, newline='', encoding='utf-8') as f:
            lignes = {l['code_recherche']: l for l in csv.DictReader(f)}
    conn = sqlite3.connect(f'file:{cfg.base_identites}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    try:
        for copie in copies:
            l = conn.execute("SELECT * FROM identites WHERE code_recherche=?",
                             (copie.code,)).fetchone()
            if l:
                lignes[copie.code] = {c: l[c] for c in champs}
    finally:
        conn.close()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with open(destination, 'w', newline='', encoding='utf-8') as f:
        ecrivain = csv.DictWriter(f, fieldnames=champs, extrasaction='ignore')
        ecrivain.writeheader()
        for code in sorted(lignes):
            ecrivain.writerow(lignes[code])
    _synchroniser(destination)
    return len(lignes)


# ─── purge ──────────────────────────────────────────────────────────────────

def enregistrer_archive(conn: sqlite3.Connection, copie: Copie, *, uuid_cle: str):
    conn.execute(
        "INSERT INTO archives (intervention_id, copie_utc, uuid_cle, dossier, octets, "
        "empreintes) VALUES (?,?,?,?,?,?)",
        (copie.intervention_id, _utc(), uuid_cle, str(copie.dossier), copie.octets,
         json.dumps(copie.empreintes, sort_keys=True)))


def purger(conn: sqlite3.Connection, copie: Copie, *, pause_s: float = 0.02) -> tuple[int, int]:
    """
    Retire du Pi les mesures et les courbes d'une intervention VÉRIFIÉE sur la
    clé. Renvoie (mesures supprimées, octets de courbes libérés).

    Les mesures partent par petits lots : le service écrit dans la même base,
    et une suppression d'un seul tenant le bloquerait assez longtemps pour
    qu'il échoue sur « database is locked ».
    """
    if not copie.verifiee:
        raise ErreurCopie(f"{copie.code} n'a pas été vérifiée sur la clé : rien n'est supprimé")
    ouverte = conn.execute("SELECT fin_utc FROM interventions WHERE id=?",
                           (copie.intervention_id,)).fetchone()
    if ouverte is None or ouverte[0] is None:
        raise ErreurCopie(f"{copie.code} est en cours : rien n'est supprimé")
    supprimees = 0
    while True:
        cur = conn.execute(
            "DELETE FROM mesures WHERE rowid IN (SELECT rowid FROM mesures "
            "WHERE intervention_id=? LIMIT ?)", (copie.intervention_id, LOT_PURGE))
        if cur.rowcount <= 0:
            break
        supprimees += cur.rowcount
        time.sleep(pause_s)
    liberes = 0
    for source in copie.courbes_source:
        try:
            liberes += source.stat().st_size
            source.unlink()
        except OSError:
            pass
    conn.execute("UPDATE archives SET purge_utc=? WHERE intervention_id=? AND purge_utc IS NULL",
                 (_utc(), copie.intervention_id))
    return supprimees, liberes


# ─── clé USB ────────────────────────────────────────────────────────────────

@dataclass
class Cle:
    peripherique: str        # /dev/sda1
    format: str
    taille: str
    uuid: str
    monte_sur: str = ''


def _aplatir(noeuds, transport=''):
    for n in noeuds:
        tran = n.get('tran') or transport
        yield n, tran
        yield from _aplatir(n.get('children') or [], tran)


def cles_usb(sortie_lsblk: str | None = None) -> list[Cle]:
    """Partitions USB lisibles sous Windows et macOS."""
    if sortie_lsblk is None:
        sortie_lsblk = subprocess.run(
            ['lsblk', '-J', '-o', 'NAME,FSTYPE,TRAN,MOUNTPOINT,SIZE,UUID'],
            capture_output=True, text=True, timeout=10, check=False).stdout
    try:
        arbre = json.loads(sortie_lsblk or '{}').get('blockdevices', [])
    except json.JSONDecodeError:
        return []
    return [Cle(peripherique=f"/dev/{n['name']}", format=n['fstype'], taille=n.get('size') or '',
                uuid=n.get('uuid') or '', monte_sur=n.get('mountpoint') or '')
            for n, tran in _aplatir(arbre)
            if tran == 'usb' and (n.get('fstype') or '') in FORMATS_CLE]


def _sudo(*commande: str) -> subprocess.CompletedProcess:
    return subprocess.run(['sudo', *commande], capture_output=True, text=True, check=False)


def monter(cle: Cle, *, lecture_seule: bool = False) -> Path:
    options = f"uid={os.getuid()},gid={os.getgid()},umask=077,noatime"
    if lecture_seule:
        options += ',ro'
    _sudo('mkdir', '-p', str(POINT_MONTAGE))
    r = _sudo('mount', '-o', options, cle.peripherique, str(POINT_MONTAGE))
    if r.returncode != 0:
        raise ErreurCopie(f"montage de {cle.peripherique} impossible : "
                          f"{r.stderr.strip() or 'erreur inconnue'}")
    return POINT_MONTAGE


def demonter(chemin: Path | str) -> bool:
    os.sync()
    return _sudo('umount', str(chemin)).returncode == 0
