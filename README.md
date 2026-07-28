# Acquisition de données physiologiques — Philips IntelliVue MX800

Capture en continu des paramètres hémodynamiques d'un moniteur Philips
IntelliVue via le protocole propriétaire **Data Export** (UDP, port 24105),
avec stockage SQLite et export CSV par intervention.

Conçu pour tourner sans surveillance sur un Raspberry Pi relié en direct au
moniteur, dans un contexte de recherche clinique en chirurgie cardiaque.

---

## ⚠ Données patients

Ce dépôt ne contient **que du code**. La base SQLite, les CSV et le fichier
d'état patient restent en dehors du dépôt et sont exclus par `.gitignore`.

Ne jamais commiter `*.db`, `data/`, `*.csv` ni `patient_demo.json` : ils
contiennent des noms, des identifiants hospitaliers et des données de santé.

---

## Installation

Sur un Raspberry Pi neuf sous Raspberry Pi OS Bookworm 64-bit :

```bash
curl -sSL https://raw.githubusercontent.com/deweynuz/DEMO_POLL/main/install.sh | bash
```

Le script pose deux questions (détection du moniteur, dossier d'installation)
puis configure tout : réseau, dnsmasq, dépendances, service systemd.

**À lancer depuis le WiFi ou Pi Connect, pas depuis une session SSH passant par
`eth0`** — l'interface est reconfigurée en cours de route.

---

## Architecture

```
Moniteur Philips ──[Ethernet direct]── Raspberry Pi
    │                                      │
    │  BOOTP  ──────────────────────────►  dnsmasq  (attribue l'adresse)
    │                                      │
    └─ Data Export UDP:24105 ───────────►  mx800_capture.py
                                           │
                                           ├─► hegp.db      (SQLite)
                                           ├─► data/*.csv   (une par intervention)
                                           └─► patient_demo.json
```

Le script maintient une association permanente avec le moniteur : interrogation
des numériques chaque seconde, des données démographiques toutes les 30 secondes,
avec reconnexion automatique en cas d'`Abort` ou de refus.

---

## Découverte automatique du moniteur

Aucune adresse IP à saisir. Le mécanisme repose sur deux couches.

**Côté réseau**, dnsmasq attribue une adresse à tout équipement dont l'adresse
MAC commence par `00:09:FB` (OUI Philips Patient Monitoring), dans une plage
qui leur est réservée :

```
dhcp-mac=philips,00:09:FB:*:*:*
dhcp-range=tag:philips,192.168.100.31,192.168.100.40,255.255.255.0,infinite
bootp-dynamic
```

`bootp-dynamic` est indispensable : le moniteur demande son adresse en **BOOTP**,
et sans cette option dnsmasq journalise `no address configured` et n'attribue
jamais rien.

**Côté script**, `discover_monitor.py` lit les baux dnsmasq, filtre sur l'OUI
Philips, puis envoie un Association Request à chaque candidat. Un moniteur
expose plusieurs points de terminaison réseau (châssis, modules), mais un seul
répond au Data Export — c'est celui-là qui est retenu.

Test manuel :

```bash
python3 discover_monitor.py --debug
```

---

## Configuration

`config.json` est généré localement par `install.sh` à partir de
`config.json`. Il n'est pas suivi par git : chaque site a le sien.

Pour activer un paramètre supplémentaire (BIS, SvO2, températures
additionnelles…), passer son `"active"` à `true` puis :

```bash
sudo systemctl restart mx800capture.service
```

La colonne correspondante est ajoutée automatiquement à la base SQLite.

---

## Exploitation

```bash
# État du service
sudo systemctl status mx800capture.service
journalctl -u mx800capture.service -f

# Volume capturé
sqlite3 hegp.db "SELECT COUNT(*), MAX(timestamp) FROM numerics;"

# Dernières mesures avec identité patient
sqlite3 -column -header hegp.db "
  SELECT n.timestamp, p.family_name, p.given_name,
         n.HR, n.SpO2, n.ABP_sys, n.ABP_dia, n.ABP_mean, n.Tcore
  FROM numerics n
  LEFT JOIN patients p ON n.patient_db_id = p.id
  ORDER BY n.id DESC LIMIT 10;"
```

---

## Dépannage

**Le moniteur n'obtient pas d'adresse**

```bash
journalctl -u dnsmasq -f
```

`no address configured` signifie que `bootp-dynamic` est absent de
`/etc/dnsmasq.d/mx800.conf`.

**Aucune donnée ne rentre**

```bash
cat /var/lib/misc/dnsmasq.leases     # le moniteur a-t-il une adresse ?
python3 discover_monitor.py --debug  # est-il joignable ?
```

**Les baux BOOTP sont perpétuels.** Chaque MAC vue consomme une adresse
définitivement. Après plusieurs changements de moniteur, la plage peut se
remplir :

```bash
sudo systemctl stop dnsmasq
sudo rm -f /var/lib/misc/dnsmasq.leases
sudo systemctl start dnsmasq
```

**Le service redémarre en boucle** — vérifier qu'aucun `WatchdogSec` ne traîne
dans l'unité systemd : le script n'implémente pas `sd_notify` et serait tué
toutes les deux minutes.

---

## Waveforms

L'option `--waves` (stockage HDF5) est implémentée mais inactive sur le
moniteur testé : celui-ci n'accorde pas l'extension `POLL_EXT_PERIOD_RTSA`
lors de l'association, et ne renvoie donc aucune courbe. Le code reste en
place pour un moniteur qui l'autoriserait.

---

## Fichiers

| Fichier | Rôle |
|---|---|
| `mx800_capture.py` | Acquisition, parsing du protocole, stockage |
| `discover_monitor.py` | Découverte du moniteur par MAC Philips |
| `install.sh` | Installation complète sur RPi neuf |
| `config.json` | Modèle de configuration des paramètres |
| `mx800capture.service` | Modèle d'unité systemd |

---

## Référence

Philips Interface Programming Guide (PIPG), réf. 4535 642 59271 — spécification
du protocole Data Export.
