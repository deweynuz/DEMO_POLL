"""
Constantes du protocole Philips IntelliVue Data Export.

RÈGLE ABSOLUE — toute constante de ce fichier porte la page du guide où elle
est définie. Une constante sans référence de page n'a rien à faire ici : le
module précédent en contenait plusieurs, déduites du trafic ou supposées, dont
au moins deux étaient fausses (cf. docs/note_protocole_pipg.md, section 6).

Source : Philips IntelliVue Data Export Interface Programming Guide, rév. G.0
         (M8000-9305G, 2011), archivé en docs/PIPG_G0_2011.pdf.
         Les numéros de page sont ceux IMPRIMÉS sur la page (= page PDF - 1).
"""

# ─────────────────────────────────────────────────────────────────────────────
# Transport
# ─────────────────────────────────────────────────────────────────────────────

PORT_MONITEUR = 24105   # le moniteur écoute ici
PORT_LOCAL    = 24106   # port d'écoute conventionnel du client

# ─────────────────────────────────────────────────────────────────────────────
# En-têtes de session (premier octet du datagramme) — p. 72-73
#
# « In most cases, it is sufficient for the Computer Client to check the first
#   byte of the association control message. » (p. 72)
# ─────────────────────────────────────────────────────────────────────────────

CN_SPDU_SI = 0x0D   # Association Request                         p. 67
AC_SPDU_SI = 0x0E   # Association Response                        p. 73
RF_SPDU_SI = 0x0C   # Refuse                                      p. 73
FN_SPDU_SI = 0x09   # Release Request                             p. 72
DN_SPDU_SI = 0x0A   # Release Response                            p. 73
AB_SPDU_SI = 0x19   # Abort                                       p. 72

NOMS_SESSION = {
    CN_SPDU_SI: 'ASSOC_REQUEST',
    AC_SPDU_SI: 'ASSOC_RESPONSE',
    RF_SPDU_SI: 'REFUSE',
    FN_SPDU_SI: 'RELEASE_REQUEST',
    DN_SPDU_SI: 'RELEASE_RESPONSE',
    AB_SPDU_SI: 'ABORT',
}

# En-tête des messages de données (Session Presentation PDU)
SPDU_MAGIC = 0xE100
SPDU_CTX   = 0x0002

# ─────────────────────────────────────────────────────────────────────────────
# Remote Operation APDU — p. 43-44
# ─────────────────────────────────────────────────────────────────────────────

ROIV_APDU  = 0x0001   # invoke
RORS_APDU  = 0x0002   # result — DERNIER message d'une série
ROER_APDU  = 0x0003   # erreur
ROLRS_APDU = 0x0005   # linked result — tous les messages SAUF le dernier

# p. 58 : « in all result messages except the last result message the ROLRSapdu
# is used instead of the RORSapdu ». C'est LE critère de fin de série ; toute
# heuristique fondée sur la taille du payload coupe au mauvais endroit.

CMD_CONFIRMED_EVENT_REPORT = 0x0001   # p. 45
CMD_CONFIRMED_ACTION       = 0x0007   # p. 47
CMD_GET                    = 0x0003   # p. 63  (GET PRIORITY LIST)
CMD_CONFIRMED_SET          = 0x0005   # p. 64  (SET PRIORITY LIST)

# ─────────────────────────────────────────────────────────────────────────────
# Classes d'objets (partition Object Oriented Elements) — p. 111, listées p. 56
# ─────────────────────────────────────────────────────────────────────────────

NOM_MOC_VMS_MDS          = 0x0021   # 33
NOM_MOC_VMO_METRIC_NU    = 0x0006   # 6   numerics
NOM_MOC_VMO_METRIC_SA_RT = 0x0009   # 9   waves temps réel
NOM_MOC_PT_DEMOG         = 0x002A   # 42  démographiques patient
NOM_MOC_VMO_AL_MON       = 0x0036   # 54  Alert Monitor (cible du keep-alive, p. 63)

