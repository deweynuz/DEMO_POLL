# DEMO_POLL — Acquisition Philips IntelliVue MX800

Système d'acquisition en continu des données d'un moniteur patient **Philips
IntelliVue MX800**. Il remplace *VSCaptureMP* et enregistre, sans interruption :

- les **paramètres numériques** (FC, SpO₂, pressions, débit cardiaque, températures, CO₂, BIS…) ;
- les **données démographiques** du patient (identité, sexe, type, statut d'admission) ;
- en option, les **courbes temps réel** (ECG, Pléth, pressions, CO₂…) au format HDF5.

Les données sont écrites dans une base **SQLite**, en **CSV**, et — pour les
courbes — en **HDF5**. Le tout est piloté par un fichier `config.json` et tourne
en service `systemd` sur un Raspberry Pi.

> Contexte : acquisition au bloc / réanimation (projet HEGP). Le protocole
> implémenté suit le *Philips Interface Programming Guide* (PIPG, réf.
> 4535 642 59271).

---

## Sommaire

- [Architecture](#architecture)
- [Prérequis](#prérequis)
- [Installation](#installation)
- [Configuration (`config.json`)](#configuration-configjson)
- [Utilisation](#utilisation)
- [Modèle de données](#modèle-de-données)
  - [Notion de « session » vs « intervention »](#notion-de-session-vs-intervention)
  - [Schéma SQL](#schéma-sql)
  - [Fichiers CSV et HDF5](#fichiers-csv-et-hdf5)
- [Paramètres disponibles](#paramètres-disponibles)
- [Courbes (waveforms)](#courbes-waveforms)
- [Exploitation & maintenance](#exploitation--maintenance)
- [Dépannage](#dépannage)
- [Fonctionnement interne (protocole)](#fonctionnement-interne-protocole)
- [Structure du dépôt](#structure-du-dépôt)

---

## Architecture

```
┌──────────────────┐   UDP 24105    ┌────────────────────────┐
│  Moniteur MX800  │◀──────────────▶│  Raspberry Pi          │
│  (Data Export)   │   port local   │  mx800_capture.py      │
└──────────────────┘   24106        │                        │
                                     │  ├─ SQLite  (hegp.db)  │
                                     │  ├─ CSV     (data/)    │
                                     │  ├─ HDF5    (waves/)   │
                                     │  └─ JSON    (démog.)   │
                                     └────────────────────────┘
```

Le script ouvre une association réseau avec le moniteur, puis interroge
(*poll*) périodiquement :

| Donnée         | Fréquence par défaut | Réglage             |
|----------------|----------------------|---------------------|
| Numériques     | 1 s                  | `poll_interval`     |
| Démographiques | 30 s                 | `demo_interval`     |
| Courbes        | ~256 ms              | fixe (si `--waves`) |

---

## Prérequis

- **Raspberry Pi OS Bookworm** (ou toute distribution Linux récente).
- **Python 3.10+** (le code utilise la syntaxe d'annotations `X | None`).
- **SQLite 3**.
- Réseau Ethernet vers le moniteur, avec l'**export de données activé** côté MX800.
- *(Optionnel, pour les courbes)* : **`h5py`** et **`numpy`**.

Aucune dépendance Python n'est requise pour le mode numériques/démographiques —
le script n'utilise que la bibliothèque standard. `h5py`/`numpy` ne sont
nécessaires que pour la capture des courbes (`--waves`).

---

## Installation

### Installation automatique (recommandée)

```bash
curl -sSL https://raw.githubusercontent.com/deweynuz/DEMO_POLL/main/install.sh | bash
```

Le script `install.sh` (interactif) :

1. vérifie Python/Git et met à jour le système ;
2. installe `sqlite3`, `python3-pip`, et tente `h5py`/`numpy` ;
3. demande l'**IP du moniteur** et le **dossier d'installation** ;
4. clone le dépôt, génère `config.json` avec l'IP saisie ;
5. crée les dossiers `data/` et `waves/` ;
6. installe et active un **service systemd** `mx800capture.service` ;
7. teste la connectivité (`ping`) et démarre le service.

> ⚠️ Ne lancez pas le script en `root` — utilisez un utilisateur normal
> disposant de `sudo`.

### Installation manuelle

```bash
git clone https://github.com/deweynuz/DEMO_POLL.git
cd DEMO_POLL

# Optionnel — courbes HDF5
pip install h5py numpy --break-system-packages

# Adaptez config.json (IP, chemins…), puis lancez :
python3 mx800_capture.py --config config.json
```

---

## Configuration (`config.json`)

Toute la configuration se fait dans `config.json`. Après modification,
**redémarrez le service** (`sudo systemctl restart mx800capture.service`).

### Réglages généraux

| Clé             | Défaut                          | Description                                   |
|-----------------|---------------------------------|-----------------------------------------------|
| `monitor_ip`    | `192.168.100.31`                | IP du moniteur, ou `"auto"` / `""` pour la [découverte automatique](#découverte-automatique-de-lip-du-moniteur) |
| `discovery_cidr`| `""`                            | Plage CIDR à balayer en découverte si le broadcast ne suffit pas (réseaux routés) |
| `poll_interval` | `1.0`                           | Intervalle d'interrogation des numériques (s)  |
| `demo_interval` | `30`                            | Intervalle d'interrogation des démographies (s)|
| `db_path`       | `/home/hegp/hegp.db`            | Chemin de la base SQLite                       |
| `csv_dir`       | `/home/hegp/data/`              | Dossier des exports CSV                        |
| `demo_json`     | `/home/hegp/patient_demo.json`  | Fichier JSON des démographies (pour une IHM)   |
| `waves`         | `false`                         | Active la capture des courbes → HDF5           |
| `hdf5_dir`      | `/home/hegp/waves/`             | Dossier des fichiers HDF5                      |

> Les arguments de ligne de commande sont **prioritaires** sur `config.json`.

### Paramètres physiologiques

Le bloc `parameters` associe chaque **identifiant physiologique Philips**
(clé hexadécimale, ex. `0x4182`) à un nom de colonne, une unité et un libellé :

```json
"0x4182": {"name": "HR", "unit": "bpm", "label": "Fréquence cardiaque", "active": true}
```

- `active: true` → le paramètre est capturé (et sa colonne créée si besoin).
- `active: false` → le paramètre est ignoré.
- Les clés commençant par `_` (ex. `_section_cardio`) sont des commentaires.

Pour **activer un paramètre**, passez son `active` à `true` et redémarrez le
service. La colonne SQL correspondante est créée automatiquement (migration à
chaud, voir plus bas).

---

## Découverte automatique de l'IP du moniteur

Pour déployer le système **sans connaître ni configurer l'IP** du moniteur (par
exemple le même Raspberry Pi réutilisé sur n'importe quel site), mettez :

```json
"monitor_ip": "auto"
```

(ou laissez le champ vide, ou lancez avec `--ip auto`).

### Comment ça marche

Le protocole étant en **UDP sans connexion**, le script :

1. envoie l'*Association Request* en **broadcast** (`255.255.255.255:24105`) sur
   le segment réseau, en la ré-émettant toutes les ~4 s tant qu'aucun moniteur
   n'a répondu ;
2. **adopte l'IP** du premier moniteur qui répond (`ASSOC_RESPONSE` /
   `MDS_CREATE`) puis passe en **unicast** pour toute la suite ;
3. si la connexion tombe (`ABORT`), il **ré-apprend** l'IP automatiquement (utile
   si le moniteur est remplacé ou si son IP DHCP change).

Si plusieurs moniteurs répondent sur le segment, le **premier** est verrouillé
(un Pi = un moniteur).

### Limite & réseaux routés

Le **broadcast ne traverse pas les routeurs/VLAN**. La découverte fonctionne donc
en *plug-and-play* tant que le Pi est sur le **même sous-réseau** que le moniteur.
Si le Pi peut être sur un sous-réseau **routé différent**, indiquez une plage à
balayer en complément :

```json
"monitor_ip": "auto",
"discovery_cidr": "192.168.10.0/24"
```

ou en ligne de commande :

```bash
python3 mx800_capture.py --ip auto --discover-cidr 192.168.10.0/24
```

Le script enverra alors l'Association Request en broadcast **et** en unicast à
chaque hôte de la plage, jusqu'à obtenir une réponse.

> Une **IP fixe** reste évidemment possible (`"monitor_ip": "192.168.100.31"`) —
> c'est le comportement historique, sans broadcast.

---

## Utilisation

### En service systemd (installation standard)

```bash
sudo systemctl start   mx800capture.service     # démarrer
sudo systemctl stop    mx800capture.service     # arrêter
sudo systemctl restart mx800capture.service     # redémarrer (après config)
sudo systemctl status  mx800capture.service     # état
journalctl -u mx800capture.service -f           # logs en direct
```

Le service est configuré avec `Restart=always` (redémarrage automatique en cas
de coupure) et démarre au boot.

### En ligne de commande

```bash
python3 mx800_capture.py --config config.json
python3 mx800_capture.py --ip 192.168.100.31 --db /home/hegp/hegp.db --csv /home/hegp/data/
python3 mx800_capture.py --config config.json --waves     # + courbes HDF5
python3 mx800_capture.py --config config.json --debug      # logs détaillés
```

| Argument          | Défaut                | Description                                          |
|-------------------|-----------------------|------------------------------------------------------|
| `--config`        | `/home/hegp/config.json` | Fichier de configuration JSON                      |
| `--ip`            | *(config.json)*       | IP du moniteur, ou `auto` pour la découverte automatique |
| `--discover-cidr` | *(config.json)*       | Plage CIDR à balayer en découverte (ex. `192.168.1.0/24`) |
| `--db`            | *(config.json)*       | Chemin de la base SQLite                             |
| `--csv`           | *(config.json)*       | Dossier CSV                                          |
| `--json`          | *(config.json)*       | Fichier JSON des démographies                        |
| `--interval`      | *(config.json)*       | Intervalle poll numériques (s)                       |
| `--demo-interval` | *(config.json)*       | Intervalle poll démographies (s)                     |
| `--waves`         | désactivé             | Active la capture des courbes → HDF5                 |
| `--hdf5`          | *(config.json)*       | Dossier HDF5                                         |
| `--debug`         | désactivé             | Journalisation en niveau `DEBUG`                     |

Arrêt propre : `Ctrl-C` (envoie une *Release Request* au moniteur, clôture la
session et l'intervention en cours, ferme les fichiers).

---

## Modèle de données

### Notion de « session » vs « intervention »

Deux niveaux de regroupement coexistent — à ne pas confondre :

- **Session** = une **connexion réseau** au moniteur (de l'association jusqu'à
  la déconnexion/reconnexion). Les reconnexions étant fréquentes, il peut y
  avoir de nombreuses sessions courtes. La table `sessions` sert de **journal
  technique** des connexions.

- **Intervention** = un **épisode patient cumulé**. Toutes les données d'un
  même patient sont regroupées sous un seul `intervention_id`, **à travers les
  déconnexions/reconnexions réseau**. Les règles :

  - une **nouvelle intervention** démarre dès que le `patient_id` courant
    **change** ;
  - un même patient qui reste branché (même après reconnexion réseau) **reste
    dans la même intervention** ;
  - si un patient **revient après un autre patient**, c'est **une nouvelle
    intervention** (la séquence patient A → B → A produit **3 interventions**) ;
  - au **redémarrage du script**, une nouvelle intervention est toujours créée
    (pas de reprise depuis la base) ;
  - l'identité patient est comparée sur le **`patient_id`** seul ;
  - une intervention n'est **pas** clôturée sur une coupure réseau (c'est le
    principe du cumul) — seulement au changement de patient et à l'arrêt propre.

Les mesures (`numerics`) et les courbes portent à la fois `session_id` (quelle
connexion) et `intervention_id` (quel patient), ce qui permet d'analyser les
données **par patient** tout en conservant la traçabilité réseau.

### Schéma SQL

Base SQLite en mode `WAL`. Quatre tables :

#### `sessions` — journal des connexions réseau

| Colonne      | Type     | Description                     |
|--------------|----------|---------------------------------|
| `id`         | INTEGER  | Clé primaire (auto-incrément)   |
| `monitor_ip` | TEXT     | IP du moniteur                  |
| `start_time` | TEXT     | Début de connexion (ISO 8601)   |
| `end_time`   | TEXT     | Fin de connexion (ISO 8601)     |

#### `interventions` — épisode patient (cumulé)

| Colonne            | Type    | Description                                      |
|--------------------|---------|--------------------------------------------------|
| `id`               | INTEGER | Clé primaire = **`intervention_id`**             |
| `start_session_id` | INTEGER | Session où l'intervention a débuté → `sessions.id` |
| `patient_id`       | TEXT    | Identifiant patient (MRN moniteur)               |
| `family_name`      | TEXT    | Nom                                              |
| `given_name`       | TEXT    | Prénom                                           |
| `sex`              | TEXT    | Sexe                                             |
| `patient_type`     | TEXT    | Type (adulte / pédiatrique / néonatal)           |
| `start_time`       | TEXT    | Début de l'intervention (ISO 8601)               |
| `end_time`         | TEXT    | Fin (changement de patient ou arrêt propre)      |

#### `patients` — démographies (une ligne par intervention)

| Colonne           | Type    | Description                              |
|-------------------|---------|------------------------------------------|
| `id`              | INTEGER | Clé primaire                             |
| `session_id`      | INTEGER | Dernière session ayant vu ce patient     |
| `intervention_id` | INTEGER | → `interventions.id`                     |
| `patient_id`      | TEXT    | Identifiant patient                      |
| `family_name`     | TEXT    | Nom                                      |
| `given_name`      | TEXT    | Prénom                                   |
| `sex`             | TEXT    | Sexe                                     |
| `patient_type`    | TEXT    | Type                                     |
| `demo_state`      | TEXT    | Statut (`ADMITTED`, `DISCHARGED`, …)     |
| `admitted_at`     | TEXT    | Horodatage de création de la ligne       |

#### `numerics` — mesures numériques

| Colonne           | Type    | Description                                  |
|-------------------|---------|----------------------------------------------|
| `id`              | INTEGER | Clé primaire                                 |
| `session_id`      | INTEGER | → `sessions.id` (connexion réseau)           |
| `intervention_id` | INTEGER | → `interventions.id` (épisode patient)       |
| `patient_db_id`   | INTEGER | → `patients.id`                              |
| `timestamp`       | TEXT    | Horodatage de la mesure (ISO 8601)           |
| `HR`, `SpO2`, …   | REAL    | Une colonne par paramètre actif de `config.json` |

Index créés : `idx_numerics_ts (session_id, timestamp)` et
`idx_numerics_intervention (intervention_id, timestamp)`.

> **Migration automatique** : au démarrage, les colonnes manquantes (nouveau
> paramètre ajouté dans `config.json`, ou colonne `intervention_id` sur une base
> antérieure) sont ajoutées automatiquement par `ALTER TABLE`. Aucune migration
> manuelle n'est nécessaire.

#### Exemples de requêtes

```sql
-- Nombre de mesures par intervention
SELECT intervention_id, COUNT(*) FROM numerics GROUP BY intervention_id;

-- Toutes les données d'un patient (cumulé), même après reconnexions
SELECT n.timestamp, n.HR, n.SpO2, n.ABP_sys, n.ABP_dia
FROM numerics n
JOIN interventions i ON i.id = n.intervention_id
WHERE i.patient_id = 'XXXX'
ORDER BY n.timestamp;

-- Liste des interventions avec leur durée
SELECT id, patient_id, family_name, start_time, end_time FROM interventions;
```

### Fichiers CSV et HDF5

- **CSV** : un fichier par intervention, nommé
  `intervention_<id>_<patient_id>.csv`, ouvert en **append**. Les données d'un
  même patient restent donc cumulées dans un seul fichier, même après une
  reconnexion réseau. L'en-tête n'est écrit qu'à la création du fichier.

- **HDF5** *(si `--waves`)* : un fichier par intervention, nommé
  `intervention_<id>_<patient_id>.h5`, également ouvert en append. Structure :
  - `waves/<canal>` — échantillons bruts (uint16),
  - `timestamps/<canal>` — horodatages,
  - `patient/` — attributs démographiques,
  - attributs racine : `intervention_id`, `patient_id`, `created_at`, `monitor_protocol`.

- **JSON démographique** : le fichier `demo_json` est réécrit à chaque lecture
  démographique (utile pour alimenter une interface temps réel).

> Les données ne sont enregistrées que lorsque le patient est **admis**
> (`demo_state == ADMITTED`).

---

## Paramètres disponibles

Les identifiants sont ceux du PIPG. La colonne **Actif** indique la valeur par
défaut livrée dans `config.json` (modifiable).

| Groupe                     | Paramètres                                                                 |
|----------------------------|---------------------------------------------------------------------------|
| Cardio-vasculaire          | `HR`, `SpO2`, `Pulse`                                                      |
| Pression artérielle (ABP)  | `ABP_sys`, `ABP_dia`, `ABP_mean`                                          |
| Pression artérielle (ART)  | `ART_sys`, `ART_dia`, `ART_mean`                                          |
| Pression aortique          | `Ao_sys`, `Ao_dia`, `Ao_mean`                                            |
| Pression pulmonaire (PAP)  | `PAP_sys`, `PAP_dia`, `PAP_mean`                                          |
| Pression veineuse (CVP)    | `CVP`, `CVP_mean`                                                          |
| Pression non invasive (NBP)| `NBP_sys`, `NBP_dia`, `NBP_mean`                                          |
| Débit cardiaque            | `CO`, `CCO`, `CI`, `CCI`, `SV`, `SI`, `SVV`                               |
| Saturations O₂             | `SaO2`, `SvO2`, `ScvO2`                                                    |
| Températures               | `Temp`, `Trect`, `Tblood`, `Tcore`, `Tskin`, `Tesoph`, `Tnaso`, `Tart`, `T1`, `T2` |
| CO₂ / Respiratoire         | `CO2`, `EtCO2`, `FiCO2`, `RR`                                             |
| BIS / EEG                  | `BIS`, `BIS_SQI`, `EMG`, `SR`, `TP`, `SEF`, `BSI`                         |
| Résistances vasculaires    | `SVR`, `PVR`, `SVRI`, `PVRI` *(inactifs par défaut)*                      |
| Gaz du sang                | `pHa`, `PaCO2`, `PaO2`, `Hb` *(inactifs par défaut)*                      |
| Ventilation                | `MV`, `TV`, `Ppeak`, `PEEP`, `Cdyn` *(inactifs par défaut)*               |

> La liste exacte et l'état `active` font foi dans `config.json`.

---

## Courbes (waveforms)

Avec `--waves` (et `h5py`/`numpy` installés), le script demande en plus les
courbes temps réel via un *Extended Poll* (~256 ms). Canaux reconnus :

`ECG_I`, `ECG_II`, `ECG_III`, `ECG_aVR`, `ECG_aVL`, `ECG_aVF`, `ECG_V`,
`Pleth`, `ABP_wave`, `ART_wave`, `CVP_wave`, `PAP_wave`, `Resp`, `CO2_wave`.

Les échantillons sont mis en tampon en mémoire puis écrits par lots (~toutes les
100 trames) dans le fichier HDF5, avec compression gzip.

> Si `--waves` est demandé mais que `h5py` est absent, le mode courbes est
> désactivé automatiquement (un avertissement est journalisé) ; l'acquisition
> numériques/démographies continue normalement.

---

## Exploitation & maintenance

```bash
# Nombre de mesures enregistrées
sqlite3 /home/hegp/hegp.db "SELECT COUNT(*) FROM numerics;"

# Interventions récentes
sqlite3 /home/hegp/hegp.db "SELECT id, patient_id, start_time, end_time FROM interventions ORDER BY id DESC LIMIT 10;"

# Changer l'IP du moniteur
nano /home/hegp/config.json          # champ monitor_ip
sudo systemctl restart mx800capture.service

# Activer un paramètre (ex. BIS)
nano /home/hegp/config.json          # "0xF04E": ... "active": true
sudo systemctl restart mx800capture.service
```

> Les chemins ci-dessus (`/home/hegp/…`) correspondent aux valeurs par défaut
> du code. L'installation via `install.sh` place les fichiers dans le dossier
> choisi (par défaut `/home/<utilisateur>/…`).

---

## Dépannage

| Symptôme                                   | Piste                                                                 |
|--------------------------------------------|-----------------------------------------------------------------------|
| Aucune donnée, « Association refusée »      | Export de données activé sur le MX800 ? Un seul client connecté ?      |
| « Abort reçu », reconnexions en boucle      | Vérifier le câble/IP ; le script retente automatiquement.             |
| Service actif mais base vide                | Patient non **admis** sur le moniteur (`demo_state != ADMITTED`).     |
| `--waves` sans effet                        | Installer `h5py`/`numpy` : `pip install h5py numpy --break-system-packages`. |
| Moniteur injoignable                        | `ping <IP>` ; vérifier le VLAN/segment réseau.                       |
| Voir les logs                               | `journalctl -u mx800capture.service -f`                              |

---

## Fonctionnement interne (protocole)

Le script implémente la partie client du protocole **Philips IntelliVue Data
Export (UDP)** :

1. **Association** — envoi d'une *Association Request* (variante avec ou sans
   courbes) sur le port `24105` ; écoute locale sur `24106`.
2. **MDS Create Event** — le moniteur annonce sa présence ; le script confirme.
3. **Polling MDIB** — interrogations périodiques :
   - numériques (`NOM_MOC_VMO_METRIC_NU`),
   - démographies (`NOM_MOC_PT_DEMOG`),
   - courbes (`NOM_MOC_VMO_METRIC_SA_RT`, via *Extended Poll*).
4. **Parsing** — les réponses (`RORS`/`ROLRS`, éventuellement en plusieurs
   paquets liés) sont décodées : listes d'attributs, valeurs `NuObsValue`,
   chaînes UTF-16, et le **FLOAT-Type Philips** (mantisse 24 bits + exposant 8
   bits signés, avec gestion des valeurs spéciales NaN/NRes/±Inf).
5. **Release** — à l'arrêt, une *Release Request* est envoyée au moniteur.

Constantes réseau : port moniteur `24105`, port local `24106`, transport UDP.

---

## Structure du dépôt

```
DEMO_POLL/
├── mx800_capture.py   # Script d'acquisition principal
├── config.json        # Configuration (IP, chemins, paramètres actifs)
├── install.sh         # Installation automatique (Raspberry Pi + systemd)
├── .gitignore
└── README.md
```

---

*Protocole conforme au Philips Interface Programming Guide (PIPG) 4535 642 59271.*
