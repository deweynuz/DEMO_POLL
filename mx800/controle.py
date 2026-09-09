"""
Pilotage local : file de commandes et petit serveur HTTP.

Le service tourne en permanence ; la page d'état et le CLI doivent pouvoir lui
parler. Un seul mécanisme sert les deux — un serveur HTTP écoutant en local —
plutôt qu'une socket pour l'un et un fichier pour l'autre.

Sûreté des fils d'exécution : le serveur HTTP tourne dans un fil séparé, mais
il ne touche JAMAIS à la boucle d'acquisition. Il dépose une commande dans une
file ; la boucle la retire à son tour de scrutation. Sans cela, un clic sur
« démarrer » depuis une tablette pourrait ouvrir une intervention au milieu de
l'écriture d'un lot.

La page n'affiche PAS l'identité du patient par défaut : elle est destinée à un
écran de salle, et le code de recherche suffit à savoir que ça enregistre.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

log = logging.getLogger('mx800.controle')


@dataclass
class Commande:
    action: str
    parametres: dict[str, Any] = field(default_factory=dict)
    reponse: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=1))

    def repondre(self, ok: bool, message: str, **extra):
        try:
            self.reponse.put_nowait({'ok': ok, 'message': message, **extra})
        except queue.Full:
            pass

    def attendre(self, delai: float = 5.0) -> dict:
        try:
            return self.reponse.get(timeout=delai)
        except queue.Empty:
            return {'ok': False, 'message': "le service n'a pas répondu à temps"}


class FileCommandes:
    """Point de rendez-vous entre les fils HTTP et la boucle d'acquisition."""

    def __init__(self, taille: int = 32):
        self._file: queue.Queue[Commande] = queue.Queue(maxsize=taille)

    def deposer(self, action: str, **parametres) -> Commande:
        commande = Commande(action=action, parametres=parametres)
        try:
            self._file.put_nowait(commande)
        except queue.Full:
            commande.repondre(False, "file de commandes saturée")
        return commande

    def retirer(self) -> Commande | None:
        try:
            return self._file.get_nowait()
        except queue.Empty:
            return None


# ─────────────────────────────────────────────────────────────────────────────
# Page d'état
# ─────────────────────────────────────────────────────────────────────────────

