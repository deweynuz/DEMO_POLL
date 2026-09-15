"""
Tests de la boucle d'acquisition contre le simulateur.

Le test central est test_reprise_association_ip_inchangee : il reproduit
exactement la panne de production. L'association est perdue, l'adresse du
moniteur NE CHANGE PAS, et l'acquisition doit repartir seule. C'est cette
condition — « l'adresse n'a pas changé » — qui bloquait l'ancien module dans un
état stable où il tournait sans rien enregistrer, six semaines durant.
"""
import threading
import time
import tomllib
from pathlib import Path

import pytest

from mx800 import acquisition as ACQ
from mx800.config import valider
from mx800.etat import RapporteurEtat
from mx800.machine import Etat
from mx800.stockage.base import Base
from outils.simulateur import Pannes, Simulateur

MAC = '00:09:fb:00:00:01'


def port_libre() -> int:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def _config(tmp_path: Path, **surcharges) -> object:
    brut = tomllib.loads(f'''
[site]
nom = "HEGP"
salle = "SALLE1"
[moniteur]
mac = "{MAC}"
bed_label = "SALLE1"
[acquisition]
periode_numerics_s = 0.5
mtu = 1364
[surveillance]
silence_donnees_s = 5.0
echecs_avant_alerte = 2
backoff_min_s = 0.2
backoff_max_s = 0.8
[stockage]
chemin = "{tmp_path}"
''')
    for section, valeurs in surcharges.items():
        brut.setdefault(section, {}).update(valeurs)
    return valider(brut)


def _baux(tmp_path: Path, ip: str = '127.0.0.1') -> Path:
    chemin = tmp_path / 'dnsmasq.leases'
    chemin.write_text(f"0 {MAC} {ip} * *\n", encoding='utf-8')
    return chemin


@pytest.fixture
def acquisition(tmp_path):
    """Fabrique une Acquisition prête à tourner contre un simulateur local."""
    ouverts = []

    def fabriquer(port: int, **surcharges):
        cfg = _config(tmp_path, **surcharges)
        base = Base(tmp_path / 'mx800.db', version_module='test')
        rapporteur = RapporteurEtat(tmp_path / 'status.json',
                                    site=cfg.site.nom, salle=cfg.site.salle)
        # port_local=0 : un port éphémère. Se lier à 24106 volerait des
        # datagrammes au service de production s'il tourne sur la machine.
        # consulter_arp=False : sans cela les tests retombent sur la table ARP
        # de la machine et émettent du trafic vers de VRAIS moniteurs cliniques.
        acq = ACQ.Acquisition(cfg, base, rapporteur, chemin_baux=_baux(tmp_path),
                              port_moniteur=port, port_local=0, consulter_arp=False)
        acq.ouvrir_socket()
        ouverts.append(acq)
        return acq, base, rapporteur

    yield fabriquer
    for acq in ouverts:
        try:
            acq.fermer('fin de test')
        except OSError:
            pass


def pomper(acq, duree: float, arret=None):
    """Fait tourner la boucle pendant `duree`, ou jusqu'à ce que `arret()` soit vrai."""
    fin = time.monotonic() + duree
    while time.monotonic() < fin:
        acq.tick()
        if arret and arret():
            return True
    return False


def lancer_simulateur(port: int, **kwargs) -> tuple[Simulateur, threading.Thread]:
    kwargs.setdefault('bed_label', 'SALLE1')
    sim = Simulateur(adresse='127.0.0.1', port=port, **kwargs)
    fil = threading.Thread(target=sim.demarrer, daemon=True)
    fil.start()
    assert sim.demarre.wait(2.0)
    return sim, fil


# ═══════════════════════════════════════════════════════════════════════════
# LE test
# ═══════════════════════════════════════════════════════════════════════════

