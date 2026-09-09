# Note technique — traçabilité protocolaire MX800

Source d'autorité unique : **Philips IntelliVue Data Export Interface Programming Guide**,
rév. G.0 (`M8000-9305G.book`, Distiller 2011, 305 p.), archivé en `docs/PIPG_G0_2011.pdf`.

**Correspondance de pagination** : `page PDF = page imprimée + 1`, vérifiée sur les 304 pages
numérotées. Toutes les références ci-dessous sont des **pages imprimées**.

Toute structure binaire du module doit figurer dans ce tableau. Une structure absente d'ici
est une structure non tracée : elle ne doit pas être écrite.

---

## 1. Association

### MDSEUserInfoStd / PollProfileSupport — p. 67-72

```c
typedef struct PollProfileSupport {          // p. 69
    PollProfileRevision poll_profile_revision;   // u_32
    RelativeTime        min_poll_period;         // u_32, unité 1/8 ms
    u_32                max_mtu_rx;
    u_32                max_mtu_tx;
    u_32                max_bw_tx;
    PollProfileOptions  options;                 // u_32
    AttributeList       optional_packages;
} PollProfileSupport;

typedef struct {                             // p. 71
    PollProfileExtOptions options;               // u_32
    AttributeList         ext_attr;
} PollProfileExt;                            // attribut NOM_ATTR_POLL_PROFILE_EXT (0xF001)
```

Constantes (p. 69-71) :

| Constante | Valeur | Page |
|---|---|---|
| `SYST_CLIENT` | `0x80000000` | 69 |
| `POLL_PROFILE_REV_0` | `0x80000000` | 70 |
| `P_OPT_DYN_CREATE_OBJECTS` | `0x40000000` | 71 |
| `P_OPT_DYN_DELETE_OBJECTS` | `0x20000000` | 71 |
| `POLL_EXT_PERIOD_NU_1SEC` | `0x80000000` | 71 |
| `POLL_EXT_PERIOD_NU_AVG_12SEC` | `0x40000000` | 71 |
| `POLL_EXT_PERIOD_NU_AVG_60SEC` | `0x20000000` | 71 |
| `POLL_EXT_PERIOD_NU_AVG_300SEC` | `0x10000000` | 71 |
| **`POLL_EXT_PERIOD_RTSA`** | **`0x08000000`** | **71** |
| `POLL_EXT_ENUM` | `0x04000000` | 71 |
| `POLL_EXT_NU_PRIO_LIST` | `0x02000000` | 71 |
| `POLL_EXT_DYN_MODALITIES` | `0x01000000` | 71 |

Règles :

- Au moins un bit de période numérique doit être posé, sinon **le paquet optionnel est ignoré
  en entier** (p. 72). Pour les courbes : `NU_1SEC | RTSA` = `0x88000000`.
- Le client **doit** parser l'Association Response pour savoir si l'option a été acceptée
  (p. 72). Le moniteur repose le bit correspondant s'il sait exporter des courbes.
- MTU : minimum 300 o exigé, **maximum négociable 1364 o sur LAN**. Au moins 500 o pour
  recevoir 256 ms de courbe en un message, 700 o avec contexte multiplexé (p. 71).
- Une seule association active à la fois ; au-delà, Refuse (p. 73).

### Timeout d'association — p. 70

| `min_poll_period` négocié | Timeout |
|---|---|
| < 3,3 s | **10 s** |
| 3,3 s … 43 s | 3 × `min_poll_period` |
| > 43 s | 130 s |

### En-têtes de session — p. 72-73

Le premier octet suffit à identifier le message d'association (p. 72).

| Octet | Message | Page |
|---|---|---|
| `0x0D` | Association Request (`CN_SPDU_SI`) | 67 |
| `0x0E` | Association Response (`AC_SPDU_SI`) | 73 |
| `0x0C` | Refuse (`RF_SPDU_SI`) | 73 |
| `0x09` | Release Request | 72 |
| `0x0A` | Release Response (`DN_SPDU_SI`) | 73 |
| `0x19` | Abort | 72 |

Début des User Data dans l'Association Response : chercher `BE 80 28 80 81` ou
`BE 80 28 80 02 01 02 81`, suivis en fin par 16 octets `0x00` (p. 73).

---

## 2. Poll étendu (courbes)

### Requête — p. 59-60