PAGE = """<!doctype html>
<html lang="fr"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Acquisition MX800 — %(salle)s</title>
<style>
 :root { color-scheme: light dark; --fond:#f5f5f4; --carte:#fff; --texte:#1c1917;
         --discret:#78716c; --bord:#e7e5e4; }
 @media (prefers-color-scheme: dark) {
   :root { --fond:#1c1917; --carte:#292524; --texte:#f5f5f4;
           --discret:#a8a29e; --bord:#44403c; } }
 * { box-sizing:border-box }
 body { margin:0; padding:1rem; background:var(--fond); color:var(--texte);
        font:16px/1.5 system-ui,-apple-system,sans-serif }
 .voyant { border-radius:14px; padding:1.5rem; text-align:center; color:#fff;
           margin-bottom:1rem }
 .vert { background:#15803d } .rouge { background:#b91c1c } .orange { background:#b45309 }
 .voyant b { display:block; font-size:2rem; line-height:1.2 }
 .voyant span { opacity:.9; font-size:1rem }
 .grille { display:grid; gap:.75rem; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)) }
 .carte { background:var(--carte); border:1px solid var(--bord); border-radius:12px;
          padding:.9rem 1rem }
 .carte h2 { margin:0 0 .35rem; font-size:.78rem; font-weight:600; letter-spacing:.05em;
             text-transform:uppercase; color:var(--discret) }
 .carte p { margin:0; font-size:1.35rem; font-variant-numeric:tabular-nums }
 .carte small { color:var(--discret) }
 .alerte { background:#b91c1c; color:#fff; border-radius:10px; padding:.8rem 1rem;
           margin-bottom:1rem; font-weight:600 }
 .actions { margin-top:1rem; display:flex; gap:.6rem; flex-wrap:wrap }
 button { font:inherit; padding:.8rem 1.3rem; border-radius:10px; border:1px solid var(--bord);
          background:var(--carte); color:var(--texte); cursor:pointer; min-height:48px }
 button.primaire { background:#15803d; color:#fff; border-color:#15803d }
 button.danger { background:#b91c1c; color:#fff; border-color:#b91c1c }
 button:disabled { opacity:.45; cursor:not-allowed }
 footer { margin-top:1.2rem; color:var(--discret); font-size:.85rem }
 table { width:100%%; border-collapse:collapse; font-size:.9rem }
 td { padding:.2rem 0 } td:last-child { text-align:right; font-variant-numeric:tabular-nums }
</style></head><body>
<div id="contenu">chargement…</div>
<script>
const F = (v, u='') => (v===null||v===undefined) ? '—' : v+u;
async function rafraichir() {
  let e;
  try { e = await (await fetch('/api/etat')).json(); }
  catch (err) { document.getElementById('contenu').innerHTML =
    '<div class="voyant rouge"><b>SERVICE INJOIGNABLE</b><span>'+err+'</span></div>'; return; }
  const enCours = e.productif && e.intervention;
  const classe = e.alerte ? 'orange' : (enCours ? 'vert' : 'rouge');
  const titre = e.alerte ? 'ATTENTION'
              : (enCours ? 'ENREGISTREMENT EN COURS' : 'PAS D\\'ENREGISTREMENT');
  document.getElementById('contenu').innerHTML = `
    ${e.alerte ? '<div class="alerte">'+e.alerte+'</div>' : ''}
    <div class="voyant ${classe}"><b>${titre}</b>
      <span>${e.site} · salle ${e.salle} · ${e.etat}</span></div>
    <div class="grille">
      <div class="carte"><h2>Intervention</h2><p>${F(e.intervention)}</p></div>
      <div class="carte"><h2>Débit</h2><p>${F(e.debit_lignes_par_min)} <small>lignes/min</small></p></div>
      <div class="carte"><h2>Dernière écriture</h2><p>${F(e.silence_donnees_s,' s')}</p>
        <small>${F(e.derniere_ligne_utc)}</small></div>
      <div class="carte"><h2>Moniteur</h2><p>${F(e.moniteur_bed_label)}</p>
        <small>${F(e.moniteur_ip)}${e.courbes_negociees?' · courbes':''}</small></div>
      <div class="carte"><h2>Horloge</h2><p>${F(e.horloge_source)}</p>
        <small>écart moniteur ${F(e.ecart_horloge_moniteur_s,' s')}</small></div>
      <div class="carte"><h2>Disque libre</h2><p>${F(e.disque_libre_mo)} <small>Mo</small></p></div>
      <div class="carte"><h2>Compteurs</h2><table>
        <tr><td>lignes écrites</td><td>${e.compteurs.lignes_ecrites}</td></tr>
        <tr><td>associations</td><td>${e.compteurs.associations}</td></tr>
        <tr><td>coupures</td><td>${e.compteurs.aborts+e.compteurs.timeouts}</td></tr>
        <tr><td>lacunes</td><td>${e.compteurs.lacunes}</td></tr>
        <tr><td>réassociations forcées</td><td>${e.compteurs.reassociations_forcees}</td></tr>
      </table></div>
    </div>
    <div class="actions">
      <button class="primaire" onclick="agir('demarrer')" ${enCours?'disabled':''}>Démarrer une intervention</button>
      <button class="danger" onclick="agir('arreter')" ${enCours?'':'disabled'}>Arrêter l'intervention</button>
    </div>
    <footer>mis à jour ${e.maj_utc} · version ${e.version} · rafraîchi toutes les 2 s</footer>`;
}
async function agir(action) {
  if (action === 'arreter' && !confirm("Arrêter l'intervention en cours ?")) return;
  const r = await (await fetch('/api/'+action, {method:'POST'})).json();
  if (!r.ok) alert(r.message);
  rafraichir();
}
rafraichir(); setInterval(rafraichir, 2000);
</script></body></html>
"""


