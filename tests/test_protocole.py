"""
Tests du codec protocolaire contre des TRAMES RÉELLES.

Le simulateur et le client partagent mx800.protocole : un défaut d'encodage y
serait invisible si l'on se contentait d'aller-retours. Ces tests comparent donc
les octets produits aux trames capturées sur un vrai MX800 (voir
tests/trames_reference/README.md).
"""
import struct
from pathlib import Path

import pytest

from mx800.protocole import constantes as C, decodage as D, trames as T

TRAMES = Path(__file__).parent / 'trames_reference'
lire = lambda nom: (TRAMES / nom).read_bytes()


# ─── Vérité de terrain : nos octets == ceux acceptés par le moniteur ─────────

def test_assoc_request_identique_a_la_trame_reelle():
    """Le moniteur SALLE1 a accepté cette trame exacte. On doit la reproduire."""
    assert T.construire_assoc_request() == lire('assoc_request.bin')


def test_blocs_asn1_proviennent_du_guide():
    """Les enveloppes p. 298-299 doivent apparaître telles quelles dans nos trames."""
    from mx800.protocole.trames import _SESSION_DATA, _PRES_HEADER_REQ, _PRES_HEADER_RSP
    assert _SESSION_DATA in T.construire_assoc_request()
    assert _PRES_HEADER_REQ in T.construire_assoc_request()
    assert _PRES_HEADER_RSP in T.construire_assoc_response()


# ─── Identification des messages ────────────────────────────────────────────

@pytest.mark.parametrize('fichier,attendu', [
    ('assoc_request.bin',    'ASSOC_REQUEST'),
    ('assoc_response.bin',   'ASSOC_RESPONSE'),
    ('release_request.bin',  'RELEASE_REQUEST'),
    ('release_response.bin', 'RELEASE_RESPONSE'),
    ('abort.bin',            'ABORT'),
    ('mds_create_event.bin', 'DONNEES'),
])
def test_type_message(fichier, attendu):
    assert D.type_message(lire(fichier)) == attendu


def test_release_response_est_reconnue():
    """Le module précédent la classait « type inconnu » : régression à interdire."""
    assert D.type_message(lire('release_response.bin')) == 'RELEASE_RESPONSE'
    assert D.type_message(T.RELEASE_RESPONSE) == 'RELEASE_RESPONSE'


# ─── Négociation ────────────────────────────────────────────────────────────

def test_reponse_reelle_du_moniteur():
    r = D.decoder_reponse_association(lire('assoc_response.bin'))
    assert r.options_etendues == C.POLL_EXT_PERIOD_NU_1SEC
    assert r.courbes_acceptees is False          # nous n'avions pas demandé RTSA
    assert r.timeout_association_s == 10.0       # min_poll_period < 3,3 s (p. 70)


@pytest.mark.parametrize('courbes', [False, True])
def test_negociation_courbes_aller_retour(courbes):
    r = D.decoder_reponse_association(T.construire_assoc_response(courbes_acceptees=courbes))
    assert r.courbes_acceptees is courbes


def test_bit_rtsa_est_bien_pose_pour_les_courbes():
    """
    Le module précédent posait 0xA0000000 (NU_1SEC | NU_AVG_60SEC) en croyant
    demander les courbes. Le bit correct est 0x08000000 (p. 71).
    """
    trame = T.construire_assoc_request(courbes=True, max_mtu_rx=1364, max_mtu_tx=1364)
    i = trame.find(struct.pack('>H', C.NOM_ATTR_POLL_PROFILE_EXT))
    options = struct.unpack_from('>I', trame, i + 4)[0]
    assert options == 0x88000000
    assert options & C.POLL_EXT_PERIOD_RTSA
    assert not options & C.POLL_EXT_PERIOD_NU_AVG_60SEC


@pytest.mark.parametrize('kwargs,motif', [
    (dict(max_mtu_rx=1456),                'hors bornes'),   # > 1364, p. 71
    (dict(max_mtu_rx=200),                 'hors bornes'),   # < 300, p. 71
    (dict(courbes=True, max_mtu_rx=400),   'insuffisant'),   # < 500, p. 71
])
def test_mtu_hors_limites_refuse(kwargs, motif):
    with pytest.raises(ValueError, match=motif):
        T.construire_assoc_request(**kwargs)


def test_paquet_optionnel_sans_periode_numerique_refuse():
    """p. 72 : sans bit de période numérique, le paquet optionnel est ignoré."""
    with pytest.raises(ValueError, match='période numérique'):
        T.construire_user_data(options_etendues=C.POLL_EXT_PERIOD_RTSA)


