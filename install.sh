#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════════
# install.sh — Installation automatique système d'acquisition MX800 HEGP
# Usage : curl -sSL https://raw.githubusercontent.com/deweynuz/DEMO_POLL/main/install.sh | bash
# ═══════════════════════════════════════════════════════════════════════════════

set -e

# ── Couleurs ─────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

ok()   { echo -e "${GREEN}✓${NC} $1"; }
info() { echo -e "${BLUE}→${NC} $1"; }
warn() { echo -e "${YELLOW}⚠${NC} $1"; }
err()  { echo -e "${RED}✗${NC} $1"; exit 1; }
step() { echo -e "\n${BOLD}${BLUE}══ $1 ══${NC}"; }

# ── Bannière ─────────────────────────────────────────────────────────────────
clear
echo -e "${BOLD}"
echo "  ╔═══════════════════════════════════════════════════╗"
echo "  ║   Système d'acquisition MX800 — HEGP             ║"
echo "  ║   Installation automatique Raspberry Pi           ║"
echo "  ╚═══════════════════════════════════════════════════╝"
echo -e "${NC}"
echo ""

# ── Vérifications préalables ─────────────────────────────────────────────────
step "Vérifications"

if [ "$EUID" -eq 0 ]; then
    err "Ne pas lancer ce script en root. Lancez-le en tant qu'utilisateur normal."
fi

if ! command -v python3 &>/dev/null; then
    err "Python3 non trouvé. Installez Raspberry Pi OS Bookworm."
fi

PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
ok "Python $PYTHON_VERSION détecté"

if ! command -v git &>/dev/null; then
    info "Installation de git..."
    sudo apt-get install -y git -q
fi
ok "Git disponible"

# ── Questions interactives ────────────────────────────────────────────────────
step "Configuration"

echo ""
echo -e "${BOLD}Détection du moniteur Philips MX800${NC}"
echo -e "  Laissez vide pour la découverte automatique (recommandé)."
echo -e "  Le moniteur est identifié par son adresse MAC Philips (00:09:FB)."
read -p "  IP moniteur [auto] : " MONITOR_IP
if [ -z "$MONITOR_IP" ]; then
    CONFIG_MONITOR_IP="auto"
    MONITOR_IP="192.168.100.31"   # utilisée seulement pour dériver le sous-réseau
    AUTO_DISCOVERY=1
else
    CONFIG_MONITOR_IP="$MONITOR_IP"
    AUTO_DISCOVERY=0
fi

echo ""
echo -e "${BOLD}Dossier d'installation ?${NC}"
echo -e "  Les donnees patients (base SQLite, CSV) y seront stockees."
read -p "  Dossier [/home/$USER] : " INSTALL_DIR
INSTALL_DIR=${INSTALL_DIR:-/home/$USER}
INSTALL_DIR="${INSTALL_DIR%/}"

# Les donnees ne doivent JAMAIS atterrir dans un depot git : elles seraient
# exposees au premier "git add ." (noms de patients, identifiants hospitaliers).
if [ -d "$INSTALL_DIR/.git" ] || [ "$(basename "$INSTALL_DIR")" = "DEMO_POLL" ]; then
    echo ""
    err "« $INSTALL_DIR » est un depot git (ou le dossier du depot).
    Y installer les donnees patients risquerait de les publier sur GitHub.
    Choisissez un dossier hors du depot, par exemple /home/$USER."
fi

# ── Configuration réseau ──────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}Configuration réseau (eth0)${NC}"
echo -e "  Le RPi doit avoir une IP fixe sur le sous-réseau du moniteur,"
echo -e "  et servir le BOOTP/DHCP pour que le MX800 obtienne son adresse."
echo ""

# Détecte l'interface utilisée pour la connexion SSH courante
SSH_IFACE=""
if [ -n "$SSH_CONNECTION" ]; then
    SSH_SERVER_IP=$(echo "$SSH_CONNECTION" | awk '{print $3}')
    SSH_IFACE=$(ip -o addr show | awk -v ip="$SSH_SERVER_IP" '$4 ~ ip"/" {print $2}' | head -1)
fi

if [ "$SSH_IFACE" = "eth0" ]; then
    warn "Votre session SSH passe par eth0 !"
    warn "Reconfigurer eth0 va couper cette connexion."
    warn "Utilisez plutôt le WiFi ou Pi Connect pour cette installation."
    echo ""
    read -p "  Configurer quand même le réseau ? [o/N] : " SETUP_NET
    SETUP_NET=${SETUP_NET:-N}
