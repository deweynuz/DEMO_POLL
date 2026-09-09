"""
Décodage des messages Data Export.

Ce module ne fait que transformer des octets en dictionnaires : ni réseau, ni
base de données, ni journalisation. C'est ce qui le rend testable sans moniteur.

Toute structure décodée ici est tracée à une page du guide (rév. G.0).
Voir docs/note_protocole_pipg.md.
"""

from __future__ import annotations
import struct
from dataclasses import dataclass, field

from . import constantes as C


class ErreurDecodage(ValueError):
    """Trame illisible. Jamais silencieuse : l'appelant doit la traiter."""


# ─────────────────────────────────────────────────────────────────────────────
# Types de base — p. 40-41
# ─────────────────────────────────────────────────────────────────────────────

def decoder_float(brut: int) -> float | None:
    """
    FLOAT-Type Philips (p. 40) : exposant 8 bits signé en poids fort,
    mantisse 24 bits signée. Valeur = mantisse * 10^exposant.

    Renvoie None pour NaN et NRes (p. 41), float('inf')/-inf pour les infinis.
    """
    exposant = brut >> 24
    mantisse = brut & 0x00FFFFFF
    if mantisse == C.FLOAT_NAN or mantisse == C.FLOAT_NRES:
        return None
    if mantisse == C.FLOAT_POS_INF:
        return float('inf')
    if mantisse == C.FLOAT_NEG_INF:
        return float('-inf')
    if mantisse & 0x800000:                 # complément à deux sur 24 bits
        mantisse -= 0x1000000
    if exposant & 0x80:                     # complément à deux sur 8 bits
        exposant -= 0x100
    return mantisse * (10.0 ** exposant)


def decoder_chaine(donnees: bytes, offset: int = 0) -> str:
    """
    Chaîne préfixée de sa longueur (p. 40). Le moniteur utilise de l'UTF-16BE
    pour les libellés (étiquette de lit) et de l'ASCII pour d'autres champs.
    """
    (longueur,) = struct.unpack_from('>H', donnees, offset)
    brut = donnees[offset + 2: offset + 2 + longueur]
    brut = brut.rstrip(b'\x00')
    if b'\x00' in brut:                     # octets nuls intercalés => UTF-16BE
        return brut.decode('utf-16-be', errors='replace').rstrip('\x00')
    return brut.decode('latin-1', errors='replace')


def decoder_temps_absolu(donnees: bytes) -> str | None:
    """
    AbsoluteTime, 8 octets BCD : siècle, année, mois, jour, heure, minute,
    seconde, centièmes. Renvoie une chaîne ISO ou None si non renseigné
    (tous les champs à 0xff — cas du poll result, p. 62).
    """
    if len(donnees) < 8 or donnees[:8] == b'\xff' * 8:
        return None
    h = donnees[:8].hex()
    try:
        return (f"{int(h[0:4]):04d}-{int(h[4:6]):02d}-{int(h[6:8]):02d}T"
                f"{int(h[8:10]):02d}:{int(h[10:12]):02d}:{int(h[12:14]):02d}."
                f"{int(h[14:16]):02d}0000")
    except ValueError:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Couche session et Remote Operations — p. 43-44, 72
# ─────────────────────────────────────────────────────────────────────────────

def type_message(donnees: bytes) -> str:
    """
    p. 72 : « it is sufficient to check the first byte of the association
    control message ». Les messages de données commencent par 0xE1 (SPpdu).
    """
    if not donnees:
        return 'VIDE'
    premier = donnees[0]
    if premier in C.NOMS_SESSION:
        return C.NOMS_SESSION[premier]
    if premier == 0xE1:
        return 'DONNEES'
    return 'INCONNU'


@dataclass
class Apdu:
    """En-tête Remote Operation décodé."""
    ro_type: int
    invoke_id: int
    command_type: int
    donnees: bytes             # corps de la commande
    dernier: bool              # RORS_APDU => dernier message de la série (p. 44, 58)
    lien_etat: int | None = None    # RorlsId.state  : FIRST / NOT_FIRST_NOT_LAST / LAST
    lien_compteur: int | None = None  # RorlsId.count : démarre à 1


