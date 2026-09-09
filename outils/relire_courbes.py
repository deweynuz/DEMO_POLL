#!/usr/bin/env python3
"""
Relecture d'un fichier de courbes HDF5.

Rouvre le fichier, applique la calibration telle qu'elle était au moment de la
mesure, trace un segment et affiche les statistiques de complétude :
échantillons reçus contre attendus, trous, masques de qualité.

Le tracé ASCII fonctionne en SSH sans rien installer. Avec --png, un tracé
matplotlib est produit si la bibliothèque est disponible.

    python3 -m outils.relire_courbes /var/lib/mx800/courbes/XXX.h5
    python3 -m outils.relire_courbes XXX.h5 --canal NOM_ECG_ELEC_POTL_II --debut 10 --duree 4
    python3 -m outils.relire_courbes XXX.h5 --png /tmp/ecg.png
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

import h5py
import numpy as np

from mx800.protocole import constantes as C

BITS_QUALITE = [
    (C.QUAL_INVALIDE,   'invalide'),
    (C.QUAL_PACEMAKER,  'pacemaker'),
    (C.QUAL_DEFIB,      'défib'),
    (C.QUAL_SATURATION, 'saturation'),
    (C.QUAL_QRS,        'QRS'),
    (C.QUAL_MANQUANT,   'MANQUANT'),
]


def calibration_a(cal: np.ndarray, reltime: int) -> np.ndarray | None:
    """
    Dernière calibration valide à l'instant `reltime`. La calibration appartient
    au contexte dynamique (PIPG p. 86) : appliquer la mauvaise fausse l'échelle.
    """
    if len(cal) == 0:
        return None
    anterieures = cal[cal['reltime'] <= reltime]
    return (anterieures[-1] if len(anterieures) else cal[0])


def appliquer(bruts: np.ndarray, calib) -> tuple[np.ndarray, str]:
    """Interpolation linéaire brut -> valeur absolue (PIPG p. 86)."""
    if calib is None:
        return bruts.astype('f8'), 'brut (aucune calibration enregistrée)'
    bas_a, haut_a = float(calib['bas_absolu']), float(calib['haut_absolu'])
    bas_b, haut_b = int(calib['bas_brut']), int(calib['haut_brut'])
    if np.isnan(bas_a) or np.isnan(haut_a) or haut_b == bas_b:
        # p. 86 : NaN => l'onde ne représente aucune valeur absolue
        return bruts.astype('f8'), 'brut (sans valeur absolue)'
    pente = (haut_a - bas_a) / (haut_b - bas_b)
    return bas_a + (bruts.astype('f8') - bas_b) * pente, 'calibré'


def sparkline(valeurs: np.ndarray, largeur: int = 100, hauteur: int = 12) -> str:
    """Tracé ASCII : lisible en SSH, sans dépendance."""
    if len(valeurs) == 0:
        return '(vide)'
    finies = valeurs[np.isfinite(valeurs)]
    if len(finies) == 0:
        return '(aucune valeur finie)'
    # On retient l'extremum de chaque colonne, pas la moyenne : sur un ECG à
    # 500 Hz réduit à 100 colonnes, une moyenne écraserait les QRS.
    pas = max(1, len(valeurs) // largeur)
    reduit = []
    for i in range(0, len(valeurs), pas):
        tranche = valeurs[i:i + pas]
        finies_ = tranche[np.isfinite(tranche)]
        if len(finies_) == 0:
            reduit.append(np.nan)
        else:
            reduit.append(finies_[np.argmax(np.abs(finies_ - np.median(finies_)))])
    reduit = np.array(reduit[:largeur])
    bas, haut = float(np.nanmin(reduit)), float(np.nanmax(reduit))
    if haut == bas:
        haut = bas + 1.0
    grille = [[' '] * len(reduit) for _ in range(hauteur)]
    for x, v in enumerate(reduit):
        if not np.isfinite(v):
            for y in range(hauteur):
                grille[y][x] = ':'
            continue
        y = int((v - bas) / (haut - bas) * (hauteur - 1))
        grille[hauteur - 1 - y][x] = '─'
    lignes = []
    for i, ligne in enumerate(grille):
        etiquette = (f"{haut:9.3g} │" if i == 0 else
                     f"{bas:9.3g} │" if i == hauteur - 1 else "          │")
        lignes.append(etiquette + ''.join(ligne))
    return '\n'.join(lignes)


def analyser(chemin: Path, canal: str | None, debut_s: float, duree_s: float,
             png: Path | None) -> int:
    with h5py.File(chemin, 'r') as f:
        a = f.attrs
        print(f"Fichier      {chemin}  ({chemin.stat().st_size / 1e6:.2f} Mo)")
        print(f"Intervention {a.get('intervention', '?')}   "
              f"site {a.get('site', '?')} / salle {a.get('salle', '?')}")
        print(f"Début        {a.get('debut_utc', '?')}   fin {a.get('fin_utc', '?')}")

        # Calage temporel : le poll result ne fournit pas d'abs_time_stamp
        # (PIPG p. 62). La conversion se refait ici, à partir du couple
        # (RelativeTime, Date-and-Time) relevé à l'association.
        origine_ticks = int(a.get('calage_moniteur_reltime', -1))
        origine_datetime = str(a.get('calage_moniteur_datetime_utc', ''))
        ecart = float(a.get('calage_ecart_horloge_s', float('nan')))
        print(f"Calage       reltime={origine_ticks} <-> {origine_datetime or '(absent)'}")
        if not np.isnan(ecart):
            print(f"             écart horloge moniteur - Pi : {ecart:+.1f} s")
            if abs(ecart) > 5:
                print("             ATTENTION : les deux horloges divergent. Les "
                      "instants ci-dessous sont ceux du MONITEUR.")
        if origine_ticks < 0 or not origine_datetime:
            print("             calage absent : seuls des temps RELATIFS sont exploitables")
        print()

        noms = sorted(f['signaux'])
        print(f"{'Canal':<26}{'Hz':>7}{'reçus':>10}{'manquants':>11}"
              f"{'complétude':>12}{'durée':>10}  qualité")
        print('─' * 104)
        total_manquants = 0
        for nom in noms:
            jeu = f[f'signaux/{nom}']
            qualite = np.array(f[f'qualite/{nom}'])
            freq = float(jeu.attrs['frequence_hz'])
            manquants = int((qualite & C.QUAL_MANQUANT).astype(bool).sum())
            recus = len(qualite) - manquants
            total = len(qualite)
            total_manquants += manquants
            completude = 100.0 * recus / total if total else 0.0
            duree = total / freq if freq else 0.0
            marques = [f"{libelle}={int((qualite & bit).astype(bool).sum())}"
                       for bit, libelle in BITS_QUALITE[:-1]
                       if (qualite & bit).any()]
            print(f"{nom:<26}{freq:>7.1f}{recus:>10}{manquants:>11}"
                  f"{completude:>11.2f}%{duree:>9.1f}s  {' '.join(marques) or '—'}")
        print('─' * 104)
        if total_manquants:
            print(f"{total_manquants} échantillon(s) manquant(s) au total — "
                  f"comblés et marqués 0x{C.QUAL_MANQUANT:02X} dans le canal de qualité.")
        else:
            print("Aucun échantillon manquant.")

        # calibrations
        print()
        for nom in noms:
            cal = np.array(f[f'calibrations/{nom}'])
            if len(cal) <= 1:
                continue
            print(f"{nom} : {len(cal)} calibrations successives "
                  f"— l'échelle a changé en cours d'acquisition")
            for ligne in cal:
                print(f"    reltime {ligne['reltime']:>12}  "
                      f"{ligne['bas_absolu']:g} .. {ligne['haut_absolu']:g} "
                      f"pour brut {ligne['bas_brut']}..{ligne['haut_brut']}")

        # segment tracé
        cible = canal or (noms[0] if noms else None)
        if cible is None:
            print("\nAucun canal dans ce fichier.")
            return 1
        if cible not in noms:
            print(f"\nCanal {cible!r} absent. Disponibles : {', '.join(noms)}")
            return 2

        jeu = f[f'signaux/{cible}']
        qualite = np.array(f[f'qualite/{cible}'])
        cal = np.array(f[f'calibrations/{cible}'])
        freq = float(jeu.attrs['frequence_hz'])
        periode_ticks = int(jeu.attrs['periode_echantillonnage_ticks'])
        i0 = int(debut_s * freq)
        i1 = min(len(jeu), i0 + int(duree_s * freq))
        if i0 >= len(jeu):
            print(f"\nDébut {debut_s} s au-delà de la fin du signal "
                  f"({len(jeu) / freq:.1f} s).")
            return 3

        bruts = np.array(jeu[i0:i1])
        q = qualite[i0:i1]
        temps = np.array(f[f'temps/{cible}'])
        reltime_debut = int(temps[0]) + i0 * periode_ticks if len(temps) else 0
        valeurs, mode = appliquer(bruts, calibration_a(cal, reltime_debut))
        valeurs[(q & C.QUAL_MANQUANT).astype(bool)] = np.nan
        valeurs[(q & C.QUAL_INVALIDE).astype(bool)] = np.nan

        instant = ''
        if origine_ticks >= 0 and origine_datetime:
            try:
                base = datetime.fromisoformat(origine_datetime)
                instant = (base + timedelta(
                    seconds=(reltime_debut - origine_ticks) / C.TICKS_PAR_SECONDE)
                ).isoformat(timespec='milliseconds')
            except ValueError:
                pass

        print(f"\n{cible} — {debut_s:.1f} à {i1 / freq:.1f} s "
              f"({i1 - i0} échantillons, {mode})")
        if instant:
            print(f"début du segment : {instant} (horloge moniteur)")
        print(f"écartés du tracé : {int(np.isnan(valeurs).sum())} échantillon(s) "
              f"manquant(s) ou invalide(s)")
        print(sparkline(valeurs))

        if png:
            try:
                import matplotlib
                matplotlib.use('Agg')
                import matplotlib.pyplot as plt
            except ImportError:
                print(f"\nmatplotlib absent : pas de {png}. "
                      f"Installer avec « sudo apt install python3-matplotlib ».")
            else:
                t = np.arange(len(valeurs)) / freq + debut_s
                plt.figure(figsize=(14, 4))
                plt.plot(t, valeurs, linewidth=0.6)
                plt.title(f"{cible} — {a.get('intervention', '')} "
                          f"({a.get('salle', '')})")
                plt.xlabel("temps depuis le début de l'enregistrement (s)")
                plt.ylabel(mode)
                plt.grid(alpha=0.3)
                plt.tight_layout()
                plt.savefig(png, dpi=110)
                print(f"\nTracé écrit : {png}")
    return 0


def principal(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Relecture d'un fichier de courbes HDF5")
    ap.add_argument('fichier', type=Path)
    ap.add_argument('--canal', help="nom du canal à tracer (défaut : le premier)")
    ap.add_argument('--debut', type=float, default=0.0, help="début du segment, en s")
    ap.add_argument('--duree', type=float, default=4.0, help="durée du segment, en s")
    ap.add_argument('--png', type=Path, help="écrire aussi un tracé PNG")
    args = ap.parse_args(argv)
    if not args.fichier.exists():
        print(f"introuvable : {args.fichier}", file=sys.stderr)
        return 1
    return analyser(args.fichier, args.canal, args.debut, args.duree, args.png)


if __name__ == '__main__':
    raise SystemExit(principal())
