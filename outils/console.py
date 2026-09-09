#!/usr/bin/env python3
"""
Commande d'exploitation `mx800`.

Conçue pour être utilisable par quelqu'un qui n'a pas écrit le code, y compris
sous stress. Chaque sortie répond à une question précise, et les messages
d'erreur disent quoi faire.

    mx800 etat                     est-ce que ça enregistre, là, maintenant ?
    mx800 demarrer [CODE]          ouvrir une intervention
    mx800 arreter                  clore l'intervention en cours
    mx800 interventions            ce qui a été enregistré
    mx800 exporter CODE            sortir une intervention en CSV
    mx800 moniteurs                quels moniteurs sont visibles
    mx800 diagnostiquer            pourquoi ça ne marche pas
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from mx800 import config as K
from mx800 import adressage
from mx800.protocole import constantes as C
from mx800.stockage import volume as V

VERT, ROUGE, ORANGE, GRIS, NORMAL = '\033[32m', '\033[31m', '\033[33m', '\033[90m', '\033[0m'


def _couleur(actif: bool = True):
    if not actif or not sys.stdout.isatty():
        globals().update(VERT='', ROUGE='', ORANGE='', GRIS='', NORMAL='')


def _api(chemin: str, port: int, methode: str = 'GET', corps: dict | None = None,
         delai: float = 5.0) -> dict | None:
    url = f'http://127.0.0.1:{port}{chemin}'
    donnees = json.dumps(corps or {}).encode() if methode == 'POST' else None
    requete = urllib.request.Request(url, data=donnees, method=methode,
                                     headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(requete, timeout=delai) as reponse:
            return json.loads(reponse.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None


def _etat(cfg: K.Configuration, port: int) -> tuple[dict | None, str]:
    """Renvoie (état, origine). Le service en direct, sinon status.json."""
    vivant = _api('/api/etat', port)
    if vivant is not None:
        return vivant, 'service'
    if cfg.chemin_etat.exists():
        try:
            return json.loads(cfg.chemin_etat.read_text(encoding='utf-8')), 'fichier'
        except (OSError, json.JSONDecodeError):
            pass
    return None, 'absent'


# ─────────────────────────────────────────────────────────────────────────────

def cmd_etat(args, cfg) -> int:
    etat, origine = _etat(cfg, args.port)
    if args.json:
        print(json.dumps(etat, ensure_ascii=False, indent=2) if etat else '{}')
        return 0 if etat else 4
    if etat is None:
        print(f"{ROUGE}SERVICE INJOIGNABLE{NORMAL} — ni page d'état sur le port "
              f"{args.port}, ni {cfg.chemin_etat}")
        print("  sudo systemctl status mx800.service")
        return 4

    productif = etat.get('productif') and etat.get('intervention')
    if etat.get('alerte'):
        print(f"{ORANGE}ATTENTION{NORMAL}  {etat['alerte']}")
    entete = (f"{VERT}ENREGISTREMENT EN COURS{NORMAL}" if productif
              else f"{ROUGE}PAS D'ENREGISTREMENT{NORMAL}")
    print(f"{entete}   {etat.get('site','?')} · salle {etat.get('salle','?')} "
          f"· état {etat.get('etat','?')}")
    if origine == 'fichier':
        print(f"{GRIS}  (service injoignable — état lu dans {cfg.chemin_etat}, "
              f"peut être périmé){NORMAL}")

    compteurs = etat.get('compteurs', {})
    lignes = [
        ("intervention",        etat.get('intervention') or '—'),
        ("débit",               f"{etat.get('debit_lignes_par_min', 0):g} lignes/min"),
        ("dernière écriture",   f"il y a {etat.get('silence_donnees_s')} s"
                                if etat.get('silence_donnees_s') is not None else '—'),
        ("moniteur",            f"{etat.get('moniteur_bed_label') or '?'} "
                                f"({etat.get('moniteur_ip') or '?'})"),
        ("courbes",             "négociées" if etat.get('courbes_negociees') else "non"),
        ("horloge",             f"{etat.get('horloge_source')} "
                                f"({'synchronisée' if etat.get('horloge_synchronisee') else 'NON SYNCHRONISÉE'})"),
        ("écart moniteur",      f"{etat.get('ecart_horloge_moniteur_s')} s"
                                if etat.get('ecart_horloge_moniteur_s') is not None else '—'),
        ("disque libre",        f"{etat.get('disque_libre_mo')} Mo"),
        ("lignes écrites",      compteurs.get('lignes_ecrites', 0)),
        ("coupures",            compteurs.get('aborts', 0) + compteurs.get('timeouts', 0)),
        ("lacunes",             compteurs.get('lacunes', 0)),
        ("réassoc. forcées",    compteurs.get('reassociations_forcees', 0)),
    ]
    for etiquette, valeur in lignes:
        print(f"  {etiquette:<20} {valeur}")
    return 0 if productif else 1


def cmd_demarrer(args, cfg) -> int:
    reponse = _api('/api/demarrer', args.port, 'POST',
                   {'code': args.code} if args.code else {})
    if reponse is None:
        print(f"{ROUGE}Service injoignable{NORMAL} sur le port {args.port}.")
        return 4
    print(('' if reponse['ok'] else ROUGE) + reponse['message'] + NORMAL)
    return 0 if reponse['ok'] else 1


def cmd_arreter(args, cfg) -> int:
    reponse = _api('/api/arreter', args.port, 'POST')
    if reponse is None:
        print(f"{ROUGE}Service injoignable{NORMAL} sur le port {args.port}.")
        return 4
    print(('' if reponse['ok'] else ROUGE) + reponse['message'] + NORMAL)
    return 0 if reponse['ok'] else 1


def cmd_interventions(args, cfg) -> int:
    if not cfg.base.exists():
        print(f"aucune base : {cfg.base}")
        return 4
    conn = sqlite3.connect(f'file:{cfg.base}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    lignes = conn.execute("""
        SELECT i.code_recherche, i.debut_utc, i.fin_utc, i.demonstration,
               (SELECT count(*) FROM mesures m WHERE m.intervention_id = i.id) AS mesures,
               (SELECT count(*) FROM lacunes l WHERE l.intervention_id = i.id) AS lacunes,
               (SELECT count(*) FROM fichiers_courbes f WHERE f.intervention_id = i.id) AS courbes
        FROM interventions i ORDER BY i.id DESC LIMIT ?""", (args.limite,)).fetchall()
    if not lignes:
        print("aucune intervention enregistrée")
        return 0
    print(f"{'Code':<26}{'Début (UTC)':<26}{'État':<12}{'mesures':>9}"
          f"{'lacunes':>9}{'courbes':>9}")
    for l in lignes:
        etat = 'EN COURS' if l['fin_utc'] is None else 'terminée'
        if l['demonstration']:
            etat += ' DÉMO'
        print(f"{l['code_recherche']:<26}{l['debut_utc'][:19]:<26}{etat:<12}"
              f"{l['mesures']:>9}{l['lacunes']:>9}{l['courbes']:>9}")
    conn.close()
    return 0


def cmd_exporter(args, cfg) -> int:
    """
    Export d'une intervention en CSV. Format LARGE : une colonne par
    paramètre, ce qui est ce qu'on manipule en analyse — le format long de la
    base est un choix de stockage, pas d'exploitation.
    """
    if not cfg.base.exists():
        print(f"aucune base : {cfg.base}")
        return 4
    conn = sqlite3.connect(f'file:{cfg.base}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    interv = conn.execute("SELECT * FROM interventions WHERE code_recherche=?",
                          (args.code,)).fetchone()
    if interv is None:
        print(f"intervention inconnue : {args.code}")
        print("  mx800 interventions   pour la liste")
        return 1

    condition = "" if args.invalides else " AND valide = 1"
    lignes = conn.execute(
        f"SELECT ts_utc, physio_id, parametre, valeur, etat FROM mesures "
        f"WHERE intervention_id = ?{condition} ORDER BY ts_utc", (interv['id'],)).fetchall()
    if not lignes:
        print(f"aucune mesure pour {args.code}"
              + ("" if args.invalides else " (essayer --invalides)"))
        return 1

    colonnes, par_instant = [], {}
    for l in lignes:
        nom = l['parametre'] or f"PHYSIO_{l['physio_id']:04X}"
        if nom not in colonnes:
            colonnes.append(nom)
        par_instant.setdefault(l['ts_utc'], {})[nom] = l['valeur']
    colonnes.sort()

    destination = args.sortie or (cfg.exports_dir / f"{args.code}.csv")
    Path(destination).parent.mkdir(parents=True, exist_ok=True)
    with open(destination, 'w', newline='', encoding='utf-8') as f:
        ecrivain = csv.writer(f)
        ecrivain.writerow(['ts_utc'] + colonnes)
        for instant in sorted(par_instant):
            valeurs = par_instant[instant]
            ecrivain.writerow([instant] + [valeurs.get(c, '') for c in colonnes])

    print(f"{len(par_instant)} instants × {len(colonnes)} paramètres → {destination}")
    if not args.invalides:
        print(f"{GRIS}  mesures invalides exclues (état non nul, PIPG p. 77) — "
              f"--invalides pour les inclure{NORMAL}")
    if not args.nominatif:
        print(f"{GRIS}  export pseudonymisé : seul {args.code} identifie le patient. "
              f"--nominatif pour joindre l'identité.{NORMAL}")
    else:
        identites = sqlite3.connect(f'file:{cfg.base_identites}?mode=ro', uri=True)
        identites.row_factory = sqlite3.Row
        ligne = identites.execute("SELECT * FROM identites WHERE code_recherche=?",
                                  (args.code,)).fetchone()
        if ligne:
            print(f"{ORANGE}  IDENTITÉ : {ligne['nom']} {ligne['prenom']} "
                  f"(ID {ligne['patient_id']}){NORMAL}")
            print(f"{ORANGE}  Ce fichier contient des données de santé nominatives.{NORMAL}")
        identites.close()

    courbes = conn.execute("SELECT chemin, octets FROM fichiers_courbes "
                           "WHERE intervention_id=?", (interv['id'],)).fetchall()
    for c in courbes:
        print(f"  courbes : {c['chemin']} ({(c['octets'] or 0) / 1e6:.1f} Mo)")
        print(f"            python3 -m outils.relire_courbes {c['chemin']}")
    conn.close()
    return 0


def cmd_moniteurs(args, cfg) -> int:
    visibles = adressage.inventaire()
    if not visibles:
        print("aucun moniteur Philips visible sur eth0")
        print("  cat /var/lib/misc/dnsmasq.leases     baux attribués")
        print("  systemctl status dnsmasq            serveur DHCP/BOOTP")
        return 1
    attendue = (cfg.moniteur.mac or '').lower()
    print(f"{'MAC':<20}{'Adresse':<17}{'Origine':<16}")
    for mac, ip, origine in visibles:
        marque = f"  {VERT}<- configuré{NORMAL}" if mac == attendue else ''
        print(f"{mac:<20}{ip:<17}{origine:<16}{marque}")
    if attendue and attendue not in {m for m, _, _ in visibles}:
        print(f"\n{ROUGE}Le moniteur configuré ({attendue}) n'est pas visible.{NORMAL}")
        return 1
    return 0


def cmd_diagnostiquer(args, cfg) -> int:
    """Passe en revue tout ce qui peut empêcher l'acquisition de fonctionner."""
    problemes = []

    def verifier(libelle: str, ok: bool, detail: str = '', bloquant: bool = True,
                 explication: str = ''):
        """
        `detail` décrit la constatation, `explication` dit pourquoi c'est un
        problème — et n'apparaît donc que si c'en est un.
        """
        marque = f"{VERT}ok{NORMAL}" if ok else (f"{ROUGE}NON{NORMAL}" if bloquant
                                                 else f"{ORANGE}! {NORMAL}")
        texte = detail if ok or not explication else f"{detail} — {explication}".strip(' —')
        print(f"  [{marque}] {libelle}" + (f"  {GRIS}{texte}{NORMAL}" if texte else ''))
        if not ok and bloquant:
            problemes.append(libelle)

    print("Configuration")
    verifier("fichier lisible et valide", True, str(args.config))
    verifier("site et salle renseignés", bool(cfg.site.nom and cfg.site.salle),
             f"{cfg.site.nom} / {cfg.site.salle}")

    print("\nStockage")
    try:
        info = V.verifier(cfg.stockage.chemin, site=cfg.site.nom, salle=cfg.site.salle,
                          uuid_attendu=cfg.stockage.uuid)
        verifier("volume vérifié", True,
                 f"{info.peripherique or '?'} — {info.libre_mo} Mo libres")
        verifier("espace suffisant", info.libre_mo >= cfg.surveillance.seuil_disque_mo,
                 f"seuil {cfg.surveillance.seuil_disque_mo} Mo")
    except V.VolumeInvalide as e:
        verifier("volume vérifié", False, str(e))

    print("\nHorloge")
    from mx800.acquisition import _source_horloge
    source, synchro = _source_horloge()
    verifier("source de temps", source != 'aucune', source, bloquant=False,
             explication="ni RTC matériel ni NTP joignable")
    verifier("horloge synchronisée", synchro, source, bloquant=False,
             explication="sans synchronisation, les horodatages ne sont pas "
                         "défendables en recherche")
    if source == 'ntp':
        print(f"       {GRIS}NTP dépend d'une liaison vers l'extérieur. En salle, "
              f"sans elle et sans RTC, l'horloge dérivera.{NORMAL}")

    print("\nRéseau")
    visibles = adressage.inventaire()
    verifier("moniteurs Philips visibles", bool(visibles),
             f"{len(visibles)} sur eth0")
    try:
        adresse = adressage.resoudre(mac=cfg.moniteur.mac, ip=cfg.moniteur.ip)
        verifier("moniteur configuré résolu", True, adresse)
        joignable = subprocess.run(['ping', '-c1', '-W1', adresse],
                                   capture_output=True, timeout=5,
                                   check=False).returncode == 0
        verifier("moniteur joignable", joignable, adresse)
    except adressage.AdresseIntrouvable as e:
        verifier("moniteur configuré résolu", False, str(e))

    print("\nService")
    etat, origine = _etat(cfg, args.port)
    verifier("service joignable", origine == 'service', f"port {args.port}",
             bloquant=False, explication="service arrêté, ou page d'état désactivée")
    if etat:
        verifier("association établie",
                 etat.get('etat') in ('ASSOCIE', 'ACQUISITION'), etat.get('etat', '?'))
        verifier("enregistrement en cours", bool(etat.get('productif')),
                 etat.get('intervention') or 'aucune intervention', bloquant=False)
        verifier("aucune alerte", not etat.get('alerte'), etat.get('alerte') or '')

    print()
    if problemes:
        print(f"{ROUGE}{len(problemes)} problème(s) bloquant(s){NORMAL} : "
              + ', '.join(problemes))
        return 1
    print(f"{VERT}Aucun problème bloquant.{NORMAL}")
    return 0


