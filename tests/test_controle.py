"""
Pilotage : file de commandes, serveur d'état, catalogue des paramètres.
"""
import json
import threading
import time
import urllib.request

import pytest

from mx800.controle import FileCommandes, ServeurEtat
from mx800.machine import Etat
from mx800.protocole import parametres as PARAM

from test_acquisition import (lancer_simulateur, pomper, port_libre,   # noqa: F401
                              acquisition, MAC)


# ─── catalogue des paramètres ───────────────────────────────────────────────

@pytest.mark.parametrize('physio_id,court,nom', [
    (0x4182, 'HR',   'NOM_ECG_CARD_BEAT_RATE'),
    (0x4BB8, 'SpO2', 'NOM_PULS_OXIM_SAT_O2'),
    (0x4A15, 'ABPs', 'NOM_PRESS_BLD_ART_ABP_SYS'),
    (0x4A17, 'ABPm', 'NOM_PRESS_BLD_ART_ABP_MEAN'),
    (0x4261, 'PVC',  'NOM_ECG_V_P_C_CNT'),
])
def test_catalogue_parametres(physio_id, court, nom):
    p = PARAM.CATALOGUE[physio_id]
    assert (p.court, p.nom) == (court, nom)


def test_noms_courts_uniques():
    """Une colonne d'export ne doit jamais désigner deux grandeurs."""
    courts = [p.court for p in PARAM.CATALOGUE.values()]
    doublons = {c for c in courts if courts.count(c) > 1}
    assert not doublons, doublons


def test_parametre_inconnu_reste_identifiable():
    assert PARAM.nom_court(0xFFFF) == 'PHYSIO_FFFF'
    assert PARAM.CATALOGUE.get(0xFFFF) is None


def test_onde_et_numeric_ne_sont_pas_confondus():
    """
    0x5000 est l'onde d'impédance respiratoire, 0x500A la fréquence
    respiratoire. Les confondre nommerait une mesure d'après un signal.
    """
    assert PARAM.CATALOGUE[0x5000].nom == 'NOM_RESP'
    assert PARAM.CATALOGUE[0x500A].nom == 'NOM_RESP_RATE'


# ─── file de commandes ──────────────────────────────────────────────────────

def test_file_commandes_aller_retour():
    file = FileCommandes()
    commande = file.deposer('demarrer', code='X-1')
    recue = file.retirer()
    assert recue is commande and recue.parametres == {'code': 'X-1'}
    recue.repondre(True, 'fait')
    assert commande.attendre(1.0) == {'ok': True, 'message': 'fait'}


def test_file_vide():
    assert FileCommandes().retirer() is None


def test_commande_sans_reponse_ne_bloque_pas():
    """Si la boucle est morte, l'appelant ne doit pas attendre indéfiniment."""
    commande = FileCommandes().deposer('demarrer')
    debut = time.monotonic()
    reponse = commande.attendre(0.3)
    assert not reponse['ok'] and 'à temps' in reponse['message']
    assert time.monotonic() - debut < 1.0


def test_file_saturee_repond_au_lieu_de_bloquer():
    file = FileCommandes(taille=2)
    for _ in range(2):
        file.deposer('x')
    trop = file.deposer('x')
    assert trop.attendre(0.5)['message'] == 'file de commandes saturée'


# ─── serveur d'état ─────────────────────────────────────────────────────────

@pytest.fixture
def serveur():
    lances = []

    def fabriquer(etat: dict, file: FileCommandes | None = None):
        file = file or FileCommandes()
        s = ServeurEtat(file, lire_etat=lambda: etat,
                        resume=lambda: etat.get('etat', '?'),
                        adresse='127.0.0.1', port=0)
        port = s.demarrer()
        lances.append(s)
        return s, port, file

    yield fabriquer
    for s in lances:
        s.arreter()


def _get(port, chemin):
    with urllib.request.urlopen(f'http://127.0.0.1:{port}{chemin}', timeout=3) as r:
        return r.status, r.read().decode()


def test_api_etat(serveur):
    _, port, _ = serveur({'etat': 'ACQUISITION', 'productif': True, 'salle': 'SALLE1'})
    code, corps = _get(port, '/api/etat')
    assert code == 200 and json.loads(corps)['etat'] == 'ACQUISITION'


def test_page_html_servie(serveur):
    _, port, _ = serveur({'etat': 'ACQUISITION', 'productif': True, 'salle': 'SALLE1'})
    code, corps = _get(port, '/')
    assert code == 200
    assert 'SALLE1' in corps and 'ENREGISTREMENT' in corps


def test_sante_repond_200_quand_ca_enregistre(serveur):
    _, port, _ = serveur({'etat': 'ACQUISITION', 'productif': True, 'alerte': None})
    assert _get(port, '/sante')[0] == 200


def test_sante_repond_503_quand_ca_n_enregistre_pas(serveur):
    _, port, _ = serveur({'etat': 'DECONNECTE', 'productif': False, 'alerte': None})
    with pytest.raises(urllib.error.HTTPError) as e:
        _get(port, '/sante')
    assert e.value.code == 503


def test_sante_repond_503_sur_alerte(serveur):
    _, port, _ = serveur({'etat': 'ACQUISITION', 'productif': True,
                          'alerte': 'courbes coupées'})
    with pytest.raises(urllib.error.HTTPError) as e:
        _get(port, '/sante')
    assert e.value.code == 503