def decoder_apdu(donnees: bytes) -> Apdu:
    """
    p. 43-44. Attention : ROLRSapdu porte un RorlsId (2 octets) AVANT invoke_id,
    contrairement à ROIVapdu et RORSapdu. Ne pas en tenir compte décale tout le
    corps de 2 octets et rend le message illisible.
    """
    if len(donnees) < 14:
        raise ErreurDecodage(f"trame trop courte pour un APDU : {len(donnees)} o")
    if struct.unpack_from('>H', donnees, 0)[0] != C.SPDU_MAGIC:
        raise ErreurDecodage(f"SPpdu attendu, trouvé 0x{donnees[0]:02X}{donnees[1]:02X}")
    ro_type, _ro_len = struct.unpack_from('>HH', donnees, 4)

    pos = 8
    lien_etat = lien_compteur = None
    if ro_type == C.ROLRS_APDU:
        lien_etat, lien_compteur = struct.unpack_from('>BB', donnees, pos)
        pos += 2
    invoke_id, command_type, cmd_len = struct.unpack_from('>HHH', donnees, pos)
    pos += 6
    corps = donnees[pos:pos + cmd_len]
    return Apdu(ro_type=ro_type, invoke_id=invoke_id, command_type=command_type,
                donnees=corps, dernier=(ro_type == C.RORS_APDU),
                lien_etat=lien_etat, lien_compteur=lien_compteur)


# ─────────────────────────────────────────────────────────────────────────────
# AttributeList — structure omniprésente
#   { u_16 count; u_16 length; AVAType value[] }
#   AVAType = { OIDType attribute_id; u_16 length; u_8 value[length] }
# Vérifiée octet à octet sur le MDS Create réel (17 attributs, 244 octets).
# ─────────────────────────────────────────────────────────────────────────────

def decoder_liste_attributs(donnees: bytes, offset: int = 0) -> tuple[dict[int, bytes], int]:
    """Renvoie ({attribute_id: valeur_brute}, offset après la liste)."""
    if offset + 4 > len(donnees):
        raise ErreurDecodage("liste d'attributs tronquée")
    nombre, longueur = struct.unpack_from('>HH', donnees, offset)
    fin = offset + 4 + longueur
    pos = offset + 4
    attributs: dict[int, bytes] = {}
    for _ in range(nombre):
        if pos + 4 > len(donnees):
            break                       # trame tronquée : on rend ce qu'on a lu
        oid, taille = struct.unpack_from('>HH', donnees, pos)
        attributs[oid] = donnees[pos + 4: pos + 4 + taille]
        pos += 4 + taille
    return attributs, min(fin, len(donnees))


# ─────────────────────────────────────────────────────────────────────────────
# MDS Create Event — p. 111 (notification), structure recoupée sur trame réelle
#   EventReportArgument : managed_object(6) + event_time(4) + event_type(2)
#                         + length(2) + MDSCreateInfo
#   MDSCreateInfo       : managed_object(6) + AttributeList
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class MdsCreate:
    invoke_id: int
    managed_object: bytes       # 6 octets, à renvoyer tels quels dans la confirmation
    event_time: bytes           # 4 octets, idem
    attributs: dict[int, bytes]
    # champs utiles extraits
    bed_label: str | None = None
    system_id: str | None = None
    modele: str | None = None
    date_heure: str | None = None       # horloge du moniteur, ISO
    temps_relatif: int | None = None    # RelativeTime, ticks de 1/8 ms


def decoder_mds_create(donnees: bytes) -> MdsCreate:
    apdu = decoder_apdu(donnees)
    if apdu.command_type != C.CMD_CONFIRMED_EVENT_REPORT:
        raise ErreurDecodage(f"command_type 0x{apdu.command_type:04X}, "
                             f"CMD_CONFIRMED_EVENT_REPORT attendu")
    corps = apdu.donnees
    if len(corps) < 14:
        raise ErreurDecodage("EventReportArgument tronqué")
    managed_object = corps[0:6]
    event_time     = corps[6:10]
    (event_type,)  = struct.unpack_from('>H', corps, 10)
    if event_type != C.NOM_NOTI_MDS_CREAT:
        raise ErreurDecodage(f"event_type 0x{event_type:04X}, NOM_NOTI_MDS_CREAT attendu")
    # event_info = MDSCreateInfo : managed_object(6) + AttributeList
    attributs, _ = decoder_liste_attributs(corps, 14 + 6)

    mds = MdsCreate(invoke_id=apdu.invoke_id, managed_object=managed_object,
                    event_time=event_time, attributs=attributs)
    if C.NOM_ATTR_ID_BED_LABEL in attributs:
        mds.bed_label = decoder_chaine(attributs[C.NOM_ATTR_ID_BED_LABEL])
    if C.NOM_ATTR_SYS_ID in attributs:
        brut = attributs[C.NOM_ATTR_SYS_ID]
        if len(brut) >= 8:
            mds.system_id = ':'.join(f'{o:02x}' for o in brut[2:8])
    if C.NOM_ATTR_ID_MODEL in attributs:
        v = attributs[C.NOM_ATTR_ID_MODEL]
        fabricant = decoder_chaine(v, 0)
        suite = 2 + struct.unpack_from('>H', v, 0)[0]
        modele = decoder_chaine(v, suite) if suite + 2 <= len(v) else ''
        mds.modele = f"{fabricant} {modele}".strip()
    if C.NOM_ATTR_TIME_ABS in attributs:
        mds.date_heure = decoder_temps_absolu(attributs[C.NOM_ATTR_TIME_ABS])
    if C.NOM_ATTR_TIME_REL in attributs:
        v = attributs[C.NOM_ATTR_TIME_REL]
        if len(v) >= 4:
            mds.temps_relatif = struct.unpack_from('>I', v, 0)[0]
    return mds


