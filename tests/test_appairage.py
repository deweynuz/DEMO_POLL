"""
Appairage automatique : débrancher le Pi d'une salle, le rebrancher dans une
autre, et que ça reprenne sans rien configurer.

Il y a un seul moniteur par salle, mais plusieurs appareils Philips peuvent
être visibles — un ensemble MX800 comporte plusieurs boîtiers, et les baux
dnsmasq sont en « infinite », donc jamais purgés. Un seul accepte une
association Data Export : c'est lui.
"""
import json
from pathlib import Path

import pytest

from mx800 import appairage
from mx800.machine import Etat

from test_acquisition import (lancer_simulateur, pomper, port_libre,   # noqa: F401
                              acquisition, MAC)

MAC_B = '00:09:fb:00:00:02'      # second boîtier, ne parle pas Data Export
MAC_FANTOME = '00:09:fb:aa:bb:cc'


def _baux(tmp_path: Path, entrees: list[tuple[str, str]]) -> Path:
    chemin = tmp_path / 'leases'
    chemin.write_text(''.join(f"0 {mac} {ip} * *\n" for mac, ip in entrees),
                      encoding='utf-8')
    return chemin


# ─── sonde ──────────────────────────────────────────────────────────────────

def test_sonde_identifie_le_moniteur(tmp_path):
    port = port_libre()
    sim, _ = lancer_simulateur(port, bed_label='SALLE2')
    sim.admettre('P-42')
    moniteur = appairage._sonder('127.0.0.1', MAC, port=port)
    assert moniteur is not None
    assert moniteur.bed_label == 'SALLE2'
    assert moniteur.modele == 'Philips M8000'
    assert moniteur.patient_admis is True
    assert moniteur.salle == 'SALLE2'


def test_sonde_relache_proprement(tmp_path):
    """
    La sonde de l'ancien module laissait le moniteur en plan : ~4 300 ABORT en
    six jours. Celle-ci confirme le MDS Create et attend la réponse au Release.
    """
    port = port_libre()
    sim, _ = lancer_simulateur(port)
    appairage._sonder('127.0.0.1', MAC, port=port)
    assert sim.stats['mds_confirme'] == 1
    assert sim.stats['release_repondu'] == 1
    assert sim.stats['abort'] == 0, "la sonde ne doit provoquer aucun ABORT"
    assert sim.etat == sim.LIBRE, "le moniteur doit être libre après la sonde"


def test_sonde_ignore_un_appareil_muet():
    """Le second boîtier de l'ensemble MX800 ne répond pas au protocole."""
    assert appairage._sonder('127.0.0.1', MAC_B, port=port_libre()) is None


def test_sonde_reconnait_un_moniteur_occupe():
    """Un Refuse prouve que c'est bien un endpoint Data Export (PIPG p. 73)."""
    port = port_libre()
    lancer_simulateur(port, pannes=__import__(
        'outils.simulateur', fromlist=['Pannes']).Pannes(refuser_association=True))
    moniteur = appairage._sonder('127.0.0.1', MAC, port=port)
    assert moniteur is not None and moniteur.mac == MAC


# ─── choix ──────────────────────────────────────────────────────────────────

def test_choix_evident():
    m = appairage.Moniteur(mac=MAC, ip='1.2.3.4')
    assert appairage.choisir([m]) is m
    assert appairage.choisir([]) is None


def test_choix_prefere_le_moniteur_avec_patient():
    sans = appairage.Moniteur(mac=MAC, ip='1.2.3.4')
    avec = appairage.Moniteur(mac=MAC_B, ip='1.2.3.5', patient_admis=True)
    assert appairage.choisir([sans, avec]) is avec


def test_choix_refuse_entre_deux_patients():
    """Deviner entre deux patients serait pire que ne rien enregistrer."""
    a = appairage.Moniteur(mac=MAC, ip='1.2.3.4', patient_admis=True)
    b = appairage.Moniteur(mac=MAC_B, ip='1.2.3.5', patient_admis=True)
    assert appairage.choisir([a, b]) is None


# ─── salle ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('etiquette,attendu', [
    ('SALLE1', 'SALLE1'),
    ('  SALLE2  ', 'SALLE2'),
    ('', 'MON-000001'),          # étiquette absente : repli sur la MAC
    ('   ', 'MON-000001'),
])
def test_salle_deduite_du_moniteur(etiquette, attendu):
    assert appairage.Moniteur(mac=MAC, ip='x', bed_label=etiquette).salle == attendu


# ─── mémorisation ───────────────────────────────────────────────────────────

