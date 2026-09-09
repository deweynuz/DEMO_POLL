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
from .stockage.courbes import EcrivainCourbes

log = logging.getLogger('mx800.acquisition')

#: Attributs porteurs de valeurs numériques (PIPG p. 76-77)
ATTR_NU_VAL_OBS      = 0x0950
ATTR_NU_CMPD_VAL_OBS = 0x0951

DELAI_REPONSE_ASSOC_S = 5.0     # au-delà, on considère la demande perdue
MARGE_KEEPALIVE       = 0.4     # fraction du timeout négocié

#: Durée demandée dans PollDataReqPeriod (PIPG p. 60). La requête doit être
#: renouvelée AVANT expiration (p. 61) ; on la renouvelle à la moitié.
PERIODE_ACTIVE_POLL_ETENDU_S = 30.0
#: Nombre de dégradations tolérées avant de couper les courbes. La perte des
#: courbes ne doit JAMAIS faire tomber les numerics.
ECHECS_COURBES_AVANT_COUPURE = 3


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

        # courbes
        self.ecrivain_courbes: EcrivainCourbes | None = None
        self.courbes_actives = False
        self.ondes_demandees: list[int] = []
        self.ondes_effectives: list[int] | None = None
        self._t_poll_etendu = 0.0
        self._sequence_attendue: int | None = None
        self._echecs_courbes = 0
        self._masques_par_canal: dict[int, dict[int, int]] = {}

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
        veut_courbes = self.config.acquisition.courbes if courbes is None else courbes
        if veut_courbes:
            self._ouvrir_courbes()
        return self.intervention_id

    def arreter_intervention(self):
        if self.intervention_id is None:
            return
        self.base.vider_lot()
        self._fermer_courbes()
        self.base.fermer_intervention(self.intervention_id)
        log.info("Intervention %s terminée", self.code_intervention)
        self.intervention_id = None
        self.code_intervention = None
        self.rapporteur.mettre_a_jour(intervention=None)

    # ── courbes ─────────────────────────────────────────────────────────────

    def _ouvrir_courbes(self):
        if self.ecrivain_courbes is not None or not self.code_intervention:
            return
        if not self.courbes_negociees:
            log.warning("Courbes demandées pour %s mais non négociées avec le "
                        "moniteur : seuls les numerics seront enregistrés.",
                        self.code_intervention)
            return
        session = self.base.conn.execute(
            "SELECT moniteur_reltime, moniteur_datetime_utc, ecart_horloge_s "
            "FROM sessions WHERE id=?", (self.session_id,)).fetchone()
        chemin = (self.config.courbes_dir /
                  f"{self.code_intervention}_{_utc()[:19].replace(':', '')}.h5")
        self.ecrivain_courbes = EcrivainCourbes(
            chemin, intervention=self.code_intervention,
            site=self.config.site.nom, salle=self.config.site.salle,
            session_id=self.session_id, debut_utc=_utc(),
            moniteur_reltime=session['moniteur_reltime'] if session else None,
            moniteur_datetime_utc=(session['moniteur_datetime_utc'] or '') if session else '',
            ecart_horloge_s=session['ecart_horloge_s'] if session else None)
        log.info("Courbes -> %s", chemin)
        self._demander_liste_ondes()
        # Le contexte statique arrive normalement multiplexé, un objet par
        # 1024 ms (p. 287). On le demande explicitement pour connaître tout de
        # suite la période d'échantillonnage de chaque onde : sans elle, les
        # premiers blocs seraient jetés — on ne stocke pas un signal dont on
        # ignore la fréquence (p. 84).
        self._envoyer(T.construire_poll_request(
            self._prochain_invoke(), C.NOM_MOC_VMO_METRIC_SA_RT,
            C.NOM_ATTR_GRP_VMO_STATIC))
        self._demander_poll_etendu(force=True)

    def _fermer_courbes(self):
        if self.ecrivain_courbes is None:
            return
        stats = self.ecrivain_courbes.fermer(fin_utc=_utc())
        chemin = self.ecrivain_courbes.chemin
        octets = chemin.stat().st_size if chemin.exists() else 0
        self.base.enregistrer_fichier_courbes(
            intervention_id=self.intervention_id, session_id=self.session_id,
            chemin=str(chemin), fin_utc=_utc(), octets=octets,
            canaux=','.join(stats))
        for nom, s in stats.items():
            log.info("Courbe %s : %d échantillons, %d manquants, complétude %.2f %%",
                     nom, s['echantillons'], s['manquants'], s['completude'])
        self.ecrivain_courbes = None
        self._sequence_attendue = None

    def _demander_liste_ondes(self):
        """
        SET PRIORITY LIST puis GET pour relire la liste EFFECTIVE (PIPG p. 63-64).
        Le moniteur ignore silencieusement les entrées invalides ou en excès
        (p. 287) : sans relecture, on croirait avoir demandé ce qu'on n'a pas.
        """
        self.ondes_demandees = self.config.physio_ids_ondes
        if not self.ondes_demandees:
            return
        self.ondes_effectives = None
        self._envoyer(T.construire_set_liste_priorite(
            self._prochain_invoke(), self.ondes_demandees))
        self._envoyer(T.construire_get_liste_priorite(self._prochain_invoke()))

    def _sur_liste_ondes(self, apdu):
        try:
            attributs, _ = D.decoder_liste_attributs(apdu.donnees, 6)
            brut = attributs.get(C.NOM_ATTR_POLL_RTSA_PRIO_LIST)
            if brut is None or len(brut) < 4:
                return
            import struct as _s
            nombre, _ = _s.unpack_from('>HH', brut, 0)
            effectives = [_s.unpack_from('>I', brut, 4 + 4 * i)[0] & 0xFFFF
                          for i in range(nombre)]
        except (D.ErreurDecodage, Exception) as e:
            log.warning("liste de priorité illisible : %s", e)
            return

        self.ondes_effectives = effectives
        manquantes = [p for p in self.ondes_demandees if p not in effectives]
        if manquantes:
            noms = ', '.join((CAT.CATALOGUE[p].nom if p in CAT.CATALOGUE
                              else f'0x{p:04X}') for p in manquantes)
            log.error("Le moniteur n'a pas retenu %d onde(s) demandée(s) : %s. "
                      "Label inexistant, objet indisponible, ou limites 3 ECG / "
                      "8 non-ECG dépassées (PIPG p. 287).", len(manquantes), noms)
            self.base.enregistrer_lacune(
                type='ondes_non_retenues', session_id=self.session_id,
                intervention_id=self.intervention_id, detail=noms)
        else:
            log.info("Liste d'ondes confirmée par le moniteur : %d onde(s)",
                     len(effectives))

    def _demander_poll_etendu(self, force: bool = False):
        maintenant = self.horloge()
        if not force and maintenant - self._t_poll_etendu < PERIODE_ACTIVE_POLL_ETENDU_S / 2:
            return
        self._t_poll_etendu = maintenant
        self._envoyer(T.construire_poll_request_etendu(
            self._prochain_invoke(), C.NOM_MOC_VMO_METRIC_SA_RT,
            C.NOM_ATTR_GRP_TOUS,          # 0 : contexte multiplexé (PIPG p. 287)
            PERIODE_ACTIVE_POLL_ETENDU_S))

    def _degrader_courbes(self, motif: str):
        """
        La perte des courbes ne doit jamais faire tomber les numerics : on
        coupe, on journalise, on continue.
        """
        self._echecs_courbes += 1
        log.warning("Courbes : %s (%d/%d)", motif, self._echecs_courbes,
                    ECHECS_COURBES_AVANT_COUPURE)
        if self._echecs_courbes < ECHECS_COURBES_AVANT_COUPURE:
            return
        log.error("Courbes coupées après %d dégradations (%s). Les numerics "
                  "continuent.", self._echecs_courbes, motif)
        self.base.enregistrer_lacune(
            type='courbes_coupees', session_id=self.session_id,
            intervention_id=self.intervention_id, detail=motif)
        self._fermer_courbes()
        self.courbes_actives = False
        self.rapporteur.mettre_a_jour(
            courbes_negociees=False,
            alerte=f"courbes coupées : {motif} — numerics maintenus")

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
        self._fermer_courbes()
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
        elif apdu.command_type == C.CMD_GET:
            self._sur_liste_ondes(apdu)

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
        if resultat.type_objet == C.NOM_MOC_VMO_METRIC_SA_RT:
            if resultat.etendu:
                self._sur_resultat_ondes(resultat)
            else:
                # réponse au poll de contexte statique : déclare les canaux
                horodatage = _utc()
                for objet in resultat.objets:
                    self._traiter_objet_onde(objet, resultat.temps_relatif, horodatage)
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

    def _sur_resultat_ondes(self, resultat):
        """
        Un résultat périodique de courbes (256 ms, PIPG p. 60).

        Le suivi du sequence_no est la seule façon de détecter un message perdu
        (p. 62) : le premier porte 0, les suivants s'incrémentent. Un saut est
        enregistré comme lacune ET comblé dans le fichier, pour que l'axe
        temporel reste continu.
        """
        if self.ecrivain_courbes is None:
            return
        self.rapporteur.etat.compteurs.resultats_ondes += 1

        sequence = resultat.sequence_no
        if sequence is not None:
            if sequence == 0:
                # confirmation que la requête étendue a été acceptée (p. 60)
                log.info("Poll étendu confirmé par le moniteur (sequence_no = 0)")
                self._sequence_attendue = 1
            elif self._sequence_attendue is not None:
                perdus = (sequence - self._sequence_attendue) & 0xFFFF
                if 0 < perdus < 1000:
                    self.rapporteur.etat.compteurs.ondes_manquantes += perdus
                    log.warning("%d résultat(s) de courbe perdu(s) "
                                "(sequence_no %d attendu, %d reçu)",
                                perdus, self._sequence_attendue, sequence)
                    self.base.enregistrer_lacune(
                        type='sequence_ondes_manquante', session_id=self.session_id,
                        intervention_id=self.intervention_id,
                        detail=f"{perdus} résultat(s), sequence_no "
                               f"{self._sequence_attendue} -> {sequence}")
                self._sequence_attendue = (sequence + 1) & 0xFFFF

        horodatage = _utc()
        for objet in resultat.objets:
            self._traiter_objet_onde(objet, resultat.temps_relatif, horodatage)

        if self.machine.etat is Etat.ASSOCIE and self.intervention_id is not None:
            # des courbes arrivent : on acquiert, même si les numerics tardent
            self.machine.transition(Etat.ACQUISITION, "courbes reçues")
        self.rapporteur.signaler_vie_sans_donnees()

    def _traiter_objet_onde(self, objet, temps_relatif: int, horodatage: str):
        # Le moniteur continue d'émettre jusqu'à expiration de l'active_period :
        # après une coupure des courbes, ces résultats tardifs sont ignorés.
        if self.ecrivain_courbes is None:
            return
        attributs = objet.attributs

        # contexte statique/dynamique, multiplexé un objet par 1024 ms (p. 287)
        specification = attributs.get(C.NOM_ATTR_SA_SPECN)
        periode = attributs.get(C.NOM_ATTR_TIME_PD_SAMP)
        calibration = attributs.get(C.NOM_ATTR_SCALE_SPECN_I16)
        masques = attributs.get(C.NOM_ATTR_SA_FIXED_VAL_SPECN)

        ondes = []
        if C.NOM_ATTR_SA_VAL_OBS in attributs:
            onde, _ = D.decoder_sa_obs_value(attributs[C.NOM_ATTR_SA_VAL_OBS])
            ondes.append(onde)
        if C.NOM_ATTR_SA_CMPD_VAL_OBS in attributs:
            # ECG composé : 3 voies à 250 sps partageant un contexte (p. 287)
            ondes.extend(D.decoder_sa_obs_value_cmp(
                attributs[C.NOM_ATTR_SA_CMPD_VAL_OBS]))
        if not ondes:
            return

        spec = D.decoder_sa_spec(specification) if specification else None
        periode_ticks = None
        if periode and len(periode) >= 4:
            import struct as _s
            periode_ticks = _s.unpack_from('>I', periode, 0)[0]
        table_masques = D.decoder_masques_qualite(masques) if masques else None

        for onde in ondes:
            canal = self.ecrivain_courbes.canaux.get(onde.physio_id)
            if canal is None:
                # p. 84 : la fréquence se LIT, elle ne se code jamais en dur.
                if periode_ticks is None:
                    log.debug("onde 0x%04X reçue avant sa période "
                              "d'échantillonnage : bloc ignoré", onde.physio_id)
                    continue
                # array_size vient de SaSpec (p. 83), pas du bloc reçu : lors
                # du poll de contexte statique le bloc est vide.
                self.ecrivain_courbes.declarer_canal(
                    onde.physio_id, periode_ticks=periode_ticks,
                    taille_tableau=(spec.taille_tableau if spec and spec.taille_tableau
                                    else onde.nombre),
                    bits_significatifs=spec.bits_significatifs if spec else 16,
                    flags=spec.flags if spec else 0)
            elif spec or periode_ticks:
                self.ecrivain_courbes.mettre_a_jour_specification(
                    onde.physio_id, periode_ticks=periode_ticks,
                    bits_significatifs=spec.bits_significatifs if spec else None,
                    flags=spec.flags if spec else None)

            if table_masques:
                self._masques_par_canal[onde.physio_id] = table_masques
            if calibration:
                self.ecrivain_courbes.ecrire_calibration(
                    onde.physio_id, D.decoder_calibration(calibration),
                    reltime=temps_relatif, ts_utc=horodatage)

            if not onde.echantillons:
                continue          # poll de contexte : rien à écrire
            combles = self.ecrivain_courbes.ecrire_bloc(
                onde.physio_id, reltime=temps_relatif,
                echantillons=onde.echantillons, etat=onde.etat,
                masques=self._masques_par_canal.get(onde.physio_id))
            if combles:
                self.rapporteur.etat.compteurs.ondes_manquantes += combles

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
            if self.ecrivain_courbes is not None:
                # p. 61 : renvoyer la requête AVANT expiration de l'active_period
                self._demander_poll_etendu()
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
        self._sequence_attendue = None
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
