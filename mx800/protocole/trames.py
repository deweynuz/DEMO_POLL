"""
Construction des messages Data Export.

Comme pour le décodage : ni réseau, ni base. Des paramètres en entrée, des
octets en sortie. Le simulateur et le client partagent ce module, ce qui a une
conséquence à connaître : un défaut d'encodage y serait invisible en test, les
deux côtés faisant la même erreur. C'est pourquoi tests/test_protocole.py
compare les octets produits ici aux TRAMES RÉELLES capturées sur un MX800.

Références : guide rév. G.0. Les blocs ASN.1 fixes viennent des « Association
Control Protocol Examples », p. 298-299, et l'exemple complet de User Data
de la p. 304.
"""

from __future__ import annotations
import struct

from . import constantes as C


# ─────────────────────────────────────────────────────────────────────────────
# Types de base
# ─────────────────────────────────────────────────────────────────────────────

def encoder_float(valeur: float | None, decimales: int = 2) -> int:
    """
    FLOAT-Type Philips (p. 40) : mantisse 24 bits signée, exposant 8 bits signé
    en poids fort. Valeur = mantisse * 10^exposant.
    """
    if valeur is None:
        return C.FLOAT_NAN
    if valeur == float('inf'):
        return C.FLOAT_POS_INF
    if valeur == float('-inf'):
        return C.FLOAT_NEG_INF
    exposant = -decimales
    mantisse = int(round(valeur * (10 ** decimales)))
    if not (-(1 << 23) <= mantisse < (1 << 23)):
        raise ValueError(f"{valeur} inencodable avec {decimales} décimales "
                         f"(mantisse {mantisse} hors 24 bits signés)")
    return ((exposant & 0xFF) << 24) | (mantisse & 0xFFFFFF)


def encoder_chaine(texte: str, taille_totale: int, utf16: bool = True) -> bytes:
    """Chaîne préfixée de sa longueur (p. 40), complétée de zéros."""
    brut = texte.encode('utf-16-be' if utf16 else 'latin-1')
    corps = brut.ljust(taille_totale, b'\x00')[:taille_totale]
    return struct.pack('>H', taille_totale) + corps