# ─────────────────────────────────────────────────────────────────────────────
# Association Response — p. 73
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ReponseAssociation:
    """Options réellement négociées. Le client DOIT les vérifier (p. 72)."""
    min_poll_period_ticks: int
    max_mtu_rx: int
    max_mtu_tx: int
    options_profil: int
    options_etendues: int

    @property
    def courbes_acceptees(self) -> bool:
        """p. 72 : le moniteur repose le bit RTSA s'il sait exporter des courbes."""
        return bool(self.options_etendues & C.POLL_EXT_PERIOD_RTSA)

    @property
    def min_poll_period_s(self) -> float:
        return C.ticks_vers_secondes(self.min_poll_period_ticks)

    @property
    def timeout_association_s(self) -> float:
        return C.timeout_association(self.min_poll_period_s)


# Marqueurs de début des User Data dans l'Association Response (p. 73)
_MARQUEURS_USER_DATA = (bytes.fromhex('BE80288081'),
                        bytes.fromhex('BE802880020102 81'.replace(' ', '')))


def decoder_reponse_association(donnees: bytes) -> ReponseAssociation:
    """
    p. 73 : localiser les User Data par la séquence 0xBE 0x80 0x28 0x80 0x81
    (ou sa variante), puis lire le MDSEUserInfoStd qui suit.
    """
    if not donnees or donnees[0] != C.AC_SPDU_SI:
        raise ErreurDecodage("ce n'est pas une Association Response (0x0E attendu)")
    debut = -1
    for marqueur in _MARQUEURS_USER_DATA:
        i = donnees.find(marqueur)
        if i >= 0:
            debut = i + len(marqueur)
            break
    if debut < 0:
        raise ErreurDecodage("marqueur de User Data introuvable (p. 73)")

    # <ASNLength> puis MDSEUserInfoStd
    longueur = donnees[debut]
    pos = debut + 1
    if longueur == 0x81:               # forme longue sur 1 octet
        pos += 1
    u = donnees[pos:]
    if len(u) < 28:
        raise ErreurDecodage("User Data trop courtes")

    # MDSEUserInfoStd : 5 x u_32, puis option_list, puis supported_aprofiles
    pos = 20                            # protocol_version..startup_mode
    _oc, ol = struct.unpack_from('>HH', u, pos); pos += 4 + ol   # option_list
    _pc, _pl = struct.unpack_from('>HH', u, pos); pos += 4       # supported_aprofiles
    oid, taille = struct.unpack_from('>HH', u, pos); pos += 4    # PollProfileSupport
    if oid != C.NOM_POLL_PROFILE_SUPPORT:
        raise ErreurDecodage(f"attribut 0x{oid:04X}, NOM_POLL_PROFILE_SUPPORT attendu")

    (_rev, min_poll, mtu_rx, mtu_tx, _bw, options) = struct.unpack_from('>IIIIII', u, pos)
    pos += 24
    options_etendues = 0
    paquets, _ = decoder_liste_attributs(u, pos)          # optional_packages
    if C.NOM_ATTR_POLL_PROFILE_EXT in paquets:
        v = paquets[C.NOM_ATTR_POLL_PROFILE_EXT]
        if len(v) >= 4:
            options_etendues = struct.unpack_from('>I', v, 0)[0]

    return ReponseAssociation(min_poll_period_ticks=min_poll, max_mtu_rx=mtu_rx,
                              max_mtu_tx=mtu_tx, options_profil=options,
                              options_etendues=options_etendues)