else
    read -p "  Configurer le réseau automatiquement ? [O/n] : " SETUP_NET
    SETUP_NET=${SETUP_NET:-O}
fi

if [[ "$SETUP_NET" =~ ^[OoYy]$ ]]; then
    NET_CONFIG=1
    # Dérive l'IP du RPi et le sous-réseau depuis l'IP du moniteur
    SUBNET=$(echo "$MONITOR_IP" | cut -d. -f1-3)
    DEFAULT_RPI_IP="${SUBNET}.1"
    echo ""
    echo -e "${BOLD}  IP du Raspberry Pi sur ce sous-réseau ?${NC}"
    read -p "    IP RPi [$DEFAULT_RPI_IP] : " RPI_IP
    RPI_IP=${RPI_IP:-$DEFAULT_RPI_IP}
    DHCP_START="${SUBNET}.31"
    DHCP_END="${SUBNET}.40"
else
    NET_CONFIG=0
fi

echo ""
echo -e "${BOLD}Récapitulatif :${NC}"
echo "  Utilisateur    : $USER"
if [ "$AUTO_DISCOVERY" = "1" ]; then
    echo "  Moniteur       : découverte automatique (MAC Philips)"
else
    echo "  IP moniteur    : $MONITOR_IP"
fi
echo "  Dossier        : $INSTALL_DIR"
echo "  Repository     : https://github.com/deweynuz/DEMO_POLL"
if [ "$NET_CONFIG" = "1" ]; then
    echo "  IP du RPi      : $RPI_IP/24 (eth0)"
    echo "  Plage DHCP     : $DHCP_START — $DHCP_END"
else
    echo "  Réseau         : non modifié"
fi
echo ""
read -p "Confirmer l'installation ? [O/n] : " CONFIRM
CONFIRM=${CONFIRM:-O}
if [[ ! "$CONFIRM" =~ ^[OoYy]$ ]]; then
    echo "Installation annulée."
    exit 0
fi

# ── Mise à jour système ───────────────────────────────────────────────────────
step "Mise à jour du système"
info "Mise à jour des paquets (peut prendre quelques minutes)..."
sudo apt-get update -q
sudo apt-get install -y sqlite3 python3-pip -q
ok "Système à jour"

# ── Configuration réseau eth0 + dnsmasq ──────────────────────────────────────
if [ "$NET_CONFIG" = "1" ]; then
    step "Configuration réseau"

    # 1. IP statique sur eth0 via NetworkManager
    info "Configuration IP statique $RPI_IP/24 sur eth0..."

    CON_NAME=$(nmcli -t -f NAME,DEVICE con show | awk -F: '$2=="eth0" {print $1; exit}')
    if [ -z "$CON_NAME" ]; then
        CON_NAME="mx800-lan"
        sudo nmcli con add type ethernet ifname eth0 con-name "$CON_NAME" &>/dev/null
    fi

    sudo nmcli con mod "$CON_NAME" \
        ipv4.method manual \
        ipv4.addresses "$RPI_IP/24" \
        ipv4.gateway "" \
        ipv4.dns "" \
        ipv4.never-default yes \
        connection.autoconnect yes
    sudo nmcli con up "$CON_NAME" &>/dev/null || true
    ok "eth0 configuré : $RPI_IP/24 (connexion « $CON_NAME »)"

    # 2. dnsmasq — BOOTP + DHCP pour le moniteur
    info "Installation de dnsmasq..."
    sudo apt-get install -y dnsmasq -q
    sudo systemctl stop dnsmasq &>/dev/null || true

    # Sauvegarde de la conf existante
    if [ -f /etc/dnsmasq.conf ] && [ ! -f /etc/dnsmasq.conf.orig ]; then
        sudo cp /etc/dnsmasq.conf /etc/dnsmasq.conf.orig
    fi

    # Sauvegarde d'une conf mx800 existante avant écrasement
    if [ -f /etc/dnsmasq.d/mx800.conf ]; then
        sudo cp /etc/dnsmasq.d/mx800.conf "/etc/dnsmasq.d/mx800.conf.bak.$(date +%Y%m%d%H%M%S)"
        info "Ancienne conf dnsmasq sauvegardée (.bak)"
    fi

    sudo tee /etc/dnsmasq.d/mx800.conf > /dev/null << DNSMASQEOF
# Configuration BOOTP pour moniteur Philips — généré par install.sh
# Ne pas éditer manuellement.

