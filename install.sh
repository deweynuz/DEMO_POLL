#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════════
# install.sh — Installation automatique système d'acquisition MX800 HEGP
# Usage : curl -sSLO https://raw.githubusercontent.com/deweynuz/DEMO_POLL/main/install.sh
#         bash install.sh
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

# Lecture des réponses depuis le terminal, même quand le script est exécuté via
# `curl ... | bash` (stdin = le script, pas le clavier). On lit alors /dev/tty.
if [ -r /dev/tty ]; then
    ask() { read -r -p "$1" "$2" < /dev/tty; }
else
    ask() { read -r -p "$1" "$2"; }
fi

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
echo -e "${BOLD}Type de raccordement au moniteur ?${NC}"
echo -e "  1) ${BOLD}Direct (plug-and-play)${NC} — câble Ethernet Pi ↔ moniteur."
echo -e "     Le Pi configure le réseau (IP fixe + serveur DHCP/BOOTP) et découvre"
echo -e "     le moniteur tout seul. Recommandé."
echo -e "  2) ${BOLD}Réseau existant${NC} — le moniteur a déjà une IP sur un réseau."
ask "  Choix [1] : " NET_CHOICE
NET_CHOICE=${NET_CHOICE:-1}

if [ "$NET_CHOICE" = "2" ]; then
    NET_MODE="network"
    echo ""
    echo -e "${BOLD}IP du moniteur ?${NC} (ou 'auto' pour la découverte par broadcast)"
    ask "  IP moniteur [auto] : " MONITOR_IP
    MONITOR_IP=${MONITOR_IP:-auto}
    DISCOVERY_CIDR=""
else
    NET_MODE="direct"
    MONITOR_IP="auto"
    DISCOVERY_CIDR="192.168.100.0/24"
fi

echo ""
echo -e "${BOLD}Dossier d'installation ?${NC}"
ask "  Dossier [/home/$USER] : " INSTALL_DIR
INSTALL_DIR=${INSTALL_DIR:-/home/$USER}

echo ""
echo -e "${BOLD}Récapitulatif :${NC}"
echo "  Utilisateur    : $USER"
echo "  Raccordement   : $([ "$NET_MODE" = "direct" ] && echo 'Direct (plug-and-play, DHCP/BOOTP)' || echo 'Réseau existant')"
echo "  IP moniteur    : $MONITOR_IP"
echo "  Dossier        : $INSTALL_DIR"
echo "  Repository     : https://github.com/deweynuz/DEMO_POLL"
echo ""
ask "Confirmer l'installation ? [O/n] : " CONFIRM
CONFIRM=${CONFIRM:-O}
if [[ ! "$CONFIRM" =~ ^[OoYy]$ ]]; then
    echo "Installation annulée."
    exit 0
fi

# ── Mise à jour système ───────────────────────────────────────────────────────
step "Mise à jour du système"

# Sur une image RPi fraîche, un upgrade interrompu laisse dpkg dans un état
# cassé (« E: dpkg was interrupted »), ce qui ferait échouer toute installation
# de paquets (dnsmasq, sqlite3…). On attend un éventuel apt en cours, puis on
# répare dpkg avant de continuer.
info "Vérification de l'état du gestionnaire de paquets..."
waited=0
while sudo fuser /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock /var/lib/apt/lists/lock >/dev/null 2>&1 \
      || pgrep -x "apt|apt-get|dpkg|unattended-upgr" >/dev/null 2>&1; do
    if [ "$waited" -ge 120 ]; then
        warn "apt/dpkg toujours occupé après 120 s — on tente de continuer."
        break
    fi
    info "apt/dpkg occupé, attente... (${waited}s)"
    sleep 5
    waited=$((waited + 5))
done

if sudo dpkg --audit 2>/dev/null | grep -q .; then
    warn "dpkg dans un état incomplet — réparation..."
    sudo dpkg --configure -a || true
    sudo apt-get -f install -y || true
    ok "dpkg réparé"
fi

info "Mise à jour des paquets (peut prendre quelques minutes)..."
if ! sudo apt-get update -q; then
    warn "apt-get update a échoué — nouvelle tentative après réparation de dpkg..."
    sudo dpkg --configure -a || true
    sudo apt-get update -q || err "apt-get update échoue (dpkg interrompu ?). Lancez 'sudo dpkg --configure -a' puis relancez l'installation."
fi
sudo apt-get install -y sqlite3 python3-pip -q
ok "Système à jour"

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

# Génère config.json à partir de celui du dépôt (source unique de vérité pour
# la liste des paramètres) en y injectant l'IP et les chemins d'installation.
python3 - "$REPO_DIR/config.json" "$INSTALL_DIR/config.json" "$MONITOR_IP" "$INSTALL_DIR" "$DISCOVERY_CIDR" << 'PYEOF'
import json, sys
src, dst, ip, install_dir, cidr = sys.argv[1:6]
with open(src, encoding='utf-8') as f:
    cfg = json.load(f)
cfg['monitor_ip']     = ip
cfg['discovery_cidr'] = cidr
cfg['db_path']    = f"{install_dir}/hegp.db"
cfg['csv_dir']    = f"{install_dir}/data/"
cfg['demo_json']  = f"{install_dir}/patient_demo.json"
cfg['hdf5_dir']   = f"{install_dir}/waves/"
with open(dst, 'w', encoding='utf-8') as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
PYEOF
ok "config.json créé (monitor_ip=$MONITOR_IP, discovery_cidr='${DISCOVERY_CIDR:-—}')"

