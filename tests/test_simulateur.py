"""
Tests du simulateur MX800.

Ils vérifient que le simulateur reproduit fidèlement le comportement OBSERVÉ
sur un vrai moniteur — c'est la condition pour que les tests du module
d'acquisition, qui s'appuieront dessus, aient une valeur.
"""
import struct
import time

import pytest

from mx800.protocole import constantes as C, decodage as D, trames as T
from mx800.protocole import ondes as CAT
from outils.simulateur import Pannes


def _mds(lot):
    for genre, donnees in lot:
        if genre == 'DONNEES':
            try:
                return D.decoder_mds_create(donnees)
            except D.ErreurDecodage:
                continue
    return None


def _reponse_assoc(lot):
    for genre, donnees in lot:
        if genre == 'ASSOC_RESPONSE':
            return D.decoder_reponse_association(donnees)
    return None


# ─── Association nominale ───────────────────────────────────────────────────

def test_association_complete_sans_abort(simulateur, client):
    sim, port = simulateur(bed_label='SIM1')
    c = client(port)
    lot = c.associer()

    assert _reponse_assoc(lot) is not None
    mds = _mds(lot)
    assert mds is not None and mds.bed_label == 'SIM1'
    assert mds.modele == 'Philips M8000'
    assert sim.etat == sim.ACTIF
    assert sim.stats['mds_confirme'] == 1
    assert sim.stats['abort'] == 0
    assert not any(g == 'ABORT' for g, _ in c.recu)


def test_association_refusee(simulateur, client):
    sim, port = simulateur(pannes=Pannes(refuser_association=True))
    c = client(port)
    lot = c.associer(confirmer_mds=False)
    assert any(g == 'REFUSE' for g, _ in lot)
    assert sim.etat == sim.LIBRE


def test_une_seule_association_a_la_fois(simulateur, client):
    """p. 73 : « the IntelliVue monitor only supports one active association »."""
    sim, port = simulateur()
    premier = client(port)
    premier.associer()
    assert sim.etat == sim.ACTIF

    second = client(port)
    second.envoyer(T.construire_assoc_request())
    assert any(g == 'REFUSE' for g, _ in second.lire(0.4))


# ─── Le défaut qui a tué l'ancien module ────────────────────────────────────

def test_mds_non_confirme_provoque_renvois_puis_abort(simulateur, client):
    """
    Comportement réel capturé sur SALLE1 : trois émissions du MDS Create puis
    ABORT. La sonde de découverte de l'ancien module tombait dedans toutes les
    deux minutes, 4300 fois en six jours.
    """
    sim, port = simulateur()
    sim.PERIODE_RENVOI_MDS_S = 0.25          # cadence accélérée pour le test
    c = client(port)
    c.associer(confirmer_mds=False, duree=0.2)
    c.lire(1.5)

    assert sim.stats['mds_envoye'] == sim.RENVOIS_MDS_MAX
    assert sim.stats['mds_confirme'] == 0
    assert sim.stats['abort'] == 1
    assert any(g == 'ABORT' for g, _ in c.recu)
    assert sim.etat == sim.LIBRE


def test_release_ignore_si_association_incomplete(simulateur, client):
    """
    Le moniteur n'honore pas un Release tant que le MDS Create n'est pas
    confirmé — c'est ce qui faisait échouer l'association suivante de l'ancien
    module, émise 6 ms après un Release qui n'avait rien libéré.
    """
    sim, port = simulateur()
    sim.PERIODE_RENVOI_MDS_S = 10.0          # pas de renvoi pendant le test
    c = client(port)
    c.associer(confirmer_mds=False, duree=0.2)

    c.envoyer(T.RELEASE_REQUEST)
    assert not any(g == 'RELEASE_RESPONSE' for g, _ in c.lire(0.3))
    assert sim.stats['release_recu'] == 1
    assert sim.stats['release_repondu'] == 0
    assert sim.etat == sim.ATTENTE_CONFIRMATION


def test_release_honore_si_association_active(simulateur, client):
    sim, port = simulateur()
    c = client(port)
    c.associer()
    c.envoyer(T.RELEASE_REQUEST)
    assert any(g == 'RELEASE_RESPONSE' for g, _ in c.lire(0.3))
    assert sim.etat == sim.LIBRE


def test_timeout_keepalive(simulateur, client):
    """p. 70 : ABORT si le client se tait plus longtemps que le timeout négocié."""
    sim, port = simulateur()
    sim.TIMEOUT_KEEPALIVE_S = 0.4
    c = client(port)
    c.associer()
    c.lire(1.0)
    assert sim.stats['abort'] == 1
    assert sim.etat == sim.LIBRE


