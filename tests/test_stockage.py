"""Schéma, migrations, et isolation de la base nominative."""
import os
import sqlite3

import pytest

from mx800.stockage import base as B
from mx800.stockage import volume as V


# ─── schéma ─────────────────────────────────────────────────────────────────

def test_base_neuve_est_a_la_derniere_version(tmp_path):
    b = B.Base(tmp_path / 'neuve.db')
    assert b.conn.execute('PRAGMA user_version').fetchone()[0] == B.VERSION_SCHEMA
    colonnes = {r[1] for r in b.conn.execute('PRAGMA table_info(sessions)')}
    assert {'mode_operation', 'mode_demonstration'} <= colonnes
    b.fermer()


def test_migration_conserve_les_donnees(tmp_path):
    """
    Une migration doit être sûre sur une base contenant déjà des données :
    on ajoute des colonnes, on ne réécrit jamais les lignes existantes.
    """
    chemin = tmp_path / 'ancienne.db'
    conn = sqlite3.connect(chemin)
    conn.executescript("""
        CREATE TABLE sessions (
            id INTEGER PRIMARY KEY, site TEXT NOT NULL, salle TEXT NOT NULL,
            moniteur_ip TEXT NOT NULL, debut_utc TEXT NOT NULL,
            horloge_source TEXT NOT NULL, horloge_synchronisee INTEGER NOT NULL);
        CREATE TABLE interventions (
            id INTEGER PRIMARY KEY, code_recherche TEXT NOT NULL UNIQUE,
            site TEXT NOT NULL, salle TEXT NOT NULL, debut_utc TEXT NOT NULL,
            fin_utc TEXT, courbes_actives INTEGER NOT NULL DEFAULT 0,
            ouverture TEXT NOT NULL DEFAULT 'auto', notes TEXT);
        CREATE TABLE mesures (
            session_id INTEGER NOT NULL, intervention_id INTEGER,
            ts_utc TEXT NOT NULL, physio_id INTEGER NOT NULL, nom TEXT,
            valeur REAL, unite INTEGER, etat INTEGER NOT NULL,
            valide INTEGER NOT NULL);
        PRAGMA user_version=1;
    """)
    conn.execute("INSERT INTO sessions (site, salle, moniteur_ip, debut_utc, "
                 "horloge_source, horloge_synchronisee) "
                 "VALUES ('HEGP','SALLE1','192.168.100.31','2026-01-01','ntp',1)")
    conn.execute("INSERT INTO interventions (code_recherche, site, salle, debut_utc) "
                 "VALUES ('ANCIENNE-1','HEGP','SALLE1','2026-01-01')")
    conn.execute("INSERT INTO mesures (session_id, ts_utc, physio_id, nom, valeur, "
                 "etat, valide) VALUES (1,'2026-01-01T00:00:00Z',16770,'HR',72.0,0,1)")
    conn.commit()
    conn.close()

    b = B.Base(chemin)
    assert b.conn.execute('PRAGMA user_version').fetchone()[0] == B.VERSION_SCHEMA
    assert b.conn.execute('SELECT count(*) FROM sessions').fetchone()[0] == 1
    ligne = b.conn.execute('SELECT * FROM interventions').fetchone()
    assert ligne['code_recherche'] == 'ANCIENNE-1'
    assert ligne['empreinte_patient'] is None
    assert ligne['demonstration'] == 0

    # la colonne `nom` de mesures est renommée `parametre`, sans perte
    mesure = b.conn.execute('SELECT * FROM mesures').fetchone()
    assert mesure['parametre'] == 'HR' and mesure['valeur'] == 72.0
    assert 'nom' not in mesure.keys()
    b.fermer()


def test_base_plus_recente_refusee(tmp_path):
    """Ne jamais rétrograder une base : on s'arrête, on n'écrit pas."""
    chemin = tmp_path / 'future.db'
    conn = sqlite3.connect(chemin)
    conn.execute(f'PRAGMA user_version={B.VERSION_SCHEMA + 1}')
    conn.close()
    with pytest.raises(RuntimeError, match='Ne pas rétrograder'):
        B.Base(chemin)


# ─── mesures ────────────────────────────────────────────────────────────────