port=0
interface=eth0
bind-interfaces
except-interface=lo
dhcp-authoritative
log-dhcp

# Tout equipement Philips Patient Monitoring (OUI 00:09:FB) recoit le tag philips
dhcp-mac=philips,00:09:FB:*:*:*

# Plage reservee aux moniteurs Philips.
# Bail infini : le BOOTP ne renouvelle pas, l'adresse doit rester stable.
dhcp-range=tag:philips,$DHCP_START,$DHCP_END,255.255.255.0,infinite

# ESSENTIEL — Le moniteur demande son adresse en BOOTP, pas en DHCP.
# Sans bootp-dynamic, dnsmasq journalise "no address configured" et le
# moniteur ne recoit jamais d'adresse. NE PAS SUPPRIMER.
bootp-dynamic
DNSMASQEOF
    sudo systemctl enable dnsmasq &>/dev/null
    sudo systemctl restart dnsmasq
    if systemctl is-active --quiet dnsmasq; then
        ok "dnsmasq actif — DHCP/BOOTP servi sur eth0 ($DHCP_START—$DHCP_END)"
    else
        warn "dnsmasq n'a pas démarré. Vérifiez : journalctl -u dnsmasq -n 30"
    fi
fi

# ── Dépendances Python ────────────────────────────────────────────────────────
step "Dépendances Python"
info "Installation h5py et numpy (waveforms, optionnel)..."
pip install h5py numpy --break-system-packages -q 2>/dev/null && ok "h5py + numpy installés" || warn "h5py non installé (waveforms désactivés)"

# ── Clonage / mise à jour GitHub ─────────────────────────────────────────────
step "Téléchargement des scripts"

REPO_DIR="$INSTALL_DIR/DEMO_POLL"

if [ -d "$REPO_DIR/.git" ]; then
    info "Repository existant détecté, mise à jour..."
    cd "$REPO_DIR"
    git pull -q
    ok "Repository mis à jour"
else
    info "Clonage du repository..."
    git clone https://github.com/deweynuz/DEMO_POLL.git "$REPO_DIR" -q
    ok "Repository cloné dans $REPO_DIR"
fi

# ── Copie des fichiers ────────────────────────────────────────────────────────
step "Installation des fichiers"

cp "$REPO_DIR/mx800_capture.py" "$INSTALL_DIR/mx800_capture.py"
ok "mx800_capture.py installé"

if [ -f "$REPO_DIR/discover_monitor.py" ]; then
    cp "$REPO_DIR/discover_monitor.py" "$INSTALL_DIR/discover_monitor.py"
    ok "discover_monitor.py installé (découverte automatique)"
fi

# config.json genere depuis le modele versionne du depot
if [ ! -f "$REPO_DIR/config.json" ]; then
    err "config.json introuvable dans le depot."
fi

if [ -f "$INSTALL_DIR/config.json" ]; then
    cp "$INSTALL_DIR/config.json" "$INSTALL_DIR/config.json.bak.$(date +%Y%m%d%H%M%S)"
    info "config.json existant sauvegarde (.bak)"
fi

if [ "$REPO_DIR/config.json" = "$INSTALL_DIR/config.json" ]; then
    err "Le dossier d'installation ne peut pas etre celui du depot."
fi

python3 - "$REPO_DIR/config.json" "$INSTALL_DIR/config.json" \
         "$CONFIG_MONITOR_IP" "$INSTALL_DIR" << 'PYCONF'
import json, sys

src, dst, monitor_ip, install_dir = sys.argv[1:5]

with open(src, encoding='utf-8') as f:
    cfg = json.load(f)

cfg['_comment']  = "Configuration locale — genere par install.sh. Non suivi par git."
cfg['monitor_ip'] = monitor_ip
cfg['db_path']    = f"{install_dir}/hegp.db"
cfg['csv_dir']    = f"{install_dir}/data/"
cfg['demo_json']  = f"{install_dir}/patient_demo.json"
cfg['hdf5_dir']   = f"{install_dir}/waves/"

with open(dst, 'w', encoding='utf-8') as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
    f.write("\n")
PYCONF

if [ "$AUTO_DISCOVERY" = "1" ]; then
    ok "config.json cree (moniteur : decouverte automatique)"
else
    ok "config.json cree (moniteur : $MONITOR_IP)"
fi