class _Gestionnaire(BaseHTTPRequestHandler):
    serveur_etat = None      # renseigné par ServeurEtat

    def log_message(self, format, *args):        # noqa: A002
        log.debug("http %s", format % args)

    def _repondre(self, code: int, contenu: bytes, type_mime: str):
        self.send_response(code)
        self.send_header('Content-Type', type_mime)
        self.send_header('Content-Length', str(len(contenu)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(contenu)

    def _json(self, donnees: dict, code: int = 200):
        self._repondre(code, json.dumps(donnees, ensure_ascii=False).encode('utf-8'),
                       'application/json; charset=utf-8')

    def do_GET(self):                            # noqa: N802
        serveur = self.serveur_etat
        if self.path in ('/', '/index.html'):
            etat = serveur.lire_etat()
            page = PAGE % {'salle': etat.get('salle', '')}
            self._repondre(200, page.encode('utf-8'), 'text/html; charset=utf-8')
        elif self.path == '/api/etat':
            self._json(serveur.lire_etat())
        elif self.path == '/sante':
            # healthcheck en une commande : code 200 si ça enregistre
            etat = serveur.lire_etat()
            ok = bool(etat.get('productif')) and not etat.get('alerte')
            self._repondre(200 if ok else 503,
                           (serveur.resume() + '\n').encode('utf-8'),
                           'text/plain; charset=utf-8')
        else:
            self._json({'ok': False, 'message': 'inconnu'}, 404)

    def do_POST(self):                           # noqa: N802
        serveur = self.serveur_etat
        actions = {'/api/demarrer': 'demarrer', '/api/arreter': 'arreter'}
        action = actions.get(self.path)
        if action is None:
            self._json({'ok': False, 'message': 'action inconnue'}, 404)
            return
        taille = int(self.headers.get('Content-Length') or 0)
        parametres = {}
        if taille:
            try:
                parametres = json.loads(self.rfile.read(taille) or b'{}')
            except json.JSONDecodeError:
                self._json({'ok': False, 'message': 'corps JSON illisible'}, 400)
                return
        commande = serveur.commandes.deposer(action, **parametres)
        self._json(commande.attendre())


class ServeurEtat:
    """
    Serveur HTTP dans un fil séparé. `lire_etat` et `resume` sont fournis par
    l'appelant : le serveur ne connaît ni la base ni la boucle.
    """

    def __init__(self, commandes: FileCommandes, lire_etat: Callable[[], dict],
                 resume: Callable[[], str], *, adresse: str = '0.0.0.0',
                 port: int = 8080):
        self.commandes = commandes
        self.lire_etat = lire_etat
        self.resume = resume
        self.adresse, self.port = adresse, port
        self._serveur: ThreadingHTTPServer | None = None
        self._fil: threading.Thread | None = None

    def demarrer(self) -> int:
        gestionnaire = type('_G', (_Gestionnaire,), {'serveur_etat': self})
        self._serveur = ThreadingHTTPServer((self.adresse, self.port), gestionnaire)
        self._serveur.daemon_threads = True
        self.port = self._serveur.server_address[1]
        self._fil = threading.Thread(target=self._serveur.serve_forever,
                                     name='page-etat', daemon=True)
        self._fil.start()
        log.info("Page d'état : http://%s:%d/  (santé : /sante)",
                 self.adresse if self.adresse != '0.0.0.0' else '<ip-du-pi>', self.port)
        return self.port

    def arreter(self):
        if self._serveur:
            self._serveur.shutdown()
            self._serveur.server_close()
            self._serveur = None
        if self._fil:
            self._fil.join(timeout=2.0)
            self._fil = None