def test_memorisation_aller_retour(tmp_path):
    m = appairage.Moniteur(mac=MAC, ip='192.168.100.31', bed_label='SALLE1',
                           system_id='00:09:fb:00:00:03', modele='Philips M8000')
    appairage.memoriser(tmp_path, m)
    assert appairage.charger(tmp_path) == m
    appairage.oublier(tmp_path)
    assert appairage.charger(tmp_path) is None


def test_memorisation_illisible_ignoree(tmp_path):
    (tmp_path / appairage.NOM_FICHIER).write_text('{ ceci nest pas du json',
                                                  encoding='utf-8')
    assert appairage.charger(tmp_path) is None


# ─── bout en bout ───────────────────────────────────────────────────────────

def test_pi_branche_sans_configuration(acquisition, tmp_path):
    """
    Aucune MAC, aucune salle en configuration. Le Pi doit trouver le moniteur,
    en tirer la salle, et enregistrer.
    """
    port = port_libre()
    sim, _ = lancer_simulateur(port, bed_label='SALLE2')
    acq, base, rapporteur = acquisition(
        port, site={'salle': ''}, moniteur={'mac': '', 'bed_label': ''})
    acq.chemin_baux = _baux(tmp_path, [(MAC, '127.0.0.1')])
    acq.moniteur = None

    assert pomper(acq, 15.0, lambda: acq.machine.etat is Etat.ASSOCIE), \
        "l'appairage automatique a échoué"
    assert acq.moniteur is not None
    assert acq.salle == 'SALLE2'
    assert (tmp_path / appairage.NOM_FICHIER).exists(), "l'appairage doit être mémorisé"

    acq.demarrer_intervention('AUTO-1')
    assert pomper(acq, 10.0, lambda: acq.machine.etat is Etat.ACQUISITION)
    session = base.conn.execute("SELECT salle, bed_label FROM sessions").fetchone()
    assert session['salle'] == 'SALLE2'
    assert session['bed_label'] == 'SALLE2'


def test_changement_de_salle(acquisition, tmp_path):
    """
    Le scénario demandé : on débranche le Pi de la salle 3, on le rebranche en
    salle 2. Le moniteur mémorisé a disparu, un autre est là.
    """
    port_a = port_libre()
    sim_a, fil_a = lancer_simulateur(port_a, bed_label='SALLE1')
    acq, base, _ = acquisition(port_a, site={'salle': ''}, moniteur={'mac': '',
                                                                     'bed_label': ''})
    acq.chemin_baux = _baux(tmp_path, [(MAC, '127.0.0.1')])
    acq.moniteur = None
    assert pomper(acq, 15.0, lambda: acq.machine.etat is Etat.ASSOCIE)
    assert acq.salle == 'SALLE1'
    acq.demarrer_intervention('SALLE3-1')
    assert pomper(acq, 10.0, lambda: acq.machine.etat is Etat.ACQUISITION)

    # déplacement : l'ancien moniteur disparaît, un autre apparaît
    sim_a.arreter(); fil_a.join(timeout=2.0)
    port_b = port_libre()
    lancer_simulateur(port_b, bed_label='SALLE2')
    acq.port_moniteur = port_b
    acq.chemin_baux = _baux(tmp_path, [(MAC_B, '127.0.0.1')])

    assert pomper(acq, 30.0, lambda: acq.machine.etat is Etat.ASSOCIE
                  and acq.moniteur is not None and acq.moniteur.mac == MAC_B), \
        "le Pi n'a pas réappairé après le changement de salle"
    assert acq.salle == 'SALLE2'
    assert acq.intervention_id is None, \
        "changer de moniteur doit clore l'intervention, pas la prolonger"

    fermee = base.conn.execute(
        "SELECT fin_utc FROM interventions WHERE code_recherche='SALLE3-1'").fetchone()
    assert fermee['fin_utc'] is not None

    acq.demarrer_intervention('SALLE2-1')
    assert pomper(acq, 10.0, lambda: acq.machine.etat is Etat.ACQUISITION)
    salles = [r['salle'] for r in base.conn.execute(
        "SELECT salle FROM sessions ORDER BY id")]
    assert 'SALLE1' in salles and 'SALLE2' in salles, salles


def test_mac_epinglee_desactive_l_appairage(acquisition, tmp_path):
    """Une MAC en configuration doit rester prioritaire."""
    port = port_libre()
    lancer_simulateur(port, bed_label='AUTRE')
    acq, base, _ = acquisition(port)          # la fixture épingle MAC
    assert pomper(acq, 10.0, lambda: acq.machine.etat is not Etat.DECONNECTE)
    assert acq.moniteur is None, "aucun appairage ne doit avoir lieu"
    assert acq.salle == 'SALLE1', "la salle configurée doit primer"
