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
echo -e "${BOLD}IP du moniteur Philips MX800 ?${NC}"
echo -e "  Tapez l'IP (ex. 192.168.100.31), ou 'auto' pour la découverte automatique"
echo -e "  (broadcast sur le réseau — le moniteur doit être sur le même sous-réseau)."
read -p "  IP moniteur [auto] : " MONITOR_IP
MONITOR_IP=${MONITOR_IP:-auto}

echo ""
echo -e "${BOLD}Dossier d'installation ?${NC}"
read -p "  Dossier [/home/$USER] : " INSTALL_DIR
INSTALL_DIR=${INSTALL_DIR:-/home/$USER}

echo ""
echo -e "${BOLD}Récapitulatif :${NC}"
echo "  Utilisateur    : $USER"
echo "  IP moniteur    : $MONITOR_IP"
echo "  Dossier        : $INSTALL_DIR"
echo "  Repository     : https://github.com/deweynuz/DEMO_POLL"
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
python3 - "$REPO_DIR/config.json" "$INSTALL_DIR/config.json" "$MONITOR_IP" "$INSTALL_DIR" << 'PYEOF'
import json, sys
src, dst, ip, install_dir = sys.argv[1:5]
with open(src, encoding='utf-8') as f:
    cfg = json.load(f)
cfg['monitor_ip'] = ip
cfg['db_path']    = f"{install_dir}/hegp.db"
cfg['csv_dir']    = f"{install_dir}/data/"
cfg['demo_json']  = f"{install_dir}/patient_demo.json"
cfg['hdf5_dir']   = f"{install_dir}/waves/"
with open(dst, 'w', encoding='utf-8') as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
PYEOF
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

if [ "$MONITOR_IP" = "auto" ]; then
    info "Mode découverte automatique : le service cherchera le moniteur par broadcast."
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