# ─────────────────────────────────────────────────────────────────────────────
# Actions et notifications — p. 49, 111
# ─────────────────────────────────────────────────────────────────────────────

NOM_ACT_POLL_MDIB_DATA     = 0x0C16   # 3094   poll simple            p. 49
NOM_ACT_POLL_MDIB_DATA_EXT = 0xF13B   # 61755  poll étendu            p. 49
NOM_NOTI_MDS_CREAT         = 0x0D06   # 3334   MDS Create Event       p. 111

# ─────────────────────────────────────────────────────────────────────────────
# Groupes d'attributs — p. 243
# ─────────────────────────────────────────────────────────────────────────────

NOM_ATTR_GRP_METRIC_VAL_OBS = 0x0803   # 2051
NOM_ATTR_GRP_VMO_DYN        = 0x0810   # 2064  contexte dynamique
NOM_ATTR_GRP_VMO_STATIC     = 0x0811   # 2065  contexte statique (keep-alive, p. 63)
NOM_ATTR_GRP_TOUS           = 0x0000   # p. 287 : contexte multiplexé, 1 objet / 1024 ms

# ─────────────────────────────────────────────────────────────────────────────
# Attributs — p. 243-246
# ─────────────────────────────────────────────────────────────────────────────

NOM_ATTR_ID_HANDLE           = 0x0921   # 2337
NOM_ATTR_ID_BED_LABEL        = 0x091E   # 2334   étiquette de lit (« SALLE1 »)
NOM_ATTR_ID_MODEL            = 0x0928   # 2344   fabricant + modèle
NOM_ATTR_ID_LABEL            = 0x0924   # 2340   TextId du label d'onde   p. 84
NOM_ATTR_SYS_ID              = 0x0984   # 2436   identifiant système
NOM_ATTR_TIME_ABS            = 0x0987   # 2439   « Date and Time »        p. 62
NOM_ATTR_TIME_REL            = 0x098F   # 2447   « Relative Time »        p. 62
NOM_ATTR_SA_SPECN            = 0x096D   # 2413   SaSpec                   p. 83
NOM_ATTR_SA_VAL_OBS          = 0x096E   # 2414   SaObsValue               p. 87
NOM_ATTR_SA_CMPD_VAL_OBS     = 0x0967   # 2407   SaObsValueCmp            p. 88
NOM_ATTR_SCALE_SPECN_I16     = 0x096F   # 2415   ScaleRangeSpec16         p. 86
NOM_ATTR_TIME_PD_SAMP        = 0x098D   # 2445   période d'échantillonnage p. 84
NOM_ATTR_SA_FIXED_VAL_SPECN  = 0x0A16   # 2582   masques de qualité       p. 83
NOM_ATTR_TIME_PD_POLL        = 0xF13E   # 61758  PollDataReqPeriod        p. 60
NOM_ATTR_POLL_RTSA_PRIO_LIST = 0xF23A   # 62010  liste priorité ondes     p. 63
NOM_ATTR_POLL_PROFILE_EXT    = 0xF001   # 61441  Poll Profile Extensions  p. 71
NOM_POLL_PROFILE_SUPPORT     = 0x0001   # 1      Poll Profile Support     p. 69

# ─────────────────────────────────────────────────────────────────────────────
# Négociation d'association — p. 69-72
# ─────────────────────────────────────────────────────────────────────────────

SYST_CLIENT = 0x80000000   # p. 69 — sans ce bit, l'association est refusée
SYST_SERVER = 0x00800000   # p. 69

POLL_PROFILE_REV_0 = 0x80000000   # p. 70

P_OPT_DYN_CREATE_OBJECTS = 0x40000000   # p. 71
P_OPT_DYN_DELETE_OBJECTS = 0x20000000   # p. 71

