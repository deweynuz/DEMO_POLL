#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# Installation du service d'acquisition MX800.
#
# Idempotent : relançable sans dommage. Toute anomalie interrompt le script
# plutôt que de produire une installation à moitié faite — le script précédent
# copiait discover_monitor.py « si présent », si bien qu'un dépôt incomplet
# donnait une installation qui échouait au premier démarrage.
#
# CE SCRIPT NE RECONFIGURE PAS LE RÉSEAU. Sur ce Pi, eth0 et dnsmasq servent
# le BOOTP aux moniteurs cliniques : les toucher peut affecter du matériel en
# service. Ils sont VÉRIFIÉS, pas modifiés. Utiliser --configurer-reseau
# uniquement sur une machine neuve.
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

RACINE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR=/etc/mx800
CONFIG="$CONFIG_DIR/config.toml"
UNITE=/etc/systemd/system/mx800.service
DONNEES_DEFAUT=/var/lib/mx800
UTILISATEUR="${SUDO_USER:-$USER}"

STOCKAGE=""; SITE=""; SALLE=""; MAC=""; IP=""
FS=""; IDENTITES=""      # renseignés par l'étape Stockage (clé exFAT)
IDENTITES_OU=""          # cle | pi : où ranger la base nominative (clé exFAT)
# Courbes : COURBES vide = non décidé en ligne de commande (question posée, ou
# valeur par défaut). ONDES = liste séparée par des virgules, ou nom d'une
# sélection type ci-dessous.
COURBES=""; ONDES=""
# Sélections types. ABP et ART désignent la même pression artérielle sous deux
# libellés : le moniteur n'en expose qu'un, selon son réglage. On demande les
# deux, il retient celui qui existe.
ONDES_STANDARD="NOM_ECG_ELEC_POTL_II,NOM_PLETH,NOM_PRESS_BLD_ART_ABP,NOM_PRESS_BLD_ART,NOM_AWAY_CO2"
ONDES_CARDIAQUE="$ONDES_STANDARD,NOM_PRESS_BLD_ART_PULM,NOM_PRESS_BLD_VEN_CENT"
INTERACTIF=1; DEMARRER=1; CONFIGURER_RESEAU=0

V=$'\033[32m'; R=$'\033[31m'; O=$'\033[33m'; G=$'\033[90m'; N=$'\033[0m'
[ -t 1 ] || { V=""; R=""; O=""; G=""; N=""; }

etape() { printf '\n%s──  %s%s\n' "$N" "$1" "$N"; }
ok()    { printf '  [%sok%s] %s\n' "$V" "$N" "$1"; }
info()  { printf '       %s%s%s\n' "$G" "$1" "$N"; }
avert() { printf '  [%s! %s] %s\n' "$O" "$N" "$1"; }
echec() { printf '  [%sNON%s] %s\n' "$R" "$N" "$1" >&2; exit 1; }

