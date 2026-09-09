#!/usr/bin/env python3
"""
Simulateur de moniteur Philips IntelliVue MX800.

Raison d'être : sans lui, tout test dépend de la disponibilité d'un bloc
opératoire. C'est la raison principale pour laquelle les bugs de reconnexion du
module précédent n'ont jamais été détectés — le service pouvait tourner six
semaines à vide sans que personne ne puisse le reproduire.

Le simulateur reproduit le comportement OBSERVÉ sur un vrai MX800 (SALLE1,
09/09/2026), pas seulement celui décrit par le guide. En particulier :

  - le MDS Create Event est ré-émis trois fois puis suivi d'un ABORT si le
    client ne le confirme pas — c'est ce qui tuait l'ancien module ;
  - un Release reçu pendant que l'association est INCOMPLÈTE est ignoré ;
  - max_mtu_tx est renvoyé à 1456, au-dessus du maximum de 1364 annoncé p. 71.

Usage :
    python3 -m outils.simulateur --bed-label SIM1 --courbes
    python3 -m outils.simulateur --panne abort:30 --panne perte-ondes:10
"""

from __future__ import annotations

import argparse
import logging
import math
import random
import selectors
import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

from mx800.protocole import constantes as C, decodage as D, trames as T
from mx800.protocole import ondes as CAT

log = logging.getLogger('simulateur')


# ═════════════════════════════════════════════════════════════════════════════
# Injection de pannes
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class Pannes:
    """Chaque champ reproduit un mode de défaillance réel."""
    refuser_association: bool = False      # renvoie 0x0C Refuse
    abort_apres_s: float | None = None     # ABORT après N s d'association
    silence_apres_s: float | None = None   # cesse toute réponse (câble arraché)
    ignorer_release: bool = False          # ne répond pas au Release
    ne_pas_repondre_mds: bool = False      # n'envoie jamais le MDS Create
    refuser_courbes: bool = False          # accepte l'association, refuse RTSA
    perdre_un_resultat_sur: int = 0        # 1 résultat d'onde sur N est jeté
    changer_calibration_apres_s: float | None = None
    redemarrage_apres_s: float | None = None   # ABORT + remise à zéro du temps
    masques_qualite: bool = False          # injecte invalid / pacer / saturation

    @classmethod
    def depuis_options(cls, options: list[str]) -> 'Pannes':
        p = cls()
        for opt in options or []:
            nom, _, valeur = opt.partition(':')
            nom = nom.replace('-', '_')
            if nom == 'refus':                p.refuser_association = True
            elif nom == 'abort':              p.abort_apres_s = float(valeur)
            elif nom == 'silence':            p.silence_apres_s = float(valeur)
            elif nom == 'ignorer_release':    p.ignorer_release = True
            elif nom == 'sans_mds':           p.ne_pas_repondre_mds = True
            elif nom == 'refuser_courbes':    p.refuser_courbes = True
            elif nom == 'perte_ondes':        p.perdre_un_resultat_sur = int(valeur)
            elif nom == 'calibration':        p.changer_calibration_apres_s = float(valeur)
            elif nom == 'redemarrage':        p.redemarrage_apres_s = float(valeur)
            elif nom == 'masques':            p.masques_qualite = True
            else: raise ValueError(f"panne inconnue : {nom}")
        return p


