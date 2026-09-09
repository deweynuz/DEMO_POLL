"""
Tests unitaires de la machine à états. Aucune E/S : ils tournent en
millisecondes et n'ont besoin ni de moniteur ni de simulateur.
"""
import pytest

from mx800.machine import Etat, MachineEtats, TransitionInterdite


@pytest.fixture
def horloge():
    return [1000.0]


@pytest.fixture
def machine(horloge):
    return MachineEtats(backoff_min_s=1.0, backoff_max_s=8.0,
                        horloge=lambda: horloge[0])


def test_demarre_deconnecte(machine):
    assert machine.etat is Etat.DECONNECTE
    assert not machine.productif


def test_cycle_nominal(machine):
    machine.transition(Etat.ASSOCIATION, 'demande')
    machine.transition(Etat.ASSOCIE, 'MDS confirmé')
    assert not machine.productif, "associé n'est pas acquérir"
    machine.transition(Etat.ACQUISITION, 'première ligne')
    assert machine.productif


def test_transition_non_declaree_leve(machine):
    """Un saut d'état est un bug de programmation, pas un incident réseau."""
    with pytest.raises(TransitionInterdite, match="n'est pas une transition prévue"):
        machine.transition(Etat.ACQUISITION, 'saut')


def test_acquisition_exige_une_session():
    """
    L'ancien module pouvait avoir associated=True et session_id=None : il
    envoyait des polls et jetait les réponses, sans un seul message d'erreur.
    """
    m = MachineEtats(session_ouverte=lambda: False)
    m.transition(Etat.ASSOCIATION, 'x')
    m.transition(Etat.ASSOCIE, 'y')
    with pytest.raises(TransitionInterdite, match='aucune session'):
        m.transition(Etat.ACQUISITION, 'sans session')


def test_reessai_inconditionnel(machine, horloge):
    """
    L'invariant central. L'ancien code écrivait :

        if found and found != monitor_ip:   # jamais vrai, l'IP ne changeait pas
            return True
        ...
        if rediscover():                    # renvoie False
            send_assoc()                    # donc jamais appelé

    Ici, doit_reessayer() ne dépend que du temps écoulé.
    """
    machine.transition(Etat.ASSOCIATION, 'demande')
    machine.abandonner('silence')
    assert not machine.doit_reessayer(), "le backoff doit d'abord s'écouler"
    horloge[0] += machine.delai_backoff_s
    assert machine.doit_reessayer(), \
        "après le délai, on réessaie — quelles que soient les circonstances"


def test_backoff_exponentiel_plafonne(machine, horloge):
    delais = []
    for _ in range(6):
        machine.transition(Etat.ASSOCIATION, 'tentative')
        machine.abandonner('échec')
        delais.append(machine.delai_backoff_s)
        horloge[0] += delais[-1]
    assert delais == [1.0, 2.0, 4.0, 8.0, 8.0, 8.0], delais
    assert max(delais) <= machine.backoff_max_s


def test_acquisition_remet_le_compteur_a_zero(machine, horloge):
    for _ in range(3):
        machine.transition(Etat.ASSOCIATION, 't')
        machine.abandonner('échec')
        horloge[0] += machine.delai_backoff_s
    assert machine.echecs_consecutifs == 3
    machine.transition(Etat.ASSOCIATION, 't')
    machine.transition(Etat.ASSOCIE, 'ok')
    machine.transition(Etat.ACQUISITION, 'données')
    assert machine.echecs_consecutifs == 0
    assert machine.delai_backoff_s == 0.0


def test_abandonner_depuis_n_importe_quel_etat(machine):
    for depart in (Etat.ASSOCIATION, Etat.ASSOCIE, Etat.ACQUISITION):
        m = MachineEtats()
        m.transition(Etat.ASSOCIATION, 'x')
        if depart is not Etat.ASSOCIATION:
            m.transition(Etat.ASSOCIE, 'y')
        if depart is Etat.ACQUISITION:
            m.transition(Etat.ACQUISITION, 'z')
        m.abandonner('coupure')
        assert m.etat is Etat.DECONNECTE


def test_chaque_changement_est_notifie(machine):
    vues = []
    machine.au_changement = vues.append
    machine.transition(Etat.ASSOCIATION, 'a')
    machine.transition(Etat.ASSOCIE, 'b')
    machine.abandonner('c')
    assert [(str(t.avant), str(t.apres)) for t in vues] == [
        ('DECONNECTE', 'ASSOCIATION'), ('ASSOCIATION', 'ASSOCIE'),
        ('ASSOCIE', 'DECONNECTE')]
    assert len(machine.historique) == 3


def test_aucun_etat_stable_non_productif_sans_reessai(machine, horloge):
    """
    Le cœur de l'exigence : il ne doit exister aucun état où la machine reste
    indéfiniment sans ni acquérir ni chercher à se reconnecter.
    """
    machine.transition(Etat.ASSOCIATION, 'x')
    machine.abandonner('perte')
    horloge[0] += 3600.0                      # une heure plus tard
    assert machine.doit_reessayer(), \
        "après une heure en DECONNECTE, le module doit encore vouloir réessayer"


def test_resume_expose_l_essentiel(machine):
    machine.transition(Etat.ASSOCIATION, 'demande')
    r = machine.resume()
    assert r['etat'] == 'ASSOCIATION'
    assert r['productif'] is False
    assert 'DECONNECTE -> ASSOCIATION' in r['derniere_transition']