# ─────────────────────────────────────────────────────────────────────────────

def principal(argv=None) -> int:
    ap = argparse.ArgumentParser(prog='mx800', description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--config', type=Path, default=K.CHEMIN_DEFAUT)
    ap.add_argument('--port', type=int, default=8080, help="port de la page d'état")
    ap.add_argument('--sans-couleur', action='store_true')
    sous = ap.add_subparsers(dest='commande', required=True)

    p = sous.add_parser('etat', help="est-ce que ça enregistre, là, maintenant ?")
    p.add_argument('--json', action='store_true')
    p.set_defaults(fonction=cmd_etat)

    p = sous.add_parser('demarrer', help="ouvrir une intervention")
    p.add_argument('code', nargs='?', help="code de recherche (sinon attribué)")
    p.set_defaults(fonction=cmd_demarrer)

    p = sous.add_parser('arreter', help="clore l'intervention en cours")
    p.set_defaults(fonction=cmd_arreter)

    p = sous.add_parser('interventions', help="ce qui a été enregistré")
    p.add_argument('--limite', type=int, default=20)
    p.set_defaults(fonction=cmd_interventions)

    p = sous.add_parser('exporter', help="sortir une intervention en CSV")
    p.add_argument('code')
    p.add_argument('--sortie', type=Path)
    p.add_argument('--invalides', action='store_true',
                   help="inclure les mesures dont l'état n'est pas nul")
    p.add_argument('--nominatif', action='store_true',
                   help="afficher l'identité du patient (données de santé)")
    p.set_defaults(fonction=cmd_exporter)

    p = sous.add_parser('moniteurs', help="quels moniteurs sont visibles")
    p.set_defaults(fonction=cmd_moniteurs)

    p = sous.add_parser('diagnostiquer', help="pourquoi ça ne marche pas")
    p.set_defaults(fonction=cmd_diagnostiquer)

    args = ap.parse_args(argv)
    _couleur(not args.sans_couleur)
    try:
        cfg = K.charger(args.config)
    except K.ErreurConfiguration as e:
        print(f"{ROUGE}{e}{NORMAL}", file=sys.stderr)
        return 2
    return args.fonction(args, cfg)


if __name__ == '__main__':
    raise SystemExit(principal())