def test_reprise_association_ip_inchangee(acquisition, tmp_path):
    """
    Reproduction de la panne de production du 3 septembre 2026.

    Le moniteur coupe l'association. Son adresse ne change pas. L'ancien module
    restait bloqué : rediscover() renvoyait False parce que l'IP était la même,
    send_assoc() n'était donc jamais rappelé, et `if not associated: continue`
    court-circuitait le polling pour toujours — service « actif », zéro ligne.

    Ici l'acquisition doit repartir seule et réécrire des mesures.
    """
    port = port_libre()
    sim, _ = lancer_simulateur(port, pannes=Pannes(abort_apres_s=1.5))
    acq, base, rapporteur = acquisition(port)

    assert pomper(acq, 6.0, lambda: acq.machine.etat is Etat.ASSOCIE), \
        "l'association initiale a échoué"
    acq.demarrer_intervention('TEST-0001')
    assert pomper(acq, 6.0, lambda: acq.machine.etat is Etat.ACQUISITION), \
        "aucune donnée écrite au départ"

    lignes_avant = base.conn.execute("SELECT count(*) FROM mesures").fetchone()[0]
    adresse_avant = acq.adresse
    assert lignes_avant > 0

    # le moniteur abandonne ; l'adresse, elle, ne bouge pas
    assert pomper(acq, 8.0, lambda: acq.machine.etat is Etat.DECONNECTE), \
        "la perte d'association n'a pas été détectée"
    assert sim.stats['abort'] >= 1

    # ... et l'acquisition doit repartir toute seule
    reprise = pomper(acq, 25.0, lambda: acq.machine.etat is Etat.ACQUISITION)
    lignes_apres = base.conn.execute("SELECT count(*) FROM mesures").fetchone()[0]

    assert reprise, ("l'acquisition n'est pas repartie : c'est exactement la "
                     "panne que cette réécriture doit rendre impossible")
    assert lignes_apres > lignes_avant, "reprise annoncée mais aucune nouvelle mesure"
    assert acq.adresse == adresse_avant, "l'adresse n'était pas censée changer"

    # la coupure laisse une trace explicite, pas une simple absence de lignes
    lacunes = base.conn.execute("SELECT type, detail FROM lacunes").fetchall()
    assert any(l['type'] == 'association_perdue' for l in lacunes), \
        f"aucune lacune enregistrée : {[dict(l) for l in lacunes]}"
    assert base.conn.execute(
        "SELECT count(*) FROM sessions WHERE fin_utc IS NOT NULL").fetchone()[0] >= 1


def test_reprise_apres_redemarrage_du_moniteur(acquisition, tmp_path):
    """Le moniteur disparaît puis revient sur la même adresse."""
    port = port_libre()
    sim, fil = lancer_simulateur(port)
    acq, base, _ = acquisition(port)
    assert pomper(acq, 6.0, lambda: acq.machine.etat is Etat.ASSOCIE)
    acq.demarrer_intervention('TEST-0002')
    assert pomper(acq, 6.0, lambda: acq.machine.etat is Etat.ACQUISITION)
    lignes_avant = base.conn.execute("SELECT count(*) FROM mesures").fetchone()[0]

    sim.arreter(); fil.join(timeout=2.0)
    pomper(acq, 8.0, lambda: acq.machine.etat is Etat.DECONNECTE)
    assert acq.machine.etat is Etat.DECONNECTE

    lancer_simulateur(port)          # même port, même adresse
    assert pomper(acq, 25.0, lambda: acq.machine.etat is Etat.ACQUISITION), \
        "l'acquisition n'a pas repris après le retour du moniteur"
    assert base.conn.execute(
        "SELECT count(*) FROM mesures").fetchone()[0] > lignes_avant


# ═══════════════════════════════════════════════════════════════════════════
# Fonctionnement nominal
# ═══════════════════════════════════════════════════════════════════════════