# ── Création des dossiers ─────────────────────────────────────────────────────
mkdir -p "$INSTALL_DIR/data"
mkdir -p "$INSTALL_DIR/waves"
ok "Dossiers data/ et waves/ créés"

# ── Configuration réseau (mode direct plug-and-play) ──────────────────────────
# Le moniteur Philips est en BOOTP et attend une IP. En liaison directe, le Pi
# joue le serveur : IP fixe sur eth0 + DHCP/BOOTP. Le moniteur reçoit alors une
# IP dans la plage, et la capture le découvre par scan unicast (port 24105).
if [ "$NET_MODE" = "direct" ]; then
    step "Configuration réseau (liaison directe)"
    LAN_IP="192.168.100.1"

    sudo apt-get install -y dnsmasq -q && ok "dnsmasq installé"

    # eth0 en IP statique permanente
    if command -v nmcli &>/dev/null && systemctl is-active --quiet NetworkManager; then
        sudo nmcli con delete mx800-eth0 &>/dev/null || true
        sudo nmcli con add type ethernet ifname eth0 con-name mx800-eth0 \
            ipv4.method manual ipv4.addresses "${LAN_IP}/24" ipv6.method ignore &>/dev/null
        sudo nmcli con up mx800-eth0 &>/dev/null || true
        ok "eth0 en statique ${LAN_IP}/24 (NetworkManager)"
    else
        if ! grep -q "mx800-eth0" /etc/dhcpcd.conf 2>/dev/null; then
            printf '\n# mx800-eth0\ninterface eth0\nstatic ip_address=%s/24\n' "$LAN_IP" | sudo tee -a /etc/dhcpcd.conf >/dev/null
        fi
        sudo ip addr add "${LAN_IP}/24" dev eth0 2>/dev/null || true
        sudo ip link set eth0 up
        ok "eth0 en statique ${LAN_IP}/24 (dhcpcd)"
    fi

    # Serveur DHCP/BOOTP sur eth0 uniquement
    sudo tee /etc/dnsmasq.d/mx800.conf >/dev/null <<'DNSEOF'
port=0
interface=eth0
bind-interfaces
dhcp-authoritative
dhcp-range=192.168.100.50,192.168.100.150,255.255.255.0,12h
# Le moniteur Philips émet en BOOTP : dnsmasq n'alloue en BOOTP qu'en présence
# d'au moins un dhcp-host. Cette entrée « déclencheur » active l'allocation
# BOOTP dynamique pour tout moniteur, quel que soit son adresse MAC.
dhcp-host=00:00:00:00:00:01,192.168.100.199
DNSEOF
    sudo systemctl enable dnsmasq &>/dev/null
    sudo systemctl restart dnsmasq
    ok "Serveur DHCP/BOOTP actif sur eth0 (plage 192.168.100.50-150)"
fi

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

if [ "$MONITOR_IP" = "auto" ]; then
    if [ "$NET_MODE" = "direct" ]; then
        info "Mode direct : le moniteur va recevoir une IP (DHCP/BOOTP), puis la capture"
        info "le découvre par scan sur 192.168.100.0/24. Cela peut prendre 1-2 minutes."
    else
        info "Mode découverte automatique : recherche du moniteur par broadcast."
    fi
    info "Démarrage du service..."
    sudo systemctl start mx800capture.service
    sleep 3
    if sudo systemctl is-active --quiet mx800capture.service; then
        ok "Service démarré (découverte du moniteur en cours)"
    else
        warn "Service démarré mais vérifiez les logs : journalctl -u mx800capture.service -f"
    fi
elif ping -c 1 -W 2 "$MONITOR_IP" &>/dev/null; then
    ok "Moniteur $MONITOR_IP accessible"
    info "Démarrage du service..."
    sudo systemctl start mx800capture.service
    sleep 3
    if sudo systemctl is-active --quiet mx800capture.service; then
        ok "Service démarré avec succès"
    else
        warn "Service démarré mais vérifiez les logs : journalctl -u mx800capture.service -f"
    fi
else
    warn "Moniteur $MONITOR_IP non accessible pour l'instant"
    warn "Le service démarrera automatiquement au prochain boot"
    warn "Vérifiez le câble Ethernet et l'IP du moniteur"
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
echo "  $INSTALL_DIR/data/         (CSV par intervention)"
echo ""
echo -e "${BOLD}Commandes utiles :${NC}"
echo "  sudo systemctl status mx800capture.service"
echo "  journalctl -u mx800capture.service -f"
echo "  sqlite3 $INSTALL_DIR/hegp.db \"SELECT COUNT(*) FROM numerics;\""
echo ""
echo -e "${BOLD}Changer l'IP du moniteur :${NC}"
echo "  nano $INSTALL_DIR/config.json"
echo "  sudo systemctl restart mx800capture.service"
echo ""
echo -e "${BOLD}Activer un paramètre optionnel (ex. ventilation, gaz du sang) :${NC}"
echo "  nano $INSTALL_DIR/config.json  →  passer \"active\": false à true"
echo "  sudo systemctl restart mx800capture.service"
echo ""
