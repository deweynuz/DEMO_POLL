#!/usr/bin/env python3
"""
Migration des données de l'ancien module vers le nouveau schéma.

Principes :

  - la source n'est JAMAIS modifiée, ni même ouverte en écriture ;
  - la correspondance colonne -> physio_id vient de l'ancien config.json, seule
    source fiable : le nom de colonne « Temp » ne dit pas de quel capteur il
    s'agit, son physio_id si ;
  - les horodatages de l'ancienne base sont marqués comme NON FIABLES. Ce Pi a
    démontré un décalage de 37 jours le 03/09/2026 : il a démarré avec
    l'horloge du 28/07 et a tourné six jours ainsi. Rien ne permet de savoir
    après coup quelles lignes sont affectées ;
  - le mode démonstration du moniteur n'était pas enregistré à l'époque : on ne
    peut pas savoir si ces mesures viennent d'un patient. C'est consigné.

    python3 -m outils.migrer --source ~/hegp.db --config /etc/mx800/config.toml
    python3 -m outils.migrer --source ~/hegp.db --config ... --essai
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from mx800 import config as K
from mx800.protocole import constantes as C
from mx800.protocole import parametres as PARAM
from mx800.stockage.base import Base, BaseIdentites

AVERTISSEMENT_HORLOGE = (
    "horodatages NON FIABLES : ce Pi n'a pas de RTC et a démontré un décalage "
    "de 37 jours le 03/09/2026 (démarrage avec l'horloge du 28/07, six jours "
    "de fonctionnement avant la première synchronisation NTP). Impossible de "
    "savoir a posteriori quelles lignes sont affectées."
)
AVERTISSEMENT_DEMO = (
    "mode opératoire du moniteur inconnu : l'ancien module n'enregistrait ni "
    "NOM_ATTR_MODE_OP ni le MeasurementState. Impossible de savoir si ces "
    "mesures proviennent d'un patient ou du mode démonstration."
)


def correspondance(chemin_config_ancien: Path) -> dict[str, int]:
    """{nom_de_colonne: physio_id} depuis l'ancien config.json."""
    brut = json.loads(Path(chemin_config_ancien).read_text(encoding='utf-8'))
    table = {}
    for cle, valeur in brut.get('parameters', {}).items():
        if cle.startswith('_') or not isinstance(valeur, dict):
            continue
        try:
            table[valeur['name']] = int(cle, 16)
        except (KeyError, ValueError):
            continue
    return table


def _iso_utc(valeur: str | None) -> str:
    """
    Les horodatages de l'ancienne base sont naïfs et en heure locale. On les
    convertit en UTC en supposant Europe/Paris — hypothèse explicite, et de
    toute façon secondaire devant le décalage de 37 jours.
    """
    if not valeur:
        return datetime.now(timezone.utc).isoformat(timespec='milliseconds')
    try:
        naif = datetime.fromisoformat(valeur)
    except ValueError:
        return valeur
    return naif.astimezone().astimezone(timezone.utc).isoformat(timespec='milliseconds')