usage() {
    sed -n '3,14p' "$0" | sed 's/^# \{0,1\}//'
    cat <<'AIDE'

Options :
  --stockage CHEMIN|/dev/sdXN  où écrire les données (défaut /var/lib/mx800) ;
                               disque ou clé USB en ext4 ou exfat
  --site NOM                   ex. HEGP
  --salle NOM                  FIGE la salle (par défaut : celle du moniteur)
  --mac XX:XX:XX:XX:XX:XX      FIGE le moniteur (par défaut : appris seul)
                               « auto » lève un épinglage existant : --salle auto --mac auto
  --ip A.B.C.D                 fige l'adresse du moniteur
  --courbes                    enregistrer les courbes (défaut d'une installation neuve)
  --sans-courbes               numerics seuls
  --ondes LISTE                standard, cardiaque, ou noms séparés par des virgules
                               (max 3 ECG et 8 non-ECG ; implique --courbes)
  --identites cle|pi           clé exFAT : noms des patients sur la clé, ou gardés
                               sur le Pi (défaut d'une installation neuve : pi)
  --non-interactif             ne rien demander ; échoue si une info manque
  --sans-demarrer              installer sans lancer le service
  --configurer-reseau          configurer eth0 et dnsmasq (MACHINE NEUVE UNIQUEMENT)
  -h, --help
AIDE
    exit 0
}

while [ $# -gt 0 ]; do
    case "$1" in
        --stockage) STOCKAGE="$2"; shift 2 ;;
        --site)     SITE="$2"; shift 2 ;;
        --salle)    SALLE="$2"; shift 2 ;;
        --mac)      MAC="$2"; shift 2 ;;
        --ip)       IP="$2"; shift 2 ;;
        --courbes)      [ "$COURBES" != 0 ] || echec "--courbes et --sans-courbes sont incompatibles"
                        COURBES=1; shift ;;
        --sans-courbes) [ "$COURBES" != 1 ] || echec "--courbes et --sans-courbes sont incompatibles"
                        COURBES=0; shift ;;
        --ondes)        ONDES="$2"; shift 2 ;;
        --identites)    case "$2" in cle|pi) IDENTITES_OU="$2" ;;
                            *) echec "--identites attend cle ou pi" ;; esac
                        shift 2 ;;
        --non-interactif) INTERACTIF=0; shift ;;
        --sans-demarrer)  DEMARRER=0; shift ;;
        --configurer-reseau) CONFIGURER_RESEAU=1; shift ;;
        -h|--help)  usage ;;
        *) echec "option inconnue : $1  (--help)" ;;
    esac
done
if [ -n "$ONDES" ]; then
    [ "$COURBES" != 0 ] || echec "--ondes et --sans-courbes sont incompatibles"
    COURBES=1
fi

demander() {   # demander <invite> <variable> [defaut]
    local invite="$1" var="$2" defaut="${3:-}" reponse
    [ -n "${!var}" ] && return 0
    [ "$INTERACTIF" = 0 ] && echec "$invite manquant (mode non interactif)"
    read -r -p "  $invite${defaut:+ [$defaut]} : " reponse
    printf -v "$var" '%s' "${reponse:-$defaut}"
    [ -n "${!var}" ] || echec "$invite est obligatoire"
}

# ── Prérequis ───────────────────────────────────────────────────────────────
etape "Prérequis"
[ "$(id -u)" -ne 0 ] || echec "ne pas lancer en root : le script appelle sudo au besoin"
command -v sudo >/dev/null || echec "sudo est requis"
# -n plutôt que -v : avec NOPASSWD, `sudo -v` demande quand même un mot de
# passe alors que les commandes passent sans.
sudo -n true 2>/dev/null || echec "sudo demande un mot de passe. Lancer depuis un terminal interactif, ou configurer NOPASSWD."
command -v python3 >/dev/null || echec "python3 introuvable"
PYVER=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' \
    || echec "Python $PYVER : 3.11 minimum (tomllib)"
ok "python3 $PYVER"

for fichier in mx800/service.py mx800/config.py outils/console.py \
               deploiement/mx800.service; do
    [ -f "$RACINE/$fichier" ] || echec "fichier manquant dans le dépôt : $fichier"
done
ok "dépôt complet"

etape "Dépendances Python"
if python3 -c 'import h5py, numpy' 2>/dev/null; then
    ok "h5py et numpy présents"
else
    info "installation de h5py et numpy (peut prendre plusieurs minutes sur ARM)"
    pip install h5py numpy --break-system-packages -q \
        || echec "installation de h5py/numpy impossible — les courbes en dépendent"
    ok "h5py et numpy installés"
fi

# ── Réseau : vérifié, pas modifié ───────────────────────────────────────────
etape "Réseau"
if [ "$CONFIGURER_RESEAU" = 1 ]; then
    # La configuration elle-même est dans deploiement/reseau.sh, que la
    # commande « mx800 reseau --configurer » appelle aussi après l'installation.
    bash "$RACINE/deploiement/reseau.sh" || exit 1