# ─── MDS Create ─────────────────────────────────────────────────────────────

def test_mds_create_decode():
    m = D.decoder_mds_create(lire('mds_create_event.bin'))
    assert m.bed_label == 'SIM1'
    assert m.modele == 'Philips M8000'
    assert len(m.attributs) == 17
    assert m.date_heure.startswith('2026-01-01T00:00:00')
    assert m.temps_relatif > 0


def test_confirmation_mds_create_reproduit_la_trame_reelle():
    """
    Sans cette confirmation le moniteur ABORT à ~10 s (abort.bin). C'est le
    correctif perdu lors de la réécriture de main le 28/07.
    """
    m = D.decoder_mds_create(lire('mds_create_event.bin'))
    produit = T.construire_mds_create_result(m.invoke_id, m.managed_object, m.event_time)
    reel = lire('mds_create_result.bin')
    assert len(produit) == len(reel)
    assert produit[:10] == reel[:10]        # en-têtes SPDU/RORS identiques


# ─── Poll results ───────────────────────────────────────────────────────────

def test_chaine_de_resultats_lies():
    """p. 44 et 58 : ROLRS(FIRST) -> ROLRS(LAST) -> RORS. Le critère de fin est
    le type d'APDU, pas la taille du payload."""
    r1, r2, r3 = (D.decoder_resultat_poll(lire(f'poll_result_nu_{i}.bin')) for i in (1, 2, 3))
    assert (r1.lien_etat, r1.lien_compteur, r1.dernier) == (C.RORLS_FIRST, 1, False)
    assert (r2.lien_etat, r2.lien_compteur, r2.dernier) == (C.RORLS_LAST, 2, False)
    assert (r3.lien_etat, r3.dernier) == (None, True)
    assert r1.poll_number == r2.poll_number == r3.poll_number


def test_abs_time_stamp_non_supporte():
    """p. 62 : tous les champs à 0xff sur ce moniteur."""
    for i in (1, 2, 3):
        assert D.decoder_resultat_poll(lire(f'poll_result_nu_{i}.bin')).temps_absolu is None


def test_valeurs_numeriques_et_etats():
    r = D.decoder_resultat_poll(lire('poll_result_nu_1.bin'))
    valeurs = {}
    for o in r.objets:
        for oid, brut in o.attributs.items():
            if oid == 0x0950:
                v = D.decoder_valeur_numerique(brut)
                valeurs[v.physio_id] = v
    assert valeurs[0x4182].valeur == 72.0            # HR (neutralisée)
    assert valeurs[0x4182].valide
    assert not valeurs[0x4BB8].valide                # SpO2 : état INVALID
    assert 'invalide' in C.noms_etat(valeurs[0x4BB8].etat)


# ─── Types de base ──────────────────────────────────────────────────────────

@pytest.mark.parametrize('valeur', [0.0, 72.0, 36.5, -12.34, 147.0, 999.99])
def test_float_aller_retour(valeur):
    assert D.decoder_float(T.encoder_float(valeur)) == pytest.approx(valeur)


def test_float_valeurs_speciales():
    assert D.decoder_float(C.FLOAT_NAN) is None
    assert D.decoder_float(C.FLOAT_NRES) is None
    assert D.decoder_float(C.FLOAT_POS_INF) == float('inf')


def test_etat_mesure_premier_octet():
    """p. 77 : valide ssi le premier octet est nul."""
    assert C.mesure_valide(0x0000)
    assert C.mesure_valide(0x0080)        # VALIDATED_DATA : octet faible
    assert not C.mesure_valide(0x8000)    # INVALID
    assert not C.mesure_valide(0x0400)    # DEMO_DATA : données de démonstration


# ─── Liste de priorité des ondes ────────────────────────────────────────────

def test_text_id_partition_scada():
    assert C.text_id(0x4BB4) == 0x00024BB4      # NLS_NOM_PULS_OXIM_PLETH
    assert C.text_id(0x4A14) == 0x00024A14      # NLS_NOM_PRESS_BLD_ART_ABP


def test_trop_d_ondes_refuse():
    """p. 287 : une entrée en trop est SILENCIEUSEMENT ignorée par le moniteur."""
    with pytest.raises(ValueError, match='maximum'):
        T.construire_set_liste_priorite(1, list(range(12)))


def test_set_liste_priorite_contient_les_text_ids():
    trame = T.construire_set_liste_priorite(7, [0x4BB4, 0x4A14])
    assert struct.pack('>I', 0x00024BB4) in trame
    assert struct.pack('>I', 0x00024A14) in trame
    assert struct.pack('>H', C.NOM_ATTR_POLL_RTSA_PRIO_LIST) in trame