def test_keepalive_maintient_l_association(simulateur, client):
    sim, port = simulateur()
    sim.TIMEOUT_KEEPALIVE_S = 0.5
    c = client(port)
    c.associer()
    for i in range(6):
        c.envoyer(T.construire_keep_alive(100 + i))
        c.lire(0.15)
    assert sim.stats['abort'] == 0
    assert sim.stats['keepalive'] >= 5
    assert sim.etat == sim.ACTIF


# ─── Numerics ───────────────────────────────────────────────────────────────

def test_poll_numerics_renvoie_des_valeurs(simulateur, client):
    sim, port = simulateur()
    c = client(port)
    c.associer()
    c.envoyer(T.construire_poll_request(5, C.NOM_MOC_VMO_METRIC_NU,
                                        C.NOM_ATTR_GRP_METRIC_VAL_OBS))
    valeurs, dernier_vu = {}, False
    for genre, donnees in c.lire(0.4):
        if genre != 'DONNEES':
            continue
        r = D.decoder_resultat_poll(donnees)
        dernier_vu |= r.dernier
        for o in r.objets:
            for oid, brut in o.attributs.items():
                if oid == 0x0950:
                    v = D.decoder_valeur_numerique(brut)
                    valeurs[v.physio_id] = v
    assert dernier_vu, "la série doit se terminer par un RORS_APDU (p. 58)"
    assert 0x4182 in valeurs and valeurs[0x4182].valide      # HR
    assert 60 < valeurs[0x4182].valeur < 90


# ─── Négociation des courbes ────────────────────────────────────────────────

def test_courbes_accordees_quand_demandees(simulateur, client):
    sim, port = simulateur(courbes_supportees=True)
    c = client(port)
    assert _reponse_assoc(c.associer(courbes=True)).courbes_acceptees is True
    assert sim.courbes_actives is True


def test_courbes_refusees_par_le_moniteur(simulateur, client):
    """Le client DOIT parser la réponse (p. 72) : ici le moniteur dit non."""
    sim, port = simulateur(courbes_supportees=False)
    c = client(port)
    assert _reponse_assoc(c.associer(courbes=True)).courbes_acceptees is False
    assert sim.courbes_actives is False


def test_courbes_non_demandees_ne_sont_pas_accordees(simulateur, client):
    sim, port = simulateur(courbes_supportees=True)
    c = client(port)
    assert _reponse_assoc(c.associer(courbes=False)).courbes_acceptees is False


def test_poll_etendu_ignore_si_rtsa_non_negocie(simulateur, client):
    """p. 59 : le poll étendu n'est permis que si le paquet optionnel a été négocié."""
    sim, port = simulateur(courbes_supportees=False)
    c = client(port)
    c.associer(courbes=True)
    c.envoyer(T.construire_poll_request_etendu(
        20, C.NOM_MOC_VMO_METRIC_SA_RT, C.NOM_ATTR_GRP_METRIC_VAL_OBS, 10.0))
    c.lire(0.4)
    assert sim.poll_etendu is None


# ─── Courbes ────────────────────────────────────────────────────────────────

def _collecter_ondes(c, duree):
    """Renvoie [(sequence_no, {physio_id: EchantillonsOnde}, attributs_contexte)]."""
    resultats = []
    for genre, donnees in c.lire(duree):
        if genre != 'DONNEES':
            continue
        try:
            r = D.decoder_resultat_poll(donnees)
        except D.ErreurDecodage:
            continue
        if not r.etendu or not r.objets:
            continue
        ondes, contexte = {}, {}
        for o in r.objets:
            contexte.update(o.attributs)
            if C.NOM_ATTR_SA_VAL_OBS in o.attributs:
                w, _ = D.decoder_sa_obs_value(o.attributs[C.NOM_ATTR_SA_VAL_OBS])
                ondes[w.physio_id] = w
            if C.NOM_ATTR_SA_CMPD_VAL_OBS in o.attributs:
                for w in D.decoder_sa_obs_value_cmp(o.attributs[C.NOM_ATTR_SA_CMPD_VAL_OBS]):
                    ondes[w.physio_id] = w
        resultats.append((r.sequence_no, ondes, contexte))
    return resultats


