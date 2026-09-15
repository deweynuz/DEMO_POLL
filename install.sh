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
  --stockage CHEMIN|/dev/sdXN  où écrire les données (défaut /var/lib/mx800)
  --site NOM                   ex. HEGP
  --salle NOM                  FIGE la salle (par défaut : celle du moniteur)
  --mac XX:XX:XX:XX:XX:XX      FIGE le moniteur (par défaut : appris seul)
  --ip A.B.C.D                 fige l'adresse du moniteur
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
        --non-interactif) INTERACTIF=0; shift ;;
        --sans-demarrer)  DEMARRER=0; shift ;;
        --configurer-reseau) CONFIGURER_RESEAU=1; shift ;;
        -h|--help)  usage ;;
        *) echec "option inconnue : $1  (--help)" ;;
    esac
done

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
    avert "reconfiguration réseau demandée — à ne faire que sur une machine neuve"
    read -r -p "  Taper OUI pour confirmer : " confirmation
    [ "$confirmation" = "OUI" ] || echec "reconfiguration réseau annulée"

    # 1. eth0 en adresse fixe. Le Pi est la passerelle du segment moniteur ;
    #    sans adresse statique, dnsmasq n'a rien à servir.
    #    Toujours le MÊME profil, lié à eth0 par son nom d'interface : se fier au
    #    profil actif échoue quand le câble n'est pas branché, et laissait le
    #    profil DHCP d'origine (« netplan-eth0 », « Wired connection 1 ») se
    #    disputer eth0 avec le nôtre.
    command -v nmcli >/dev/null || echec "nmcli introuvable (NetworkManager requis)"
    CON="mx800-eth0"
    nmcli -t -f NAME con show | grep -qxF "$CON" \
        || sudo nmcli con add type ethernet ifname eth0 con-name "$CON" >/dev/null
    sudo nmcli con mod "$CON" connection.interface-name eth0 \
        ipv4.method manual ipv4.addresses 192.168.100.1/24 \
        ipv4.gateway "" ipv4.dns "" ipv6.method disabled \
        connection.autoconnect yes connection.autoconnect-priority 100
    # Les autres profils Ethernet pouvant prendre eth0 (interface eth0 ou non
    # précisée) : autoconnexion désactivée, pas supprimés. Le Wi-Fi n'est pas
    # concerné — c'est souvent lui qui porte la session SSH.
    nmcli -t -f NAME,TYPE con show | sed 's/\\:/\x1f/g' | while IFS=: read -r nom type; do
        nom=${nom//$'\x1f'/:}
        [ "$type" = "802-3-ethernet" ] && [ "$nom" != "$CON" ] || continue
        ifname=$(nmcli -g connection.interface-name con show "$nom")
        [ -z "$ifname" ] || [ "$ifname" = "eth0" ] || continue
        sudo nmcli con mod "$nom" connection.autoconnect no
        info "profil concurrent « $nom » : autoconnexion désactivée"
    done
    sudo nmcli con up "$CON" >/dev/null 2>&1 || true
    if [ "$(cat /sys/class/net/eth0/carrier 2>/dev/null)" = "1" ]; then
        ip -4 addr show eth0 | grep -q '192\.168\.100\.1/' \
            || echec "eth0 n'a pas pris l'adresse 192.168.100.1"
        ok "eth0 : 192.168.100.1/24"
    else
        # Sans câble, NetworkManager n'applique pas l'adresse : c'est normal, il
        # le fera au branchement. On vérifie le profil, pas l'interface.
        nmcli -g ipv4.addresses con show "$CON" | grep -q '192\.168\.100\.1/24' \
            || echec "le profil $CON n'a pas l'adresse 192.168.100.1/24"
        ok "eth0 : 192.168.100.1/24 configuré"
        avert "câble eth0 non branché : l'adresse sera appliquée au branchement du moniteur"
    fi

    # 2. dnsmasq : DHCP et surtout BOOTP pour les moniteurs Philips
    command -v dnsmasq >/dev/null || sudo apt-get install -y dnsmasq -q \
        || echec "installation de dnsmasq impossible"
    [ -f /etc/dnsmasq.d/mx800.conf ] && sudo cp /etc/dnsmasq.d/mx800.conf \
        "/etc/dnsmasq.d/mx800.conf.bak.$(date +%Y%m%d%H%M%S)"
    sudo tee /etc/dnsmasq.d/mx800.conf >/dev/null <<'DNS'
port=0
interface=eth0
# bind-dynamic et non bind-interfaces : dnsmasq démarre même si eth0 n'a pas
# encore d'adresse (Pi démarré sans câble), et la prend en compte au branchement.
# Avec bind-interfaces, il échouait au boot sur « unknown interface eth0 ».
bind-dynamic
dhcp-authoritative
log-dhcp
dhcp-mac=philips,00:09:FB:*:*:*
dhcp-range=tag:philips,192.168.100.31,192.168.100.40,255.255.255.0,infinite
# ESSENTIEL — le moniteur demande son adresse en BOOTP, pas en DHCP.
bootp-dynamic
DNS
    # Filet de sécurité : l'unité Debian a Restart=no, un échec au démarrage
    # laissait le moniteur sans adresse jusqu'à intervention manuelle.
    sudo mkdir -p /etc/systemd/system/dnsmasq.service.d
    sudo tee /etc/systemd/system/dnsmasq.service.d/mx800.conf >/dev/null <<'UNIT'
[Unit]
After=NetworkManager.service
[Service]
Restart=on-failure
RestartSec=5
UNIT
    sudo systemctl daemon-reload
    sudo systemctl enable dnsmasq >/dev/null 2>&1 || true
    sudo systemctl restart dnsmasq
    systemctl is-active --quiet dnsmasq \
        || echec "dnsmasq n'a pas démarré : journalctl -u dnsmasq -n 30"
    ok "dnsmasq actif — DHCP/BOOTP servi sur eth0 (192.168.100.31-40)"
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
    printf '    1) carte SD                %6s Mo libres   %s(usure à surveiller)%s\n' \
           "$LIBRE_SD" "$G" "$N"
    mapfile -t DISQUES < <(lsblk -rno NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,TRAN 2>/dev/null \
        | awk '$3=="part" && $6=="usb" {print $1" "$2" "$4" "$5}')
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
    [ -n "$UUID" ] || echec "$STOCKAGE n'a pas d'UUID : formater d'abord (mkfs.ext4)"
    POINT=/mnt/mx800
    sudo mkdir -p "$POINT"
    if ! grep -q "UUID=$UUID" /etc/fstab; then
        sudo cp /etc/fstab "/etc/fstab.bak.$(date +%Y%m%d%H%M%S)"
        # nofail : un disque absent ne doit JAMAIS empêcher le Pi de démarrer.
        # Sur une machine sans écran dans un bloc, un boot bloqué en mode
        # maintenance serait pire que l'absence de données.
        echo "UUID=$UUID  $POINT  ext4  defaults,noatime,nofail,x-systemd.device-timeout=10  0  2" \
            | sudo tee -a /etc/fstab >/dev/null
        ok "fstab complété (sauvegarde horodatée conservée)"
    else
        ok "fstab contient déjà ce disque"
    fi
    sudo systemctl daemon-reload
    mountpoint -q "$POINT" || sudo mount "$POINT" \
        || echec "montage de $POINT impossible — fstab restauré manuellement au besoin"
    mountpoint -q "$POINT" || echec "$POINT n'est pas monté après mount"
    ok "$STOCKAGE monté sur $POINT (UUID $UUID)"
    DONNEES="$POINT"
else
    DONNEES="$STOCKAGE"
    sudo mkdir -p "$DONNEES"
    UUID=$(findmnt -no UUID --target "$DONNEES" || true)
    ok "données sur $DONNEES"
fi
sudo chown -R "$UTILISATEUR:$UTILISATEUR" "$DONNEES"
sudo -u "$UTILISATEUR" mkdir -p "$DONNEES/courbes" "$DONNEES/exports"

# ── Identité du site ────────────────────────────────────────────────────────
etape "Identité de ce Pi"
if [ -f "$CONFIG" ]; then
    SITE="${SITE:-$(grep -Po '^\s*nom\s*=\s*"\K[^"]+' "$CONFIG" | head -1 || true)}"
    SALLE="${SALLE:-$(grep -Po '^\s*salle\s*=\s*"\K[^"]+' "$CONFIG" | head -1 || true)}"
    MAC="${MAC:-$(grep -Po '^\s*mac\s*=\s*"\K[^"]+' "$CONFIG" | head -1 || true)}"
    info "valeurs reprises de la configuration existante"
fi
demander "Site" SITE "HEGP"
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
# Courbes : mettre à true ET lister les ondes. Maximum 3 ECG et 8 non-ECG.
courbes = false
ondes   = []
mtu     = 1364

[surveillance]
silence_donnees_s   = 60
echecs_avant_alerte = 3
seuil_disque_mo     = 2000

[stockage]
chemin = "$DONNEES"
uuid   = "${UUID:-}"

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
  Journal           journalctl -u mx800.service -f

  Configuration     sudo nano $CONFIG
                    sudo systemctl restart mx800.service
  Données           $DONNEES

FIN
