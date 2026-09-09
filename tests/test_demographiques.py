"""
Démographiques patient et interventions automatiques.

Le plug-and-play visé : personne ne lance rien en salle. Le moniteur annonce
l'admission d'un patient, l'intervention s'ouvre ; il annonce la sortie, elle
se ferme. Un nouveau patient en ouvre une autre, sans que les deux se
mélangent.
"""
import sqlite3

import pytest

from mx800.machine import Etat
from mx800.protocole import constantes as C
from mx800.protocole import decodage as D

from test_acquisition import (lancer_simulateur, pomper, port_libre,   # noqa: F401
                              acquisition, MAC)

AUTO = {'acquisition': {'periode_demographiques_s': 0.5}}


def _associer(fabrique, port, **surcharges):
    """`fabrique` est la fixture `acquisition` ; le paramètre porte un autre nom
    pour ne pas entrer en collision avec la section de configuration."""
    fusion = {section: dict(valeurs) for section, valeurs in AUTO.items()}
    for section, valeurs in surcharges.items():
        fusion.setdefault(section, {}).update(valeurs)
    acq, base, rapporteur = fabrique(port, **fusion)
    assert pomper(acq, 8.0, lambda: acq.machine.etat is Etat.ASSOCIE)
    return acq, base, rapporteur


# ─── décodage ───────────────────────────────────────────────────────────────

def test_etats_patient(): 
    """p. 103 : seul ADMITTED signifie « informations présentes et valides »."""
    import struct
    for etat, admis in ((C.PT_EMPTY, False), (C.PT_PRE_ADMITTED, False),
                        (C.PT_ADMITTED, True), (C.PT_DISCHARGED, False)):
        d = D.decoder_demographiques({C.NOM_ATTR_PT_DEMOG_ST: struct.pack('>H', etat)})
        assert d.admis is admis, d.etat_nom


def test_identifiant_de_repli_sur_le_nom():
    """Sans patient_id, deux patients successifs seraient confondus."""
    d = D.Demographiques(etat=C.PT_ADMITTED, nom='Dupont', prenom='Marie')
    assert d.identifiant == 'Dupont|Marie'
    d.patient_id = '1234567890'
    assert d.identifiant == '1234567890'


# ─── ouverture et fermeture automatiques ────────────────────────────────────

def test_intervention_ouverte_a_l_admission(acquisition):
    port = port_libre()
    sim, _ = lancer_simulateur(port)
    acq, base, rapporteur = _associer(acquisition, port)

    pomper(acq, 2.0)
    assert acq.intervention_id is None, "aucun patient : aucune intervention"

    sim.admettre('1234567890', nom='Dupont', prenom='Marie', sexe=2)
    assert pomper(acq, 6.0, lambda: acq.intervention_id is not None), \
        "l'admission n'a pas ouvert d'intervention"

    interv = base.conn.execute("SELECT * FROM interventions").fetchone()
    assert interv['ouverture'] == 'auto'
    assert interv['empreinte_patient'] == '1234567890'
    assert interv['code_recherche'].startswith('HEGP-SALLE1-')
    assert interv['code_recherche'].endswith('-0001')
    assert rapporteur.etat.intervention == interv['code_recherche']

    assert pomper(acq, 6.0, lambda: acq.machine.etat is Etat.ACQUISITION)
    assert base.conn.execute("SELECT count(*) FROM mesures").fetchone()[0] > 0


def test_intervention_fermee_a_la_sortie(acquisition):
    port = port_libre()
    sim, _ = lancer_simulateur(port)
    acq, base, _ = _associer(acquisition, port)
    sim.admettre('P1')
    assert pomper(acq, 6.0, lambda: acq.intervention_id is not None)
    pomper(acq, 2.0)

    sim.sortir()
    assert pomper(acq, 6.0, lambda: acq.intervention_id is None), \
        "la sortie du patient n'a pas fermé l'intervention"
    interv = base.conn.execute("SELECT * FROM interventions").fetchone()
    assert interv['fin_utc'] is not None