# ─────────────────────────────────────────────────────────────────────────────
# Poll Result — p. 56-58 (simple) et p. 62 (étendu)
#   PollMdibDataReply    : poll_number(2) rel_time(4) abs_time(8)
#                          obj_type(4) attr_grp(2) PollInfoList
#   PollMdibDataReplyExt : poll_number(2) sequence_no(2) rel_time(4) abs_time(8)
#                          obj_type(4) attr_grp(2) PollInfoList
#   PollInfoList      : { count, length, SingleContextPoll[] }
#   SingleContextPoll : { context_id(2), { count, length, ObservationPoll[] } }
#   ObservationPoll   : { obj_handle(2), AttributeList }
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ObjetObserve:
    handle: int
    attributs: dict[int, bytes]


@dataclass
class ResultatPoll:
    poll_number: int
    sequence_no: int | None       # None pour un poll simple (p. 56)
    temps_relatif: int            # ticks de 1/8 ms
    temps_absolu: str | None      # toujours None sur ce moniteur (p. 62)
    type_objet: int
    groupe_attributs: int
    objets: list[ObjetObserve] = field(default_factory=list)
    dernier: bool = True
    invoke_id: int = 0
    etendu: bool = False
    lien_etat: int | None = None
    lien_compteur: int | None = None


def decoder_resultat_poll(donnees: bytes) -> ResultatPoll:
    apdu = decoder_apdu(donnees)
    corps = apdu.donnees
    if len(corps) < 12:
        raise ErreurDecodage("ActionResult tronqué")
    # ActionResult : managed_object(6) + action_type(2) + length(2)
    (action_type,) = struct.unpack_from('>H', corps, 6)
    etendu = (action_type == C.NOM_ACT_POLL_MDIB_DATA_EXT)
    pos = 10

    poll_number = struct.unpack_from('>H', corps, pos)[0]; pos += 2
    sequence_no = None
    if etendu:
        sequence_no = struct.unpack_from('>H', corps, pos)[0]; pos += 2
    temps_relatif = struct.unpack_from('>I', corps, pos)[0]; pos += 4
    temps_absolu = decoder_temps_absolu(corps[pos:pos + 8]); pos += 8
    type_objet = struct.unpack_from('>I', corps, pos)[0] & 0xFFFF; pos += 4
    groupe = struct.unpack_from('>H', corps, pos)[0]; pos += 2

    resultat = ResultatPoll(poll_number=poll_number, sequence_no=sequence_no,
                            temps_relatif=temps_relatif, temps_absolu=temps_absolu,
                            type_objet=type_objet, groupe_attributs=groupe,
                            dernier=apdu.dernier, invoke_id=apdu.invoke_id,
                            etendu=etendu, lien_etat=apdu.lien_etat,
                            lien_compteur=apdu.lien_compteur)

    # PollInfoList
    if pos + 4 > len(corps):
        return resultat                      # terminateur à liste vide (p. 58)
    nb_contextes, _lg = struct.unpack_from('>HH', corps, pos); pos += 4
    for _ in range(nb_contextes):
        if pos + 6 > len(corps):
            break
        pos += 2                             # context_id
        nb_objets, _lgo = struct.unpack_from('>HH', corps, pos); pos += 4
        for _ in range(nb_objets):
            if pos + 6 > len(corps):
                break
            handle = struct.unpack_from('>H', corps, pos)[0]; pos += 2
            attributs, pos = decoder_liste_attributs(corps, pos)
            resultat.objets.append(ObjetObserve(handle=handle, attributs=attributs))
    return resultat


# ─────────────────────────────────────────────────────────────────────────────
# Valeurs numériques — p. 76-77
#   NuObsValue    : { physio_id(2), state(2), unit_code(2), value(4) }  = 10 o
#   NuObsValueCmp : { count(2), length(2), NuObsValue[] }
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ValeurNumerique:
    physio_id: int
    etat: int
    unit_code: int
    valeur: float | None

    @property
    def valide(self) -> bool:
        return C.mesure_valide(self.etat) and self.valeur is not None


def decoder_valeur_numerique(brut: bytes, offset: int = 0) -> ValeurNumerique:
    physio_id, etat, unite, val = struct.unpack_from('>HHHI', brut, offset)
    return ValeurNumerique(physio_id=physio_id, etat=etat, unit_code=unite,
                           valeur=decoder_float(val))


