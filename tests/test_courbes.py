"""
Acquisition des courbes, de bout en bout contre le simulateur.

Les assertions portent sur ce qui compte en recherche : la complétude est
mesurable, les trous sont tracés, la calibration est conservée telle qu'elle
était au moment de la mesure, et la perte des courbes ne fait jamais tomber
les numerics.
"""
import time
from pathlib import Path

import h5py
import numpy as np
import pytest

from mx800.machine import Etat
from mx800.protocole import constantes as C
from outils.simulateur import Pannes

# tests/ n'est pas un paquet : pytest place le répertoire sur sys.path,
# l'import est donc absolu. `acquisition` est réimportée pour que la fixture
# soit visible dans ce module.
from test_acquisition import (lancer_simulateur, pomper, port_libre,   # noqa: F401
                              acquisition, MAC)

ONDES = ['NOM_ECG_ELEC_POTL_II', 'NOM_PLETH', 'NOM_PRESS_BLD_ART_ABP', 'NOM_AWAY_CO2']
AVEC_COURBES = {'acquisition': {'courbes': True, 'ondes': ONDES}}


def _demarrer(acquisition, port, code='COURBE-0001', duree=8.0, **surcharges):
    acq, base, rapporteur = acquisition(port, **(surcharges or AVEC_COURBES))
    assert pomper(acq, duree, lambda: acq.machine.etat is Etat.ASSOCIE), \
        "association impossible"
    acq.demarrer_intervention(code)
    return acq, base, rapporteur


def _fichier(base) -> Path:
    ligne = base.conn.execute("SELECT chemin FROM fichiers_courbes").fetchone()
    assert ligne, "aucun fichier de courbes enregistré en base"
    return Path(ligne['chemin'])


def test_acquisition_de_courbes_bout_en_bout(acquisition):
    port = port_libre()
    sim, _ = lancer_simulateur(port)
    acq, base, rapporteur = _demarrer(acquisition, port)
    assert acq.courbes_negociees, "RTSA aurait dû être négocié"

    pomper(acq, 4.0)
    acq.arreter_intervention()

    with h5py.File(_fichier(base)) as f:
        canaux = sorted(f['signaux'])
        assert set(canaux) == set(ONDES), canaux

        ecg = f['signaux/NOM_ECG_ELEC_POTL_II']
        assert ecg.attrs['frequence_hz'] == pytest.approx(500.0)
        assert ecg.attrs['periode_echantillonnage_ticks'] == 16
        assert ecg.attrs['taille_tableau'] == 128
        assert ecg.shape[0] > 1000, f"trop peu d'échantillons ECG : {ecg.shape[0]}"

        co2 = f['signaux/NOM_AWAY_CO2']
        assert co2.attrs['frequence_hz'] == pytest.approx(62.5)
        # rapport des fréquences respecté entre canaux
        assert ecg.shape[0] / co2.shape[0] == pytest.approx(8.0, rel=0.25)

        # signal réellement variable, pas un tableau de zéros
        assert np.ptp(np.array(ecg[:500])) > 100

        # calage temporel exploitable
        assert f.attrs['calage_moniteur_reltime'] >= 0
        assert f.attrs['ticks_par_seconde'] == C.TICKS_PAR_SECONDE
        assert f['temps/NOM_ECG_ELEC_POTL_II'].shape[0] == ecg.attrs['blocs']

    assert rapporteur.etat.compteurs.resultats_ondes > 5


def test_courbes_refusees_par_le_moniteur(acquisition):
    """
    p. 72 : le client DOIT vérifier la réponse. Courbes refusées -> aucun
    fichier, mais les numerics doivent continuer.
    """
    port = port_libre()
    sim, _ = lancer_simulateur(port, courbes_supportees=False)
    acq, base, _ = _demarrer(acquisition, port)
    assert not acq.courbes_negociees
    assert acq.ecrivain_courbes is None

    assert pomper(acq, 8.0, lambda: acq.machine.etat is Etat.ACQUISITION), \
        "les numerics doivent continuer malgré le refus des courbes"
    assert base.conn.execute("SELECT count(*) FROM mesures").fetchone()[0] > 0
    assert base.conn.execute("SELECT count(*) FROM fichiers_courbes").fetchone()[0] == 0


