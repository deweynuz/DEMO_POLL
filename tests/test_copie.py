"""Copie vers une clé USB et purge du Pi : rien n'est effacé sans relecture."""
import csv
import sqlite3

import pytest

from mx800 import config as K
from mx800.stockage import base as B
from outils import console, copie as X


@pytest.fixture
def pi(tmp_path):
    """Un Pi avec deux interventions terminées (dont une avec courbes) et une en cours."""
    donnees = tmp_path / 'pi'
    f = tmp_path / 'config.toml'
    f.write_text(f'[site]\nnom = "HEGP"\n[stockage]\nchemin = "{donnees}"\n')
    cfg = K.charger(f)
    b = B.Base(cfg.base)
    session = b.ouvrir_session(site='HEGP', salle='S1', moniteur_ip='192.0.2.1',
                               horloge_source='ntp', horloge_synchronisee=1)
    cfg.courbes_dir.mkdir(parents=True)
    for n, code in enumerate(('HEGP-S1-2026-0001', 'HEGP-S1-2026-0002', 'HEGP-S1-2026-0003')):
        i = b.ouvrir_intervention(code, site='HEGP', salle='S1', courbes=(n == 0))
        for t in range(50):
            for physio, nom in ((0x4182, 'HR'), (0x4BB8, 'SpO2')):
                b.empiler_mesure(session_id=session, intervention_id=i,
                                 ts_utc=f'2026-11-24T08:00:{t:02d}.000+00:00', physio_id=physio,
                                 parametre=nom, valeur=60.0 + t, unite=0,
                                 etat=0 if t else 0x8000, valide=bool(t))
        b.vider_lot()
        b.enregistrer_lacune(type='coupure', detail='test', session_id=session,
                             intervention_id=i)
        if n == 0:
            h5 = cfg.courbes_dir / f'{code}_2026-11-24T080000.h5'
            h5.write_bytes(bytes(range(256)) * 400)
            b.enregistrer_fichier_courbes(intervention_id=i, session_id=session,
                                          chemin=str(h5), octets=h5.stat().st_size)
        if n < 2:
            b.fermer_intervention(i)
    b.fermer()
    ident = B.BaseIdentites(cfg.base_identites)
    ident.enregistrer('HEGP-S1-2026-0001', {
        'patient_id': 'P1', 'nom': 'DUPONT', 'prenom': 'Jean', 'sexe': 'M',
        'type_patient': 'adulte', 'admis_utc': '2026-11-24T07:55:00+00:00'})
    ident.fermer()
    cle = tmp_path / 'cle'
    cle.mkdir()
    return cfg, f, cle


def _mesures(cfg, code):
    conn = sqlite3.connect(cfg.base)
    n = conn.execute("SELECT count(*) FROM mesures m JOIN interventions i "
                     "ON i.id = m.intervention_id WHERE i.code_recherche=?", (code,)).fetchone()[0]
    conn.close()
    return n


def lancer(f, *args):
    return console.principal(['--config', str(f), '--sans-couleur', 'copier', *args])


def test_copie_complete_et_relisible(pi, capsys):
    cfg, f, cle = pi
    assert lancer(f, '--vers', str(cle)) == 0
    dossier = cle / 'mx800' / 'HEGP' / 'HEGP-S1-2026-0001'
    assert (dossier / 'HEGP-S1-2026-0001_2026-11-24T080000.h5').read_bytes() == bytes(range(256)) * 400
    # CSV large : les 49 instants valides, une colonne par paramètre
    lignes = list(csv.reader(open(dossier / 'HEGP-S1-2026-0001.csv', encoding='utf-8')))
    assert lignes[0] == ['ts_utc', 'HR', 'SpO2'] and len(lignes) == 50
    # extrait : TOUTES les mesures, états compris, et la lacune
    comptes = X.compter_extrait(dossier / 'HEGP-S1-2026-0001.db')
    assert comptes == {'interventions': 1, 'mesures': 100, 'lacunes': 1,
                       'fichiers_courbes': 1, 'sessions': 1}
    assert len(X._lire_manifeste(dossier)) == 3
    # sans --supprimer, le Pi garde tout ; l'intervention en cours n'est pas copiée
    assert _mesures(cfg, 'HEGP-S1-2026-0001') == 100
    assert not (cle / 'mx800' / 'HEGP' / 'HEGP-S1-2026-0003').exists()
    assert not (cle / 'mx800' / 'HEGP' / 'identites.csv').exists()
    assert 'HEGP-S1-2026-0003 : en cours' in capsys.readouterr().out