```c
typedef struct {                             // p. 59
    u_16          poll_number;
    TYPE          polled_obj_type;
    OIDType       polled_attr_grp;
    AttributeList poll_ext_attr;
} PollMdibDataReqExt;

typedef struct { RelativeTime active_period; } PollDataReqPeriod;   // p. 60
```
Action `NOM_ACT_POLL_MDIB_DATA_EXT` sur `{NOM_MOC_VMS_MDS, 0, 0}`, commande
`CMD_CONFIRMED_ACTION` (p. 59). Attribut `NOM_ATTR_TIME_PD_POLL` (p. 60).

### Réponse — p. 62

```c
typedef struct PollMdibDataReplyExt {
    u_16         poll_number;
    u_16         sequence_no;
    RelativeTime rel_time_stamp;
    AbsoluteTime abs_time_stamp;
    TYPE         polled_obj_type;
    OIDType      polled_attr_grp;
    PollInfoList poll_info_list;
} PollMdibDataReplyExt;
```

- `sequence_no = 0` = confirmation que la requête a été acceptée ; incrémenté à chaque
  résultat périodique. C'est **le** compteur de détection de perte (p. 62).
- `rel_time_stamp` : pour les courbes, **début de la période de 256 ms** (p. 62).
- `abs_time_stamp` : **non supporté, tous les champs à `0xff`**. Conversion obligatoire via
  les attributs MDS « Relative Time » et « Date and Time » (p. 62).

### Périodes de résultat — p. 60

| Source | Période |
|---|---|
| courbes temps réel | **256 ms** |
| mesures temps réel | 1 s |
| moyennes 12 s / 1 min / 5 min | 6 s / 30 s / 150 s |
| alertes | 1 s |

### Keep-alive — p. 63

Obligatoire en poll étendu. Recommandé : un Poll Request sur l'objet **Alert Monitor**,
groupe **VMO Static Context** — le résultat est court. À envoyer bien avant le timeout de 10 s.

### Contexte multiplexé — p. 287

Si `polled_attr_grp = 0` dans un poll périodique, le moniteur émet le contexte statique et
dynamique **d'un objet par 1024 ms**, inclus dans le poll d'observation. C'est ainsi qu'on
récupère `ScaleRangeSpec16`, `SaSpec` et la période d'échantillonnage sans poll séparé.

---

## 3. Liste de priorité des courbes

- `GET PRIORITY LIST` : `CMD_GET` sur `{NOM_MOC_VMS_MDS,0,0}`,
  AttributeIdList = `NOM_ATTR_POLL_RTSA_PRIO_LIST` (p. 63).
- `SET PRIORITY LIST` : `CMD_CONFIRMED_SET`, ModificationList avec opération **REPLACE**
  et une `TextIdList`. `SET_TO_DEFAULT` = attribut vide (length 0).
  **`ADD_VALUES` et `REMOVE_VALUES` ne sont pas supportées** (p. 64).

```c
typedef struct { u_16 count; u_16 length; TextId value[1]; } TextIdList;   // p. 64
```

`TextId` = label de l'onde tel que renvoyé dans le contexte dynamique.
Partition SCADA : `label = 0x00020000 | physio_id`.

### Limites — p. 286-287

- Soit **jusqu'à 3 ondes ECG individuelles à 500 sps**, soit **l'onde ECG composée**
  (3 voies à 250 sps, `SaObsValueCmp`, contexte commun, label `NLS_NOM_ECG_ELEC_POTL`
  en mode non-EASI).
- **Plus jusqu'à 8 ondes non-ECG** à 125 ou 62,5 sps.
- Une entrée est **silencieusement ignorée** si le label n'existe pas, si l'objet est
  indisponible, ou si les limites sont dépassées (p. 287).
  → relecture obligatoire par `GET PRIORITY LIST` et comparaison au demandé.
- Le handle est le même pour toutes les ondes ECG ; **elles se distinguent par leur
  `physio_id`** (p. 286).
- Le client doit suivre les horodatages des poll results pour détecter les échantillons
  manquants (p. 287).

---

## 4. Décodage des échantillons

```c
typedef struct {                             // p. 87
    OIDType          physio_id;                  // u_16, partition SCADA
    MeasurementState state;                      // u_16
    struct { u_16 length; u_8 value[1]; } array; // length en OCTETS
} SaObsValue;

typedef struct {                             // p. 88
    u_16 count;                                  // nb de SaObsValue
    u_16 length;                                 // taille du tableau en octets
    SaObsValue value[1];
} SaObsValueCmp;
```

**Mesure valide si et seulement si le premier octet de `state` est nul** (p. 87).

`array.length` est en octets : 128 échantillons 16 bits → `length = 256`.
Règle générale confirmée p. 286 : « Length fields denote the length of data appended,
excluding the size of the length field. »

