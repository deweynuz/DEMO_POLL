"""Réglages après installation : nom de salle, sans casser la configuration."""
import pytest

from mx800 import config as K
from mx800.stockage import base as B
from outils import console, reglages as R

CONFIG = '''# en-tête
[site]
nom = "HEGP"
# salle vide = celle qu'annonce le moniteur
salle = "S3B3"

[moniteur]
mac = ""
bed_label = "S3B3"   # épinglé
verifier_bed_label = true

[stockage]
chemin = "{donnees}"
'''


@pytest.fixture
def pi(tmp_path, monkeypatch):
    f = tmp_path / 'config.toml'
    f.write_text(CONFIG.format(donnees=tmp_path / 'pi'))
    redemarrages = []
    monkeypatch.setattr(R, 'redemarrer_service', lambda: redemarrages.append(1) or True)
    return f, redemarrages


def lancer(f, *args):
    return console.principal(['--config', str(f), '--sans-couleur', '--port', '1', *args])


def test_modifier_toml_garde_les_commentaires():
    texte = R.modifier_toml(CONFIG, {('site', 'salle'): 'Bloc 4', ('moniteur', 'bed_label'): ''})
    assert 'salle = "Bloc 4"' in texte and 'bed_label = ""' in texte
    assert '# salle vide = celle qu\'annonce le moniteur' in texte and '# en-tête' in texte
    assert 'nom = "HEGP"' in texte and 'verifier_bed_label = true' in texte


def test_modifier_toml_ajoute_une_cle_absente():
    texte = R.modifier_toml('[site]\nnom = "HEGP"\n[moniteur]\nmac = ""\n',
                            {('site', 'salle'): 'S1', ('moniteur', 'bed_label'): ''})
    assert texte == '[site]\nsalle = "S1"\nnom = "HEGP"\n[moniteur]\nbed_label = ""\nmac = ""\n'
    with pytest.raises(R.ErreurReglage):
        R.modifier_toml('[site]\nnom = "x"\n', {('moniteur', 'bed_label'): ''})


def test_salle_impose_un_nom_sans_verrouiller_le_moniteur(pi, capsys):
    f, redemarrages = pi
    assert lancer(f, 'salle', 'Bloc 4') == 0
    cfg = K.charger(f)
    # le nom étiquette les données ; l'étiquette du moniteur n'est plus exigée
    assert cfg.site.salle == 'Bloc 4' and cfg.moniteur.bed_label == ''
    assert redemarrages == [1]
    assert len(list(f.parent.glob('config.toml.bak.*'))) == 1


def test_salle_auto_leve_l_epinglage(pi):
    f, _ = pi
    assert lancer(f, 'salle', 'auto') == 0
    cfg = K.charger(f)
    assert cfg.site.salle == '' and cfg.moniteur.bed_label == ''


def test_salle_sans_argument_ne_change_rien(pi, capsys):
    f, redemarrages = pi
    avant = f.read_text()
    assert lancer(f, 'salle') == 0
    sortie = capsys.readouterr().out
    assert 'S3B3' in sortie and 'épinglé' in sortie
    assert f.read_text() == avant and redemarrages == []


def test_salle_nom_invalide_rien_n_est_ecrit(pi):
    f, redemarrages = pi
    avant = f.read_text()
    assert lancer(f, 'salle', 'a"b') == 1
    assert lancer(f, 'salle', 'x' * 40) == 1
    assert f.read_text() == avant and redemarrages == []


def test_salle_refusee_pendant_une_intervention(pi, capsys):
    f, redemarrages = pi
    cfg = K.charger(f)
    b = B.Base(cfg.base)
    b.ouvrir_intervention('HEGP-S3B3-2026-0001', site='HEGP', salle='S3B3')
    b.fermer()
    avant = f.read_text()
    assert lancer(f, 'salle', 'Bloc 4') == 1
    assert 'en cours' in capsys.readouterr().out
    assert f.read_text() == avant and redemarrages == []
    assert lancer(f, 'salle', 'Bloc 4', '--forcer') == 0
    assert K.charger(f).site.salle == 'Bloc 4'


def test_profil_wifi():
    profil = R.profil_wifi('Bloc HEGP', 'motdepasse1')
    assert 'ssid=Bloc HEGP' in profil and 'psk=motdepasse1' in profil
    assert 'autoconnect=true' in profil
    assert 'wifi-security' not in R.profil_wifi('Ouvert', '')
    for ssid, mdp in (('', 'motdepasse1'), ('a;b', 'motdepasse1'), ('ok', 'court'),
                      ('x' * 33, 'motdepasse1'), ('ok', 'a\nb' * 5)):
        with pytest.raises(R.ErreurReglage):
            R.profil_wifi(ssid, mdp)