def test_acquisition_nominale(acquisition):
    port = port_libre()
    sim, _ = lancer_simulateur(port)
    acq, base, rapporteur = acquisition(port)
    assert pomper(acq, 6.0, lambda: acq.machine.etat is Etat.ASSOCIE)
    acq.demarrer_intervention('TEST-0003')
    assert pomper(acq, 8.0, lambda: acq.machine.etat is Etat.ACQUISITION)

    lignes = base.conn.execute(
        "SELECT physio_id, parametre, valeur, valide FROM mesures").fetchall()
    assert lignes
    noms = {l['parametre'] for l in lignes if l['parametre']}
    assert 'NOM_ECG_CARD_BEAT_RATE' in noms or lignes, noms
    assert all(l['valeur'] is not None for l in lignes if l['valide'])

    session = base.conn.execute("SELECT * FROM sessions").fetchone()
    assert session['bed_label'] == 'SALLE1'
    assert session['modele'] == 'Philips M8000'
    assert session['horloge_source'] in ('ntp', 'rtc', 'aucune', 'inconnue')
    assert session['moniteur_reltime'] is not None

    rapporteur.ecrire()
    assert rapporteur.etat.productif
    assert rapporteur.etat.compteurs.lignes_ecrites == len(lignes)


def test_mds_create_toujours_confirme(acquisition):
    """Sans confirmation, le moniteur ABORT à ~10 s (correctif perdu de main)."""
    port = port_libre()
    sim, _ = lancer_simulateur(port)
    acq, _, _ = acquisition(port)
    pomper(acq, 6.0, lambda: acq.machine.etat is Etat.ASSOCIE)
    assert sim.stats['mds_confirme'] == 1
    pomper(acq, 3.0)
    assert sim.stats['abort'] == 0, "le moniteur a abandonné : MDS non confirmé"


def test_keepalive_evite_le_timeout(acquisition):
    """p. 63 et 70 : sans keep-alive, ABORT après le timeout négocié."""
    port = port_libre()
    sim, _ = lancer_simulateur(port)
    sim.TIMEOUT_KEEPALIVE_S = 2.0
    acq, _, _ = acquisition(port)
    pomper(acq, 6.0, lambda: acq.machine.etat is Etat.ASSOCIE)
    pomper(acq, 6.0)
    assert sim.stats['keepalive'] >= 1
    assert sim.stats['abort'] == 0


# ═══════════════════════════════════════════════════════════════════════════
# Refus et sécurité
# ═══════════════════════════════════════════════════════════════════════════

def test_etiquette_de_lit_non_conforme_refusee(acquisition):
    """
    Enregistrer les constantes d'un autre patient serait pire que ne rien
    enregistrer. La configuration attend SALLE1, le moniteur annonce SALLE2.
    """
    port = port_libre()
    sim, _ = lancer_simulateur(port)
    sim.bed_label = 'SALLE2'
    acq, base, rapporteur = acquisition(port)
    acq.demarrer_intervention('TEST-0004')
    pomper(acq, 8.0)

    assert acq.machine.etat is not Etat.ACQUISITION
    assert base.conn.execute("SELECT count(*) FROM mesures").fetchone()[0] == 0
    assert rapporteur.etat.moniteur_conforme is False
    assert 'SALLE2' in (rapporteur.etat.alerte or '')


def test_association_refusee_puis_backoff(acquisition):
    """
    Le moniteur refuse. On doit réessayer, avec un délai qui croît — mais SANS
    jamais renoncer : un refus signifie souvent qu'une autre association est
    active, situation temporaire.
    """
    port = port_libre()
    sim, _ = lancer_simulateur(port, pannes=Pannes(refuser_association=True))
    acq, _, rapporteur = acquisition(port)
    pomper(acq, 4.0)

    # l'état instantané oscille entre DECONNECTE et ASSOCIATION : c'est le
    # comportement voulu. Ce qui compte est qu'on réessaie et que le délai croisse.
    assert acq.machine.etat in (Etat.DECONNECTE, Etat.ASSOCIATION)
    assert rapporteur.etat.compteurs.refus >= 2, "le module doit réessayer, pas renoncer"
    assert acq.machine.echecs_consecutifs >= 2
    assert 0 < acq.machine.delai_backoff_s <= acq.config.surveillance.backoff_max_s
    assert sim.stats['refuse'] >= 2