def test_physio_id_inconnu_est_conserve(tmp_path):
    """
    En format large, un paramètre imprévu était perdu. Deux sont apparus en
    40 s lors du test sur SALLE1 (0x4261, 0x480A).
    """
    b = B.Base(tmp_path / 'x.db')
    s = b.ouvrir_session(site='HEGP', salle='SALLE1', moniteur_ip='1.2.3.4',
                         horloge_source='ntp', horloge_synchronisee=1)
    b.empiler_mesure(session_id=s, intervention_id=None, ts_utc='2026-01-01T00:00:00Z',
                     physio_id=0x4261, parametre=None, valeur=1.0, unite=0x0AA0,
                     etat=0, valide=True)
    assert b.vider_lot() == 1
    ligne = b.conn.execute('SELECT physio_id, parametre FROM mesures').fetchone()
    assert ligne['physio_id'] == 0x4261 and ligne['parametre'] is None
    b.fermer()


def test_etat_de_mesure_est_conserve(tmp_path):
    """Sans l'état par paramètre, impossible de distinguer plus tard une
    valeur mesurée d'une valeur périmée (PIPG p. 77)."""
    from mx800.protocole import constantes as C
    b = B.Base(tmp_path / 'x.db')
    s = b.ouvrir_session(site='HEGP', salle='SALLE1', moniteur_ip='1.2.3.4',
                         horloge_source='ntp', horloge_synchronisee=1)
    for etat in (0, C.MS_INVALID, C.MS_DEMO_DATA):
        b.empiler_mesure(session_id=s, intervention_id=None, ts_utc='T', physio_id=0x4182,
                         parametre='HR', valeur=70.0, unite=0, etat=etat,
                         valide=C.mesure_valide(etat))
    b.vider_lot()
    etats = {r['etat']: r['valide'] for r in b.conn.execute('SELECT etat, valide FROM mesures')}
    assert etats == {0: 1, C.MS_INVALID: 0, C.MS_DEMO_DATA: 0}
    b.fermer()


# ─── base nominative ────────────────────────────────────────────────────────

def test_identites_isolees_et_permissions(tmp_path):
    chemin = tmp_path / 'identites.db'
    bi = B.BaseIdentites(chemin)
    bi.enregistrer('HEGP-0001', {'patient_id': '1234567890', 'nom': 'X', 'prenom': 'Y'})
    bi.fermer()
    assert oct(chemin.stat().st_mode & 0o777) == '0o600'

    # permissions relâchées à la main : le module les resserre
    os.chmod(chemin, 0o644)
    B.BaseIdentites(chemin).fermer()
    assert oct(chemin.stat().st_mode & 0o777) == '0o600'


def test_aucune_identite_dans_la_base_principale(tmp_path):
    """Le nominatif ne doit exister que dans identites.db."""
    b = B.Base(tmp_path / 'mx800.db')
    tables = [r[0] for r in b.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]
    for table in tables:
        colonnes = {r[1].lower() for r in b.conn.execute(f'PRAGMA table_info("{table}")')}
        interdites = colonnes & {'nom', 'prenom', 'patient_id', 'family_name',
                                 'given_name', 'naissance'}
        assert not interdites, f"{table} contient {interdites}"
    b.fermer()


# ─── volume ─────────────────────────────────────────────────────────────────

def test_volume_sans_marqueur_refuse(tmp_path):
    """Le SSD n'est pas monté : ne pas écrire sur la carte SD en silence."""
    with pytest.raises(V.VolumeInvalide, match='marqueur'):
        V.verifier(tmp_path, site='HEGP', salle='SALLE1')


def test_volume_d_une_autre_salle_refuse(tmp_path):
    V.ecrire_marqueur(tmp_path, site='HEGP', salle='SALLE1')
    V.verifier(tmp_path, site='HEGP', salle='SALLE1')          # celui-ci passe
    with pytest.raises(V.VolumeInvalide, match='salle'):
        V.verifier(tmp_path, site='HEGP', salle='SALLE2')


def test_volume_sature_refuse(tmp_path):
    V.ecrire_marqueur(tmp_path, site='HEGP', salle='SALLE1')
    with pytest.raises(V.VolumeInvalide, match='Arrêt volontaire'):
        V.verifier(tmp_path, site='HEGP', salle='SALLE1', seuil_libre_mo=10 ** 9)