else
    if systemctl is-active --quiet dnsmasq; then
        ok "dnsmasq actif (non modifié)"
    else
        avert "dnsmasq inactif : le moniteur n'obtiendra pas d'adresse"
    fi
    if ip -4 addr show eth0 2>/dev/null | grep -q 'inet '; then
        ok "eth0 : $(ip -4 -brief addr show eth0 | awk '{print $3}') (non modifié)"
    else
        avert "eth0 sans adresse IPv4"
    fi
fi
NB_MONITEURS=$(grep -ci '00:09:fb' /var/lib/misc/dnsmasq.leases 2>/dev/null || true)
info "moniteurs Philips dans les baux : ${NB_MONITEURS:-0}"

# ── Stockage ────────────────────────────────────────────────────────────────
etape "Stockage"
if [ -z "$STOCKAGE" ] && [ "$INTERACTIF" = 1 ]; then
    echo "  Où écrire les données ?"
    echo
    LIBRE_SD=$(df -m --output=avail / | tail -1 | tr -d ' ')
    printf '    1) carte SD                %6s Mo libres   %s(recommandé — « mx800 copier » sort les données sur une clé)%s\n' \
           "$LIBRE_SD" "$G" "$N"
    # lsblk ne renseigne TRAN (usb) que sur le disque entier, jamais sur ses
    # partitions : filtrer les partitions sur TRAN ne trouvait donc aucun
    # disque USB. On part des disques USB, puis on liste leurs partitions (ou
    # le disque lui-même s'il porte directement un système de fichiers).
    mapfile -t DISQUES < <(lsblk -dnro NAME,TRAN 2>/dev/null | awk '$2=="usb" {print $1}' \
        | while read -r usb; do
              lsblk -nro NAME,SIZE,TYPE,FSTYPE "/dev/$usb" 2>/dev/null \
                  | awk '$3=="part" || ($3=="disk" && $4!="") {print $1" "$2" "($4==""?"non-formaté":$4)}'
          done)
    i=2
    for disque in "${DISQUES[@]}"; do
        printf '    %d) /dev/%-12s %8s  %s  %s\n' "$i" $disque
        i=$((i+1))
    done
    printf '    %d) autre chemin\n\n' "$i"
    read -r -p "  Choix [1] : " choix; choix="${choix:-1}"
    if [ "$choix" = "1" ]; then
        STOCKAGE="$DONNEES_DEFAUT"
    elif [ "$choix" = "$i" ]; then
        read -r -p "  Chemin absolu : " STOCKAGE
    else
        STOCKAGE="/dev/$(echo "${DISQUES[$((choix-2))]}" | awk '{print $1}')"
    fi
fi
STOCKAGE="${STOCKAGE:-$DONNEES_DEFAUT}"