def test_poll_etendu_premier_resultat_a_sequence_zero(simulateur, client):
    """p. 60-62 : le premier résultat porte sequence_no = 0, c'est la confirmation."""
    sim, port = simulateur()
    c = client(port)
    c.associer(courbes=True)
    c.envoyer(T.construire_poll_request_etendu(
        30, C.NOM_MOC_VMO_METRIC_SA_RT, C.NOM_ATTR_GRP_METRIC_VAL_OBS, 10.0))
    res = _collecter_ondes(c, 1.2)
    assert res, "aucun résultat de courbe reçu"
    assert res[0][0] == 0
    assert [s for s, _, _ in res] == list(range(len(res)))


def test_cadence_256ms_et_taille_des_tableaux(simulateur, client):
    """Table p. 286 : 128 éch. à 500 sps, 32 à 125 sps, 16 à 62,5 sps, tous les 256 ms."""
    sim, port = simulateur()
    c = client(port)
    c.associer(courbes=True)
    c.envoyer(T.construire_poll_request_etendu(
        31, C.NOM_MOC_VMO_METRIC_SA_RT, C.NOM_ATTR_GRP_METRIC_VAL_OBS, 10.0))
    res = _collecter_ondes(c, 1.3)
    assert len(res) >= 4, f"attendu ~5 résultats en 1,3 s, reçu {len(res)}"
    tailles = {0x0102: 128, 0x4BB4: 32, 0x4A14: 32, 0x50AC: 16}
    for physio, attendu in tailles.items():
        assert res[-1][1][physio].nombre == attendu


def test_ecart_de_sequence_detectable(simulateur, client):
    """
    p. 287 : le client doit suivre les horodatages pour détecter les
    échantillons manquants. Le simulateur jette un résultat sur trois.
    """
    sim, port = simulateur(pannes=Pannes(perdre_un_resultat_sur=3))
    c = client(port)
    c.associer(courbes=True)
    c.envoyer(T.construire_poll_request_etendu(
        32, C.NOM_MOC_VMO_METRIC_SA_RT, C.NOM_ATTR_GRP_METRIC_VAL_OBS, 10.0))
    sequences = [s for s, _, _ in _collecter_ondes(c, 1.5)]
    assert sim.stats['resultats_perdus'] >= 1
    trous = [b - a for a, b in zip(sequences, sequences[1:]) if b - a > 1]
    assert trous, f"aucun trou détectable dans {sequences}"


def test_contexte_multiplexe_fournit_calibration_et_periode(simulateur, client):
    """p. 287 : un objet de contexte par 1024 ms, inclus dans le poll d'observation."""
    sim, port = simulateur()
    c = client(port)
    c.associer(courbes=True)
    c.envoyer(T.construire_poll_request_etendu(
        33, C.NOM_MOC_VMO_METRIC_SA_RT, C.NOM_ATTR_GRP_METRIC_VAL_OBS, 10.0))
    vus = set()
    for _, _, contexte in _collecter_ondes(c, 1.6):
        vus |= set(contexte)
    assert C.NOM_ATTR_SCALE_SPECN_I16 in vus
    assert C.NOM_ATTR_TIME_PD_SAMP in vus
    assert C.NOM_ATTR_SA_SPECN in vus
    assert C.NOM_ATTR_SA_FIXED_VAL_SPECN in vus


def test_changement_de_calibration_en_cours_d_acquisition(simulateur, client):
    """
    p. 86 : ScaleRangeSpec16 est du contexte DYNAMIQUE. Sans STATIC_SCALE, elle
    peut changer — d'où l'obligation de la stocker horodatée avec les données.
    """
    sim, port = simulateur(pannes=Pannes(changer_calibration_apres_s=0.7))
    c = client(port)
    c.associer(courbes=True)
    c.envoyer(T.construire_poll_request_etendu(
        34, C.NOM_MOC_VMO_METRIC_SA_RT, C.NOM_ATTR_GRP_METRIC_VAL_OBS, 10.0))
    bornes = []
    for _, _, contexte in _collecter_ondes(c, 2.0):
        if C.NOM_ATTR_SCALE_SPECN_I16 in contexte:
            cal = D.decoder_calibration(contexte[C.NOM_ATTR_SCALE_SPECN_I16])
            bornes.append(cal.borne_haute_absolue)
    assert len(set(bornes)) > 1, f"la calibration n'a pas changé : {set(bornes)}"