def test_post_depose_une_commande(serveur):
    _, port, file = serveur({'etat': 'ASSOCIE', 'productif': False})
    resultats = {}

    def client():
        requete = urllib.request.Request(f'http://127.0.0.1:{port}/api/demarrer',
                                         data=b'{}', method='POST',
                                         headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(requete, timeout=5) as r:
            resultats['reponse'] = json.loads(r.read())

    fil = threading.Thread(target=client)
    fil.start()
    for _ in range(50):
        commande = file.retirer()
        if commande:
            commande.repondre(True, 'intervention X démarrée', code='X')
            break
        time.sleep(0.05)
    fil.join(timeout=5)
    assert resultats['reponse'] == {'ok': True, 'message': 'intervention X démarrée',
                                    'code': 'X'}


# ─── commandes exécutées par la boucle ──────────────────────────────────────

def test_demarrage_et_arret_par_commande(acquisition):
    port = port_libre()
    sim, _ = lancer_simulateur(port)
    file = FileCommandes()
    acq, base, rapporteur = acquisition(port,
                                        acquisition={'intervention_auto': False})
    acq.commandes = file
    assert pomper(acq, 8.0, lambda: acq.machine.etat is Etat.ASSOCIE)

    commande = file.deposer('demarrer', code='MANUEL-1')
    pomper(acq, 1.0)
    reponse = commande.attendre(1.0)
    assert reponse['ok'], reponse
    assert acq.code_intervention == 'MANUEL-1'
    interv = base.conn.execute("SELECT ouverture FROM interventions").fetchone()
    assert interv['ouverture'] == 'manuelle'

    doublon = file.deposer('demarrer')
    pomper(acq, 1.0)
    assert not doublon.attendre(1.0)['ok'], "un second démarrage doit être refusé"

    arret = file.deposer('arreter')
    pomper(acq, 1.0)
    assert arret.attendre(1.0)['ok']
    assert acq.intervention_id is None


def test_commande_inconnue_repond_sans_planter(acquisition):
    port = port_libre()
    lancer_simulateur(port)
    file = FileCommandes()
    acq, _, _ = acquisition(port)
    acq.commandes = file
    commande = file.deposer('autodestruction')
    pomper(acq, 0.5)
    assert not commande.attendre(1.0)['ok']


def test_demarrage_refuse_si_non_associe(acquisition):
    port = port_libre()          # personne n'écoute
    file = FileCommandes()
    acq, _, _ = acquisition(port)
    acq.commandes = file
    commande = file.deposer('demarrer')
    pomper(acq, 1.0)
    reponse = commande.attendre(1.0)
    assert not reponse['ok'] and 'non associé' in reponse['message']


# ─── reprise après redémarrage ──────────────────────────────────────────────

def test_intervention_reprise_apres_redemarrage(acquisition, tmp_path):
    """
    Le watchdog peut faire redémarrer le service EN PLEINE INTERVENTION.
    Sans reprise, le cas serait coupé en deux enregistrements distincts.
    """
    port = port_libre()
    sim, _ = lancer_simulateur(port)
    acq, base, _ = acquisition(port, acquisition={'intervention_auto': False})
    assert pomper(acq, 8.0, lambda: acq.machine.etat is Etat.ASSOCIE)
    acq.demarrer_intervention('REPRISE-1')
    assert pomper(acq, 8.0, lambda: acq.machine.etat is Etat.ACQUISITION)
    mesures_avant = base.conn.execute("SELECT count(*) FROM mesures").fetchone()[0]
    acq.fermer('arrêt simulé')

    # nouveau processus, même base : l'intervention doit être reprise
    acq2, base2, _ = acquisition(port, acquisition={'intervention_auto': False})
    assert acq2.code_intervention == 'REPRISE-1'
    assert pomper(acq2, 10.0, lambda: acq2.machine.etat is Etat.ACQUISITION)
    assert base2.conn.execute(
        "SELECT count(*) FROM mesures").fetchone()[0] > mesures_avant
    assert base2.conn.execute(
        "SELECT count(*) FROM interventions").fetchone()[0] == 1, \
        "un second enregistrement aurait coupé le cas en deux"
    assert base2.conn.execute(
        "SELECT count(*) FROM lacunes WHERE type='service_redemarre'").fetchone()[0] == 1


def test_espace_libre_mesure_le_volume_de_donnees(tmp_path):
    """
    status.json vit sous /run (tmpfs de quelques centaines de Mo) tandis que
    les données vont sur un tout autre système de fichiers. Mesurer celui de
    status.json ferait surveiller une valeur sans rapport avec le risque —
    constaté en production le 09/09/2026 : 742 Mo affichés pour 20 540 réels.
    """
    import shutil
    from mx800.etat import RapporteurEtat

    donnees = tmp_path / 'donnees'
    donnees.mkdir()
    etat = tmp_path / 'run' / 'status.json'
    rapporteur = RapporteurEtat(etat, chemin_donnees=donnees)
    rapporteur.ecrire()
    attendu = shutil.disk_usage(donnees).free // (1024 * 1024)
    assert abs(rapporteur.etat.disque_libre_mo - attendu) <= 2

    # sans chemin_donnees, on retombe sur le répertoire de status.json
    defaut = RapporteurEtat(tmp_path / 'run2' / 'status.json')
    assert defaut.chemin_donnees == (tmp_path / 'run2')