if [[ "$STOCKAGE" == /dev/* ]]; then
    [ -b "$STOCKAGE" ] || echec "$STOCKAGE n'est pas un périphérique bloc"
    UUID=$(lsblk -no UUID "$STOCKAGE" | head -1)
    FS=$(lsblk -no FSTYPE "$STOCKAGE" | head -1)
    # Vérifié AVANT de toucher à fstab. Deux formats acceptés :
    #   ext4   robuste aux coupures de courant, illisible sous Windows ;
    #   exfat  lisible partout (clé relue sur un PC), mais sans journal ni
    #          permissions — d'où les précautions plus bas.
    case "$FS" in
        ext4)  OPTIONS_MONTAGE="defaults,noatime,nofail,x-systemd.device-timeout=10"; PASSE=2 ;;
        exfat) # Sans permissions propres, tout appartient à l'utilisateur du
               # service et reste fermé aux autres comptes du Pi.
               OPTIONS_MONTAGE="uid=$(id -u "$UTILISATEUR"),gid=$(id -g "$UTILISATEUR"),umask=077,noatime,nofail,x-systemd.device-timeout=10"
               # fsck.exfat répare au démarrage ce qu'une coupure a laissé.
               command -v fsck.exfat >/dev/null || sudo apt-get install -y exfatprogs -q >/dev/null 2>&1 || true
               if command -v fsck.exfat >/dev/null; then PASSE=2; else
                   PASSE=0; avert "exfatprogs absent : pas de vérification de la clé au démarrage"
               fi ;;
        *) echec "$STOCKAGE est en « ${FS:-non formaté} » : ext4 ou exfat attendu. Formater (EFFACE le disque) : sudo mkfs.exfat $STOCKAGE (lisible sous Windows) ou sudo mkfs.ext4 $STOCKAGE" ;;
    esac
    [ -n "$UUID" ] || echec "$STOCKAGE n'a pas d'UUID : le reformater"
    POINT=/mnt/mx800
    sudo mkdir -p "$POINT"
    if ! grep -q "UUID=$UUID" /etc/fstab; then
        sudo cp /etc/fstab "/etc/fstab.bak.$(date +%Y%m%d%H%M%S)"
        # nofail : un disque absent ne doit JAMAIS empêcher le Pi de démarrer.
        # Sur une machine sans écran dans un bloc, un boot bloqué en mode
        # maintenance serait pire que l'absence de données.
        echo "UUID=$UUID  $POINT  $FS  $OPTIONS_MONTAGE  0  $PASSE" \
            | sudo tee -a /etc/fstab >/dev/null
        ok "fstab complété (sauvegarde horodatée conservée)"
    else
        ok "fstab contient déjà ce disque"
    fi
    sudo systemctl daemon-reload
    mountpoint -q "$POINT" || sudo mount "$POINT" \
        || echec "montage de $POINT impossible — fstab restauré manuellement au besoin"
    mountpoint -q "$POINT" || echec "$POINT n'est pas monté après mount"
    ok "$STOCKAGE monté sur $POINT ($FS, UUID $UUID)"
    DONNEES="$POINT"
else
    DONNEES="$STOCKAGE"
    sudo mkdir -p "$DONNEES"
    UUID=$(findmnt -no UUID --target "$DONNEES" || true)
    # Un chemin donné à la main peut désigner la clé déjà montée (relance).
    FS=$(findmnt -no FSTYPE --target "$DONNEES" || true)
    ok "données sur $DONNEES"
fi
if [ "$FS" = "exfat" ]; then
    avert "clé exFAT : lisible sous Windows, mais fragile en cas de coupure de courant"
    info "avant de retirer la clé : éteindre le Pi (sudo poweroff)"
    # Où ranger identites.db (correspondance code de recherche <-> patient) ?
    # Sur une clé exFAT elle est lisible en clair par tout ordinateur où on
    # la branche : c'est une décision de l'utilisateur, pas du script.
    if [ -z "$IDENTITES_OU" ]; then
        # Réglage actuel : clé « identites » renseignée = sur le Pi ; config
        # existante sans elle = dans le volume, donc sur la clé.
        ACTUEL=pi
        if [ -f "$CONFIG" ] && [ -z "$(sudo grep -Po '^\s*identites\s*=\s*"\K[^"]+' "$CONFIG" | head -1 || true)" ]; then
            ACTUEL=cle
        fi
        if [ "$INTERACTIF" = 1 ]; then
            echo
            echo "  Où ranger les noms des patients (correspondance code de recherche <-> patient) ?"
            echo
            echo "    1) sur la clé   tout au même endroit ; lisible par quiconque a la clé"
            echo "    2) sur le Pi    la clé ne porte que des données pseudonymisées"
            echo
            defaut=2; [ "$ACTUEL" = cle ] && defaut=1
            read -r -p "  Choix [$defaut] : " choix
            case "${choix:-$defaut}" in
                1) IDENTITES_OU=cle ;;
                2) IDENTITES_OU=pi ;;
                *) echec "choix invalide : $choix" ;;
            esac
        else
            IDENTITES_OU="$ACTUEL"
        fi
    fi
    if [ "$IDENTITES_OU" = pi ]; then
        IDENTITES="$DONNEES_DEFAUT/identites.db"
        sudo mkdir -p "$DONNEES_DEFAUT"
        sudo chown "$UTILISATEUR:$UTILISATEUR" "$DONNEES_DEFAUT"; sudo chmod 700 "$DONNEES_DEFAUT"
        ok "noms des patients gardés sur le Pi : $IDENTITES"
        AUTRE="$DONNEES/identites.db"
    else
        ok "noms des patients sur la clé : $DONNEES/identites.db"
        avert "la clé contient des données nominatives en clair : ne pas la laisser traîner"
        AUTRE="$DONNEES_DEFAUT/identites.db"
    fi
    # Une base nominative à l'autre emplacement n'est ni déplacée ni fusionnée.
    if sudo test -s "$AUTRE"; then
        avert "une base nominative existe aussi dans $AUTRE : elle n'est pas déplacée"
    fi
fi
# exFAT n'a pas de propriétaire : il est fixé au montage (uid=, gid=).
[ "${FS:-}" = "exfat" ] || sudo chown -R "$UTILISATEUR:$UTILISATEUR" "$DONNEES"
sudo -u "$UTILISATEUR" mkdir -p "$DONNEES/courbes" "$DONNEES/exports"

# ── Identité du site ────────────────────────────────────────────────────────
etape "Identité de ce Pi"
SALLE_DEMANDEE="$SALLE"; MAC_DEMANDEE="$MAC"
if [ -f "$CONFIG" ]; then
    SITE="${SITE:-$(grep -Po '^\s*nom\s*=\s*"\K[^"]+' "$CONFIG" | head -1 || true)}"
    SALLE="${SALLE:-$(grep -Po '^\s*salle\s*=\s*"\K[^"]+' "$CONFIG" | head -1 || true)}"
    MAC="${MAC:-$(grep -Po '^\s*mac\s*=\s*"\K[^"]+' "$CONFIG" | head -1 || true)}"
    info "valeurs reprises de la configuration existante"
fi
# « auto » efface un épinglage hérité de la configuration existante.
[ "$SALLE" = auto ] && SALLE=""
[ "$MAC" = auto ] && MAC=""
demander "Site" SITE "HEGP"
# Un épinglage repris d'une ancienne configuration se reconduit à chaque
# relance sans que personne l'ait redemandé, et le Pi refuse alors tout autre
# moniteur. On le dit, et on propose de le lever.
if [ "$INTERACTIF" = 1 ] && [ -z "$SALLE_DEMANDEE$MAC_DEMANDEE" ] && [ -n "$SALLE$MAC" ]; then
    avert "ce Pi est épinglé : salle ${SALLE:-<auto>}, moniteur ${MAC:-<auto>}"
    info "branché sur un autre moniteur, il refusera d'enregistrer"
    read -r -p "  Le laisser s'adapter au moniteur branché ? [o/N] : " reponse
    case "${reponse:-N}" in [OoYy]*) SALLE=""; MAC="" ;; esac
fi
# La salle et le moniteur ne sont PAS demandés : le Pi les apprend. Il est
# conçu pour être débranché d'une salle et rebranché dans une autre. Les
# renseigner (--salle, --mac) épingle et désactive l'apprentissage.
if [ -n "$SALLE" ] || [ -n "$MAC" ] || [ -n "$IP" ]; then
    ok "site $SITE — épinglé : salle ${SALLE:-<auto>}, moniteur ${MAC:-${IP:-<auto>}}"
    avert "valeurs figées : ce Pi ne s'adaptera pas à un changement de salle"
else
    ok "site $SITE — salle et moniteur appris automatiquement"
    if [ -s /var/lib/misc/dnsmasq.leases ]; then
        info "appareils Philips actuellement visibles :"
        grep -i '00:09:fb' /var/lib/misc/dnsmasq.leases \
            | awk '{printf "         %s  %s\n", $2, $3}'
    fi
fi

# ── Courbes ─────────────────────────────────────────────────────────────────
# Activées par défaut sur une installation neuve. Sur un Pi déjà configuré,
# le réglage existant est proposé (interactif) ou conservé (non interactif) :
# relancer le script ne doit jamais changer en silence ce qui est enregistré.
etape "Courbes"
COURBES_EXIST=""; ONDES_EXIST=""
if [ -f "$CONFIG" ]; then
    read -r COURBES_EXIST ONDES_EXIST < <(sudo cat "$CONFIG" | python3 -c '
import sys, tomllib
try:
    acq = tomllib.loads(sys.stdin.read()).get("acquisition", {})
except Exception:
    sys.exit(0)
print("1" if acq.get("courbes") else "0", ",".join(acq.get("ondes") or []))') || true
fi

if [ -z "$COURBES" ]; then
    if [ "$INTERACTIF" = 1 ]; then
        echo "  Les courbes (ECG, pléthysmographie, pressions invasives, CO2) s'ajoutent"
        echo "  aux numerics : de l'ordre de 6 à 10 Mo/h au lieu de 1 Mo/h."
        case "$COURBES_EXIST" in
            1) info "actuellement : activées" ;;
            0) info "actuellement : désactivées" ;;
        esac
        read -r -p "  Enregistrer les courbes ? [O/n] : " reponse
        case "${reponse:-O}" in
            [OoYy]*) COURBES=1 ;;
            [Nn]*)   COURBES=0 ;;
            *) echec "réponse attendue : O ou n" ;;
        esac
    elif [ -n "$COURBES_EXIST" ]; then
        COURBES="$COURBES_EXIST"
        info "réglage de la configuration existante conservé (--courbes / --sans-courbes pour le changer)"
    else
        COURBES=1
    fi
fi

if [ "$COURBES" = 1 ]; then
    if [ -z "$ONDES" ] && [ "$INTERACTIF" = 1 ]; then
        echo
        echo "  Quelles courbes ?"
        echo
        echo "    1) standard             ECG II, pléth, pression artérielle, CO2"
        echo "    2) chirurgie cardiaque  standard + artère pulmonaire + PVC"
        echo "    3) liste personnalisée"
        defaut=2
        if [ -n "$ONDES_EXIST" ]; then
            echo "    4) conserver la sélection actuelle"
            info "   ${ONDES_EXIST//,/, }"
            defaut=4
        fi
        echo
        read -r -p "  Choix [$defaut] : " choix; choix="${choix:-$defaut}"
        case "$choix" in
            1) ONDES=standard ;;
            2) ONDES=cardiaque ;;
            3) info "noms valides : mx800/protocole/ondes.py (ex. NOM_ECG_ELEC_POTL_II,NOM_PLETH)"
               read -r -p "  Ondes, séparées par des virgules : " ONDES
               [ -n "$ONDES" ] || echec "aucune onde saisie" ;;
            4) [ -n "$ONDES_EXIST" ] || echec "choix invalide : $choix"
               ONDES="$ONDES_EXIST" ;;
            *) echec "choix invalide : $choix" ;;
        esac
    fi
    ONDES="${ONDES:-${ONDES_EXIST:-cardiaque}}"
    case "$ONDES" in
        standard)  ONDES="$ONDES_STANDARD" ;;
        cardiaque) ONDES="$ONDES_CARDIAQUE" ;;
    esac
    # Contrôle AVANT d'écrire la configuration : une onde inconnue ou en excès
    # doit arrêter l'installation ici, pas laisser un service qui ne démarre plus.
    ONDES_TOML=$(PYTHONPATH="$RACINE" python3 - "$ONDES" <<'PYEOF'
import sys
from mx800.protocole import ondes as CAT
noms = [n for n in sys.argv[1].replace(',', ' ').split() if n]
problemes = [f"onde inconnue : {n!r}" for n in noms if CAT.onde(n) is None]
if not noms:
    problemes.append("aucune onde")
if not problemes:
    problemes = CAT.verifier_selection([CAT.onde(n).physio_id for n in noms])
if problemes:
    for p in problemes:
        print(f"       {p}", file=sys.stderr)
    print("       noms valides : mx800/protocole/ondes.py", file=sys.stderr)
    sys.exit(1)
print('[' + ', '.join(f'"{n}"' for n in noms) + ']')
PYEOF
    ) || echec "sélection d'ondes invalide"
    COURBES_TOML=true
    NB_ONDES=$(grep -o '"' <<<"$ONDES_TOML" | wc -l); NB_ONDES=$((NB_ONDES / 2))
    ok "courbes activées — $NB_ONDES ondes demandées"
    info "${ONDES//,/, }"
    info "une onde que le moniteur n'expose pas (capteur absent, autre libellé) est"
    info "ignorée par lui et signalée dans le journal ; les autres sont enregistrées"
    if findmnt -no SOURCE --target "$DONNEES" 2>/dev/null | grep -q mmcblk; then
        info "données sur la carte SD : de l'ordre de 10 Mo par heure d'intervention ;"
        info "« mx800 copier --supprimer » les transfère sur une clé USB et libère la carte"
    fi
else
    COURBES_TOML=false; ONDES_TOML="[]"
    ok "courbes désactivées — numerics seuls"
    info "pour les activer plus tard : ./install.sh --courbes"
fi

# ── Configuration ───────────────────────────────────────────────────────────
etape "Configuration"
sudo mkdir -p "$CONFIG_DIR"
if [ -f "$CONFIG" ]; then
    sudo cp "$CONFIG" "$CONFIG.bak.$(date +%Y%m%d%H%M%S)"
    info "configuration existante sauvegardée"
fi
sudo tee "$CONFIG" >/dev/null <<CONFEOF
# Configuration du service d'acquisition MX800 — générée par install.sh.
# Modifier puis : sudo systemctl restart mx800.service
# Toute clé inconnue empêche le démarrage : c'est voulu, une faute de frappe
# ne doit pas passer pour un réglage pris en compte.

[site]
nom = "$SITE"
# salle vide = celle qu'annonce le moniteur (son étiquette de lit), ou
# MON-<MAC> s'il n'en annonce pas. Ne renseigner que pour forcer un nom.
salle = "$SALLE"

[moniteur]
# mac vide = le moniteur est appris au démarrage : le Pi essaie une fois
# chaque appareil Philips visible et retient celui qui accepte une
# association Data Export. L'appairage est mémorisé dans moniteur.json et
# n'est refait que si ce moniteur disparaît du segment.
# Renseigner mac pour épingler un appareil précis.
mac = "$MAC"
ip  = "$IP"
appairage_auto = true
# bed_label non vide = l'étiquette est VÉRIFIÉE à chaque association et une
# discordance fait refuser l'enregistrement. Vide, elle est simplement suivie.
bed_label = "$SALLE"
verifier_bed_label = true

[acquisition]
periode_numerics_s       = 1.0
periode_demographiques_s = 30.0
intervention_auto        = true
refuser_mode_demo        = true
# Courbes : true ET une liste d'ondes, ou false ET une liste vide.
# Maximum 3 ECG et 8 non-ECG. Une onde que le moniteur n'expose pas est ignorée
# par lui et signalée dans le journal. Noms valides : mx800/protocole/ondes.py.
# Réglage aussi possible par ./install.sh --courbes | --sans-courbes | --ondes.
courbes = $COURBES_TOML
ondes   = $ONDES_TOML
mtu     = 1364

[surveillance]
silence_donnees_s   = 60
echecs_avant_alerte = 3
seuil_disque_mo     = 2000

[stockage]
chemin = "$DONNEES"
uuid   = "${UUID:-}"
# identites vide = identites.db dans le volume de données. Renseigné quand ce
# volume est une clé exFAT et qu'on a choisi de garder les noms sur le Pi
# (./install.sh --identites cle|pi).
identites = "$IDENTITES"

[etat]
chemin = "/run/mx800/status.json"

[journal]
niveau = "INFO"
CONFEOF
sudo chown root:"$UTILISATEUR" "$CONFIG"; sudo chmod 640 "$CONFIG"
ok "$CONFIG"

# Marqueur de volume : la parade au « SSD non monté, on écrit sur la carte SD »
sudo -u "$UTILISATEUR" PYTHONPATH="$RACINE" python3 - "$DONNEES" "$SITE" "$SALLE" <<'PYEOF'
import sys
from mx800.stockage import volume as V
info = V.ecrire_marqueur(sys.argv[1], site=sys.argv[2], salle=sys.argv[3])
print(f"       marqueur posé : UUID {info['uuid'] or 'n/a'} sur {info['peripherique'] or '?'}")
PYEOF
ok "marqueur de volume posé"

PYTHONPATH="$RACINE" python3 -c "
from mx800 import config
config.charger('$CONFIG')
print('       configuration validée')" || echec "configuration invalide"

# ── Commande mx800 ──────────────────────────────────────────────────────────
etape "Commande mx800"
sudo tee /usr/local/bin/mx800 >/dev/null <<LANCEUR
#!/usr/bin/env bash
# Lanceur de la commande d'exploitation. Généré par install.sh.
exec env PYTHONPATH="$RACINE" python3 -m outils.console --config "$CONFIG" "\$@"
LANCEUR
sudo chmod 755 /usr/local/bin/mx800
ok "/usr/local/bin/mx800"

# ── Service ─────────────────────────────────────────────────────────────────
etape "Service systemd"
sed -e "s|__UTILISATEUR__|$UTILISATEUR|g" -e "s|__RACINE__|$RACINE|g" \
    -e "s|__DONNEES__|$DONNEES|g"        -e "s|__CONFIG__|$CONFIG|g" \
    -e "s|__IDENTITES_DIR__|$(dirname "${IDENTITES:-$DONNEES/identites.db}")|g" \
    "$RACINE/deploiement/mx800.service" | sudo tee "$UNITE" >/dev/null
sudo systemctl daemon-reload
ok "$UNITE"

# Ancien service : arrêté et désactivé, jamais supprimé.
if systemctl list-unit-files 2>/dev/null | grep -q '^mx800capture.service'; then
    sudo systemctl disable --now mx800capture.service 2>/dev/null || true
    avert "ancien mx800capture.service arrêté et désactivé (fichier conservé)"
fi

sudo systemctl enable mx800.service >/dev/null
if [ "$DEMARRER" = 1 ]; then
    sudo systemctl restart mx800.service
    sleep 4
    if systemctl is-active --quiet mx800.service; then
        ok "service démarré"
    else
        echo; sudo journalctl -u mx800.service -n 25 --no-pager
        echec "le service n'a pas démarré (journal ci-dessus)"
    fi
else
    ok "service installé, non démarré (--sans-demarrer)"
fi

# ── Vérification ────────────────────────────────────────────────────────────
etape "Vérification"
if [ "$DEMARRER" = 1 ]; then
    sleep 2
    PYTHONPATH="$RACINE" python3 -m outils.console --config "$CONFIG" etat || true
fi

cat <<FIN

${V}Installation terminée.${N}

  État              mx800 etat            (ou http://$(hostname -I | awk '{print $1}'):8080/)
  Diagnostic        mx800 diagnostiquer
  Interventions     mx800 interventions
  Export            mx800 exporter CODE
  Clé USB           mx800 copier          (brancher une clé exFAT ; --supprimer libère le Pi)
  Réglages          mx800 salle [NOM|auto]   ·   mx800 reseau
  Journal           journalctl -u mx800.service -f

  Configuration     sudo nano $CONFIG
                    sudo systemctl restart mx800.service
  Données           $DONNEES
  Courbes           $([ "$COURBES" = 1 ] && echo "activées ($NB_ONDES ondes)" || echo "désactivées")  —  ./install.sh --courbes | --sans-courbes | --ondes LISTE

FIN