def migrer(source: Path, cfg: K.Configuration, table: dict[str, int],
           *, essai: bool = False, prefixe: str = 'LEGACY') -> dict:
    if not source.exists():
        raise SystemExit(f"source introuvable : {source}")
    ancienne = sqlite3.connect(f'file:{source}?mode=ro', uri=True)   # lecture seule
    ancienne.row_factory = sqlite3.Row

    tables = {r[0] for r in ancienne.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if 'numerics' not in tables:
        raise SystemExit(f"{source} ne contient pas de table `numerics` : "
                         f"est-ce bien une base de l'ancien module ?")

    colonnes = [r[1] for r in ancienne.execute("PRAGMA table_info(numerics)")]
    ignorees = {'id', 'session_id', 'intervention_id', 'patient_db_id', 'timestamp',
                'patient_id', 'family_name', 'given_name'}
    mesurables = [c for c in colonnes if c not in ignorees]
    inconnues = sorted(c for c in mesurables if c not in table)

    stats = {'sessions': 0, 'interventions': 0, 'mesures': 0, 'identites': 0,
             'colonnes_inconnues': inconnues, 'lignes_source': 0}
    stats['lignes_source'] = ancienne.execute(
        "SELECT count(*) FROM numerics").fetchone()[0]

    if essai:
        remplies = []
        for c in mesurables:
            n = ancienne.execute(
                f'SELECT count("{c}") FROM numerics WHERE "{c}" IS NOT NULL').fetchone()[0]
            if n:
                physio = table.get(c)
                cible = PARAM.CATALOGUE.get(physio) if physio else None
                remplies.append((c, n, physio, cible.court if cible else None))
        stats['colonnes_remplies'] = remplies
        ancienne.close()
        return stats

    nouvelle = Base(cfg.base, version_module='migration')
    identites = BaseIdentites(cfg.base_identites)

    # une session par session ancienne, plus une session « orpheline »
    sessions = {}
    anciennes_sessions = (list(ancienne.execute("SELECT * FROM sessions"))
                          if 'sessions' in tables else [])
    for s in anciennes_sessions:
        sessions[s['id']] = nouvelle.ouvrir_session(
            site=cfg.site.nom, salle=cfg.site.salle,
            moniteur_ip=s['monitor_ip'] or 'inconnue',
            debut_utc=_iso_utc(s['start_time']),
            horloge_source='inconnue', horloge_synchronisee=0,
            mode_demonstration=0)
        if s['end_time']:
            nouvelle.fermer_session(sessions[s['id']], 'migration')
        stats['sessions'] += 1
    session_orpheline = nouvelle.ouvrir_session(
        site=cfg.site.nom, salle=cfg.site.salle, moniteur_ip='inconnue',
        horloge_source='inconnue', horloge_synchronisee=0)
    stats['sessions'] += 1

    # interventions : celles de l'ancienne base, plus une pour les lignes orphelines
    interventions = {}
    if 'interventions' in tables:
        for i in ancienne.execute("SELECT * FROM interventions"):
            code = f"{prefixe}-{source.stem}-{i['id']:03d}"
            interventions[i['id']] = nouvelle.ouvrir_intervention(
                code, site=cfg.site.nom, salle=cfg.site.salle, ouverture='migration',
                empreinte_patient=i['patient_id'] or None)
            nouvelle.conn.execute(
                "UPDATE interventions SET debut_utc=?, fin_utc=?, notes=? WHERE id=?",
                (_iso_utc(i['start_time']), _iso_utc(i['end_time']) if i['end_time'] else None,
                 f"{AVERTISSEMENT_HORLOGE}\n{AVERTISSEMENT_DEMO}",
                 interventions[i['id']]))
            if i['family_name'] or i['patient_id']:
                identites.enregistrer(code, {
                    'patient_id': i['patient_id'], 'nom': i['family_name'],
                    'prenom': i['given_name'], 'sexe': i['sex'],
                    'type_patient': i['patient_type'],
                    'admis_utc': _iso_utc(i['start_time'])})
                stats['identites'] += 1
            stats['interventions'] += 1

    code_orphelin = f"{prefixe}-{source.stem}-ORPHELINES"
    intervention_orpheline = nouvelle.ouvrir_intervention(
        code_orphelin, site=cfg.site.nom, salle=cfg.site.salle, ouverture='migration')
    nouvelle.conn.execute(
        "UPDATE interventions SET notes=? WHERE id=?",
        (f"lignes sans intervention dans la base d'origine.\n"
         f"{AVERTISSEMENT_HORLOGE}\n{AVERTISSEMENT_DEMO}", intervention_orpheline))
    stats['interventions'] += 1

    for ligne in ancienne.execute("SELECT * FROM numerics ORDER BY id"):
        cles = ligne.keys()
        id_session = sessions.get(ligne['session_id'] if 'session_id' in cles else None,
                                  session_orpheline)
        id_interv = interventions.get(
            ligne['intervention_id'] if 'intervention_id' in cles else None,
            intervention_orpheline)
        horodatage = _iso_utc(ligne['timestamp'])
        for colonne in mesurables:
            valeur = ligne[colonne]
            if valeur is None:
                continue
            physio = table.get(colonne)
            connu = PARAM.CATALOGUE.get(physio) if physio else None
            nouvelle.empiler_mesure(
                session_id=id_session, intervention_id=id_interv, ts_utc=horodatage,
                physio_id=physio if physio else 0,
                parametre=connu.court if connu else colonne,
                valeur=float(valeur), unite=connu.unite if connu else None,
                # L'état n'était pas enregistré. On ne peut pas prétendre que la
                # mesure était valide : on la marque comme indéterminée.
                etat=C.MS_QUESTIONABLE, valide=0)
            stats['mesures'] += 1
        if stats['mesures'] % 5000 == 0:
            nouvelle.vider_lot()
    nouvelle.vider_lot()

    for identifiant in {intervention_orpheline, *interventions.values()}:
        nouvelle.enregistrer_lacune(
            type='donnees_migrees', intervention_id=identifiant,
            detail=f"importées de {source} — {AVERTISSEMENT_HORLOGE}")
    nouvelle.conn.commit()
    nouvelle.fermer()
    identites.fermer()
    ancienne.close()
    return stats


def principal(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Migration de l'ancienne base MX800")
    ap.add_argument('--source', type=Path, required=True, help="ancienne hegp.db")
    ap.add_argument('--config', type=Path, default=K.CHEMIN_DEFAUT)
    ap.add_argument('--config-ancien', type=Path,
                    help="ancien config.json (correspondance colonne -> physio_id)")
    ap.add_argument('--prefixe', default='LEGACY')
    ap.add_argument('--essai', action='store_true',
                    help="analyser sans rien écrire")
    args = ap.parse_args(argv)

    cfg = K.charger(args.config)
    ancien = args.config_ancien or (args.source.parent / 'config.json')
    if not Path(ancien).exists():
        print(f"ancien config.json introuvable : {ancien}", file=sys.stderr)
        print("  il porte la correspondance colonne -> physio_id, indispensable :",
              file=sys.stderr)
        print("  sans lui, « Temp » ne dit pas de quel capteur il s'agit.", file=sys.stderr)
        return 2
    table = correspondance(ancien)

    stats = migrer(args.source, cfg, table, essai=args.essai, prefixe=args.prefixe)

    print(f"Source        {args.source}  ({stats['lignes_source']} lignes)")
    print(f"Correspondance {len(table)} colonnes déclarées dans {ancien}")
    if args.essai:
        print("\nColonnes renseignées :")
        for colonne, n, physio, court in stats['colonnes_remplies']:
            cible = court or (f"0x{physio:04X} absent du catalogue" if physio
                              else "NON MAPPÉE — sera conservée sous son nom d'origine")
            print(f"  {colonne:<14}{n:>6} valeurs   -> {cible}")
        print("\nEssai : rien n'a été écrit. Relancer sans --essai pour migrer.")
        return 0

    print(f"\n  sessions       {stats['sessions']}")
    print(f"  interventions  {stats['interventions']}")
    print(f"  mesures        {stats['mesures']}")
    print(f"  identités      {stats['identites']} (dans {cfg.base_identites})")
    if stats['colonnes_inconnues']:
        print(f"  colonnes sans physio_id, conservées sous leur nom d'origine : "
              f"{', '.join(stats['colonnes_inconnues'][:8])}")
    print(f"\n  Les mesures migrées portent l'état QUESTIONABLE et valide = 0 :")
    print(f"  l'ancien module n'enregistrait pas le MeasurementState, on ne peut")
    print(f"  pas affirmer après coup qu'elles étaient valides.")
    print(f"\n  {AVERTISSEMENT_HORLOGE}")
    print(f"  La source n'a pas été modifiée.")
    return 0


if __name__ == '__main__':
    raise SystemExit(principal())