# PollProfileExtOptions — p. 71
POLL_EXT_PERIOD_NU_1SEC       = 0x80000000
POLL_EXT_PERIOD_NU_AVG_12SEC  = 0x40000000
POLL_EXT_PERIOD_NU_AVG_60SEC  = 0x20000000
POLL_EXT_PERIOD_NU_AVG_300SEC = 0x10000000
POLL_EXT_PERIOD_RTSA          = 0x08000000   # <<< les courbes. Le module
                                             # précédent posait 0xA0000000 en
                                             # croyant poser ce bit : il
                                             # demandait en fait des numerics
                                             # moyennés sur 1 minute.
POLL_EXT_ENUM                 = 0x04000000
POLL_EXT_NU_PRIO_LIST         = 0x02000000
POLL_EXT_DYN_MODALITIES       = 0x01000000

# p. 72 : « The Computer Client must set at least one of the bits for the
# numeric period, otherwise the optional package is ignored. »
OPTIONS_NUMERICS = POLL_EXT_PERIOD_NU_1SEC
OPTIONS_COURBES  = POLL_EXT_PERIOD_NU_1SEC | POLL_EXT_PERIOD_RTSA   # 0x88000000

# MTU — p. 71
MTU_MIN_EXIGE     = 300    # en deçà, le profil n'est pas supporté
MTU_MAX_LAN       = 1364   # maximum négociable sur LAN
MTU_MIN_COURBES   = 500    # 256 ms de courbe en un message
MTU_MIN_COURBES_MUX = 700  # idem avec contexte multiplexé

# ─────────────────────────────────────────────────────────────────────────────
# Temps
# ─────────────────────────────────────────────────────────────────────────────

# RelativeTime est un u_32 en unités de 1/8 ms. Vérifié empiriquement contre
# le moniteur SALLE1 le 09/09/2026 : 973 056 ticks pour 121,65 s → 7 998,8 ticks/s.
TICKS_PAR_SECONDE = 8000

def ticks_vers_secondes(ticks: int) -> float:
    return ticks / TICKS_PAR_SECONDE

def secondes_vers_ticks(secondes: float) -> int:
    return int(round(secondes * TICKS_PAR_SECONDE))

# Timeout d'association selon min_poll_period négocié — table p. 70
def timeout_association(min_poll_period_s: float) -> float:
    if min_poll_period_s < 3.3:
        return 10.0
    if min_poll_period_s <= 43.0:
        return 3.0 * min_poll_period_s
    return 130.0

# Périodes de résultat en poll étendu — table p. 60
PERIODE_RESULTAT_COURBES_S  = 0.256
PERIODE_RESULTAT_NUMERICS_S = 1.0

# ─────────────────────────────────────────────────────────────────────────────
# Objets Wave — p. 83-84, 86-88
# ─────────────────────────────────────────────────────────────────────────────

# SaFlags — p. 83
SMOOTH_CURVE     = 0x8000
DELAYED_CURVE    = 0x4000
STATIC_SCALE     = 0x2000   # la calibration ne changera pas
SA_EXT_VAL_RANGE = 0x1000   # masquer les bits non significatifs

# SaFixedValId — p. 83-84
SA_FIX_UNSPEC            = 0
SA_FIX_INVALID_MASK      = 1
SA_FIX_PACER_MASK        = 2
SA_FIX_DEFIB_MARKER_MASK = 3
SA_FIX_SATURATION        = 4
SA_FIX_QRS_MASK          = 5

NOMS_MASQUE_QUALITE = {
    SA_FIX_INVALID_MASK:      'invalide',
    SA_FIX_PACER_MASK:        'pacemaker',
    SA_FIX_DEFIB_MARKER_MASK: 'defib',
    SA_FIX_SATURATION:        'saturation',
    SA_FIX_QRS_MASK:          'qrs',
}

# Bits du canal de qualité écrit en HDF5 (un octet par échantillon)
QUAL_INVALIDE   = 0x01
QUAL_PACEMAKER  = 0x02
QUAL_DEFIB      = 0x04
QUAL_SATURATION = 0x08
QUAL_QRS        = 0x10
QUAL_MANQUANT   = 0x80   # échantillon jamais reçu (trou comblé)

