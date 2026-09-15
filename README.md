# Acquisition MX800 — HEGP

Acquisition continue de données physiologiques depuis un moniteur Philips
IntelliVue MX800, pour la recherche clinique en anesthésie-réanimation
(phénotypage de trajectoires hémodynamiques peropératoires).

- **Exploitation** : [`docs/exploitation.md`](docs/exploitation.md)
- **Protocole** : [`docs/note_protocole_pipg.md`](docs/note_protocole_pipg.md)

---

## Installation

**Raspberry Pi neuf** (Raspberry Pi OS 64 bits, SSH et Wi-Fi configurés — eth0
est réservé au moniteur) :

```bash
sudo apt update && sudo apt install -y git python3-pip sqlite3
git clone https://github.com/deweynuz/DEMO_POLL.git ~/mx800 && cd ~/mx800
./install.sh --configurer-reseau
```

`--configurer-reseau` (confirmation `OUI` exigée) met `eth0` en
`192.168.100.1/24` et installe `dnsmasq` en `bootp-dynamic` pour servir une
adresse au moniteur.

**Pi déjà en service** : `./install.sh` sans option. Le réseau est vérifié,
jamais modifié — `eth0` et `dnsmasq` servent le BOOTP aux moniteurs cliniques.

Le script ne demande que le site. La salle et le moniteur sont appris seuls
(`--salle` et `--mac` les figent si besoin). Il propose le stockage (carte SD
ou disque USB), génère `/etc/mx800/config.toml`, installe l'unité systemd et
vérifie que le service démarre. Il est idempotent.

Vérifier ensuite : `mx800 diagnostiquer` puis `mx800 etat`.

**Mise à jour** : `cd ~/mx800 && git pull && sudo systemctl restart mx800.service`

Détails (heure RTC, courbes, dépannage) : [`docs/exploitation.md`](docs/exploitation.md), section 9.

## Usage courant

```bash
mx800 etat              # est-ce que ça enregistre, là, maintenant ?
mx800 interventions     # ce qui a été enregistré
mx800 exporter CODE     # CSV d'une intervention
mx800 diagnostiquer     # pourquoi ça ne marche pas
```

Page d'état : `http://<pi>:8080/` — bandeau vert ou rouge, boutons de
démarrage et d'arrêt. Healthcheck : `curl -sf http://localhost:8080/sante`.

---

## Organisation

```
mx800/
  protocole/     codec Data Export : constantes, encodage, décodage,
                 catalogues des 691 paramètres et des 55 ondes.
                 Aucune E/S — testable sans moniteur.
  machine.py     machine à états. Aucune E/S.
  acquisition.py boucle : association, polling, courbes, watchdog
  stockage/      SQLite (format long), HDF5 (courbes), vérification du volume
  controle.py    page d'état locale et file de commandes
  service.py     point d'entrée systemd
outils/
  simulateur.py     moniteur MX800 simulé, avec injection de pannes
  console.py        commande `mx800`
  relire_courbes.py relecture HDF5 : calibration, complétude, tracé
  migrer.py         import de l'ancienne base
deploiement/     unité systemd
tests/           127 tests, dont les trames réelles capturées sur un MX800
```

## Principes de conception

**Un service « actif » qui n'enregistre rien doit être impossible.** Le
watchdog systemd n'est alimenté que par le chemin de données : si plus rien ne
s'écrit, systemd redémarre. La machine à états n'a aucun état stable
improductif, et la réassociation ne dépend d'aucune condition.

**Rien n'est supposé sur le protocole.** Chaque structure binaire est tracée à
une page du guide Philips G.0 (voir la note de traçabilité). Les tests
comparent les octets produits à des trames réellement capturées sur un
moniteur : sans cela, un défaut d'encodage resterait invisible, le client et
le simulateur partageant le même codec.

**Les trous sont enregistrés, pas déduits d'une absence.** Coupures,
échantillons manquants, réassociations forcées, redémarrages : tout laisse une
ligne dans `lacunes`.

**Rien n'est affirmé qui ne soit vérifié.** Les mesures portent leur
`MeasurementState`. Les deux horloges — Pi et moniteur — sont conservées avec
leur écart, aucune n'est élue en silence. Les courbes sont stockées brutes,
avec leur calibration horodatée à côté.

## Tests

```bash
python3 -m pytest
python3 -m outils.simulateur --bed-label SIM1 --panne perte-ondes:7 --panne masques
```

Le simulateur reproduit le comportement observé d'un vrai MX800, y compris ses
écarts au guide, et injecte dix modes de panne.

## Données

Hors dépôt, dans le volume configuré (`/var/lib/mx800` par défaut) :
`mx800.db` (aucune identité), `identites.db` (0600), `courbes/`, `exports/`.