def decoder_valeurs_numeriques_composees(brut: bytes) -> list[ValeurNumerique]:
    nombre, _longueur = struct.unpack_from('>HH', brut, 0)
    return [decoder_valeur_numerique(brut, 4 + 10 * i) for i in range(nombre)
            if 4 + 10 * i + 10 <= len(brut)]


# ─────────────────────────────────────────────────────────────────────────────
# Ondes — p. 83, 86, 87, 88
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class EchantillonsOnde:
    physio_id: int
    etat: int
    echantillons: bytes          # int16 big-endian, bruts, NON calibrés

    @property
    def valide(self) -> bool:
        return C.mesure_valide(self.etat)

    @property
    def nombre(self) -> int:
        return len(self.echantillons) // 2


def decoder_sa_obs_value(brut: bytes, offset: int = 0) -> tuple[EchantillonsOnde, int]:
    """
    SaObsValue (p. 87) : physio_id(2) + state(2) + { length(2) + value[length] }.
    `length` est en OCTETS (p. 87 : « u_8 value[1] » ; règle générale p. 286).
    """
    physio_id, etat, longueur = struct.unpack_from('>HHH', brut, offset)
    debut = offset + 6
    return (EchantillonsOnde(physio_id=physio_id, etat=etat,
                             echantillons=brut[debut:debut + longueur]),
            debut + longueur)


def decoder_sa_obs_value_cmp(brut: bytes) -> list[EchantillonsOnde]:
    """SaObsValueCmp (p. 88) : count(2) + length(2) + SaObsValue[]."""
    nombre, _longueur = struct.unpack_from('>HH', brut, 0)
    ondes, pos = [], 4
    for _ in range(nombre):
        if pos + 6 > len(brut):
            break
        onde, pos = decoder_sa_obs_value(brut, pos)
        ondes.append(onde)
    return ondes


@dataclass
class SpecificationOnde:
    """SaSpec — p. 83."""
    taille_tableau: int
    bits_par_echantillon: int
    bits_significatifs: int
    flags: int

    @property
    def masquer_bits_non_significatifs(self) -> bool:
        return bool(self.flags & C.SA_EXT_VAL_RANGE)

    @property
    def calibration_statique(self) -> bool:
        return bool(self.flags & C.STATIC_SCALE)

    @property
    def masque_significatif(self) -> int:
        return (1 << self.bits_significatifs) - 1


def decoder_sa_spec(brut: bytes) -> SpecificationOnde:
    taille, taille_ech, bits_signif, flags = struct.unpack_from('>HBBH', brut, 0)
    return SpecificationOnde(taille_tableau=taille, bits_par_echantillon=taille_ech,
                             bits_significatifs=bits_signif, flags=flags)


@dataclass
class Calibration:
    """ScaleRangeSpec16 — p. 86. 12 octets."""
    borne_basse_absolue: float | None
    borne_haute_absolue: float | None
    borne_basse_brute: int
    borne_haute_brute: int

    @property
    def representable(self) -> bool:
        """p. 86 : NaN dans les bornes absolues => l'onde n'a pas d'unité physique."""
        return (self.borne_basse_absolue is not None
                and self.borne_haute_absolue is not None
                and self.borne_haute_brute != self.borne_basse_brute)

    def vers_absolu(self, brut):
        """Interpolation linéaire brut -> valeur physique (p. 86)."""
        if not self.representable:
            return brut
        pente = ((self.borne_haute_absolue - self.borne_basse_absolue)
                 / (self.borne_haute_brute - self.borne_basse_brute))
        return self.borne_basse_absolue + (brut - self.borne_basse_brute) * pente


def decoder_calibration(brut: bytes) -> Calibration:
    bas_abs, haut_abs, bas_brut, haut_brut = struct.unpack_from('>IIHH', brut, 0)
    return Calibration(borne_basse_absolue=decoder_float(bas_abs),
                       borne_haute_absolue=decoder_float(haut_abs),
                       borne_basse_brute=bas_brut, borne_haute_brute=haut_brut)


def decoder_masques_qualite(brut: bytes) -> dict[int, int]:
    """
    SaFixedValSpec16 (p. 83) : count(2) + length(2) + { id(2), valeur(2) }[].
    Renvoie {SaFixedValId: masque}.
    """
    nombre, _longueur = struct.unpack_from('>HH', brut, 0)
    masques = {}
    for i in range(nombre):
        pos = 4 + 4 * i
        if pos + 4 > len(brut):
            break
        ident, valeur = struct.unpack_from('>HH', brut, pos)
        masques[ident] = valeur
    return masques