# ═════════════════════════════════════════════════════════════════════════════
# Génération de signaux
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class OndeSimulee:
    """Une onde exportable, avec sa spécification statique (p. 83)."""
    physio_id: int
    periode_echantillonnage_ms: float     # 2, 4, 8 ou 16 ms (p. 286)
    taille_tableau: int                   # échantillons par résultat de 256 ms
    bits_significatifs: int = 12
    flags: int = C.SA_EXT_VAL_RANGE
    borne_basse: float = -1.0
    borne_haute: float = 1.0
    forme: str = 'ecg'

    @property
    def frequence_hz(self) -> float:
        return 1000.0 / self.periode_echantillonnage_ms

    def echantillons(self, t0: float, fc_bpm: float) -> list[int]:
        """Échantillons bruts sur 16 bits pour une fenêtre de 256 ms."""
        n, pas = self.taille_tableau, self.periode_echantillonnage_ms / 1000.0
        pleine = (1 << self.bits_significatifs) - 1
        milieu = pleine // 2
        out = []
        for i in range(n):
            t = t0 + i * pas
            phase = (t * fc_bpm / 60.0) % 1.0
            if self.forme == 'ecg':
                v = (math.exp(-((phase - 0.20) / 0.012) ** 2) * 0.95
                     - math.exp(-((phase - 0.17) / 0.010) ** 2) * 0.18
                     - math.exp(-((phase - 0.24) / 0.012) ** 2) * 0.25
                     + math.exp(-((phase - 0.42) / 0.050) ** 2) * 0.22
                     + math.exp(-((phase - 0.08) / 0.030) ** 2) * 0.12)
            elif self.forme == 'pleth':
                v = 0.5 * (1 - math.cos(2 * math.pi * min(phase / 0.75, 1.0))) \
                    * math.exp(-1.4 * phase)
            elif self.forme == 'abp':
                v = (0.45 + 0.5 * math.exp(-((phase - 0.15) / 0.09) ** 2)
                     + 0.10 * math.exp(-((phase - 0.36) / 0.05) ** 2)
                     - 0.18 * phase)
            elif self.forme == 'co2':
                cycle = (t / 4.0) % 1.0                       # ~15 cycles/min
                v = 0.0 if cycle < 0.35 else min(1.0, (cycle - 0.35) / 0.10)
            else:
                v = 0.5 * math.sin(2 * math.pi * phase)
            brut = int(milieu + v * milieu * 0.85)
            out.append(max(0, min(pleine, brut)))
        return out


# sélection réaliste en anesthésie : ~1,8 ko/s (cf. cahier des charges)
ONDES_DEFAUT = [
    OndeSimulee(0x0102, 2.0, 128, forme='ecg',   borne_basse=-1.0, borne_haute=1.0),
    OndeSimulee(0x4BB4, 8.0,  32, forme='pleth', borne_basse=0.0,  borne_haute=1.0),
    OndeSimulee(0x4A14, 8.0,  32, forme='abp',   borne_basse=0.0,  borne_haute=200.0),
    OndeSimulee(0x50AC, 16.0, 16, forme='co2',   borne_basse=0.0,  borne_haute=60.0),
]

# numerics : (physio_id, unit_code, valeur de base, amplitude)
# Identifiants et unités relevés dans le catalogue extrait des p. 115-188.
NUMERICS_DEFAUT = [
    (0x4182, 0x0AA0,  72.0,  8.0),    # HR    NOM_ECG_CARD_BEAT_RATE, bpm
    (0x4BB8, 0x0220,  98.0,  1.5),    # SpO2  NOM_PULS_OXIM_SAT_O2, %
    (0x480A, 0x0AA0,  72.0,  8.0),    # Pouls NOM_PULS_RATE, bpm
    (0x4B48, 0x17A0,  36.5,  0.2),    # Temp  NOM_TEMP, °C
    (0x500A, 0x0AA0,  12.0,  1.0),    # RR    NOM_RESP_RATE — et non 0x5000,
                                      #       qui est l'ONDE d'impédance respiratoire
    (0x4A15, 0x0F20, 118.0,  9.0),    # ABPs  NOM_PRESS_BLD_ART_ABP_SYS, mmHg
    (0x4A16, 0x0F20,  64.0,  6.0),    # ABPd
    (0x4A17, 0x0F20,  82.0,  6.0),    # ABPm
    (0x4261, 0x0AA0,   0.0,  0.4),    # PVC   NOM_ECG_V_P_C_CNT
]


# ═════════════════════════════════════════════════════════════════════════════
# Simulateur
# ═════════════════════════════════════════════════════════════════════════════