def encoder_temps_absolu(annee, mois, jour, heure, minute, seconde, centiemes=0) -> bytes:
    """AbsoluteTime, 8 octets BCD (p. 62 pour l'usage, format déduit des trames)."""
    bcd = lambda n: int(str(n).zfill(2), 16)
    return bytes([bcd(annee // 100), bcd(annee % 100), bcd(mois), bcd(jour),
                  bcd(heure), bcd(minute), bcd(seconde), bcd(centiemes)])


ABS_TIME_NON_SUPPORTE = b'\xff' * 8   # p. 62 : le moniteur met tout à 0xff


def encoder_liste_attributs(attributs: list[tuple[int, bytes]]) -> bytes:
    """AttributeList : { count, length, AVAType[] } ; AVAType = { oid, len, val }."""
    corps = b''.join(struct.pack('>HH', oid, len(val)) + val for oid, val in attributs)
    return struct.pack('>HH', len(attributs), len(corps)) + corps


def _longueur_asn(n: int) -> bytes:
    """
    ASNLength (p. 68) : un octet si <= 127 ; sinon 0x80|nb_octets suivi de la
    longueur en gros-boutien.
    """
    if n <= 127:
        return bytes([n])
    octets = n.to_bytes((n.bit_length() + 7) // 8, 'big')
    return bytes([0x80 | len(octets)]) + octets


# ─────────────────────────────────────────────────────────────────────────────
# Enveloppes ASN.1 fixes — p. 298 (requête) et p. 299 (réponse)
# Recopiées telles quelles du guide. Le champ <LI> est calculé à l'exécution.
# ─────────────────────────────────────────────────────────────────────────────

_SESSION_DATA = bytes.fromhex(   # p. 298 et 299 — identique pour requête et réponse
    '0508130100160102800014020002'
)

_PRES_HEADER_REQ = bytes.fromhex(   # p. 298, sans l'octet 0xC1 ni le champ <LI>
    '3180a0808001010000a280a003000001a4803080020101060452010001308006'
    '025101000000003080020102060c2a8648ce14020100000001013080060c2a86'
    '48ce140201000000020100000000000061803080020101a0806080a180060c2a'
    '8648ce14020100000003010000be802880060c2a8648ce140201000000010102'
    '010281'
)

_PRES_HEADER_RSP = bytes.fromhex(   # p. 299, sans l'octet 0xC1 ni le champ <LI>
    '3180a0808001010000a280a003000001a5803080800100810251010000308080'
    '0100810c2a8648ce14020100000002010000000061803080020101a0806180a1'
    '80060c2a8648ce14020100000003010000a203020100a305a103020100be8028'
    '8002010281'
)

_PRES_TRAILER = bytes(16)                                               # p. 298, 299


def _envelopper_association(type_session: int, entete_pres: bytes, user_data: bytes) -> bytes:
    """
    p. 68 : le champ longueur de l'en-tête de présentation couvre tout ce qui
    suit ; celui de l'en-tête de session couvre tout, remorque comprise.
    """
    interieur = entete_pres + user_data + _PRES_TRAILER
    presentation = bytes([0xC1, len(interieur)]) + interieur
    corps = _SESSION_DATA + presentation
    return bytes([type_session, len(corps)]) + corps


# ─────────────────────────────────────────────────────────────────────────────
# MDSEUserInfoStd — p. 68-71, exemple complet p. 304
# ─────────────────────────────────────────────────────────────────────────────

MDDL_VERSION1  = 0x80000000   # p. 304
NOMEN_VERSION  = 0x40000000   # p. 304
COLD_START     = 0x20000000   # p. 69


def construire_user_data(*, type_systeme: int = C.SYST_CLIENT,
                         min_poll_period_ticks: int = 2500,
                         max_mtu_rx: int = 1000, max_mtu_tx: int = 1000,
                         options_profil: int = 0x60000000,
                         options_etendues: int = C.OPTIONS_NUMERICS) -> bytes:
    """
    Construit <ASNLength><MDSEUserInfoStd>.

    `options_etendues` est LE champ qui décide de l'export des courbes :
    poser C.POLL_EXT_PERIOD_RTSA (0x08000000) en plus d'un bit de période
    numérique (p. 72). Le module précédent posait 0xA0000000 en croyant y
    mettre RTSA — il demandait en réalité des numerics moyennés sur 1 minute.
    """
    # Pas de contrôle de MTU ici : cette fonction sert aussi au simulateur, qui
    # doit pouvoir rejouer le comportement réel du moniteur — lequel renvoie
    # max_mtu_tx = 1456, AU-DESSUS du maximum de 1364 annoncé p. 71 (observé sur
    # SALLE1 le 09/09/2026). Les garde-fous portent sur ce que NOUS demandons,
    # dans construire_assoc_request().
    if options_etendues and not (options_etendues & 0xF0000000):
        raise ValueError("aucun bit de période numérique posé : le paquet optionnel "
                         "serait ignoré en entier (p. 72)")

    profil_ext = encoder_liste_attributs([
        (C.NOM_ATTR_POLL_PROFILE_EXT,
         struct.pack('>I', options_etendues) + struct.pack('>HH', 0, 0)),   # + ext_attr vide
    ])
    poll_profile = (struct.pack('>IIIII', C.POLL_PROFILE_REV_0, min_poll_period_ticks,
                                max_mtu_rx, max_mtu_tx, 0xFFFFFFFF)
                    + struct.pack('>I', options_profil) + profil_ext)
    aprofiles = encoder_liste_attributs([(C.NOM_POLL_PROFILE_SUPPORT, poll_profile)])

    info = (struct.pack('>IIIII', MDDL_VERSION1, NOMEN_VERSION, 0,
                        type_systeme, COLD_START)
            + struct.pack('>HH', 0, 0)      # option_list vide
            + aprofiles)
    return _longueur_asn(len(info)) + info


def construire_assoc_request(*, courbes: bool = False, max_mtu_rx: int = 1000,
                             max_mtu_tx: int = 1000,
                             min_poll_period_ticks: int = 2500) -> bytes:
    """Association Request (p. 67-68, blocs p. 298)."""
    options = C.OPTIONS_COURBES if courbes else C.OPTIONS_NUMERICS
    if not (C.MTU_MIN_EXIGE <= max_mtu_rx <= C.MTU_MAX_LAN):
        raise ValueError(f"max_mtu_rx={max_mtu_rx} hors bornes "
                         f"[{C.MTU_MIN_EXIGE}, {C.MTU_MAX_LAN}] (p. 71)")
    if not (C.MTU_MIN_EXIGE <= max_mtu_tx <= C.MTU_MAX_LAN):
        raise ValueError(f"max_mtu_tx={max_mtu_tx} hors bornes "
                         f"[{C.MTU_MIN_EXIGE}, {C.MTU_MAX_LAN}] (p. 71)")
    if courbes and max_mtu_rx < C.MTU_MIN_COURBES:
        raise ValueError(f"MTU {max_mtu_rx} insuffisant pour 256 ms de courbe "
                         f"en un message : {C.MTU_MIN_COURBES} minimum (p. 71)")
    user = construire_user_data(min_poll_period_ticks=min_poll_period_ticks,
                                max_mtu_rx=max_mtu_rx, max_mtu_tx=max_mtu_tx,
                                options_etendues=options)
    return _envelopper_association(C.CN_SPDU_SI, _PRES_HEADER_REQ, user)


def construire_assoc_response(*, courbes_acceptees: bool = False,
                              min_poll_period_ticks: int = 4000,
                              max_mtu_rx: int = 1000, max_mtu_tx: int = 1456) -> bytes:
    """
    Association Response (p. 73, blocs p. 299). Utilisée par le SIMULATEUR.
    Le moniteur repose le bit RTSA s'il accepte l'export de courbes (p. 72).
    """
    options = C.OPTIONS_NUMERICS | (C.POLL_EXT_PERIOD_RTSA if courbes_acceptees else 0)
    user = construire_user_data(type_systeme=C.SYST_SERVER,
                                min_poll_period_ticks=min_poll_period_ticks,
                                max_mtu_rx=max_mtu_rx, max_mtu_tx=max_mtu_tx,
                                options_etendues=options)
    return _envelopper_association(C.AC_SPDU_SI, _PRES_HEADER_RSP, user)


# Release / Abort : pas de données variables (p. 72). Octets réels observés.
RELEASE_REQUEST  = bytes.fromhex('0918c11661803080020101a08062808001000000000000000000')
RELEASE_RESPONSE = bytes.fromhex('0a18c11661803080020101a08063808001000000000000000000')


# ─────────────────────────────────────────────────────────────────────────────
# Messages de données
# ─────────────────────────────────────────────────────────────────────────────

def _spdu(charge: bytes) -> bytes:
    return struct.pack('>HH', C.SPDU_MAGIC, C.SPDU_CTX) + charge


def _roiv(invoke_id: int, command_type: int, corps: bytes) -> bytes:
    interieur = struct.pack('>HHH', invoke_id & 0xFFFF, command_type, len(corps)) + corps
    return _spdu(struct.pack('>HH', C.ROIV_APDU, len(interieur)) + interieur)


def _rors(invoke_id: int, command_type: int, corps: bytes) -> bytes:
    interieur = struct.pack('>HHH', invoke_id & 0xFFFF, command_type, len(corps)) + corps
    return _spdu(struct.pack('>HH', C.RORS_APDU, len(interieur)) + interieur)


def _rolrs(etat: int, compteur: int, invoke_id: int, command_type: int, corps: bytes) -> bytes:
    """ROLRSapdu (p. 44) : RorlsId{state, count} AVANT invoke_id."""
    interieur = (struct.pack('>BB', etat, compteur)
                 + struct.pack('>HHH', invoke_id & 0xFFFF, command_type, len(corps)) + corps)
    return _spdu(struct.pack('>HH', C.ROLRS_APDU, len(interieur)) + interieur)


def _action(invoke_id: int, action_type: int, charge: bytes) -> bytes:
    corps = (struct.pack('>HHH', C.NOM_MOC_VMS_MDS, 0, 0)      # managed_object
             + struct.pack('>HH', action_type, len(charge)) + charge)
    return _roiv(invoke_id, C.CMD_CONFIRMED_ACTION, corps)


def construire_poll_request(invoke_id: int, classe_objet: int, groupe_attributs: int) -> bytes:
    """SINGLE POLL DATA REQUEST — p. 55."""
    charge = struct.pack('>HHHH', invoke_id & 0xFFFF, 0x0001, classe_objet, groupe_attributs)
    return _action(invoke_id, C.NOM_ACT_POLL_MDIB_DATA, charge)


def construire_keep_alive(invoke_id: int) -> bytes:
    """
    p. 63 : « A suitable keep alive message would be a Poll Request for the
    Alert Monitor object, requesting the VMO Static Context Attribute group. »
    Le résultat est court, donc peu coûteux pour le moniteur.
    """
    return construire_poll_request(invoke_id, C.NOM_MOC_VMO_AL_MON,
                                   C.NOM_ATTR_GRP_VMO_STATIC)


def construire_poll_request_etendu(invoke_id: int, classe_objet: int,
                                   groupe_attributs: int,
                                   periode_active_s: float) -> bytes:
    """
    EXTENDED POLL DATA REQUEST — p. 59-60.
    `periode_active_s` remplit PollDataReqPeriod.active_period : durée pendant
    laquelle le moniteur émettra des résultats périodiques. Il faut renvoyer une
    nouvelle requête AVANT son expiration (p. 61).
    """
    periode = encoder_liste_attributs([
        (C.NOM_ATTR_TIME_PD_POLL, struct.pack('>I', C.secondes_vers_ticks(periode_active_s))),
    ])
    charge = (struct.pack('>HHHH', invoke_id & 0xFFFF, 0x0001, classe_objet, groupe_attributs)
              + periode)
    return _action(invoke_id, C.NOM_ACT_POLL_MDIB_DATA_EXT, charge)


def construire_mds_create_result(invoke_id: int, managed_object: bytes,
                                 event_time: bytes) -> bytes:
    """
    Confirmation du MDS Create Event. SANS ELLE, le moniteur ré-émet
    l'événement trois fois puis ABORT à ~10 s — comportement capturé sur
    SALLE1 le 09/09/2026 (cf. tests/trames_reference/abort.bin).
    """
    corps = managed_object + event_time + struct.pack('>HH', C.NOM_NOTI_MDS_CREAT, 0)
    return _rors(invoke_id, C.CMD_CONFIRMED_EVENT_REPORT, corps)


def construire_get_liste_priorite(invoke_id: int) -> bytes:
    """GET PRIORITY LIST REQUEST — p. 63. AttributeIdList = liste des ondes."""
    corps = (struct.pack('>HHH', C.NOM_MOC_VMS_MDS, 0, 0)
             + struct.pack('>HH', 1, 2)                     # AttributeIdList
             + struct.pack('>H', C.NOM_ATTR_POLL_RTSA_PRIO_LIST))
    return _roiv(invoke_id, C.CMD_GET, corps)


# ModificationList : opérations de SET (p. 64)
REPLACE         = 0
ADD_VALUES      = 1   # non supportée par le moniteur (p. 64)
REMOVE_VALUES   = 2   # non supportée par le moniteur (p. 64)
SET_TO_DEFAULT  = 3


def construire_set_liste_priorite(invoke_id: int, physio_ids: list[int]) -> bytes:
    """
    SET PRIORITY LIST REQUEST — p. 64, opération REPLACE avec une TextIdList.

    Les limites de la p. 286-287 sont vérifiées ici plutôt que subies : une
    entrée en trop est SILENCIEUSEMENT ignorée par le moniteur, donc mieux vaut
    refuser tout de suite que croire avoir demandé douze ondes.
    """
    if len(physio_ids) > C.MAX_ONDES_ECG + C.MAX_ONDES_NON_ECG:
        raise ValueError(f"{len(physio_ids)} ondes demandées : maximum "
                         f"{C.MAX_ONDES_ECG} ECG + {C.MAX_ONDES_NON_ECG} non-ECG (p. 286-287)")
    text_ids = b''.join(struct.pack('>I', C.text_id(p)) for p in physio_ids)
    liste = struct.pack('>HH', len(physio_ids), len(text_ids)) + text_ids
    modification = (struct.pack('>H', REPLACE)
                    + struct.pack('>HH', C.NOM_ATTR_POLL_RTSA_PRIO_LIST, len(liste))
                    + liste)
    corps = (struct.pack('>HHH', C.NOM_MOC_VMS_MDS, 0, 0)
             + struct.pack('>HH', 1, len(modification)) + modification)
    return _roiv(invoke_id, C.CMD_CONFIRMED_SET, corps)


def construire_set_liste_priorite_defaut(invoke_id: int) -> bytes:
    """SET_TO_DEFAULT : attribut vide, longueur 0 (p. 64)."""
    modification = (struct.pack('>H', SET_TO_DEFAULT)
                    + struct.pack('>HH', C.NOM_ATTR_POLL_RTSA_PRIO_LIST, 0))
    corps = (struct.pack('>HHH', C.NOM_MOC_VMS_MDS, 0, 0)
             + struct.pack('>HH', 1, len(modification)) + modification)
    return _roiv(invoke_id, C.CMD_CONFIRMED_SET, corps)
