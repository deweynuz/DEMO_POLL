"""
Machine à états de l'acquisition.

Le défaut structurel du module précédent était qu'il pouvait se stabiliser dans
un état qui n'acquiert rien, sans le signaler. Trois chemins y menaient :
`associated` bloqué à False, `associated=True` avec `session_id=None`, et la
boucle de redécouverte qui n'appelait jamais `send_assoc()`.

Ce module rend ces situations impossibles par construction :

  1. `self._etat` n'est écrit QUE par transition(). Impossible de changer d'état
     sans que ce soit journalisé, horodaté et exposé dans status.json.
  2. Les transitions autorisées sont déclarées. Une transition non prévue lève.
  3. ACQUISITION exige une session ouverte : la machine refuse d'y entrer sans.
  4. Le temps passé dans un état non productif est mesuré, et l'appelant dispose
     de `doit_reessayer()` — un backoff inconditionnel, jamais subordonné à un
     changement d'adresse ou à quoi que ce soit d'autre.

Ce module ne fait aucune E/S : pas de réseau, pas de base, pas de fichier.
Il est testable en une milliseconde.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

log = logging.getLogger('mx800.machine')


class Etat(str, Enum):
    DECONNECTE  = 'DECONNECTE'    # aucune association ; on va (re)tenter
    ASSOCIATION = 'ASSOCIATION'   # Association Request envoyée, réponse attendue
    ASSOCIE     = 'ASSOCIE'       # association établie, MDS confirmé, pas encore de données
    ACQUISITION = 'ACQUISITION'   # des lignes s'écrivent réellement

    def __str__(self) -> str:
        return self.value


#: Transitions autorisées. Tout le reste lève.
TRANSITIONS: dict[Etat, set[Etat]] = {
    Etat.DECONNECTE:  {Etat.ASSOCIATION},
    Etat.ASSOCIATION: {Etat.ASSOCIE, Etat.DECONNECTE},
    Etat.ASSOCIE:     {Etat.ACQUISITION, Etat.DECONNECTE},
    Etat.ACQUISITION: {Etat.ASSOCIE, Etat.DECONNECTE},
}

#: Seul état où l'on considère que le système remplit sa fonction.
ETATS_PRODUCTIFS = {Etat.ACQUISITION}


class TransitionInterdite(RuntimeError):
    """Une transition non déclarée a été tentée : c'est un bug, pas un incident."""


@dataclass
class Transition:
    horodatage: float
    avant: Etat
    apres: Etat
    motif: str


@dataclass
class MachineEtats:
    """
    `horloge` est injectable pour que les tests n'aient pas à dormir.
    `session_ouverte` est un prédicat fourni par l'appelant : la machine
    l'interroge avant d'autoriser l'entrée en ACQUISITION.
    """
    backoff_min_s: float = 1.0
    backoff_max_s: float = 60.0
    horloge: Callable[[], float] = time.monotonic
    session_ouverte: Callable[[], bool] = lambda: True
    au_changement: Callable[[Transition], None] | None = None

    _etat: Etat = field(default=Etat.DECONNECTE, init=False)
    _depuis: float = field(default=0.0, init=False)
    _echecs: int = field(default=0, init=False)
    _prochaine_tentative: float = field(default=0.0, init=False)
    historique: list[Transition] = field(default_factory=list, init=False)

    def __post_init__(self):
        self._depuis = self.horloge()

    # ── lecture ─────────────────────────────────────────────────────────────

    @property
    def etat(self) -> Etat:
        return self._etat

    @property
    def productif(self) -> bool:
        return self._etat in ETATS_PRODUCTIFS

    @property
    def depuis_s(self) -> float:
        return self.horloge() - self._depuis

    @property
    def echecs_consecutifs(self) -> int:
        return self._echecs

    @property
    def delai_backoff_s(self) -> float:
        """Exponentiel plafonné : 1, 2, 4, 8… jusqu'à backoff_max_s."""
        if self._echecs == 0:
            return 0.0
        return min(self.backoff_min_s * (2 ** (self._echecs - 1)), self.backoff_max_s)

    # ── transitions ─────────────────────────────────────────────────────────

    def transition(self, vers: Etat, motif: str) -> Transition:
        """
        Le SEUL point d'écriture de l'état. Journalise, horodate, notifie.
        """
        if vers is self._etat:
            return self.historique[-1] if self.historique else Transition(
                self.horloge(), self._etat, self._etat, motif)
        if vers not in TRANSITIONS[self._etat]:
            raise TransitionInterdite(
                f"{self._etat} -> {vers} n'est pas une transition prévue "
                f"(motif : {motif})")
        if vers is Etat.ACQUISITION and not self.session_ouverte():
            raise TransitionInterdite(
                "entrée en ACQUISITION refusée : aucune session ouverte. "
                "C'est précisément l'état où l'ancien module envoyait des polls "
                "et jetait les réponses.")

        maintenant = self.horloge()
        t = Transition(maintenant, self._etat, vers, motif)
        # Un retour à DECONNECTE depuis un état plus avancé est un échec ;
        # atteindre ACQUISITION remet le compteur à zéro.
        if vers is Etat.DECONNECTE:
            self._echecs += 1
            self._prochaine_tentative = maintenant + self.delai_backoff_s
        elif vers is Etat.ACQUISITION:
            self._echecs = 0
            self._prochaine_tentative = 0.0

        niveau = logging.WARNING if vers is Etat.DECONNECTE else logging.INFO
        log.log(niveau, "%s -> %s (%s)%s", self._etat, vers, motif,
                f" — nouvelle tentative dans {self.delai_backoff_s:.1f} s "
                f"(échec {self._echecs})" if vers is Etat.DECONNECTE else "")

        self._etat, self._depuis = vers, maintenant
        self.historique.append(t)
        if len(self.historique) > 500:
            del self.historique[:-500]
        if self.au_changement:
            self.au_changement(t)
        return t

    # ── décisions de l'appelant ─────────────────────────────────────────────

    def doit_reessayer(self) -> bool:
        """
        Faut-il relancer une association maintenant ?

        Inconditionnel : ne dépend NI d'un changement d'adresse, NI d'une
        redécouverte, NI de quoi que ce soit d'autre. C'est exactement ce que
        l'ancien module conditionnait à `found != monitor_ip`, condition qui
        n'était jamais vraie — l'adresse n'avait pas changé.
        """
        return (self._etat is Etat.DECONNECTE
                and self.horloge() >= self._prochaine_tentative)

    def abandonner(self, motif: str) -> Transition:
        """Retour à DECONNECTE depuis n'importe quel état."""
        if self._etat is Etat.DECONNECTE:
            self._echecs += 1
            self._prochaine_tentative = self.horloge() + self.delai_backoff_s
            log.warning("échec supplémentaire en DECONNECTE (%s) — "
                        "nouvelle tentative dans %.1f s (échec %d)",
                        motif, self.delai_backoff_s, self._echecs)
            return Transition(self.horloge(), self._etat, self._etat, motif)
        return self.transition(Etat.DECONNECTE, motif)

    def resume(self) -> dict:
        return {
            'etat': str(self._etat),
            'depuis_s': round(self.depuis_s, 1),
            'productif': self.productif,
            'echecs_consecutifs': self._echecs,
            'prochaine_tentative_dans_s': max(
                0.0, round(self._prochaine_tentative - self.horloge(), 1)),
            'derniere_transition': (
                f"{self.historique[-1].avant} -> {self.historique[-1].apres} "
                f"({self.historique[-1].motif})" if self.historique else None),
        }