def test_supprimer_ne_retire_que_ce_qui_est_relu(pi):
    cfg, f, cle = pi
    assert lancer(f, '--vers', str(cle), '--supprimer', '--nominatif') == 0
    assert _mesures(cfg, 'HEGP-S1-2026-0001') == 0
    assert _mesures(cfg, 'HEGP-S1-2026-0002') == 0
    assert _mesures(cfg, 'HEGP-S1-2026-0003') == 100           # en cours : intacte
    assert list(cfg.courbes_dir.glob('*.h5')) == []
    conn = sqlite3.connect(cfg.base)
    # la trace reste : interventions, lacunes, et où sont parties les données
    assert conn.execute("SELECT count(*) FROM interventions").fetchone()[0] == 3
    assert conn.execute("SELECT count(*) FROM lacunes").fetchone()[0] == 3
    assert conn.execute("SELECT count(*) FROM archives WHERE purge_utc IS NOT NULL").fetchone()[0] == 2
    conn.close()
    identites = list(csv.DictReader(open(cle / 'mx800' / 'HEGP' / 'identites.csv', encoding='utf-8')))
    assert [(l['code_recherche'], l['nom']) for l in identites] == [('HEGP-S1-2026-0001', 'DUPONT')]
    assert cfg.base_identites.exists()                          # jamais purgée
    # relancer ne recopie pas ce qui a été retiré du Pi, et n'échoue pas
    assert lancer(f, '--vers', str(cle), '--supprimer') == 0


def test_copie_alteree_rien_n_est_supprime(pi, monkeypatch, capsys):
    """La clé renvoie autre chose que ce qui a été écrit : le Pi garde tout."""
    cfg, f, cle = pi
    vraie = X.verifier_copie

    def alterer_puis_verifier(cfg_, conn, c, racine):
        if c.code.endswith('0001'):
            h5 = next((racine / c.dossier).glob('*.h5'))
            h5.write_bytes(b'\0' * h5.stat().st_size)
        return vraie(cfg_, conn, c, racine)

    monkeypatch.setattr(X, 'verifier_copie', alterer_puis_verifier)
    assert lancer(f, '--vers', str(cle), '--supprimer') == 1
    assert 'ÉCHEC' in capsys.readouterr().out
    assert _mesures(cfg, 'HEGP-S1-2026-0001') == 100
    assert len(list(cfg.courbes_dir.glob('*.h5'))) == 1
    assert _mesures(cfg, 'HEGP-S1-2026-0002') == 0              # celle-là était bonne
    # à la relance, la copie douteuse est refaite puis l'intervention purgée
    monkeypatch.setattr(X, 'verifier_copie', vraie)
    assert lancer(f, '--vers', str(cle), '--supprimer') == 0
    assert _mesures(cfg, 'HEGP-S1-2026-0001') == 0


def test_deja_sur_la_cle_puis_supprimer(pi):
    cfg, f, cle = pi
    assert lancer(f, '--vers', str(cle)) == 0
    avant = (cle / 'mx800' / 'HEGP' / 'HEGP-S1-2026-0002' / 'HEGP-S1-2026-0002.db').stat().st_mtime_ns
    assert lancer(f, '--vers', str(cle), '--supprimer') == 0
    assert (cle / 'mx800' / 'HEGP' / 'HEGP-S1-2026-0002' / 'HEGP-S1-2026-0002.db').stat().st_mtime_ns == avant
    assert _mesures(cfg, 'HEGP-S1-2026-0002') == 0


def test_purge_refusee_sans_verification(pi):
    cfg, f, cle = pi
    conn = sqlite3.connect(cfg.base)
    c = X.Copie(code='HEGP-S1-2026-0001', intervention_id=1, dossier=cle)
    with pytest.raises(X.ErreurCopie):
        X.purger(conn, c)
    c.verifiee, c.intervention_id = True, 3                     # en cours
    with pytest.raises(X.ErreurCopie):
        X.purger(conn, c)
    conn.close()


def test_detection_des_cles_usb():
    sortie = '''{"blockdevices": [
      {"name":"mmcblk0","fstype":null,"tran":"mmc","mountpoint":null,"size":"59G","uuid":null,
       "children":[{"name":"mmcblk0p2","fstype":"ext4","tran":null,"mountpoint":"/","size":"58G","uuid":"a"}]},
      {"name":"sda","fstype":null,"tran":"usb","mountpoint":null,"size":"57.3G","uuid":null,
       "children":[{"name":"sda1","fstype":"exfat","tran":null,"mountpoint":null,"size":"57.3G","uuid":"1234-ABCD"}]},
      {"name":"sdb","fstype":null,"tran":"usb","mountpoint":null,"size":"1T","uuid":null,
       "children":[{"name":"sdb1","fstype":"ext4","tran":null,"mountpoint":"/mnt/mx800","size":"1T","uuid":"b"}]}]}'''
    cles = X.cles_usb(sortie)
    assert [(c.peripherique, c.format, c.uuid) for c in cles] == [('/dev/sda1', 'exfat', '1234-ABCD')]
