#!/usr/bin/env bash
# Configuration du réseau moniteur : eth0 en 192.168.100.1/24 et dnsmasq
# (DHCP/BOOTP) pour les moniteurs Philips.
#
#   ./install.sh --configurer-reseau      à l'installation
#   mx800 reseau --configurer             ensuite
#
# NE TOUCHE PAS AU WI-FI — c'est souvent lui qui porte la session SSH.
# Sur un Pi en service, relancer ce script coupe brièvement le lien avec le
# moniteur : à faire hors intervention.
#
# --oui : ne pas demander de confirmation (l'appelant l'a déjà obtenue).

set -euo pipefail
[ "$(id -u)" -ne 0 ] || { echo "Ne pas lancer en root : le script utilise sudo quand il le faut." >&2; exit 1; }

V=$'\033[32m'; R=$'\033[31m'; O=$'\033[33m'; G=$'\033[90m'; N=$'\033[0m'
[ -t 1 ] || { V=""; R=""; O=""; G=""; N=""; }
ok()    { printf '  [%sok%s] %s\n' "$V" "$N" "$1"; }
info()  { printf '       %s%s%s\n' "$G" "$1" "$N"; }
avert() { printf '  [%s! %s] %s\n' "$O" "$N" "$1"; }
echec() { printf '  [%sNON%s] %s\n' "$R" "$N" "$1" >&2; exit 1; }

if [ "${1:-}" != "--oui" ]; then
    avert "reconfiguration de eth0 et de dnsmasq — coupe brièvement le lien avec le moniteur"
    read -r -p "  Taper OUI pour confirmer : " confirmation
    [ "$confirmation" = "OUI" ] || echec "reconfiguration réseau annulée"
fi

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
