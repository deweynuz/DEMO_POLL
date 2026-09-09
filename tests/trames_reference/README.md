# Trames de référence

Octets **réels** capturés par `tcpdump` passif sur le moniteur MX800 `SALLE1`,
le 09/09/2026. Ils sont la vérité de terrain du projet :

- les tests vérifient que nos encodeurs reproduisent ces octets **à l'identique** ;
- le simulateur les rejoue, au lieu d'inventer des trames plausibles.

Tout ce qui identifie le site ou fige un instant a été neutralisé. Les valeurs physiologiques du patient réel présent lors de la capture ont été
**remplacées** dans les poll results par des constantes déterministes : seule la
structure d'imbrication est conservée.

| Fichier | Taille | SHA-256 (12) | Provenance |
|---|---:|---|---|
| `assoc_request.bin` | 238 o | `b46b645e7479` | PI -> moniteur, Association Request. Généré par le module existant et ACCEPTÉ par un vrai MX800 : référence de non-régression pour notre encodeur. |
| `assoc_response.bin` | 208 o | `3b6bf0b2f1b2` | moniteur -> PI, Association Response réelle (SALLE1, 09/09/2026). |
| `mds_create_event.bin` | 282 o | `9ba26ccbbe13` | moniteur -> PI, MDS Create Event réel. NEUTRALISÉ : bed_label -> SIM1, system_id -> 00:00:00:00:00:01, Date-and-Time -> 2026-01-01T00:00:00. |
| `mds_create_result.bin` | 28 o | `41bad62b5205` | PI -> moniteur, confirmation du MDS Create (RORS / CMD_CONFIRMED_EVENT_REPORT). SANS ELLE le moniteur ré-émet 3 fois puis ABORT à ~10 s (cf. abort.bin). |
| `release_request.bin` | 26 o | `887b1f553582` | PI -> moniteur, Release Request. Honoré (0x0A en 120 ms) si l'association est complète. |
| `release_response.bin` | 26 o | `aebb9d223627` | moniteur -> PI, Release Response réelle. Le module précédent la classait « type inconnu ». |
| `abort.bin` | 48 o | `73eba38b29c8` | moniteur -> PI, ABORT réel émis ~10 s après un MDS Create NON confirmé. Comportement observé 4300 fois en 6 jours sur un moniteur clinique. |
| `poll_request_nu.bin` | 36 o | `c7a4968d24ed` | PI -> moniteur, Single Poll Data Request sur les numerics (ROIV / CMD_CONFIRMED_ACTION). |
| `poll_result_nu_1.bin` | 514 o | `f3c83220c3b0` | moniteur -> PI, poll result numerics 1/3 — ROLRS_APDU (résultat lié, PAS le dernier). Conservé pour sa STRUCTURE ; le simulateur régénère les valeurs physiologiques. |
| `poll_result_nu_2.bin` | 180 o | `be555c8864c1` | moniteur -> PI, poll result numerics 2/3 — ROLRS_APDU (résultat lié, PAS le dernier). Conservé pour sa STRUCTURE ; le simulateur régénère les valeurs physiologiques. |
| `poll_result_nu_3.bin` | 48 o | `2218df26283c` | moniteur -> PI, poll result numerics 3/3 — RORS_APDU (DERNIER de la série). Conservé pour sa STRUCTURE ; le simulateur régénère les valeurs physiologiques. |