def test_resultats_perdus_traces_et_combles(acquisition):
    """
    p. 62 : le sequence_no est la seule façon de repérer un message perdu.
    Le trou doit apparaître en base ET dans le canal de qualité.
    """
    port = port_libre()
    sim, _ = lancer_simulateur(port, pannes=Pannes(perdre_un_resultat_sur=4))
    acq, base, rapporteur = _demarrer(acquisition, port)
    pomper(acq, 5.0)
    acq.arreter_intervention()

    assert sim.stats['resultats_perdus'] >= 1
    lacunes = base.conn.execute(
        "SELECT detail FROM lacunes WHERE type='sequence_ondes_manquante'").fetchall()
    assert lacunes, "les pertes de séquence doivent être enregistrées"
    assert rapporteur.etat.compteurs.ondes_manquantes > 0

    with h5py.File(_fichier(base)) as f:
        qualite = np.array(f['qualite/NOM_ECG_ELEC_POTL_II'])
        manquants = int((qualite & C.QUAL_MANQUANT).astype(bool).sum())
        assert manquants > 0, "les échantillons perdus doivent être marqués manquants"
        # l'axe temporel reste continu : signal et qualité de même longueur
        assert f['signaux/NOM_ECG_ELEC_POTL_II'].shape[0] == len(qualite)


def test_changement_de_calibration_conserve(acquisition):
    """
    p. 86 : ScaleRangeSpec16 est du contexte dynamique. Les deux valeurs
    doivent être conservées, horodatées, pas écrasées.
    """
    port = port_libre()
    sim, _ = lancer_simulateur(port, pannes=Pannes(changer_calibration_apres_s=2.0))
    acq, base, _ = _demarrer(acquisition, port)
    pomper(acq, 6.0)
    acq.arreter_intervention()

    with h5py.File(_fichier(base)) as f:
        cal = np.array(f['calibrations/NOM_ECG_ELEC_POTL_II'])
        assert len(cal) >= 2, f"un seul jeu de calibration enregistré : {cal}"
        assert not np.allclose(cal['haut_absolu'][0], cal['haut_absolu'][-1]), \
            "le changement de calibration n'a pas été conservé"
        assert cal['reltime'][0] < cal['reltime'][-1], "les calibrations sont horodatées"


def test_masques_de_qualite_separes_du_signal(acquisition):
    """
    p. 83-84 : un échantillon invalide ou un spike de pacemaker ne doit jamais
    être stocké comme une valeur physiologique ordinaire.
    """
    port = port_libre()
    sim, _ = lancer_simulateur(port, pannes=Pannes(masques_qualite=True))
    acq, base, _ = _demarrer(acquisition, port)
    pomper(acq, 6.0)
    acq.arreter_intervention()

    with h5py.File(_fichier(base)) as f:
        marques = 0
        for nom in f['qualite']:
            q = np.array(f[f'qualite/{nom}'])
            marques += int((q & (C.QUAL_INVALIDE | C.QUAL_PACEMAKER
                                 | C.QUAL_SATURATION)).astype(bool).sum())
        assert marques > 0, "aucun échantillon marqué alors que le simulateur en injecte"


def test_liste_d_ondes_non_retenue_est_signalee(acquisition):
    """
    p. 287 : le moniteur ignore SILENCIEUSEMENT une onde indisponible. La
    relecture par GET doit rendre l'écart visible.
    """
    port = port_libre()
    sim, _ = lancer_simulateur(port)
    # le moniteur ne sait produire que l'ECG : les trois autres seront ignorées
    sim.ondes = [o for o in sim.ondes if o.physio_id == 0x0102]
    sim.liste_priorite = [0x0102]

    acq, base, _ = _demarrer(acquisition, port)
    pomper(acq, 4.0)

    assert acq.ondes_effectives == [0x0102], acq.ondes_effectives
    lacunes = base.conn.execute(
        "SELECT detail FROM lacunes WHERE type='ondes_non_retenues'").fetchall()
    assert lacunes, "l'écart demandé/effectif doit être tracé"
    assert 'NOM_PLETH' in lacunes[0]['detail']


def test_perte_des_courbes_ne_fait_pas_tomber_les_numerics(acquisition):
    """Exigence explicite : dégrader, journaliser, continuer."""
    port = port_libre()
    sim, _ = lancer_simulateur(port)
    acq, base, rapporteur = _demarrer(acquisition, port)
    assert pomper(acq, 8.0, lambda: acq.machine.etat is Etat.ACQUISITION)

    for _ in range(3):
        acq._degrader_courbes("débit insoutenable (simulé)")
    assert acq.ecrivain_courbes is None
    assert not acq.courbes_actives
    assert 'courbes coupées' in (rapporteur.etat.alerte or '')

    mesures_avant = base.conn.execute("SELECT count(*) FROM mesures").fetchone()[0]
    pomper(acq, 3.0)
    assert base.conn.execute(
        "SELECT count(*) FROM mesures").fetchone()[0] > mesures_avant, \
        "les numerics doivent continuer après la coupure des courbes"
