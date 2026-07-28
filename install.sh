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
read -p "  Dossier [/home/$USER] : " INSTALL_DIR
INSTALL_DIR=${INSTALL_DIR:-/home/$USER}

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

# Crée config.json avec l'IP saisie
cat > "$INSTALL_DIR/config.json" << CONFIGEOF
{
  "_comment": "Configuration mx800_capture.py — modifier ce fichier puis redémarrer le service",
  "_doc": "Pour ajouter un paramètre : active: false → active: true puis redémarrer le service",

  "monitor_ip": "$CONFIG_MONITOR_IP",
  "poll_interval": 1.0,
  "demo_interval": 30,
  "db_path": "$INSTALL_DIR/hegp.db",
  "csv_dir": "$INSTALL_DIR/data/",
  "demo_json": "$INSTALL_DIR/patient_demo.json",
  "waves": false,
  "hdf5_dir": "$INSTALL_DIR/waves/",

  "parameters": {

    "_section_cardio": "── Cardio-vasculaire ──────────────────────────────",
    "0x4182": {"name": "HR",          "unit": "bpm",   "label": "Fréquence cardiaque",     "active": true},
    "0x4BB8": {"name": "SpO2",        "unit": "%",     "label": "Saturation O2 (SpO2)",    "active": true},
    "0x4822": {"name": "Pulse",       "unit": "bpm",   "label": "Pouls",                   "active": true},

    "_section_abp": "── Pression artérielle invasive (ABP) ─────────────",
    "0x4A15": {"name": "ABP_sys",     "unit": "mmHg",  "label": "ABP systolique",          "active": true},
    "0x4A16": {"name": "ABP_dia",     "unit": "mmHg",  "label": "ABP diastolique",         "active": true},
    "0x4A17": {"name": "ABP_mean",    "unit": "mmHg",  "label": "ABP moyenne",             "active": true},

    "_section_art": "── Pression artérielle ART ────────────────────────",
    "0x4A11": {"name": "ART_sys",     "unit": "mmHg",  "label": "ART systolique",          "active": true},
    "0x4A12": {"name": "ART_dia",     "unit": "mmHg",  "label": "ART diastolique",         "active": true},
    "0x4A13": {"name": "ART_mean",    "unit": "mmHg",  "label": "ART moyenne",             "active": true},

    "_section_pap": "── Pression artérielle pulmonaire (PAP) ───────────",
    "0x4A1D": {"name": "PAP_sys",     "unit": "mmHg",  "label": "PAP systolique",          "active": true},
    "0x4A1E": {"name": "PAP_dia",     "unit": "mmHg",  "label": "PAP diastolique",         "active": true},
    "0x4A1F": {"name": "PAP_mean",    "unit": "mmHg",  "label": "PAP moyenne",             "active": true},

    "_section_cvp": "── Pression veineuse centrale (CVP) ───────────────",
    "0x4A44": {"name": "CVP",         "unit": "mmHg",  "label": "CVP",                     "active": true},
    "0x4A47": {"name": "CVP_mean",    "unit": "mmHg",  "label": "CVP moyenne",             "active": true},

    "_section_nbp": "── Pression artérielle non invasive (NBP) ─────────",
    "0x4A05": {"name": "NBP_sys",     "unit": "mmHg",  "label": "NBP systolique",          "active": true},
    "0x4A06": {"name": "NBP_dia",     "unit": "mmHg",  "label": "NBP diastolique",         "active": true},
    "0x4A07": {"name": "NBP_mean",    "unit": "mmHg",  "label": "NBP moyenne",             "active": true},

    "_section_co": "── Débit cardiaque ────────────────────────────────",
    "0x4B04": {"name": "CO",          "unit": "L/min", "label": "Débit cardiaque",         "active": true},
    "0x4BDC": {"name": "CCO",         "unit": "L/min", "label": "Débit cardiaque continu", "active": true},
    "0x490C": {"name": "CI",          "unit": "L/min/m2","label": "Index cardiaque",       "active": true},
    "0x4B84": {"name": "SV",          "unit": "mL",    "label": "Volume éjection systolique","active": true},
    "0xF049": {"name": "SVV",         "unit": "%",     "label": "Variation VES",           "active": true},

    "_section_sat": "── Saturations O2 ─────────────────────────────────",
    "0x4B34": {"name": "SaO2",        "unit": "%",     "label": "Saturation O2 artérielle","active": true},
    "0x4B3C": {"name": "SvO2",        "unit": "%",     "label": "Saturation O2 veineuse",  "active": true},
    "0xF100": {"name": "ScvO2",       "unit": "%",     "label": "Sat O2 veineuse centrale","active": true},

    "_section_temp": "── Températures ───────────────────────────────────",
    "0x4B48": {"name": "Temp",        "unit": "°C",    "label": "Température générique",   "active": true},
    "0xE014": {"name": "Tblood",      "unit": "°C",    "label": "Température sanguine",    "active": true},
    "0x4B60": {"name": "Tcore",       "unit": "°C",    "label": "Température centrale",    "active": true},
    "0x4B74": {"name": "Tskin",       "unit": "°C",    "label": "Température cutanée",     "active": true},
    "0x4B64": {"name": "Tesoph",      "unit": "°C",    "label": "Température oesophagienne","active": true},
    "0x4B6C": {"name": "Tnaso",       "unit": "°C",    "label": "Température naso-pharyngée","active": true},
    "0xF0C7": {"name": "T1",          "unit": "°C",    "label": "Température 1",           "active": true},
    "0xF0C8": {"name": "T2",          "unit": "°C",    "label": "Température 2",           "active": true},

    "_section_co2": "── CO2 / Respiratoire ─────────────────────────────",
    "0x50B0": {"name": "EtCO2",       "unit": "mmHg",  "label": "EtCO2 end-tidal",         "active": true},
    "0x50BA": {"name": "FiCO2",       "unit": "mmHg",  "label": "FiCO2 inspiré",           "active": true},
    "0x5012": {"name": "RR",          "unit": "rpm",   "label": "Fréquence respiratoire",  "active": true},

    "_section_bis": "── BIS / EEG ──────────────────────────────────────",
    "0xF04E": {"name": "BIS",         "unit": "",      "label": "Bispectral Index",        "active": false},
    "0xF04D": {"name": "BIS_SQI",     "unit": "%",     "label": "Signal Quality Index",    "active": false},
    "0x593C": {"name": "EMG",         "unit": "dB",    "label": "Electromyographie",       "active": false},
    "0xF04A": {"name": "SR",          "unit": "%",     "label": "Suppression Ratio",       "active": false}
  }
}
CONFIGEOF
ok "config.json créé avec IP=$MONITOR_IP"

# ── Création des dossiers ─────────────────────────────────────────────────────
mkdir -p "$INSTALL_DIR/data"
mkdir -p "$INSTALL_DIR/waves"
ok "Dossiers data/ et waves/ créés"

# ── Service systemd ───────────────────────────────────────────────────────────
step "Service systemd"

SERVICE_FILE="/etc/systemd/system/mx800capture.service"

sudo tee "$SERVICE_FILE" > /dev/null << SERVICEEOF
[Unit]
Description=MX800 Patient Data Capture
After=network.target

[Service]
User=$USER
ExecStart=/usr/bin/python3 $INSTALL_DIR/mx800_capture.py --config $INSTALL_DIR/config.json
Restart=always
RestartSec=15
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SERVICEEOF

sudo systemctl daemon-reload
sudo systemctl enable mx800capture.service
ok "Service mx800capture.service créé et activé"

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