def test_deux_patients_ne_se_melangent_pas(acquisition):
    port = port_libre()
    sim, _ = lancer_simulateur(port)
    acq, base, _ = _associer(acquisition, port)

    sim.admettre('PATIENT-A')
    assert pomper(acq, 6.0, lambda: acq.intervention_id is not None)
    assert pomper(acq, 6.0, lambda: acq.machine.etat is Etat.ACQUISITION)
    premiere = acq.intervention_id
    pomper(acq, 1.5)

    sim.admettre('PATIENT-B')
    assert pomper(acq, 8.0, lambda: acq.intervention_id not in (None, premiere)), \
        "le changement de patient n'a pas ouvert une nouvelle intervention"
    seconde = acq.intervention_id
    pomper(acq, 2.0)

    codes = base.conn.execute(
        "SELECT id, code_recherche, empreinte_patient, fin_utc "
        "FROM interventions ORDER BY id").fetchall()
    assert len(codes) == 2
    assert [c['empreinte_patient'] for c in codes] == ['PATIENT-A', 'PATIENT-B']
    assert codes[0]['fin_utc'] is not None, "la première doit être close"
    assert codes[1]['fin_utc'] is None
    assert codes[0]['code_recherche'].endswith('-0001')
    assert codes[1]['code_recherche'].endswith('-0002')

    # aucune mesure ne doit être rattachée aux deux
    par_intervention = dict(base.conn.execute(
        "SELECT intervention_id, count(*) FROM mesures GROUP BY intervention_id"))
    assert set(par_intervention) == {premiere, seconde}, par_intervention


def test_identite_isolee_dans_sa_base(acquisition, tmp_path):
    """Le nominatif ne doit jamais entrer dans mx800.db."""
    port = port_libre()
    sim, _ = lancer_simulateur(port)
    acq, base, _ = _associer(acquisition, port)
    sim.admettre('1234567890', nom='Dupont', prenom='Marie', sexe=2)
    assert pomper(acq, 6.0, lambda: acq.intervention_id is not None)

    identites = sqlite3.connect(acq.config.base_identites)
    identites.row_factory = sqlite3.Row
    ligne = identites.execute("SELECT * FROM identites").fetchone()
    assert ligne['nom'] == 'Dupont' and ligne['prenom'] == 'Marie'
    assert ligne['patient_id'] == '1234567890'
    assert ligne['code_recherche'] == acq.code_intervention
    identites.close()
    assert oct(acq.config.base_identites.stat().st_mode & 0o777) == '0o600'

    contenu = acq.config.base.read_bytes()
    assert b'Dupont' not in contenu and b'Marie' not in contenu, \
        "une identité s'est glissée dans la base principale"


def test_mode_manuel_ignore_les_demographiques(acquisition):
    port = port_libre()
    sim, _ = lancer_simulateur(port)
    acq, base, _ = _associer(acquisition, port,
                             acquisition={'intervention_auto': False})
    sim.admettre('P1')
    pomper(acq, 4.0)
    assert acq.intervention_id is None
    assert base.conn.execute("SELECT count(*) FROM interventions").fetchone()[0] == 0


# ─── mode démonstration ─────────────────────────────────────────────────────

def test_mode_demonstration_detecte_et_refuse(acquisition):
    """
    p. 96 : en mode DEMO le moniteur fabrique des signaux. Les enregistrer
    comme des données cliniques serait une faute.
    """
    port = port_libre()
    sim, _ = lancer_simulateur(port,
                               mode_operation=C.OPMODE_MONITOR | C.OPMODE_DEMO)
    acq, base, rapporteur = _associer(acquisition, port)

    session = base.conn.execute("SELECT * FROM sessions").fetchone()
    assert session['mode_demonstration'] == 1
    assert session['mode_operation'] == C.OPMODE_MONITOR | C.OPMODE_DEMO

    sim.admettre('DEMO-1')
    pomper(acq, 4.0)
    assert acq.intervention_id is None, "aucune intervention en mode démonstration"
    assert 'démonstration' in (rapporteur.etat.alerte or '')
    assert base.conn.execute("SELECT count(*) FROM mesures").fetchone()[0] == 0


def test_mode_demonstration_accepte_si_demande(acquisition):
    """Enregistrer malgré tout reste possible, mais les données sont marquées."""
    port = port_libre()
    sim, _ = lancer_simulateur(port,
                               mode_operation=C.OPMODE_MONITOR | C.OPMODE_DEMO)
    acq, base, _ = _associer(acquisition, port,
                             acquisition={'refuser_mode_demo': False})
    sim.admettre('DEMO-1')
    assert pomper(acq, 6.0, lambda: acq.intervention_id is not None)
    interv = base.conn.execute("SELECT * FROM interventions").fetchone()
    assert interv['demonstration'] == 1, \
        "l'intervention doit porter la marque du mode démonstration"
