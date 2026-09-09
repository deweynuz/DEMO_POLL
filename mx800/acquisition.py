"""
Boucle d'acquisition.

Trois invariants gouvernent ce module, et ils répondent chacun à un défaut
constaté sur le module précédent :

  1. La réassociation est INCONDITIONNELLE. Elle ne dépend ni d'un changement
     d'adresse, ni d'une redécouverte. L'ancien code la subordonnait à
     `found != monitor_ip`, condition jamais vraie : sur 8 646 lignes de
     journal, « Envoi Association Request » n'apparaît qu'une seule fois.

  2. Le MDS Create Event est TOUJOURS confirmé, même si l'Association Response
     est arrivée avant. Sans cela le moniteur ré-émet trois fois puis ABORT à
     ~10 s (comportement capturé sur SALLE1).

  3. Le watchdog est alimenté par le CHEMIN DE DONNÉES. Si plus rien ne
     s'écrit alors qu'on se croit en acquisition, on le journalise en ERROR, on
     force une réassociation, et systemd finit par redémarrer le service faute
     de battement. Un service « actif » qui n'enregistre rien devient
     impossible à ignorer.
"""

from __future__ import annotations

import logging
import socket
import time
from datetime import datetime, timezone

from . import adressage
from .config import Configuration
from .etat import RapporteurEtat
from .machine import Etat, MachineEtats
from .protocole import constantes as C, decodage as D, trames as T
from .protocole import ondes as CAT
from .stockage.base import Base

log = logging.getLogger('mx800.acquisition')

#: Attributs porteurs de valeurs numériques (PIPG p. 76-77)
ATTR_NU_VAL_OBS      = 0x0950
ATTR_NU_CMPD_VAL_OBS = 0x0951

DELAI_REPONSE_ASSOC_S = 5.0     # au-delà, on considère la demande perdue
MARGE_KEEPALIVE       = 0.4     # fraction du timeout négocié


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