def test_masques_de_qualite(simulateur, client):
    sim, port = simulateur(pannes=Pannes(masques_qualite=True))
    c = client(port)
    c.associer(courbes=True)
    c.envoyer(T.construire_poll_request_etendu(
        35, C.NOM_MOC_VMO_METRIC_SA_RT, C.NOM_ATTR_GRP_METRIC_VAL_OBS, 10.0))
    etats = [w.etat for _, ondes, _ in _collecter_ondes(c, 2.0) for w in ondes.values()]
    assert any(e & C.MS_INVALID for e in etats), "aucun échantillon marqué invalide"


# ─── Liste de priorité ──────────────────────────────────────────────────────

def test_liste_de_priorite_tronquee_silencieusement(simulateur, client):
    """
    p. 287 : les entrées invalides ou en excès sont ignorées SANS erreur.
    D'où l'obligation de relire la liste effective avec un GET.
    """
    sim, port = simulateur()
    c = client(port)
    c.associer(courbes=True)
    demande = [0x0102, 0x4BB4, 0xFFFF]          # 0xFFFF : onde indisponible
    c.envoyer(T.construire_set_liste_priorite(40, demande))
    c.lire(0.3)
    assert sim.liste_priorite == [0x0102, 0x4BB4]

    c.envoyer(T.construire_get_liste_priorite(41))
    effective = None
    for genre, donnees in c.lire(0.3):
        if genre != 'DONNEES':
            continue
        apdu = D.decoder_apdu(donnees)
        if apdu.command_type != C.CMD_GET:
            continue
        attributs, _ = D.decoder_liste_attributs(apdu.donnees, 6)
        brut = attributs[C.NOM_ATTR_POLL_RTSA_PRIO_LIST]
        nombre, _ = struct.unpack_from('>HH', brut, 0)
        effective = [struct.unpack_from('>I', brut, 4 + 4 * i)[0] & 0xFFFF
                     for i in range(nombre)]
    assert effective == [0x0102, 0x4BB4]
    assert len(effective) != len(demande), "l'écart demandé/effectif doit être visible"


def test_seules_les_ondes_de_la_liste_sont_emises(simulateur, client):
    sim, port = simulateur()
    c = client(port)
    c.associer(courbes=True)
    c.envoyer(T.construire_set_liste_priorite(42, [0x4BB4]))
    c.lire(0.2)
    c.envoyer(T.construire_poll_request_etendu(
        43, C.NOM_MOC_VMO_METRIC_SA_RT, C.NOM_ATTR_GRP_METRIC_VAL_OBS, 10.0))
    vues = {p for _, ondes, _ in _collecter_ondes(c, 1.0) for p in ondes}
    assert vues == {0x4BB4}


# ─── Pannes brutales ────────────────────────────────────────────────────────

def test_abort_injecte(simulateur, client):
    sim, port = simulateur(pannes=Pannes(abort_apres_s=0.3))
    c = client(port)
    c.associer()
    c.lire(0.8)
    assert any(g == 'ABORT' for g, _ in c.recu)
    assert sim.etat == sim.LIBRE


def test_silence_brutal(simulateur, client):
    """Câble arraché : plus aucune réponse, sans ABORT ni fermeture propre."""
    sim, port = simulateur(pannes=Pannes(silence_apres_s=0.3))
    c = client(port)
    c.associer()
    time.sleep(0.4)
    avant = len(c.recu)
    for i in range(3):
        c.envoyer(T.construire_poll_request(50 + i, C.NOM_MOC_VMO_METRIC_NU,
                                            C.NOM_ATTR_GRP_METRIC_VAL_OBS))
        c.lire(0.2)
    assert len(c.recu) == avant, "le simulateur devait rester muet"


def test_redemarrage_du_moniteur(simulateur, client):
    """
    Après redémarrage, l'origine du RelativeTime du moniteur avance : le compteur
    repart de zéro. Toute conversion rel_time -> temps absolu établie avant
    devient fausse (p. 62). Le module devra détecter ce recul et refaire son
    calage, sans quoi les courbes seraient replacées au mauvais instant.
    """
    sim, port = simulateur(pannes=Pannes(redemarrage_apres_s=0.4))
    origine_avant = sim.t_demarrage

    c = client(port)
    c.associer()
    fin = time.monotonic() + 2.0
    while time.monotonic() < fin and sim.etat != sim.LIBRE:
        time.sleep(0.02)

    assert sim.etat == sim.LIBRE
    assert sim.stats['abort'] == 1
    assert sim.t_demarrage > origine_avant, (
        "l'origine du RelativeTime doit avoir avancé : le compteur repart de zéro")

    c2 = client(port)
    assert _mds(c2.associer()) is not None, "le moniteur doit réaccepter une association"
    assert sim.etat == sim.ACTIF
