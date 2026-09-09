"""Outils partagés par les tests d'intégration contre le simulateur."""
import socket
import threading
import time

import pytest

from mx800.protocole import constantes as C, decodage as D, trames as T
from outils.simulateur import Simulateur, Pannes


def port_libre() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class ClientTest:
    """
    Client minimal : juste assez pour piloter le simulateur dans les tests.
    Ce n'est PAS le module d'acquisition — celui-ci aura sa propre machine à
    états et sera testé séparément.
    """

    def __init__(self, port: int, timeout: float = 0.4):
        self.cible = ('127.0.0.1', port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(timeout)
        self.sock.bind(('127.0.0.1', 0))
        self.recu: list[tuple[str, bytes]] = []

    def envoyer(self, donnees: bytes):
        self.sock.sendto(donnees, self.cible)

    def lire(self, duree: float = 0.5) -> list[tuple[str, bytes]]:
        """Collecte tout ce qui arrive pendant `duree` secondes."""
        fin, lot = time.monotonic() + duree, []
        while time.monotonic() < fin:
            try:
                donnees, _ = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            couple = (D.type_message(donnees), donnees)
            lot.append(couple)
            self.recu.append(couple)
        return lot

    def associer(self, courbes: bool = False, confirmer_mds: bool = True,
                 duree: float = 0.6) -> list[tuple[str, bytes]]:
        self.envoyer(T.construire_assoc_request(
            courbes=courbes, max_mtu_rx=1364 if courbes else 1000,
            max_mtu_tx=1364 if courbes else 1000))
        lot = self.lire(duree)
        if confirmer_mds:
            for genre, donnees in lot:
                if genre == 'DONNEES':
                    try:
                        mds = D.decoder_mds_create(donnees)
                    except D.ErreurDecodage:
                        continue
                    self.envoyer(T.construire_mds_create_result(
                        mds.invoke_id, mds.managed_object, mds.event_time))
                    lot += self.lire(0.15)
                    break
        return lot

    def fermer(self):
        self.sock.close()


@pytest.fixture
def simulateur():
    """Fabrique de simulateurs ; chacun sur un port libre, arrêté en fin de test."""
    lances = []

    def fabriquer(**kwargs) -> tuple[Simulateur, int]:
        port = kwargs.pop('port', None) or port_libre()
        sim = Simulateur(adresse='127.0.0.1', port=port, **kwargs)
        fil = threading.Thread(target=sim.demarrer, daemon=True)
        fil.start()
        assert sim.demarre.wait(2.0), "le simulateur n'a pas démarré"
        lances.append((sim, fil))
        return sim, port

    yield fabriquer
    for sim, fil in lances:
        sim.arreter()
        fil.join(timeout=2.0)


@pytest.fixture
def client():
    ouverts = []

    def fabriquer(port: int) -> ClientTest:
        c = ClientTest(port)
        ouverts.append(c)
        return c

    yield fabriquer
    for c in ouverts:
        c.fermer()