### Contexte statique — p. 83-84

```c
typedef struct { u_16 array_size; SampleType sample_type; SaFlags flags; } SaSpec;
typedef struct { u_8 sample_size; u_8 significant_bits; } SampleType;

typedef u_16 SaFlags;
#define SMOOTH_CURVE      0x8000
#define DELAYED_CURVE     0x4000
#define STATIC_SCALE      0x2000   // la calibration ne change pas
#define SA_EXT_VAL_RANGE  0x1000   // masquer les bits non significatifs
```

`NOM_ATTR_TIME_PD_SAMP` (`RelativeTime`, **mandatory**, VMO Static Context) donne la période
d'échantillonnage — **jamais coder la fréquence en dur** (p. 84).

### Masques de qualité — p. 83-84

```c
typedef struct { u_16 count; u_16 length; SaFixedValSpecEntry16 value[1]; } SaFixedValSpec16;
typedef struct { SaFixedValId sa_fixed_val_id; u_16 sa_fixed_val; } SaFixedValSpecEntry16;
```

| `SaFixedValId` | Valeur | Sens |
|---|---|---|
| `SA_FIX_UNSPEC` | 0 | non spécifié |
| `SA_FIX_INVALID_MASK` | 1 | échantillon invalide |
| `SA_FIX_PACER_MASK` | 2 | spike de pacemaker |
| `SA_FIX_DEFIB_MARKER_MASK` | 3 | marqueur de défibrillation |
| `SA_FIX_SATURATION` | 4 | saturation du capteur (c'est un masque) |
| `SA_FIX_QRS_MASK` | 5 | trigger QRS |

Attribut `NOM_ATTR_SA_FIXED_VAL_SPECN`, VMO Static Context, **optionnel**.
Ces marqueurs vont dans un canal de qualité parallèle, jamais mélangés au signal.

### Calibration — p. 86

```c
typedef struct {
    FLOATType lower_absolute_value;   // NaN si l'onde ne représente aucune valeur absolue
    FLOATType upper_absolute_value;
    u_16      lower_scaled_value;
    u_16      upper_scaled_value;
} ScaleRangeSpec16;                   // 12 octets
```

Attribut `NOM_ATTR_SCALE_SPECN_I16`, VMO **Dynamic** Context, **mandatory**.
Dynamique = peut changer en cours d'acquisition, sauf si `STATIC_SCALE` est posé.
→ la calibration est **stockée horodatée avec les données**, jamais appliquée à la volée.

---

## 5. Volumétrie — p. 286

| Type d'onde | Période éch. | Taille tableau | Débit |
|---|---|---|---|
| 500 sps (ECG) | 2 ms | 128 éch. | 1064 o/s |
| 250 sps (ECG composé) | 4 ms | 3 × 64 éch. | 1640 o/s |
| 125 sps | 8 ms | 32 éch. | 296 o/s |
| 62,5 sps | 16 ms | 16 éch. | 168 o/s |

Hors contexte. Cause documentée d'échantillons manquants : trop d'objets Wave interrogés
→ réduire la liste de priorité (p. 290).

---

## 6. Défauts tracés dans le code existant

| Défaut | Constat | Référence |
|---|---|---|
| Bit RTSA absent | `0xA0000000` posé = `NU_1SEC \| NU_AVG_60SEC`. Attendu `0x88000000` | p. 71 |
| MTU hors limite | `max_mtu_rx = 1456` demandé, maximum LAN 1364 | p. 71 |
| Release Response non reconnue | `0x0A` classé « type inconnu » | p. 73 |
| Keep-alive absent | `min_poll_period` = 312 ms → timeout 10 s | p. 70, 63 |
| `sequence_no` ignoré | aucune détection de perte possible | p. 62 |
| Fréquence supposée | `NOM_ATTR_TIME_PD_SAMP` jamais lu | p. 84 |

---

## 7. Correspondance code → page

Chaque structure binaire manipulée par le module, et l'endroit du guide dont
elle est tirée. Une entrée absente de cette table est une structure non tracée :
elle ne doit pas exister dans le code.

### Construction (`mx800/protocole/trames.py`)

| Fonction | Structure | Page |
|---|---|---|
| `encoder_float` | FLOAT-Type | 40-41 |
| `encoder_chaine` | chaîne préfixée de sa longueur | 40 |
| `_longueur_asn` | ASNLength | 68 |
| `_SESSION_DATA`, `_PRES_HEADER_REQ` | blocs ASN.1 de l'Association Request | 298 |
| `_PRES_HEADER_RSP` | blocs ASN.1 de l'Association Response | 299 |
| `construire_user_data` | MDSEUserInfoStd, PollProfileSupport, PollProfileExt | 68-71, exemple 304 |
| `construire_assoc_request` | Association Request | 67-68 |
| `construire_assoc_response` | Association Response | 73 |
| `_roiv` / `_rors` / `_rolrs` | ROIVapdu / RORSapdu / ROLRSapdu | 43-44 |
| `_action` | ActionArgument (avec `u_32 scope`) | 49 |
| `construire_poll_request` | SINGLE POLL DATA REQUEST | 55-56 |
| `construire_poll_request_etendu` | PollMdibDataReqExt, PollDataReqPeriod | 59-60 |
| `construire_keep_alive` | Poll sur l'Alert Monitor, contexte statique | 63 |
| `construire_mds_create_result` | confirmation du MDS Create Event | 111 |
| `construire_get_liste_priorite` | GetArgument (avec `scope`) | 50, 63 |
| `construire_set_liste_priorite` | SetArgument, ModificationList, TextIdList | 51, 64 |
| `encoder_nu_obs_value(_cmp)` | NuObsValue, NuObsValueCmp | 76-77 |
| `encoder_sa_obs_value(_cmp)` | SaObsValue, SaObsValueCmp | 87-88 |
| `encoder_scale_range_spec16` | ScaleRangeSpec16 | 86 |
| `encoder_sa_spec` | SaSpec, SampleType, SaFlags | 83 |
| `encoder_masques_qualite` | SaFixedValSpec16 | 83-84 |
| `construire_serie_poll_result` | chaînage ROLRS puis RORS terminal | 44, 58 |

### Décodage (`mx800/protocole/decodage.py`)

| Fonction | Structure | Page |
|---|---|---|
| `type_message` | en-têtes de session | 72-73 |
| `decoder_apdu` | ROIV/RORS/ROLRS, RorlsId | 43-44 |
| `decoder_liste_attributs` | AttributeList, AVAType | recoupée sur trame réelle |
| `decoder_mds_create` | EventReportArgument, MDSCreateInfo | 111 |
| `decoder_reponse_association` | User Data, options négociées | 72-73 |
| `decoder_resultat_poll` | PollMdibDataReply(Ext), PollInfoList, SingleContextPoll, ObservationPoll | 56-58, 62 |
| `decoder_valeur_numerique(_composees)` | NuObsValue, NuObsValueCmp, MeasurementState | 76-77 |
| `decoder_sa_obs_value(_cmp)` | SaObsValue, SaObsValueCmp | 87-88 |
| `decoder_sa_spec` | SaSpec | 83 |
| `decoder_calibration` | ScaleRangeSpec16 | 86 |
| `decoder_masques_qualite` | SaFixedValSpec16 | 83-84 |
| `decoder_temps_absolu` | AbsoluteTime (0xff = non supporté) | 62 |

### Démographiques et mode opératoire

| Élément | Structure | Page |
|---|---|---|
| `decoder_demographiques` | attributs de l'objet Patient Demographics | 103-105 |
| `PatDemoState` (EMPTY / PRE_ADMITTED / ADMITTED / DISCHARGED) | p. 103 — seul `ADMITTED` signifie « présentes et valides » | 103 |
| `PatientType` (adulte / pédiatrique / néonatal) | | 104 |
| `PatientSex` | | 105 |
| `NOM_ATTR_MODE_OP`, bit `DEMO = 0x2000` | mode opératoire du moniteur | 96 |

Le bit `DEMO` est capital en recherche : en mode démonstration le moniteur
fabrique des signaux qui n'ont rien d'un patient. Le module le lit dans le MDS
Create, le consigne dans la session, et refuse par défaut d'ouvrir une
intervention (`refuser_mode_demo`).

### Catalogue (`mx800/protocole/ondes.py`)

55 ondes extraites automatiquement des tables « Waves », p. 179-188.
Règle `label = 0x00020000 | physio_id` vérifiée pour chaque entrée.

### Points où le comportement réel s'écarte du guide

Constatés sur le moniteur SALLE1 le 09/09/2026, et reproduits par le simulateur :

| Constat | Guide |
|---|---|
| `max_mtu_tx` renvoyé à **1456** | maximum annoncé 1364 (p. 71) |
| Release non honoré tant que le MDS Create n'est pas confirmé | p. 72 ne le précise pas |
| MDS Create ré-émis 3 fois puis ABORT à ~10 s | conséquence du timeout p. 70 |