BIT_QUALITE_POUR_MASQUE = {
    SA_FIX_INVALID_MASK:      QUAL_INVALIDE,
    SA_FIX_PACER_MASK:        QUAL_PACEMAKER,
    SA_FIX_DEFIB_MARKER_MASK: QUAL_DEFIB,
    SA_FIX_SATURATION:        QUAL_SATURATION,
    SA_FIX_QRS_MASK:          QUAL_QRS,
}

# Limites de sélection des ondes — p. 286-287
MAX_ONDES_ECG     = 3   # à 500 sps, OU l'onde composée 3x250 sps
MAX_ONDES_NON_ECG = 8   # à 125 ou 62,5 sps

# TextId d'un label dans la partition SCADA — p. 84 et exemples p. 179-188
PARTITION_SCADA = 0x00020000

def text_id(physio_id: int) -> int:
    """TextId du label d'une onde à partir de son physio_id (partition SCADA)."""
    return PARTITION_SCADA | physio_id

# ─────────────────────────────────────────────────────────────────────────────
# MeasurementState — p. 76-77
#
# « The measurement is valid if the first octet of the state is all 0. » (p. 77)
# Autrement dit : valide ssi (state & 0xFF00) == 0. Les bits de l'octet de poids
# faible (validation manuelle, alarme, mesure en cours) n'invalident PAS la mesure.
# ─────────────────────────────────────────────────────────────────────────────

MS_INVALID                 = 0x8000
MS_QUESTIONABLE            = 0x4000
MS_UNAVAILABLE             = 0x2000
MS_CALIBRATION_ONGOING     = 0x1000
MS_TEST_DATA               = 0x0800   # signal de test généré, PAS un signal patient
MS_DEMO_DATA               = 0x0400   # moniteur en mode démonstration
MS_VALIDATED_DATA          = 0x0080
MS_EARLY_INDICATION        = 0x0040
MS_MSMT_ONGOING            = 0x0020
MS_MSMT_STATE_IN_ALARM     = 0x0002
MS_MSMT_STATE_AL_INHIBITED = 0x0001

NOMS_ETAT_MESURE = {
    MS_INVALID: 'invalide', MS_QUESTIONABLE: 'douteuse',
    MS_UNAVAILABLE: 'indisponible', MS_CALIBRATION_ONGOING: 'calibration',
    MS_TEST_DATA: 'signal_test', MS_DEMO_DATA: 'mode_demo',
    MS_VALIDATED_DATA: 'validee', MS_EARLY_INDICATION: 'estimation_precoce',
    MS_MSMT_ONGOING: 'mesure_en_cours', MS_MSMT_STATE_IN_ALARM: 'en_alarme',
    MS_MSMT_STATE_AL_INHIBITED: 'alarmes_inhibees',
}

def mesure_valide(state: int) -> bool:
    """p. 77 : valide ssi le premier octet de l'état est nul."""
    return (state & 0xFF00) == 0

def noms_etat(state: int) -> list[str]:
    return [nom for bit, nom in NOMS_ETAT_MESURE.items() if state & bit]

# ─────────────────────────────────────────────────────────────────────────────
# FLOAT-Type — p. 40-41
# 32 bits : exposant 8 bits signé (poids fort) + mantisse 24 bits signée.
# Valeur = mantisse * 10^exposant. Les deux sont en complément à deux.
# ─────────────────────────────────────────────────────────────────────────────

FLOAT_NAN     = 0x7FFFFF   # mantisse +(2^23 - 1)
FLOAT_NRES    = 0x800000   # « Not at this resolution », mantisse -(2^23)
FLOAT_POS_INF = 0x7FFFFE
FLOAT_NEG_INF = 0x800002

# RorlsId.state — p. 44. Permet de vérifier qu'aucun résultat lié n'a été perdu.
RORLS_FIRST              = 1
RORLS_NOT_FIRST_NOT_LAST = 2
RORLS_LAST               = 3   # dernier ROLRS ; un RORS suit
