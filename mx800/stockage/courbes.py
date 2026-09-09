"""
Écriture des courbes en HDF5.

Choix structurants, chacun motivé :

  int16 BRUTS, jamais calibrés à la volée. ScaleRangeSpec16 appartient au
  contexte DYNAMIQUE (PIPG p. 86) : sans le drapeau STATIC_SCALE, la
  calibration peut changer en cours d'acquisition. L'appliquer à la volée en
  perdant ses bornes rendrait le signal irrécupérable. Elle est donc stockée
  horodatée, dans son propre jeu de données, et appliquée à la relecture.

  Canal de QUALITÉ parallèle, un octet par échantillon. Un échantillon
  invalide, un spike de pacemaker ou une saturation de capteur ne doivent
  jamais être stockés comme une valeur physiologique ordinaire (PIPG p. 83-84).

  Axe temporel CONTINU. Quand des résultats manquent, les échantillons absents
  sont écrits — marqués QUAL_MANQUANT — plutôt que sautés. L'indice reste
  proportionnel au temps, et un trou se voit dans le canal de qualité au lieu
  de se dissimuler dans un décalage.

  Le temps de chaque bloc de 256 ms est conservé tel que le moniteur l'a émis
  (RelativeTime, ticks de 1/8 ms). La conversion vers le temps absolu se fait
  à la relecture, à partir du calage enregistré dans les attributs — elle
  reste ainsi vérifiable, et refaisable si le calage était faux.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import h5py
import numpy as np

from ..protocole import constantes as C
from ..protocole import ondes as CAT
from ..protocole.decodage import Calibration

log = logging.getLogger('mx800.courbes')

VERSION_FORMAT = 1

_TYPE_CALIBRATION = np.dtype([
    ('reltime', '<i8'), ('ts_utc', 'S32'),
    ('bas_absolu', '<f8'), ('haut_absolu', '<f8'),
    ('bas_brut', '<i4'), ('haut_brut', '<i4'),
])


@dataclass
class Canal:
    physio_id: int
    nom: str
    periode_ticks: int
    taille_tableau: int
    bits_significatifs: int = 16
    flags: int = 0
    unite: int = 0
    echantillons_ecrits: int = 0
    echantillons_manquants: int = 0
    blocs: int = 0
    dernier_reltime: int | None = None
    calibrations: int = 0

    @property
    def frequence_hz(self) -> float:
        return C.TICKS_PAR_SECONDE / self.periode_ticks if self.periode_ticks else 0.0

    @property
    def masque_significatif(self) -> int:
        return (1 << self.bits_significatifs) - 1


class EcrivainCourbes:
    """Un fichier HDF5 par intervention."""

    def __init__(self, chemin: Path, *, intervention: str, site: str, salle: str,
                 session_id: int | None = None, debut_utc: str = '',
                 moniteur_reltime: int | None = None,
                 moniteur_datetime_utc: str = '', ecart_horloge_s: float | None = None,
                 compression: int = 4):
        self.chemin = Path(chemin)
        self.chemin.parent.mkdir(parents=True, exist_ok=True)
        self.compression = compression
        self.canaux: dict[int, Canal] = {}
        self._fichier = h5py.File(self.chemin, 'w')
        self._signaux = self._fichier.create_group('signaux')
        self._qualite = self._fichier.create_group('qualite')
        self._temps = self._fichier.create_group('temps')
        self._calibrations = self._fichier.create_group('calibrations')

        a = self._fichier.attrs
        a['version_format'] = VERSION_FORMAT
        a['intervention'] = intervention
        a['site'] = site
        a['salle'] = salle
        a['debut_utc'] = debut_utc
        a['session_id'] = -1 if session_id is None else session_id
        # Calage temporel : le poll result ne fournit pas d'abs_time_stamp
        # (PIPG p. 62, tous les champs à 0xff). On enregistre le couple
        # (RelativeTime, Date-and-Time) lu dans le MDS Create, et l'écart
        # constaté avec l'horloge du Pi. La conversion est ainsi refaisable.
        a['calage_moniteur_reltime'] = -1 if moniteur_reltime is None else moniteur_reltime
        a['calage_moniteur_datetime_utc'] = moniteur_datetime_utc
        a['calage_ecart_horloge_s'] = (float('nan') if ecart_horloge_s is None
                                       else ecart_horloge_s)
        a['ticks_par_seconde'] = C.TICKS_PAR_SECONDE
        a['note'] = ("Échantillons int16 BRUTS. Appliquer la calibration du groupe "
                     "/calibrations valide à l'instant considéré. Le canal /qualite "
                     "porte les masques SA_FIX (PIPG p. 83-84) ; l'octet "
                     f"0x{C.QUAL_MANQUANT:02X} marque un échantillon jamais reçu.")

    # ── déclaration des canaux ──────────────────────────────────────────────

    def declarer_canal(self, physio_id: int, *, periode_ticks: int,
                       taille_tableau: int, bits_significatifs: int = 16,
                       flags: int = 0) -> Canal:
        if physio_id in self.canaux:
            return self.canaux[physio_id]
        onde = CAT.CATALOGUE.get(physio_id)
        nom = onde.nom if onde else f'PHYSIO_{physio_id:04X}'
        canal = Canal(physio_id=physio_id, nom=nom, periode_ticks=periode_ticks,
                      taille_tableau=taille_tableau,
                      bits_significatifs=bits_significatifs, flags=flags,
                      unite=onde.unite if onde else 0)
        self.canaux[physio_id] = canal

        options = dict(maxshape=(None,), chunks=(max(taille_tableau, 64),),
                       compression='gzip', compression_opts=self.compression,
                       shuffle=True)
        signal = self._signaux.create_dataset(nom, shape=(0,), dtype='<i2', **options)
        self._qualite.create_dataset(nom, shape=(0,), dtype='u1', **options)
        self._temps.create_dataset(nom, shape=(0,), dtype='<i8',
                                   maxshape=(None,), chunks=(256,),
                                   compression='gzip', compression_opts=self.compression)
        self._calibrations.create_dataset(nom, shape=(0,), dtype=_TYPE_CALIBRATION,
                                          maxshape=(None,), chunks=(16,))

        s = signal.attrs
        s['physio_id'] = physio_id
        s['nom'] = nom
        s['label_nom'] = onde.label_nom if onde else ''
        s['label'] = onde.label if onde else 0
        s['unite'] = canal.unite
        s['periode_echantillonnage_ticks'] = periode_ticks
        s['frequence_hz'] = canal.frequence_hz
        s['bits_significatifs'] = bits_significatifs
        s['taille_tableau'] = taille_tableau
        s['saflags'] = flags
        s['calibration_statique'] = bool(flags & C.STATIC_SCALE)
        s['masquer_bits_non_significatifs'] = bool(flags & C.SA_EXT_VAL_RANGE)
        log.info("Canal déclaré : %s (%.1f Hz, %d éch./bloc, %d bits significatifs)",
                 nom, canal.frequence_hz, taille_tableau, bits_significatifs)
        return canal

    def mettre_a_jour_specification(self, physio_id: int, *, periode_ticks: int | None = None,
                                    bits_significatifs: int | None = None,
                                    flags: int | None = None):
        """Le contexte statique arrive multiplexé, parfois après les données (p. 287)."""
        canal = self.canaux.get(physio_id)
        if canal is None:
            return
        jeu = self._signaux[canal.nom]
        if periode_ticks and periode_ticks != canal.periode_ticks:
            log.warning("%s : période d'échantillonnage %d -> %d ticks",
                        canal.nom, canal.periode_ticks, periode_ticks)
            canal.periode_ticks = periode_ticks
            jeu.attrs['periode_echantillonnage_ticks'] = periode_ticks
            jeu.attrs['frequence_hz'] = canal.frequence_hz
        if bits_significatifs and bits_significatifs != canal.bits_significatifs:
            canal.bits_significatifs = bits_significatifs
            jeu.attrs['bits_significatifs'] = bits_significatifs
        if flags is not None and flags != canal.flags:
            canal.flags = flags
            jeu.attrs['saflags'] = flags
            jeu.attrs['calibration_statique'] = bool(flags & C.STATIC_SCALE)
            jeu.attrs['masquer_bits_non_significatifs'] = bool(flags & C.SA_EXT_VAL_RANGE)

    # ── écriture ────────────────────────────────────────────────────────────

    def _agrandir(self, jeu, valeurs: np.ndarray):
        n = jeu.shape[0]
        jeu.resize((n + len(valeurs),))
        jeu[n:] = valeurs

    def ecrire_calibration(self, physio_id: int, calibration: Calibration,
                           *, reltime: int, ts_utc: str = ''):
        canal = self.canaux.get(physio_id)
        if canal is None:
            return
        jeu = self._calibrations[canal.nom]
        nan = float('nan')
        ligne = np.array([(
            reltime, ts_utc.encode('utf-8')[:32],
            nan if calibration.borne_basse_absolue is None else calibration.borne_basse_absolue,
            nan if calibration.borne_haute_absolue is None else calibration.borne_haute_absolue,
            calibration.borne_basse_brute, calibration.borne_haute_brute,
        )], dtype=_TYPE_CALIBRATION)
        if jeu.shape[0]:
            derniere = jeu[-1]
            memes = (np.allclose([derniere['bas_absolu'], derniere['haut_absolu']],
                                 [ligne['bas_absolu'][0], ligne['haut_absolu'][0]],
                                 equal_nan=True)
                     and derniere['bas_brut'] == ligne['bas_brut'][0]
                     and derniere['haut_brut'] == ligne['haut_brut'][0])
            if memes:
                return          # inchangée : on n'enregistre que les transitions
            log.warning("%s : la calibration a changé en cours d'acquisition "
                        "(%.4g..%.4g -> %.4g..%.4g)", canal.nom,
                        derniere['bas_absolu'], derniere['haut_absolu'],
                        ligne['bas_absolu'][0], ligne['haut_absolu'][0])
        self._agrandir(jeu, ligne)
        canal.calibrations += 1

    def ecrire_bloc(self, physio_id: int, *, reltime: int, echantillons: bytes,
                    etat: int = 0, masques: dict[int, int] | None = None) -> int:
        """
        Écrit un bloc de 256 ms. Renvoie le nombre d'échantillons manquants
        comblés avant ce bloc.
        """
        canal = self.canaux.get(physio_id)
        if canal is None:
            return 0

        bruts = np.frombuffer(echantillons, dtype='>i2').astype('<i2')
        qualite = np.zeros(len(bruts), dtype='u1')

        # PIPG p. 87 : la mesure n'est valide que si le premier octet de l'état
        # est nul. Un bloc invalide est conservé mais entièrement marqué.
        if not C.mesure_valide(etat):
            qualite |= C.QUAL_INVALIDE

        # Masques par échantillon (SaFixedValSpec16, p. 83-84). Ils sont
        # extraits AVANT le masquage des bits non significatifs, faute de quoi
        # le bit de marquage disparaîtrait avec eux.
        for identifiant, masque in (masques or {}).items():
            bit = C.BIT_QUALITE_POUR_MASQUE.get(identifiant)
            if bit is None or not masque:
                continue
            touches = (bruts.astype('<u2') & np.uint16(masque)) != 0
            qualite[touches] |= bit

        # p. 83 : masquer les bits non significatifs si SA_EXT_VAL_RANGE
        if canal.flags & C.SA_EXT_VAL_RANGE and canal.bits_significatifs < 16:
            bruts = (bruts.astype('<u2') & np.uint16(canal.masque_significatif)).astype('<i2')

        combles = self._combler(canal, reltime)

        self._agrandir(self._signaux[canal.nom], bruts)
        self._agrandir(self._qualite[canal.nom], qualite)
        self._agrandir(self._temps[canal.nom], np.array([reltime], dtype='<i8'))
        canal.echantillons_ecrits += len(bruts)
        canal.blocs += 1
        canal.dernier_reltime = reltime
        return combles

    def _combler(self, canal: Canal, reltime: int) -> int:
        """
        Comble les échantillons manquants entre le dernier bloc et celui-ci,
        pour que l'indice reste proportionnel au temps.
        """
        if canal.dernier_reltime is None or not canal.periode_ticks:
            return 0
        attendu = canal.dernier_reltime + canal.taille_tableau * canal.periode_ticks
        ecart_ticks = reltime - attendu
        # Un moniteur aligne rel_time_stamp sur son horloge d'échantillonnage :
        # deux blocs consécutifs sont exactement contigus. On tolère malgré tout
        # une gigue d'un demi-bloc avant de conclure à un trou — en deçà, il
        # s'agit d'imprécision d'émission, pas d'échantillons perdus. Au-delà
        # d'un demi-bloc, un résultat entier manque nécessairement.
        tolerance = max(1, canal.taille_tableau // 2) * canal.periode_ticks
        if ecart_ticks <= tolerance:
            return 0
        manquants = int(round(ecart_ticks / canal.periode_ticks))
        if manquants <= 0:
            return 0
        # garde-fou : un recul de RelativeTime (redémarrage du moniteur) ne doit
        # pas provoquer une allocation démesurée
        plafond = int(600 * C.TICKS_PAR_SECONDE / canal.periode_ticks)
        if manquants > plafond:
            log.error("%s : saut de %d échantillons (%.1f s) — probablement un "
                      "redémarrage du moniteur. Trou tronqué à %d échantillons.",
                      canal.nom, manquants,
                      manquants * canal.periode_ticks / C.TICKS_PAR_SECONDE, plafond)
            manquants = plafond
        self._agrandir(self._signaux[canal.nom], np.zeros(manquants, dtype='<i2'))
        self._agrandir(self._qualite[canal.nom],
                       np.full(manquants, C.QUAL_MANQUANT, dtype='u1'))
        canal.echantillons_manquants += manquants
        log.warning("%s : %d échantillons manquants comblés (%.2f s)",
                    canal.nom, manquants,
                    manquants * canal.periode_ticks / C.TICKS_PAR_SECONDE)
        return manquants

    # ── clôture ─────────────────────────────────────────────────────────────

    def statistiques(self) -> dict:
        return {c.nom: {
            'physio_id': c.physio_id, 'frequence_hz': round(c.frequence_hz, 2),
            'echantillons': c.echantillons_ecrits, 'blocs': c.blocs,
            'manquants': c.echantillons_manquants,
            'completude': (round(100 * c.echantillons_ecrits /
                                 (c.echantillons_ecrits + c.echantillons_manquants), 2)
                           if c.echantillons_ecrits + c.echantillons_manquants else 0.0),
            'calibrations': c.calibrations,
        } for c in self.canaux.values()}

    def fermer(self, *, fin_utc: str = '') -> dict:
        stats = self.statistiques()
        self._fichier.attrs['fin_utc'] = fin_utc
        for canal in self.canaux.values():
            jeu = self._signaux[canal.nom]
            jeu.attrs['echantillons'] = canal.echantillons_ecrits
            jeu.attrs['manquants'] = canal.echantillons_manquants
            jeu.attrs['blocs'] = canal.blocs
        self._fichier.close()
        return stats