class Simulateur:
    LIBRE, ATTENTE_CONFIRMATION, ACTIF = 'LIBRE', 'ATTENTE_CONFIRMATION', 'ACTIF'

    # cadences observées sur le vrai moniteur
    PERIODE_RENVOI_MDS_S = 3.1     # ré-émission du MDS Create non confirmé
    RENVOIS_MDS_MAX      = 3       # puis ABORT (~10 s au total)
    TIMEOUT_KEEPALIVE_S  = 10.0    # p. 70, min_poll_period négocié < 3,3 s

    def __init__(self, *, adresse: str = '0.0.0.0', port: int = C.PORT_MONITEUR,
                 bed_label: str = 'SIM1', system_id: bytes = b'\x00\x00\x00\x00\x00\x01',
                 courbes_supportees: bool = True, pannes: Pannes | None = None,
                 ondes: list[OndeSimulee] | None = None, graine: int = 1,
                 mode_operation: int = C.OPMODE_MONITOR):
        self.adresse, self.port = adresse, port
        self.bed_label, self.system_id = bed_label, system_id
        self.courbes_supportees = courbes_supportees
        self.mode_operation = mode_operation
        # Démographiques : état EMPTY tant que personne n'est admis (p. 103)
        self.patient = dict(etat=C.PT_EMPTY, patient_id='', nom='', prenom='',
                            sexe=C.SEXES_PATIENT and 0, type_patient=1)
        self.pannes = pannes or Pannes()
        self.ondes = ondes if ondes is not None else list(ONDES_DEFAUT)
        self.alea = random.Random(graine)

        self.sock: socket.socket | None = None
        self._arret = threading.Event()
        self.demarre = threading.Event()

        # compteurs consultables par les tests
        self.stats = {'assoc_request': 0, 'assoc_response': 0, 'refuse': 0,
                      'mds_envoye': 0, 'mds_confirme': 0, 'abort': 0,
                      'poll_recu': 0, 'poll_etendu_recu': 0, 'resultats_envoyes': 0,
                      'resultats_perdus': 0, 'keepalive': 0,
                      'release_recu': 0, 'release_repondu': 0}
        self._reinitialiser_association()
        self.t_demarrage = time.monotonic()
        self.liste_priorite: list[int] = [o.physio_id for o in self.ondes]

    # ── état ────────────────────────────────────────────────────────────────

    def _reinitialiser_association(self):
        self.etat = self.LIBRE
        self.pair = None
        self.courbes_actives = False
        self.t_association = None
        self.t_dernier_message_client = None
        self.mds_invoke_id = 1
        self.mds_renvois = 0
        self.t_prochain_renvoi_mds = None
        self.poll_etendu = None       # dict(invoke_id, poll_number, fin, prochaine, sequence)
        self.sequence_no = 0
        self.resultats_ondes = 0
        # rel_time_stamp des blocs de courbes. Un vrai moniteur l'aligne sur son
        # horloge d'ÉCHANTILLONNAGE, pas sur l'horloge murale : deux blocs
        # consécutifs diffèrent d'exactement taille_tableau x periode, soit
        # 2048 ticks (256 ms) pour tous les types du tableau p. 286. L'aligner
        # ici évite de fabriquer de faux micro-trous à chaque bloc.
        self._reltime_bloc: int | None = None

    def _temps_relatif(self) -> int:
        """RelativeTime du moniteur, en ticks de 1/8 ms (p. 62)."""
        return int((time.monotonic() - self.t_demarrage) * C.TICKS_PAR_SECONDE) & 0xFFFFFFFF

    def _horodatage(self) -> bytes:
        m = datetime.now()
        return T.encoder_temps_absolu(m.year, m.month, m.day, m.hour, m.minute, m.second)

    def _calibration(self, onde: OndeSimulee) -> bytes:
        """La calibration peut changer en cours d'acquisition (p. 86, STATIC_SCALE absent)."""
        facteur = 1.0
        if (self.pannes.changer_calibration_apres_s is not None and self.t_association
                and time.monotonic() - self.t_association
                > self.pannes.changer_calibration_apres_s):
            facteur = 2.0
        pleine = (1 << onde.bits_significatifs) - 1
        return T.encoder_scale_range_spec16(onde.borne_basse * facteur,
                                            onde.borne_haute * facteur, 0, pleine)

    # ── boucle ──────────────────────────────────────────────────────────────

    def demarrer(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((self.adresse, self.port))
        selecteur = selectors.DefaultSelector()
        selecteur.register(self.sock, selectors.EVENT_READ)
        log.info("Simulateur MX800 sur %s:%d — lit « %s », courbes %s",
                 self.adresse, self.port, self.bed_label,
                 "supportées" if self.courbes_supportees else "NON supportées")
        self.demarre.set()
        try:
            while not self._arret.is_set():
                for _cle, _ev in selecteur.select(timeout=0.02):
                    try:
                        donnees, pair = self.sock.recvfrom(65535)
                    except OSError:
                        continue
                    self._traiter(donnees, pair)
                self._echeances()
        finally:
            selecteur.close()
            self.sock.close()
            log.info("Simulateur arrêté. %s", self.stats)

    def arreter(self):
        self._arret.set()

    def _muet(self) -> bool:
        return (self.pannes.silence_apres_s is not None and self.t_association is not None
                and time.monotonic() - self.t_association > self.pannes.silence_apres_s)

    def _envoyer(self, donnees: bytes, pair=None):
        cible = pair or self.pair
        if cible is None or self._muet():
            return
        self.sock.sendto(donnees, cible)

    # ── réception ───────────────────────────────────────────────────────────

    def _traiter(self, donnees: bytes, pair):
        genre = D.type_message(donnees)
        self.t_dernier_message_client = time.monotonic()

        if genre == 'ASSOC_REQUEST':
            self._sur_assoc_request(donnees, pair)
        elif genre == 'RELEASE_REQUEST':
            self._sur_release(pair)
        elif genre == 'ABORT':
            log.info("ABORT reçu du client")
            self._reinitialiser_association()
        elif genre == 'DONNEES':
            self._sur_donnees(donnees, pair)

    def _sur_assoc_request(self, donnees: bytes, pair):
        self.stats['assoc_request'] += 1
        if self.pannes.refuser_association:
            self.stats['refuse'] += 1
            log.info("Association REFUSÉE (panne injectée)")
            self.sock.sendto(T.construire_refuse(), pair)
            return
        # p. 73 : une seule association active à la fois
        if self.etat != self.LIBRE and self.pair != pair:
            self.stats['refuse'] += 1
            log.info("Association refusée : une association est déjà active (p. 73)")
            self.sock.sendto(T.construire_refuse(), pair)
            return

        demande = 0
        try:
            i = donnees.find(struct.pack('>H', C.NOM_ATTR_POLL_PROFILE_EXT))
            if i > 0:
                demande = struct.unpack_from('>I', donnees, i + 4)[0]
        except struct.error:
            pass
        veut_courbes = bool(demande & C.POLL_EXT_PERIOD_RTSA)
        accorde = veut_courbes and self.courbes_supportees and not self.pannes.refuser_courbes

        self.pair = pair
        self.etat = self.ATTENTE_CONFIRMATION
        self.t_association = time.monotonic()
        self.courbes_actives = accorde
        self.stats['assoc_response'] += 1
        log.info("Association acceptée — courbes demandées=%s accordées=%s",
                 veut_courbes, accorde)
        self._envoyer(T.construire_assoc_response(courbes_acceptees=accorde))

        if not self.pannes.ne_pas_repondre_mds:
            self._envoyer_mds_create()

    def _envoyer_mds_create(self):
        self.stats['mds_envoye'] += 1
        self.mds_renvois += 1
        self.t_prochain_renvoi_mds = time.monotonic() + self.PERIODE_RENVOI_MDS_S
        self._envoyer(T.construire_mds_create_event(
            self.mds_invoke_id, temps_relatif=self._temps_relatif(),
            bed_label=self.bed_label, system_id=self.system_id,
            date_heure=self._horodatage(),
            attributs_sup=[(C.NOM_ATTR_MODE_OP,
                            struct.pack('>H', self.mode_operation))]))

    def _sur_release(self, pair):
        self.stats['release_recu'] += 1
        if self.pannes.ignorer_release:
            log.info("Release IGNORÉ (panne injectée)")
            return
        # Comportement RÉEL observé : un Release reçu alors que le MDS Create
        # n'a pas été confirmé n'est pas honoré ; le moniteur poursuit ses
        # renvois puis ABORT. C'est exactement ce que faisait la sonde de
        # découverte de l'ancien module, 4300 fois en six jours.
        if self.etat != self.ACTIF:
            log.info("Release reçu sur une association incomplète : ignoré")
            return
        self.stats['release_repondu'] += 1
        log.info("Release honoré")
        self.sock.sendto(T.RELEASE_RESPONSE, pair)
        self._reinitialiser_association()

    def _sur_donnees(self, donnees: bytes, pair):
        try:
            apdu = D.decoder_apdu(donnees)
        except D.ErreurDecodage as e:
            log.warning("trame de données illisible : %s", e)
            return

        # confirmation du MDS Create
        if apdu.ro_type == C.RORS_APDU and apdu.command_type == C.CMD_CONFIRMED_EVENT_REPORT:
            if self.etat == self.ATTENTE_CONFIRMATION:
                self.etat = self.ACTIF
                self.t_prochain_renvoi_mds = None
                self.stats['mds_confirme'] += 1
                log.info("MDS Create confirmé — association ACTIVE")
            return

        if apdu.ro_type != C.ROIV_APDU or self.etat != self.ACTIF:
            return

        if apdu.command_type == C.CMD_CONFIRMED_ACTION:
            # ActionArgument (p. 49) : managed_object(6) + scope(4)
            #                           + action_type(2) + length(2) + charge
            # charge : poll_number(2) + 0x0001(2) + classe(2) + groupe(2)
            corps = apdu.donnees
            action = struct.unpack_from('>H', corps, 10)[0]
            classe = struct.unpack_from('>H', corps, 18)[0]
            groupe = struct.unpack_from('>H', corps, 20)[0]
            if action == C.NOM_ACT_POLL_MDIB_DATA:
                self._sur_poll(apdu.invoke_id, classe, groupe)
            elif action == C.NOM_ACT_POLL_MDIB_DATA_EXT:
                self._sur_poll_etendu(apdu.invoke_id, corps, classe, groupe)
        elif apdu.command_type == C.CMD_GET:
            self._sur_get_liste_priorite(apdu.invoke_id)
        elif apdu.command_type == C.CMD_CONFIRMED_SET:
            self._sur_set_liste_priorite(apdu.invoke_id, apdu.donnees)

    # ── polls ───────────────────────────────────────────────────────────────

    def _objets_numerics(self):
        t = time.monotonic() - self.t_demarrage
        objets = []
        for k, (physio, unite, base, ampli) in enumerate(NUMERICS_DEFAUT):
            valeur = base + ampli * math.sin(t / (7.0 + k)) + self.alea.gauss(0, ampli / 12)
            objets.append((0x8600 + k * 4,
                           [(0x0950, T.encoder_nu_obs_value(physio, round(valeur, 2), unite))]))
        return objets

    def _sur_poll(self, invoke_id: int, classe: int, groupe: int):
        self.stats['poll_recu'] += 1
        if classe == C.NOM_MOC_VMO_AL_MON:
            self.stats['keepalive'] += 1          # keep-alive recommandé p. 63
            objets = []
        elif classe == C.NOM_MOC_VMO_METRIC_NU:
            objets = self._objets_numerics()
        elif classe == C.NOM_MOC_PT_DEMOG:
            objets = self._objets_demographiques()
        elif classe == C.NOM_MOC_VMO_METRIC_SA_RT and groupe == C.NOM_ATTR_GRP_VMO_STATIC:
            # contexte statique des ondes : le client peut le demander
            # explicitement au lieu d'attendre le multiplexage (p. 287)
            objets = self._contexte_statique_ondes()
        else:
            objets = []
        for m in T.construire_serie_poll_result(
                invoke_id, poll_number=invoke_id, temps_relatif=self._temps_relatif(),
                classe_objet=classe, groupe_attributs=groupe, objets=objets):
            self._envoyer(m)
            self.stats['resultats_envoyes'] += 1

    def _sur_poll_etendu(self, invoke_id: int, corps: bytes, classe: int, groupe: int):
        self.stats['poll_etendu_recu'] += 1
        if classe == C.NOM_MOC_VMO_METRIC_SA_RT and not self.courbes_actives:
            log.warning("poll étendu sur les ondes alors que RTSA n'a pas été négocié "
                        "— ignoré (p. 59)")
            return
        duree = 30.0
        try:
            attributs, _ = D.decoder_liste_attributs(corps, 22)
            if C.NOM_ATTR_TIME_PD_POLL in attributs:
                duree = C.ticks_vers_secondes(
                    struct.unpack_from('>I', attributs[C.NOM_ATTR_TIME_PD_POLL], 0)[0])
        except (struct.error, D.ErreurDecodage):
            pass
        periode = (C.PERIODE_RESULTAT_COURBES_S if classe == C.NOM_MOC_VMO_METRIC_SA_RT
                   else C.PERIODE_RESULTAT_NUMERICS_S)
        self.sequence_no = 0
        self._reltime_bloc = self._temps_relatif()
        self.poll_etendu = dict(invoke_id=invoke_id, classe=classe, groupe=groupe,
                                periode=periode, fin=time.monotonic() + duree,
                                prochaine=time.monotonic())
        log.info("Poll étendu accepté : classe=0x%04X période=%.3fs durée=%.1fs",
                 classe, periode, duree)

    def _emettre_resultat_etendu(self):
        pe = self.poll_etendu
        # p. 62 : le premier résultat porte sequence_no = 0, c'est la confirmation
        sequence = self.sequence_no
        self.sequence_no = (self.sequence_no + 1) & 0xFFFF

        temps_relatif = self._temps_relatif()
        if pe['classe'] == C.NOM_MOC_VMO_METRIC_SA_RT:
            self.resultats_ondes += 1
            if self._reltime_bloc is None:
                self._reltime_bloc = temps_relatif
            temps_relatif = self._reltime_bloc
            # 256 ms exactement, que le résultat parte ou soit perdu : un
            # résultat manquant laisse donc un trou d'exactement un bloc.
            self._reltime_bloc += C.secondes_vers_ticks(C.PERIODE_RESULTAT_COURBES_S)
            if (self.pannes.perdre_un_resultat_sur
                    and self.resultats_ondes % self.pannes.perdre_un_resultat_sur == 0):
                self.stats['resultats_perdus'] += 1
                return          # le sequence_no saute : le client doit le détecter
            objets = self._objets_ondes()
        else:
            objets = self._objets_numerics()

        for m in T.construire_serie_poll_result(
                pe['invoke_id'], poll_number=pe['invoke_id'],
                temps_relatif=temps_relatif, classe_objet=pe['classe'],
                groupe_attributs=pe['groupe'], objets=objets,
                etendu=True, sequence_no=sequence):
            self._envoyer(m)
            self.stats['resultats_envoyes'] += 1

    # ── démographiques (p. 103-105) ─────────────────────────────────────────

    def admettre(self, patient_id: str, *, nom: str = 'Test', prenom: str = 'Patient',
                 sexe: int = 1, type_patient: int = 1):
        self.patient = dict(etat=C.PT_ADMITTED, patient_id=patient_id, nom=nom,
                            prenom=prenom, sexe=sexe, type_patient=type_patient)
        log.info("Patient admis : %s", patient_id)

    def sortir(self):
        """p. 103 : DISCHARGED — les données restent, le patient n'est plus assigné."""
        self.patient = dict(self.patient, etat=C.PT_DISCHARGED)
        log.info("Patient sorti")

    def _objets_demographiques(self):
        p = self.patient
        attributs = [(C.NOM_ATTR_PT_DEMOG_ST, struct.pack('>H', p['etat']))]
        if p['etat'] in (C.PT_ADMITTED, C.PT_DISCHARGED):
            attributs += [
                (C.NOM_ATTR_PT_ID, T.encoder_chaine(p['patient_id'], 20)),
                (C.NOM_ATTR_PT_NAME_FAMILY, T.encoder_chaine(p['nom'], 38)),
                (C.NOM_ATTR_PT_NAME_GIVEN, T.encoder_chaine(p['prenom'], 38)),
                (C.NOM_ATTR_PT_SEX, struct.pack('>H', p['sexe'])),
                (C.NOM_ATTR_PT_TYPE, struct.pack('>H', p['type_patient'])),
            ]
        return [(0x0001, attributs)]

    def _contexte_statique_ondes(self):
        objets = []
        for k, onde in enumerate(self.ondes):
            if onde.physio_id not in self.liste_priorite:
                continue
            objets.append((0x0300 + k, [
                (C.NOM_ATTR_SA_VAL_OBS,
                 T.encoder_sa_obs_value(onde.physio_id, b'', 0)),
                (C.NOM_ATTR_SA_SPECN, T.encoder_sa_spec(
                    onde.taille_tableau, 16, onde.bits_significatifs, onde.flags)),
                (C.NOM_ATTR_TIME_PD_SAMP, struct.pack(
                    '>I', C.secondes_vers_ticks(onde.periode_echantillonnage_ms / 1000))),
                (C.NOM_ATTR_SCALE_SPECN_I16, self._calibration(onde)),
                (C.NOM_ATTR_SA_FIXED_VAL_SPECN, T.encoder_masques_qualite({
                    C.SA_FIX_INVALID_MASK: 0x8000, C.SA_FIX_PACER_MASK: 0x8000,
                    C.SA_FIX_SATURATION: 0x4000})),
            ]))
        return objets

    def _objets_ondes(self):
        t = time.monotonic() - self.t_demarrage
        fc = 72.0 + 6.0 * math.sin(t / 11.0)
        objets = []
        for k, onde in enumerate(self.ondes):
            if onde.physio_id not in self.liste_priorite:
                continue
            bruts = onde.echantillons(t, fc)
            etat = 0
            if self.pannes.masques_qualite and self.alea.random() < 0.25:
                quoi = self.alea.choice(('invalide', 'pacer', 'saturation'))
                if quoi == 'invalide':
                    etat = C.MS_INVALID
                elif quoi == 'pacer' and CAT.est_ecg(onde.physio_id):
                    bruts[len(bruts) // 2] |= 0x8000      # bit de masque pacer
                else:
                    pleine = (1 << onde.bits_significatifs) - 1
                    for i in range(len(bruts) // 4):
                        bruts[i] = pleine
            donnees = b''.join(struct.pack('>H', v & 0xFFFF) for v in bruts)
            attributs = [(C.NOM_ATTR_SA_VAL_OBS,
                          T.encoder_sa_obs_value(onde.physio_id, donnees, etat))]
            # contexte multiplexé : un objet par 1024 ms (p. 287)
            if self.sequence_no % 4 == k % 4:
                attributs += [
                    (C.NOM_ATTR_SA_SPECN, T.encoder_sa_spec(
                        onde.taille_tableau, 16, onde.bits_significatifs, onde.flags)),
                    (C.NOM_ATTR_TIME_PD_SAMP, struct.pack(
                        '>I', C.secondes_vers_ticks(onde.periode_echantillonnage_ms / 1000))),
                    (C.NOM_ATTR_SCALE_SPECN_I16, self._calibration(onde)),
                    (C.NOM_ATTR_SA_FIXED_VAL_SPECN, T.encoder_masques_qualite({
                        C.SA_FIX_INVALID_MASK: 0x8000, C.SA_FIX_PACER_MASK: 0x8000,
                        C.SA_FIX_SATURATION: 0x4000})),
                ]
            objets.append((0x0300 + k, attributs))
        return objets

    # ── liste de priorité (p. 63-64) ────────────────────────────────────────

    def _sur_get_liste_priorite(self, invoke_id: int):
        text_ids = b''.join(struct.pack('>I', C.text_id(p)) for p in self.liste_priorite)
        liste = struct.pack('>HH', len(self.liste_priorite), len(text_ids)) + text_ids
        corps = (struct.pack('>HHH', C.NOM_MOC_VMS_MDS, 0, 0)
                 + T.encoder_liste_attributs([(C.NOM_ATTR_POLL_RTSA_PRIO_LIST, liste)]))
        self._envoyer(T._rors(invoke_id, C.CMD_GET, corps))
        log.info("GET liste de priorité -> %d ondes", len(self.liste_priorite))

    def _sur_set_liste_priorite(self, invoke_id: int, corps: bytes):
        """
        p. 287 : une entrée est SILENCIEUSEMENT ignorée si le label n'existe pas,
        si l'objet est indisponible, ou si les limites 3 ECG / 8 non-ECG sont
        dépassées. Le simulateur reproduit ce silence — c'est précisément
        pourquoi le client doit relire la liste effective avec un GET.
        """
        # SetArgument (p. 51) : managed_object(6) + scope(4) + ModificationList(4)
        #                       + ModifyOperator(2) + AVAType(oid 2, len 2) + TextIdList
        try:
            operation = struct.unpack_from('>H', corps, 14)[0]
            oid, longueur = struct.unpack_from('>HH', corps, 16)
            if operation == T.SET_TO_DEFAULT:
                self.liste_priorite = [o.physio_id for o in self.ondes]
            elif operation == T.REPLACE and oid == C.NOM_ATTR_POLL_RTSA_PRIO_LIST:
                nombre, _ = struct.unpack_from('>HH', corps, 20)
                demandes = [struct.unpack_from('>I', corps, 24 + 4 * i)[0] & 0xFFFF
                            for i in range(nombre)]
                retenues, n_ecg, n_autres = [], 0, 0
                for physio in demandes:
                    if not any(o.physio_id == physio for o in self.ondes):
                        continue                                   # onde indisponible
                    if CAT.est_ecg(physio):
                        if n_ecg >= C.MAX_ONDES_ECG: continue
                        n_ecg += 1
                    else:
                        if n_autres >= C.MAX_ONDES_NON_ECG: continue
                        n_autres += 1
                    retenues.append(physio)
                if len(retenues) != len(demandes):
                    log.info("SET liste : %d demandées, %d retenues (le reste ignoré "
                             "silencieusement, p. 287)", len(demandes), len(retenues))
                self.liste_priorite = retenues
        except struct.error as e:
            log.warning("SET liste de priorité illisible : %s", e)
        reponse = (struct.pack('>HHH', C.NOM_MOC_VMS_MDS, 0, 0)
                   + T.encoder_liste_attributs([(C.NOM_ATTR_POLL_RTSA_PRIO_LIST, b'')]))
        self._envoyer(T._rors(invoke_id, C.CMD_CONFIRMED_SET, reponse))

    # ── échéances périodiques ───────────────────────────────────────────────

    def _echeances(self):
        maintenant = time.monotonic()
        if self.etat == self.LIBRE:
            return

        ecoule = maintenant - self.t_association

        if self.pannes.redemarrage_apres_s is not None and ecoule > self.pannes.redemarrage_apres_s:
            log.warning("REDÉMARRAGE du moniteur (panne injectée)")
            self._abandonner("redémarrage")
            self.t_demarrage = maintenant           # le RelativeTime repart de zéro
            self.pannes.redemarrage_apres_s = None
            return

        if self.pannes.abort_apres_s is not None and ecoule > self.pannes.abort_apres_s:
            log.warning("ABORT (panne injectée)")
            self._abandonner("panne injectée")
            self.pannes.abort_apres_s = None
            return

        # renvoi du MDS Create non confirmé, puis ABORT — comportement réel
        if (self.etat == self.ATTENTE_CONFIRMATION and self.t_prochain_renvoi_mds
                and maintenant > self.t_prochain_renvoi_mds):
            if self.mds_renvois >= self.RENVOIS_MDS_MAX:
                log.warning("MDS Create jamais confirmé après %d envois -> ABORT",
                            self.mds_renvois)
                self._abandonner("MDS Create non confirmé")
                return
            log.info("MDS Create non confirmé, renvoi %d", self.mds_renvois + 1)
            self._envoyer_mds_create()
            return

        # timeout d'association (p. 70) : 10 s sans message du client
        if (self.etat == self.ACTIF and self.t_dernier_message_client
                and maintenant - self.t_dernier_message_client > self.TIMEOUT_KEEPALIVE_S):
            log.warning("Aucun message du client depuis %.1f s -> ABORT (p. 70)",
                        self.TIMEOUT_KEEPALIVE_S)
            self._abandonner("timeout de keep-alive")
            return

        # résultats périodiques du poll étendu
        pe = self.poll_etendu
        if pe and self.etat == self.ACTIF:
            if maintenant > pe['fin']:
                log.info("Période active du poll étendu expirée")
                self.poll_etendu = None
            elif maintenant >= pe['prochaine']:
                pe['prochaine'] += pe['periode']
                if pe['prochaine'] < maintenant:      # rattrapage après retard
                    pe['prochaine'] = maintenant + pe['periode']
                self._emettre_resultat_etendu()

    def _abandonner(self, motif: str):
        self.stats['abort'] += 1
        if self.pair and not self._muet():
            self.sock.sendto(T.construire_abort(), self.pair)
        log.warning("ABORT envoyé (%s)", motif)
        self._reinitialiser_association()


# ═════════════════════════════════════════════════════════════════════════════

def principal(argv=None):
    ap = argparse.ArgumentParser(description="Simulateur de moniteur Philips MX800")
    ap.add_argument('--adresse', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=C.PORT_MONITEUR)
    ap.add_argument('--bed-label', default='SIM1')
    ap.add_argument('--sans-courbes', action='store_true',
                    help="le moniteur ne sait pas exporter de courbes")
    ap.add_argument('--panne', action='append', metavar='NOM[:VALEUR]',
                    help="refus | abort:S | silence:S | ignorer-release | sans-mds | "
                         "refuser-courbes | perte-ondes:N | calibration:S | "
                         "redemarrage:S | masques")
    ap.add_argument('--debug', action='store_true')
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format='%(asctime)s %(levelname)-7s %(message)s')
    sim = Simulateur(adresse=args.adresse, port=args.port, bed_label=args.bed_label,
                     courbes_supportees=not args.sans_courbes,
                     pannes=Pannes.depuis_options(args.panne))
    try:
        sim.demarrer()
    except KeyboardInterrupt:
        sim.arreter()


if __name__ == '__main__':
    principal()