def test_moniteur_muet_declenche_le_timeout(acquisition):
    """Aucune réponse à l'Association Request : on ne reste pas bloqué."""
    port = port_libre()                       # personne n'écoute
    acq, _, rapporteur = acquisition(port)
    assert pomper(acq, 12.0, lambda: rapporteur.etat.compteurs.timeouts >= 1)
    assert acq.machine.etat in (Etat.DECONNECTE, Etat.ASSOCIATION)
    assert acq.machine.echecs_consecutifs >= 1


# ═══════════════════════════════════════════════════════════════════════════
# Watchdog de données
# ═══════════════════════════════════════════════════════════════════════════

def test_veille_sans_intervention_n_ecrit_rien(acquisition):
    """Associé, aucune intervention : rien à écrire, et ce n'est pas une panne."""
    port = port_libre()
    sim, _ = lancer_simulateur(port)
    acq, base, rapporteur = acquisition(port)
    pomper(acq, 6.0, lambda: acq.machine.etat is Etat.ASSOCIE)
    pomper(acq, 3.0)
    assert acq.machine.etat is Etat.ASSOCIE
    assert base.conn.execute("SELECT count(*) FROM mesures").fetchone()[0] == 0
    assert base.conn.execute("SELECT count(*) FROM lacunes").fetchone()[0] == 0


def test_watchdog_donnees_force_la_reassociation(acquisition):
    """
    Le moniteur reste associé mais cesse de répondre aux polls. Le module se
    croit en acquisition et n'écrit plus : c'est le scénario « actif mais
    n'enregistre rien ». Il doit le voir et forcer une réassociation.
    """
    port = port_libre()
    sim, _ = lancer_simulateur(port)
    acq, base, rapporteur = acquisition(port)
    pomper(acq, 6.0, lambda: acq.machine.etat is Etat.ASSOCIE)
    acq.demarrer_intervention('TEST-0005')
    assert pomper(acq, 8.0, lambda: acq.machine.etat is Etat.ACQUISITION)

    sim.pannes.silence_apres_s = 0.0          # le moniteur devient muet
    sim.t_association = time.monotonic() - 1

    assert pomper(acq, 20.0,
                  lambda: rapporteur.etat.compteurs.reassociations_forcees >= 1), \
        "le watchdog de données n'a pas déclenché"
    silences = base.conn.execute(
        "SELECT detail FROM lacunes WHERE type='silence_donnees'").fetchall()
    assert silences, "la lacune de silence doit être tracée en base"
    assert acq.machine.etat is not Etat.ACQUISITION


def test_sans_moniteur_le_watchdog_est_alimente_par_les_tentatives(acquisition):
    """
    Pi débranché, ou en route vers une autre salle : aucun moniteur ne répond.
    Redémarrer le service n'y changerait rien — il en résultait une lacune
    « service_redemarre » toutes les 90 s. Tant que la boucle RÉESSAYE, elle
    alimente le watchdog ; si elle cessait de réessayer, le signal s'arrêterait.
    """
    from mx800.etat import NotificateurSystemd

    class Compteur(NotificateurSystemd):      # hors systemd : pret/statut sans effet
        battements = 0
        def battement(self):
            Compteur.battements += 1

    port = port_libre()                       # aucun simulateur : personne ne répond
    acq, base, rapporteur = acquisition(port)
    rapporteur.notificateur = Compteur()

    pomper(acq, 7.0)
    assert acq.machine.etat in (Etat.DECONNECTE, Etat.ASSOCIATION)
    # t=0 : avant et après l'envoi ; ~5,2 s : timeout puis nouvelle tentative
    assert Compteur.battements >= 3, Compteur.battements
    assert base.conn.execute("SELECT count(*) FROM mesures").fetchone()[0] == 0