# ── Création des dossiers ─────────────────────────────────────────────────────
mkdir -p "$INSTALL_DIR/data"
mkdir -p "$INSTALL_DIR/waves"
ok "Dossiers data/ et waves/ créés"

# ── Service systemd ───────────────────────────────────────────────────────────
step "Service systemd"

SERVICE_FILE="/etc/systemd/system/mx800capture.service"

if [ ! -f "$REPO_DIR/mx800capture.service" ]; then
    err "mx800capture.service introuvable dans le depot."
fi

sed -e "s|__USER__|$USER|g" \
    -e "s|__INSTALL_DIR__|$INSTALL_DIR|g" \
    "$REPO_DIR/mx800capture.service" | sudo tee "$SERVICE_FILE" > /dev/null

sudo systemctl daemon-reload
sudo systemctl enable mx800capture.service &>/dev/null
ok "Service mx800capture.service cree et active"

# ── Test de connectivité réseau ───────────────────────────────────────────────
step "Test de connectivité"

info "Recherche du moniteur $MONITOR_IP (30s max)..."
FOUND=0
for i in $(seq 1 10); do
    if ping -c 1 -W 2 "$MONITOR_IP" &>/dev/null; then
        FOUND=1
        break
    fi
    sleep 1
done

if [ "$FOUND" = "1" ]; then
    ok "Moniteur $MONITOR_IP accessible"
    info "Démarrage du service..."
    sudo systemctl start mx800capture.service
    sleep 5
    if sudo systemctl is-active --quiet mx800capture.service; then
        ok "Service démarré avec succès"
    else
        warn "Service démarré mais vérifiez : journalctl -u mx800capture.service -n 30"
    fi
else
    warn "Moniteur $MONITOR_IP non joignable pour l'instant"
    echo ""
    echo -e "  ${BOLD}État du réseau :${NC}"
    ip -brief addr show eth0 2>/dev/null | sed 's/^/    /'
    if [ "$NET_CONFIG" = "1" ]; then
        echo -e "  ${BOLD}Baux DHCP attribués :${NC}"
        if [ -s /var/lib/misc/dnsmasq.leases ]; then
            sed 's/^/    /' /var/lib/misc/dnsmasq.leases
        else
            echo "    (aucun — le moniteur n'a pas encore demandé d'adresse)"
        fi
    fi
    echo ""
    echo -e "  ${BOLD}À vérifier :${NC}"
    echo "    • Câble Ethernet branché entre le RPi et le moniteur"
    echo "    • Moniteur allumé avec l'export de données activé"
    echo "    • IP réelle du moniteur (voir les baux DHCP ci-dessus)"
    echo ""
    warn "Le service est activé : il se connectera automatiquement dès que le moniteur répondra"
    sudo systemctl start mx800capture.service &>/dev/null || true
fi

# ── Résumé final ─────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${GREEN}═══════════════════════════════════════════════════${NC}"
echo -e "${BOLD}${GREEN}  Installation terminée !${NC}"
echo -e "${BOLD}${GREEN}═══════════════════════════════════════════════════${NC}"
echo ""
echo -e "${BOLD}Fichiers installés :${NC}"
echo "  $INSTALL_DIR/mx800_capture.py"
echo "  $INSTALL_DIR/config.json"
echo "  $INSTALL_DIR/hegp.db       (créé au premier démarrage)"
echo "  $INSTALL_DIR/data/         (CSV par session)"
echo ""
echo -e "${BOLD}Commandes utiles :${NC}"
echo "  sudo systemctl status mx800capture.service"
echo "  journalctl -u mx800capture.service -f"
echo "  sqlite3 $INSTALL_DIR/hegp.db \"SELECT COUNT(*) FROM numerics;\""
echo ""
echo -e "${BOLD}Diagnostic réseau :${NC}"
echo "  ip -brief addr show eth0            # IP du RPi"
echo "  cat /var/lib/misc/dnsmasq.leases    # adresses attribuées au moniteur"
echo "  journalctl -u dnsmasq -f            # requêtes BOOTP/DHCP en direct"
echo "  ping $MONITOR_IP"
echo ""
echo -e "${BOLD}Changer l'IP du moniteur :${NC}"
echo "  nano $INSTALL_DIR/config.json"
echo "  sudo systemctl restart mx800capture.service"
echo ""
echo -e "${BOLD}Activer le BIS :${NC}"
echo "  nano $INSTALL_DIR/config.json  →  BIS: \"active\": true"
echo "  sudo systemctl restart mx800capture.service"
echo ""
