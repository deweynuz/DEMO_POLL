"""
Stockage SQLite — format long.

Pourquoi le format long plutôt qu'une colonne par paramètre : lors du test du
09/09 sur SALLE1, deux physio_id absents du catalogue de configuration sont
apparus en 40 secondes (0x4261, 0x480A). En format large ils étaient perdus, ou
provoquaient un ALTER TABLE à chaud à chaque insertion — ce que faisait
l'ancien module. Ici une mesure inconnue est enregistrée telle quelle, avec son
identifiant brut, et pourra être nommée plus tard.

Le format long est aussi le seul qui permette de stocker le MeasurementState
PAR paramètre. Sans lui, impossible de distinguer dans six mois une valeur
mesurée d'une valeur périmée, ou de données de démonstration (bit DEMO_DATA,
PIPG p. 77).

Le format large reste ce qu'on manipule : il est produit à l'export, pas stocké.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger('mx800.base')

VERSION_SCHEMA = 2

#: Schéma complet, appliqué tel quel à une base NEUVE. Une base existante
#: passe par MIGRATIONS, une étape par version.
SCHEMA_COMPLET = """
CREATE TABLE sessions (
    id                    INTEGER PRIMARY KEY,
    site                  TEXT NOT NULL,
    salle                 TEXT NOT NULL,
    moniteur_ip           TEXT NOT NULL,
    moniteur_mac          TEXT,
    bed_label             TEXT,
    system_id             TEXT,
    modele                TEXT,
    debut_utc             TEXT NOT NULL,
    fin_utc               TEXT,
    fin_motif             TEXT,
    courbes_negociees     INTEGER NOT NULL DEFAULT 0,
    -- horloges : on enregistre les DEUX et leur écart, plutôt que d'en élire
    -- une en silence. Le moniteur SALLE1 avançait de 104 s le 09/09/2026.
    horloge_source        TEXT NOT NULL,
    horloge_synchronisee  INTEGER NOT NULL,
    moniteur_datetime_utc TEXT,
    moniteur_reltime      INTEGER,
    ecart_horloge_s       REAL,
    version_module        TEXT,
    git_commit            TEXT,
    -- NOM_ATTR_MODE_OP (PIPG p. 96). Le bit DEMO signale que le moniteur
    -- fabrique des signaux fictifs : les prendre pour des données cliniques
    -- serait une faute. On le consigne plutôt que de le déduire après coup.
    mode_operation        INTEGER,
    mode_demonstration    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE interventions (
    id               INTEGER PRIMARY KEY,
    code_recherche   TEXT NOT NULL UNIQUE,
    site             TEXT NOT NULL,
    salle            TEXT NOT NULL,
    debut_utc        TEXT NOT NULL,
    fin_utc          TEXT,
    courbes_actives  INTEGER NOT NULL DEFAULT 0,
    ouverture        TEXT NOT NULL DEFAULT 'auto',   -- auto | manuelle
    -- Empreinte de l'identifiant patient annoncé par le moniteur. Elle sert à
    -- reconnaître le MÊME patient après une coupure, sans stocker d'identité
    -- dans cette base : le nominatif vit dans identites.db, à part.
    empreinte_patient TEXT,
    demonstration    INTEGER NOT NULL DEFAULT 0,
    notes            TEXT
);
CREATE INDEX idx_interventions_empreinte ON interventions(empreinte_patient);

CREATE TABLE mesures (
    session_id      INTEGER NOT NULL REFERENCES sessions(id),
    intervention_id INTEGER REFERENCES interventions(id),
    ts_utc          TEXT NOT NULL,
    physio_id       INTEGER NOT NULL,
    -- « parametre » et non « nom » : dans une base clinique, une colonne
    -- nommée `nom` se lit spontanément comme le nom du patient.
    parametre       TEXT,
    valeur          REAL,
    unite           INTEGER,
    etat            INTEGER NOT NULL,
    valide          INTEGER NOT NULL
);
CREATE INDEX idx_mesures_intervention ON mesures(intervention_id, physio_id, ts_utc);
CREATE INDEX idx_mesures_session      ON mesures(session_id, ts_utc);

-- Les trous sont enregistrés explicitement. Une absence de lignes ne doit
-- jamais être la seule trace d'une interruption.
CREATE TABLE lacunes (
    id              INTEGER PRIMARY KEY,
    session_id      INTEGER REFERENCES sessions(id),
    intervention_id INTEGER REFERENCES interventions(id),
    debut_utc       TEXT NOT NULL,
    fin_utc         TEXT,
    duree_s         REAL,
    type            TEXT NOT NULL,
    detail          TEXT
);
CREATE INDEX idx_lacunes_intervention ON lacunes(intervention_id, debut_utc);

CREATE TABLE fichiers_courbes (
    id              INTEGER PRIMARY KEY,
    intervention_id INTEGER NOT NULL REFERENCES interventions(id),
    session_id      INTEGER REFERENCES sessions(id),
    chemin          TEXT NOT NULL,
    debut_utc       TEXT,
    fin_utc         TEXT,
    octets          INTEGER,
    sha256          TEXT,
    canaux          TEXT
);

CREATE TABLE evenements (
    id          INTEGER PRIMARY KEY,
    ts_utc      TEXT NOT NULL,
    session_id  INTEGER,
    niveau      TEXT NOT NULL,
    etat_avant  TEXT,
    etat_apres  TEXT,
    message     TEXT NOT NULL
);
CREATE INDEX idx_evenements_ts ON evenements(ts_utc);
"""

#: Migrations incrémentales. La v1 n'a jamais été déployée ; la v2 est écrite
#: malgré tout pour que le mécanisme soit exercé avant d'en avoir besoin.
MIGRATIONS: dict[int, str] = {
    2: """
        ALTER TABLE sessions      ADD COLUMN mode_operation INTEGER;
        ALTER TABLE sessions      ADD COLUMN mode_demonstration INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE interventions ADD COLUMN empreinte_patient TEXT;
        ALTER TABLE interventions ADD COLUMN demonstration INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE mesures RENAME COLUMN nom TO parametre;
        CREATE INDEX IF NOT EXISTS idx_interventions_empreinte
            ON interventions(empreinte_patient);
    """,
}

SCHEMA_IDENTITES = """
CREATE TABLE identites (
    code_recherche TEXT PRIMARY KEY,
    patient_id     TEXT,
    nom            TEXT,
    prenom         TEXT,
    sexe           TEXT,
    type_patient   TEXT,
    admis_utc      TEXT,
    maj_utc        TEXT NOT NULL
);
"""


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


class Base:
    """
    Accès SQLite. Les écritures de mesures sont groupées par lot et validées
    en une transaction — l'ancien module faisait deux commit() par ligne, soit
    autant de fsync sur la carte SD.
    """

    def __init__(self, chemin: Path, *, version_module: str = '', git_commit: str = ''):
        self.chemin = Path(chemin)
        self.chemin.parent.mkdir(parents=True, exist_ok=True)
        self.version_module, self.git_commit = version_module, git_commit
        self.conn = sqlite3.connect(self.chemin, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._migrer()
        self._lot: list[tuple] = []

    # ── schéma ──────────────────────────────────────────────────────────────

    def _migrer(self):
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version == VERSION_SCHEMA:
            return
        if version > VERSION_SCHEMA:
            raise RuntimeError(
                f"{self.chemin} est en version de schéma {version}, ce module "
                f"connaît la {VERSION_SCHEMA}. Ne pas rétrograder une base : "
                f"utiliser une version plus récente du module.")
        if version == 0:
            log.info("Création du schéma v%d dans %s", VERSION_SCHEMA, self.chemin)
            self.conn.executescript("BEGIN;" + SCHEMA_COMPLET +
                                    f"PRAGMA user_version={VERSION_SCHEMA};COMMIT;")
            return
        # Migrations incrémentales, une par version. Chacune doit être sûre sur
        # une base contenant déjà des données : on ajoute, on ne réécrit pas.
        for cible in range(version + 1, VERSION_SCHEMA + 1):
            script = MIGRATIONS.get(cible)
            if script is None:
                raise RuntimeError(f"aucune migration vers la version {cible}")
            log.warning("Migration du schéma %d -> %d de %s", cible - 1, cible,
                        self.chemin)
            self.conn.executescript("BEGIN;" + script +
                                    f"PRAGMA user_version={cible};COMMIT;")

    # ── sessions ────────────────────────────────────────────────────────────

    def ouvrir_session(self, **champs) -> int:
        champs.setdefault('debut_utc', _utc())
        champs['version_module'] = self.version_module
        champs['git_commit'] = self.git_commit
        colonnes = ','.join(champs)
        marques = ','.join('?' * len(champs))
        cur = self.conn.execute(
            f"INSERT INTO sessions ({colonnes}) VALUES ({marques})",
            tuple(champs.values()))
        return cur.lastrowid

    def fermer_session(self, session_id: int, motif: str):
        self.conn.execute("UPDATE sessions SET fin_utc=?, fin_motif=? WHERE id=?",
                          (_utc(), motif, session_id))

    # ── interventions ───────────────────────────────────────────────────────

    def ouvrir_intervention(self, code: str, *, site: str, salle: str,
                            courbes: bool = False, ouverture: str = 'auto',
                            empreinte_patient: str | None = None,
                            demonstration: bool = False) -> int:
        existante = self.conn.execute(
            "SELECT id FROM interventions WHERE code_recherche=?", (code,)).fetchone()
        if existante:
            return existante['id']
        cur = self.conn.execute(
            "INSERT INTO interventions (code_recherche, site, salle, debut_utc, "
            "courbes_actives, ouverture, empreinte_patient, demonstration) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (code, site, salle, _utc(), int(courbes), ouverture,
             empreinte_patient, int(demonstration)))
        log.info("Intervention ouverte : %s (%s)%s", code, ouverture,
                 " — MONITEUR EN MODE DÉMONSTRATION" if demonstration else "")
        return cur.lastrowid

    def intervention_pour_empreinte(self, empreinte: str) -> sqlite3.Row | None:
        """Retrouve une intervention encore ouverte pour ce patient."""
        return self.conn.execute(
            "SELECT * FROM interventions WHERE empreinte_patient=? AND fin_utc IS NULL "
            "ORDER BY id DESC LIMIT 1", (empreinte,)).fetchone()

    def fermer_intervention(self, intervention_id: int):
        self.conn.execute("UPDATE interventions SET fin_utc=? WHERE id=? AND fin_utc IS NULL",
                          (_utc(), intervention_id))

    def intervention_ouverte(self) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM interventions WHERE fin_utc IS NULL "
            "ORDER BY id DESC LIMIT 1").fetchone()

    # ── mesures ─────────────────────────────────────────────────────────────

    def empiler_mesure(self, *, session_id: int, intervention_id: int | None,
                       ts_utc: str, physio_id: int, parametre: str | None,
                       valeur: float | None, unite: int | None,
                       etat: int, valide: bool):
        self._lot.append((session_id, intervention_id, ts_utc, physio_id, parametre,
                          valeur, unite, etat, int(valide)))

    def vider_lot(self) -> int:
        """Écrit le lot en une transaction. Renvoie le nombre de lignes."""
        if not self._lot:
            return 0
        lot, self._lot = self._lot, []
        with self.conn:
            self.conn.executemany(
                "INSERT INTO mesures (session_id, intervention_id, ts_utc, physio_id, "
                "parametre, valeur, unite, etat, valide) VALUES (?,?,?,?,?,?,?,?,?)", lot)
        return len(lot)

    # ── lacunes et événements ───────────────────────────────────────────────

    def enregistrer_lacune(self, *, type: str, detail: str = '',
                           session_id: int | None = None,
                           intervention_id: int | None = None,
                           debut_utc: str | None = None, duree_s: float | None = None):
        self.conn.execute(
            "INSERT INTO lacunes (session_id, intervention_id, debut_utc, fin_utc, "
            "duree_s, type, detail) VALUES (?,?,?,?,?,?,?)",
            (session_id, intervention_id, debut_utc or _utc(), _utc(),
             duree_s, type, detail))
        log.warning("Lacune enregistrée : %s — %s", type, detail)

    def enregistrer_evenement(self, *, niveau: str, message: str,
                              session_id: int | None = None,
                              etat_avant: str | None = None,
                              etat_apres: str | None = None):
        self.conn.execute(
            "INSERT INTO evenements (ts_utc, session_id, niveau, etat_avant, "
            "etat_apres, message) VALUES (?,?,?,?,?,?)",
            (_utc(), session_id, niveau, etat_avant, etat_apres, message))

    def enregistrer_fichier_courbes(self, **champs) -> int:
        colonnes = ','.join(champs)
        cur = self.conn.execute(
            f"INSERT INTO fichiers_courbes ({colonnes}) "
            f"VALUES ({','.join('?' * len(champs))})", tuple(champs.values()))
        return cur.lastrowid

    def fermer(self):
        self.vider_lot()
        self.conn.close()


class BaseIdentites:
    """
    Table de correspondance nominative, dans un FICHIER SÉPARÉ à permissions
    0600. Décision assumée par l'utilisateur ; l'isolation limite les dégâts
    d'un export ou d'une copie distraite. Les exports utilisent le
    code_recherche par défaut, l'export nominatif demande --nominatif.
    """

    def __init__(self, chemin: Path):
        self.chemin = Path(chemin)
        self.chemin.parent.mkdir(parents=True, exist_ok=True)
        neuve = not self.chemin.exists()
        self.conn = sqlite3.connect(self.chemin, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        if neuve:
            os.chmod(self.chemin, 0o600)
        else:
            mode = self.chemin.stat().st_mode & 0o777
            if mode & 0o077:
                log.warning("permissions %o sur %s : resserrées à 0600",
                            mode, self.chemin)
                os.chmod(self.chemin, 0o600)
        if self.conn.execute("PRAGMA user_version").fetchone()[0] == 0:
            self.conn.executescript("BEGIN;" + SCHEMA_IDENTITES +
                                    "PRAGMA user_version=1;COMMIT;")

    def enregistrer(self, code_recherche: str, demographiques: dict):
        self.conn.execute(
            "INSERT INTO identites (code_recherche, patient_id, nom, prenom, sexe, "
            "type_patient, admis_utc, maj_utc) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(code_recherche) DO UPDATE SET "
            "patient_id=excluded.patient_id, nom=excluded.nom, prenom=excluded.prenom, "
            "sexe=excluded.sexe, type_patient=excluded.type_patient, maj_utc=excluded.maj_utc",
            (code_recherche, demographiques.get('patient_id'),
             demographiques.get('nom'), demographiques.get('prenom'),
             demographiques.get('sexe'), demographiques.get('type_patient'),
             demographiques.get('admis_utc'), _utc()))

    def fermer(self):
        self.conn.close()