class Acquisition:
    """
    Une itération = un appel à `tick()`. `executer()` boucle dessus.
    Ce découpage permet aux tests de piloter la boucle pas à pas.
    """

    def __init__(self, config: Configuration, base: Base, rapporteur: RapporteurEtat,
                 *, horloge=time.monotonic, chemin_baux=adressage.BAUX_DNSMASQ,
                 interface: str = 'eth0'):
        self.config = config
        self.base = base
        self.rapporteur = rapporteur
        self.horloge = horloge
        self.chemin_baux = chemin_baux
        self.interface = interface

        self.machine = MachineEtats(
            backoff_min_s=config.surveillance.backoff_min_s,
            backoff_max_s=config.surveillance.backoff_max_s,
            horloge=horloge,
            session_ouverte=lambda: self.session_id is not None,
            au_changement=self._sur_transition)

        self.sock: socket.socket | None = None
        self.adresse: str = ''
        self.session_id: int | None = None
        self.intervention_id: int | None = None
        self.code_intervention: str | None = None
        self.courbes_negociees = False
        self.timeout_association_s = 10.0
        self.bed_label = ''

        self._invoke = 1
        self._t_demande_assoc = 0.0
        self._t_dernier_poll = 0.0
        self._t_dernier_keepalive = 0.0
        self._t_derniere_ecriture = 0.0
        self._t_debut_silence: float | None = None
        self._echecs_watchdog = 0
        self._en_cours: dict[int, list] = {}     # invoke_id -> mesures accumulées
        self._horodatage_lot: dict[int, str] = {}

    # ── utilitaires ─────────────────────────────────────────────────────────

    def _prochain_invoke(self) -> int:
        self._invoke = (self._invoke % 0xFFFE) + 1
        return self._invoke

    def _sur_transition(self, t):
        self.base.enregistrer_evenement(
            niveau='WARNING' if t.apres is Etat.DECONNECTE else 'INFO',
            message=t.motif, session_id=self.session_id,
            etat_avant=str(t.avant), etat_apres=str(t.apres))
        self.rapporteur.mettre_a_jour(etat=str(t.apres), productif=self.machine.productif)
        self.rapporteur.ecrire()

    def _envoyer(self, donnees: bytes):
        if self.sock and self.adresse:
            self.sock.sendto(donnees, (self.adresse, C.PORT_MONITEUR))

    # ── cycle de vie ────────────────────────────────────────────────────────

    def ouvrir_socket(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.settimeout(0.2)
        try:
            self.sock.bind(('', C.PORT_LOCAL))
        except OSError as e:
            log.warning("port %d indisponible (%s) : port éphémère", C.PORT_LOCAL, e)
            self.sock.bind(('', 0))

    def fermer(self, motif: str = 'arrêt'):
        if self.session_id is not None:
            self._fermer_session(motif)
        if self.sock:
            if self.adresse and self.machine.etat is not Etat.DECONNECTE:
                self._envoyer(T.RELEASE_REQUEST)
            self.sock.close()
            self.sock = None

    # ── interventions ───────────────────────────────────────────────────────

    def demarrer_intervention(self, code: str, *, courbes: bool | None = None,
                              ouverture: str = 'manuelle') -> int:
        if self.intervention_id is not None:
            self.arreter_intervention()
        self.intervention_id = self.base.ouvrir_intervention(
            code, site=self.config.site.nom, salle=self.config.site.salle,
            courbes=self.config.acquisition.courbes if courbes is None else courbes,
            ouverture=ouverture)
        self.code_intervention = code
        self.rapporteur.mettre_a_jour(intervention=code)
        log.info("Intervention %s démarrée (%s)", code, ouverture)
        return self.intervention_id

    def arreter_intervention(self):
        if self.intervention_id is None:
            return
        self.base.vider_lot()
        self.base.fermer_intervention(self.intervention_id)
        log.info("Intervention %s terminée", self.code_intervention)
        self.intervention_id = None
        self.code_intervention = None
        self.rapporteur.mettre_a_jour(intervention=None)

    # ── sessions ────────────────────────────────────────────────────────────

    def _ouvrir_session(self, mds: D.MdsCreate):
        depuis_moniteur = mds.date_heure
        ecart = None
        if depuis_moniteur:
            try:
                horloge_moniteur = datetime.fromisoformat(depuis_moniteur)
                ecart = (horloge_moniteur - datetime.now()).total_seconds()
            except ValueError:
                pass
        source, synchro = _source_horloge()
        self.session_id = self.base.ouvrir_session(
            site=self.config.site.nom, salle=self.config.site.salle,
            moniteur_ip=self.adresse, moniteur_mac=self.config.moniteur.mac,
            bed_label=mds.bed_label, system_id=mds.system_id, modele=mds.modele,
            courbes_negociees=int(self.courbes_negociees),
            horloge_source=source, horloge_synchronisee=int(synchro),
            moniteur_datetime_utc=depuis_moniteur,
            moniteur_reltime=mds.temps_relatif,
            ecart_horloge_s=ecart)
        self.bed_label = mds.bed_label or ''
        self.rapporteur.mettre_a_jour(
            moniteur_ip=self.adresse, moniteur_bed_label=self.bed_label,
            courbes_negociees=self.courbes_negociees,
            horloge_source=source, horloge_synchronisee=synchro,
            ecart_horloge_moniteur_s=round(ecart, 1) if ecart is not None else None)
        self.rapporteur.etat.compteurs.associations += 1
        if ecart is not None and abs(ecart) > 5:
            log.warning("L'horloge du moniteur s'écarte de %.0f s de celle du Pi. "
                        "Les deux sont enregistrées ; l'écart est dans la session %d.",
                        ecart, self.session_id)

    def _fermer_session(self, motif: str):
        self.base.vider_lot()
        if self.session_id is not None:
            self.base.fermer_session(self.session_id, motif)
        self.session_id = None

    # ── boucle ──────────────────────────────────────────────────────────────

    def executer(self, jusqu_a: float | None = None):
        if self.sock is None:
            self.ouvrir_socket()
        self.rapporteur.notificateur.pret()
        try:
            while jusqu_a is None or self.horloge() < jusqu_a:
                self.tick()
        finally:
            self.fermer()

    def tick(self):
        self._recevoir()
        self._echeances()

    # ── réception ───────────────────────────────────────────────────────────

    def _recevoir(self):
        try:
            donnees, pair = self.sock.recvfrom(65535)
        except socket.timeout:
            return
        except OSError as e:
            log.error("réception impossible : %s", e)
            return
        if pair[0] != self.adresse:
            log.warning("paquet ignoré : reçu de %s, attendu de %s", pair[0], self.adresse)
            return

        genre = D.type_message(donnees)
        if genre == 'ASSOC_RESPONSE':
            self._sur_reponse_association(donnees)
        elif genre == 'REFUSE':
            self.rapporteur.etat.compteurs.refus += 1
            self.machine.abandonner("association refusée par le moniteur "
                                    "(une autre association est peut-être active)")
        elif genre == 'ABORT':
            self.rapporteur.etat.compteurs.aborts += 1
            self._perdre("abort reçu du moniteur")
        elif genre == 'RELEASE_RESPONSE':
            self._perdre("release confirmé par le moniteur")
        elif genre == 'DONNEES':
            self._sur_donnees(donnees)

    def _sur_reponse_association(self, donnees: bytes):
        try:
            reponse = D.decoder_reponse_association(donnees)
        except D.ErreurDecodage as e:
            self.rapporteur.etat.compteurs.erreurs_decodage += 1
            self.machine.abandonner(f"Association Response illisible : {e}")
            return
        self.courbes_negociees = reponse.courbes_acceptees
        self.timeout_association_s = reponse.timeout_association_s

        if self.config.acquisition.courbes and not reponse.courbes_acceptees:
            log.error("Courbes demandées mais REFUSÉES par le moniteur "
                      "(options négociées 0x%08X). Aucune courbe ne sera acquise ; "
                      "les numerics continuent.", reponse.options_etendues)
        log.info("Association acceptée — courbes négociées : %s, MTU rx %d, timeout %.0f s",
                 "oui" if reponse.courbes_acceptees else "non",
                 reponse.max_mtu_rx, reponse.timeout_association_s)
        # On reste en ASSOCIATION : l'association n'est établie qu'une fois le
        # MDS Create confirmé. C'est ce raccourci qui faisait croire à l'ancien
        # module qu'il était connecté alors que le moniteur allait l'abandonner.

    def _sur_donnees(self, donnees: bytes):
        try:
            apdu = D.decoder_apdu(donnees)
        except D.ErreurDecodage as e:
            self.rapporteur.etat.compteurs.erreurs_decodage += 1
            log.warning("trame illisible : %s", e)
            return

        if (apdu.ro_type == C.ROIV_APDU
                and apdu.command_type == C.CMD_CONFIRMED_EVENT_REPORT):
            self._sur_mds_create(donnees)
            return
        if apdu.command_type == C.CMD_CONFIRMED_ACTION:
            self._sur_resultat_poll(donnees, apdu)

    def _sur_mds_create(self, donnees: bytes):
        try:
            mds = D.decoder_mds_create(donnees)
        except D.ErreurDecodage as e:
            self.rapporteur.etat.compteurs.erreurs_decodage += 1
            log.warning("MDS Create illisible : %s", e)
            return

        # TOUJOURS confirmer, y compris si l'on est déjà associé : le moniteur
        # ré-émet l'événement et ABORT au bout de ~10 s sans confirmation.
        self._envoyer(T.construire_mds_create_result(
            mds.invoke_id, mds.managed_object, mds.event_time))

        if self.machine.etat is not Etat.ASSOCIATION:
            return

        attendu = self.config.moniteur.bed_label
        if self.config.moniteur.verifier_bed_label and attendu and mds.bed_label != attendu:
            self.rapporteur.mettre_a_jour(
                moniteur_conforme=False,
                alerte=f"moniteur inattendu : lit {mds.bed_label!r} au lieu de {attendu!r}")
            log.error("Le moniteur %s annonce le lit %r, or la configuration attend %r. "
                      "Acquisition REFUSÉE : enregistrer les constantes d'un autre "
                      "patient serait pire que ne rien enregistrer.",
                      self.adresse, mds.bed_label, attendu)
            self._envoyer(T.RELEASE_REQUEST)
            self.machine.abandonner("étiquette de lit non conforme")
            return

        self.rapporteur.mettre_a_jour(moniteur_conforme=True, alerte=None)
        self._ouvrir_session(mds)
        self.machine.transition(Etat.ASSOCIE, "MDS Create confirmé")
        self._t_dernier_poll = 0.0
        self._t_dernier_keepalive = self.horloge()

    def _sur_resultat_poll(self, donnees: bytes, apdu):
        try:
            resultat = D.decoder_resultat_poll(donnees)
        except D.ErreurDecodage as e:
            self.rapporteur.etat.compteurs.erreurs_decodage += 1
            log.warning("poll result illisible : %s", e)
            return
        if resultat.type_objet != C.NOM_MOC_VMO_METRIC_NU:
            return

        lot = self._en_cours.setdefault(resultat.invoke_id, [])
        self._horodatage_lot.setdefault(resultat.invoke_id, _utc())
        for objet in resultat.objets:
            for oid, brut in objet.attributs.items():
                if oid == ATTR_NU_VAL_OBS:
                    lot.append(D.decoder_valeur_numerique(brut))
                elif oid == ATTR_NU_CMPD_VAL_OBS:
                    lot.extend(D.decoder_valeurs_numeriques_composees(brut))

        # Fin de série = RORS_APDU (PIPG p. 44 et 58). Surtout PAS une
        # heuristique sur la taille du payload, qui coupait au mauvais endroit.
        if not resultat.dernier:
            return
        valeurs = self._en_cours.pop(resultat.invoke_id, [])
        horodatage = self._horodatage_lot.pop(resultat.invoke_id, _utc())
        if valeurs:
            self._enregistrer(valeurs, horodatage)

    def _enregistrer(self, valeurs, horodatage: str):
        if self.intervention_id is None:
            return          # veille assumée : on interroge, on n'archive pas
        for v in valeurs:
            catalogue = CAT.CATALOGUE.get(v.physio_id)
            self.base.empiler_mesure(
                session_id=self.session_id, intervention_id=self.intervention_id,
                ts_utc=horodatage, physio_id=v.physio_id,
                nom=catalogue.nom if catalogue else None,
                valeur=v.valeur, unite=v.unit_code, etat=v.etat, valide=v.valide)
        ecrites = self.base.vider_lot()
        if not ecrites:
            return
        self.rapporteur.signaler_donnees(ecrites)
        self._t_derniere_ecriture = self.horloge()
        self._t_debut_silence = None
        self._echecs_watchdog = 0
        if self.machine.etat is Etat.ASSOCIE:
            self.machine.transition(Etat.ACQUISITION, f"{ecrites} mesures écrites")

    # ── échéances ───────────────────────────────────────────────────────────

    def _echeances(self):
        maintenant = self.horloge()

        if self.machine.etat is Etat.DECONNECTE:
            if self.machine.doit_reessayer():
                self._tenter_association()
            return

        if (self.machine.etat is Etat.ASSOCIATION
                and maintenant - self._t_demande_assoc > DELAI_REPONSE_ASSOC_S):
            self.rapporteur.etat.compteurs.timeouts += 1
            self._perdre(f"aucune réponse du moniteur en {DELAI_REPONSE_ASSOC_S:.0f} s")
            return

        if self.machine.etat in (Etat.ASSOCIE, Etat.ACQUISITION):
            self._surveiller_donnees(maintenant)
            self._interroger(maintenant)
            self._keepalive(maintenant)

    def _tenter_association(self):
        try:
            self.adresse = adressage.resoudre(
                mac=self.config.moniteur.mac, ip=self.config.moniteur.ip,
                interface=self.interface, chemin_baux=self.chemin_baux)
        except adressage.AdresseIntrouvable as e:
            self.machine.abandonner(f"adresse introuvable : {e}")
            return
        self.machine.transition(Etat.ASSOCIATION, f"Association Request -> {self.adresse}")
        self._t_demande_assoc = self.horloge()
        self._envoyer(T.construire_assoc_request(
            courbes=self.config.acquisition.courbes,
            max_mtu_rx=self.config.acquisition.mtu,
            max_mtu_tx=self.config.acquisition.mtu))

    def _interroger(self, maintenant: float):
        if maintenant - self._t_dernier_poll < self.config.acquisition.periode_numerics_s:
            return
        self._t_dernier_poll = maintenant
        self._envoyer(T.construire_poll_request(
            self._prochain_invoke(), C.NOM_MOC_VMO_METRIC_NU,
            C.NOM_ATTR_GRP_METRIC_VAL_OBS))

    def _keepalive(self, maintenant: float):
        """
        p. 63 : en poll étendu le client parle peu et le moniteur ferme
        l'association par timeout. On envoie bien avant l'échéance négociée.
        """
        periode = self.timeout_association_s * MARGE_KEEPALIVE
        if maintenant - self._t_dernier_keepalive < periode:
            return
        self._t_dernier_keepalive = maintenant
        self._envoyer(T.construire_keep_alive(self._prochain_invoke()))
        if self.intervention_id is None:
            # veille assumée : associé, aucune intervention, rien à écrire.
            # Le watchdog est alimenté par le keep-alive, pas par les données.
            self.rapporteur.signaler_vie_sans_donnees()

    def _surveiller_donnees(self, maintenant: float):
        """
        Watchdog de DONNÉES, pas de processus. Ne s'applique que si une
        intervention est en cours : sans intervention, l'absence d'écriture est
        normale, et cette distinction est portée par l'état, pas par un cas
        particulier enfoui dans le code.
        """
        if self.intervention_id is None:
            return
        seuil = self.config.surveillance.silence_donnees_s
        derniere = self._t_derniere_ecriture or self.machine._depuis
        silence = maintenant - derniere
        if silence < seuil:
            return

        self._echecs_watchdog += 1
        self.rapporteur.etat.compteurs.reassociations_forcees += 1
        log.error("Aucune donnée écrite depuis %.0f s alors que l'intervention %s "
                  "est en cours (seuil %.0f s). Réassociation forcée (échec %d/%d).",
                  silence, self.code_intervention, seuil,
                  self._echecs_watchdog, self.config.surveillance.echecs_avant_alerte)
        self.base.enregistrer_lacune(
            type='silence_donnees', session_id=self.session_id,
            intervention_id=self.intervention_id, duree_s=round(silence, 1),
            detail=f"aucune écriture depuis {silence:.0f} s, seuil {seuil:.0f} s")
        if self._echecs_watchdog >= self.config.surveillance.echecs_avant_alerte:
            self.rapporteur.mettre_a_jour(
                alerte=f"{self._echecs_watchdog} réassociations sans reprise des "
                       f"données — intervention {self.code_intervention}")
        self._t_derniere_ecriture = maintenant     # évite de boucler à chaque tick
        self._perdre("watchdog de données")

    def _perdre(self, motif: str):
        self.rapporteur.etat.compteurs.lacunes += 1
        if self.session_id is not None:
            self.base.enregistrer_lacune(
                type='association_perdue', session_id=self.session_id,
                intervention_id=self.intervention_id, detail=motif)
            self._fermer_session(motif)
        self.machine.abandonner(motif)


def _source_horloge() -> tuple[str, bool]:
    """
    D'où vient l'heure, et est-elle synchronisée ? Un décalage silencieux
    invalide les données : le 03/09/2026, ce Pi a démarré avec 37 jours de
    retard et a tourné six jours ainsi avant qu'un serveur NTP soit joignable.
    """
    import subprocess
    from pathlib import Path
    try:
        # Sans --value : systemd réordonne les propriétés (sortie alphabétique),
        # donc on lit par clé et jamais par position.
        sortie = subprocess.run(['timedatectl', 'show'], capture_output=True,
                                text=True, timeout=5, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return 'inconnue', False
    proprietes = dict(l.split('=', 1) for l in sortie.splitlines() if '=' in l)
    synchro = proprietes.get('NTPSynchronized') == 'yes'
    a_rtc = any(Path('/sys/class/rtc').glob('rtc*'))
    return ('ntp' if synchro else 'rtc' if a_rtc else 'aucune'), synchro
