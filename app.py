from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room, leave_room
import random
import string
import time

app = Flask(__name__)
app.config['SECRET_KEY'] = 'killer_secret_key'
socketio = SocketIO(app, async_mode='threading', cors_allowed_origins='*')

# --- GLOBALS ---
games = {}
sid_to_room = {}
admin_sids = set() 

# --- CLASSES ---
class Joueur:
    def __init__(self, sid, nom, is_bot=False):
        self.sid = sid
        self.nom = nom
        self.pv = 0
        self.des_pv = [] 
        self.est_pret = False 
        self.is_bot = is_bot 

    def to_dict(self):
        return {
            'nom': self.nom, 
            'pv': self.pv, 
            'sid': self.sid, 
            'is_bot': self.is_bot,
            'est_pret': self.est_pret,
            'des_pv': self.des_pv if self.des_pv else []
        }

class Partie:
    def __init__(self, room_id, nom_salon):
        self.id = room_id
        self.nom_salon = nom_salon
        self.joueurs = []
        self.reset_jeu()

    def reset_jeu(self):
        self.etat = "ATTENTE" 
        self.joueur_actuel_idx = 0
        self.des_sur_table, self.des_gardes = [], []
        self.message, self.vainqueur = "En attente...", None
        self.createur_sid = self.joueurs[0].sid if self.joueurs else None
        self.valeur_killer, self.liste_victimes = 0, []
        self.victime_actuelle_idx, self.degats_accumules = -1, 0
        
        # MODIFICATION : Liste pour stocker qui était vivant au début du tour
        self.ids_vivants_debut_tour = []

    def verifier_proprietaire(self):
        chef_actuel = next((p for p in self.joueurs if p.sid == self.createur_sid), None)
        if not chef_actuel or chef_actuel.is_bot:
            nouveau_chef = next((p for p in self.joueurs if not p.is_bot), None)
            if nouveau_chef: self.createur_sid = nouveau_chef.sid

    def broadcast_etat(self, msg=None):
        if msg: self.message = msg
        v_nom = self.joueurs[self.victime_actuelle_idx].nom if self.victime_actuelle_idx != -1 else ""
        self.verifier_proprietaire()

        socketio.emit('update_jeu', {
            'joueurs': [j.to_dict() for j in self.joueurs],
            'etat': self.etat,
            'joueur_actuel': self.joueurs[self.joueur_actuel_idx].nom if self.joueurs else "",
            'joueur_actuel_sid': self.joueurs[self.joueur_actuel_idx].sid if self.joueurs else "",
            'des_table': self.des_sur_table, 'des_gardes': self.des_gardes,
            'message': self.message, 'valeur_killer': self.valeur_killer,
            'nom_victime': v_nom, 'degats_accumules': self.degats_accumules,
            'vainqueur': self.vainqueur, 'createur_sid': self.createur_sid,
            'room_id': self.id, 'nom_salon': self.nom_salon
        }, to=self.id)
        
        broadcast_game_list()
        
        # Gestion Bots
        cur = self.get_joueur_actuel()
        if cur and cur.is_bot and self.etat != "FIN" and self.etat != "ATTENTE" and self.etat != "ATTRIBUTION_PV":
            socketio.start_background_task(bot_play_turn, self)

    def get_info_publique(self):
        return {'id': self.id, 'nom': self.nom_salon, 'nb_joueurs': len(self.joueurs), 'statut': "En cours" if self.etat not in ["ATTENTE", "FIN"] else "En attente"}

    def get_joueur_actuel(self):
        if not self.joueurs: return None
        return self.joueurs[self.joueur_actuel_idx]

    # --- MODIFICATION PRINCIPALE ICI ---
    def passer_suivant(self):
        # 1. On regarde qui est vivant (PV >= 0) MAINTENANT
        survivants = [j for j in self.joueurs if j.pv >= 0]
        
        # Condition de fin : Il reste 1 seul survivant OU tout le monde est mort (0 survivant)
        if len(self.joueurs) > 1 and len(survivants) <= 1:
            
            if len(survivants) == 1:
                # Cas standard : Il reste un vrai survivant
                self.vainqueur = survivants[0].nom
            else:
                # Cas "Tout le monde est mort ce tour-ci"
                # On départage parmi ceux qui étaient vivants AU DÉBUT DU TOUR
                candidats = [j for j in self.joueurs if j.sid in self.ids_vivants_debut_tour]
                
                # Sécurité (si bug vide), on prend tout le monde
                if not candidats: candidats = self.joueurs
                
                # On trie par PV décroissant (le moins négatif gagne : ex -7 gagne contre -10)
                candidats.sort(key=lambda x: x.pv, reverse=True)
                
                if candidats:
                    self.vainqueur = candidats[0].nom
                else:
                    self.vainqueur = "Personne"

            self.etat = "FIN"
            self.broadcast_etat(f"🏆 VICTOIRE ! {self.vainqueur} gagne !")
        
        else:
            # La partie continue
            self.joueur_actuel_idx = (self.joueur_actuel_idx + 1) % len(self.joueurs)
            self.des_gardes, self.des_sur_table, self.etat = [], [], "TRANSITION_TOUR"
            
            # IMPORTANT : On met à jour la liste des éligibles pour le PROCHAIN tour
            # Seuls ceux qui sont positifs maintenant pourront gagner si tout le monde meurt au prochain tour
            self.ids_vivants_debut_tour = [j.sid for j in survivants]
            
            self.broadcast_etat(f"Au tour de {self.joueurs[self.joueur_actuel_idx].nom}")

    def lancer_des(self, nombre):
        self.des_sur_table = [random.randint(1, 6) for _ in range(nombre)]

    def init_phase_attaque(self):
        self.liste_victimes = []
        nb_j = len(self.joueurs)
        for i in range(1, nb_j):
            cible_idx = (self.joueur_actuel_idx + i) % nb_j
            self.liste_victimes.append(cible_idx)
        self.preparer_prochaine_victime()

    def preparer_prochaine_victime(self):
        if not self.liste_victimes:
            socketio.emit('notification', {'msg': "Tour Killer terminé."}, to=self.id)
            self.passer_suivant() 
            return
        self.victime_actuelle_idx = self.liste_victimes.pop(0)
        self.degats_accumules = 0 
        self.des_gardes = []       
        self.des_sur_table = []    
        self.etat = "ATTENTE_LANCER"
        nom_cible = self.joueurs[self.victime_actuelle_idx].nom
        self.broadcast_etat(f"Prêt à attaquer {nom_cible} ?")

# --- FONCTION BOT VALIDATION PV ---
def bot_validate_sequence(jeu):
    """Simule les bots qui regardent leurs PV et valident"""
    bots = [p for p in jeu.joueurs if p.is_bot]
    for bot in bots:
        time.sleep(random.uniform(1.0, 3.0))
        
        if jeu.etat != "ATTRIBUTION_PV": return

        with app.app_context():
            bot.est_pret = True
            socketio.emit('notification', {'msg': f"🤖 {bot.nom} a validé ses PV.", 'sound':'dice'}, to=jeu.id)
            jeu.broadcast_etat()
            check_start_real_game(jeu)

def check_start_real_game(jeu):
    """Vérifie si tout le monde a validé pour lancer le 1er tour"""
    if all(p.est_pret for p in jeu.joueurs):
        jeu.joueurs.sort(key=lambda p: p.pv) # On trie par PV
        jeu.joueur_actuel_idx = 0
        jeu.etat = "TRANSITION_TOUR"
        
        # MODIFICATION : Initialisation des "Vivants" au tout début de la partie réelle
        jeu.ids_vivants_debut_tour = [p.sid for p in jeu.joueurs if p.pv >= 0]
        
        noms = " > ".join([p.nom for p in jeu.joueurs])
        emit('notification', {'msg': f"Tout le monde est prêt ! Ordre : {noms}", 'sound': 'win'}, to=jeu.id)
        jeu.broadcast_etat("La partie commence !")

# --- CERVEAU DU BOT (JEU) ---
def bot_play_turn(jeu):
    time.sleep(1.5)
    cur = jeu.get_joueur_actuel()
    if not cur or not cur.is_bot: return 

    with app.app_context():
        if jeu.etat == "TRANSITION_TOUR":
            jeu.etat = "TOUR_CHOIX"
            jeu.lancer_des(5)
            jeu.broadcast_etat("Le Bot lance les dés...")

        elif jeu.etat == "TOUR_CHOIX":
            des = jeu.des_sur_table
            
            def val_low(d):
                if d == 1: return 6000
                if d == 2: return 4000
                if d == 3: return 2000
                if d == 4: return 1000
                if d == 5: return 500
                return 0 

            def val_high(d):
                if d == 6: return 6000
                if d == 5: return 4000
                if d == 4: return 2000
                if d == 3: return 1000
                if d == 2: return 500
                return 0

            mode = "NEUTRE"
            nb_low_gardes = len([d for d in jeu.des_gardes if d <= 3])
            nb_high_gardes = len([d for d in jeu.des_gardes if d >= 4])
            
            if nb_high_gardes > nb_low_gardes: mode = "HIGH"
            elif nb_low_gardes > nb_high_gardes: mode = "LOW"
            else:
                score_total_low = sum([val_low(d) for d in des])
                score_total_high = sum([val_high(d) for d in des])
                if score_total_low >= score_total_high: mode = "LOW"
                else: mode = "HIGH"

            indices_finaux = []
            if mode == "LOW":
                for i, val in enumerate(des):
                    if val_low(val) >= 4000: indices_finaux.append(i)
                if not indices_finaux:
                    for i, val in enumerate(des):
                        if val == 3: indices_finaux.append(i)
            else:
                for i, val in enumerate(des):
                    if val_high(val) >= 4000: indices_finaux.append(i)
                if not indices_finaux:
                    for i, val in enumerate(des):
                        if val == 4: indices_finaux.append(i)

            if not indices_finaux and des:
                if mode == "LOW":
                    scores = [val_low(d) for d in des]
                    indices_finaux = [scores.index(max(scores))]
                else:
                    scores = [val_high(d) for d in des]
                    indices_finaux = [scores.index(max(scores))]

            indices_finaux = list(set(indices_finaux))
            indices_finaux.sort(reverse=True)
            for i in indices_finaux: jeu.des_gardes.append(jeu.des_sur_table.pop(i))
            
            if len(jeu.des_gardes) == 5:
                s = sum(jeu.des_gardes); j = jeu.get_joueur_actuel()
                if 5<=s<=10:
                    jeu.valeur_killer = 11 - s
                    socketio.emit('notification', {'msg': f"🤖 Bot KILLER {jeu.valeur_killer} !", 'sound':'sword'}, to=jeu.id)
                    jeu.init_phase_attaque()
                elif s==11 or s==24:
                    jeu.etat, jeu.des_gardes, jeu.des_sur_table = "TOUR_REGEN", [], []
                    socketio.emit('notification', {'msg': "🤖 Bot Regen !", 'sound':'dice'}, to=jeu.id)
                    jeu.broadcast_etat()
                elif 12<=s<=17:
                    p=s-11; j.pv-=p; socketio.emit('notification', {'msg': f"🤖 Bot -{p} PV", 'sound':'oof'}, to=jeu.id); jeu.passer_suivant()
                elif 18<=s<=23:
                    p=24-s; j.pv-=p; socketio.emit('notification', {'msg': f"🤖 Bot -{p} PV", 'sound':'oof'}, to=jeu.id); jeu.passer_suivant()
                elif 25<=s<=30:
                    jeu.valeur_killer = s - 24
                    socketio.emit('notification', {'msg': f"🤖 Bot KILLER {jeu.valeur_killer} !", 'sound':'sword'}, to=jeu.id)
                    jeu.init_phase_attaque()
                else: jeu.passer_suivant()
            else:
                jeu.lancer_des(5 - len(jeu.des_gardes))
                jeu.broadcast_etat(f"Bot joue {mode}...")

        elif jeu.etat == "TOUR_REGEN":
            v = random.randint(1,6); jeu.des_sur_table = [v]; jeu.joueurs[jeu.joueur_actuel_idx].pv += v
            jeu.etat = "RESULTAT_REGEN"
            socketio.emit('notification', {'msg': f"🤖 Bot +{v} PV", 'sound':'dice'}, to=jeu.id)
            jeu.broadcast_etat()
        elif jeu.etat == "RESULTAT_REGEN": jeu.passer_suivant()
        elif jeu.etat == "ATTENTE_LANCER":
            jeu.lancer_des(5)
            if jeu.valeur_killer in jeu.des_sur_table: jeu.etat = "TOUR_ATTAQUE"
            else: 
                jeu.etat = "ATTAQUE_RATEE"; socketio.emit('notification', {'msg': "🤖 Bot rate son attaque."}, to=jeu.id)
            jeu.broadcast_etat()
        elif jeu.etat == "TOUR_ATTAQUE":
            ind = [i for i, x in enumerate(jeu.des_sur_table) if x == jeu.valeur_killer]
            if ind:
                ind.sort(reverse=True)
                for i in ind: v = jeu.des_sur_table.pop(i); jeu.des_gardes.append(v); jeu.degats_accumules += v
                if len(jeu.des_gardes) == 5:
                    jeu.des_gardes = []; socketio.emit('notification', {'msg': "🤖 Bot FULL ! Relance !"}, to=jeu.id); jeu.lancer_des(5)
                    if jeu.valeur_killer not in jeu.des_sur_table: jeu.etat = "FIN_ATTAQUE"
                else:
                    jeu.lancer_des(5 - len(jeu.des_gardes))
                    if jeu.valeur_killer not in jeu.des_sur_table: jeu.etat = "FIN_ATTAQUE"
                jeu.broadcast_etat("Le Bot attaque...")
            else: jeu.etat = "FIN_ATTAQUE"; jeu.broadcast_etat()
        elif jeu.etat == "FIN_ATTAQUE" or jeu.etat == "ATTAQUE_RATEE":
            vic = jeu.joueurs[jeu.victime_actuelle_idx]
            if jeu.degats_accumules > 0:
                vic.pv -= jeu.degats_accumules
                socketio.emit('notification', {'msg': f"💥 Bot inflige {jeu.degats_accumules} dégâts !", 'sound':'punch'}, to=jeu.id)
            else: socketio.emit('notification', {'msg': "Bot finit sans dégâts."}, to=jeu.id)
            jeu.etat = "RESULTAT_ATTAQUE"; jeu.broadcast_etat()
        elif jeu.etat == "RESULTAT_ATTAQUE": jeu.preparer_prochaine_victime()

# --- ROUTES & EVENTS ---
def broadcast_game_list(): socketio.emit('update_game_list', [g.get_info_publique() for g in games.values()], to='hall')
def get_game(sid):
    rid = sid_to_room.get(sid)
    return games[rid] if rid in games else None

@app.route('/')
def index(): return render_template('index.html', room_id=request.args.get('room', ""))

@socketio.on('join_hall')
def handle_hall(): join_room('hall'); broadcast_game_list()

@socketio.on('creer_salon')
def handle_create(data):
    rid = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
    games[rid] = Partie(rid, data.get('nom_salon', 'Salon'))
    emit('salon_cree', {'room_id': rid}); broadcast_game_list()

@socketio.on('rejoindre')
def handle_join(data):
    rid, nom = data['room_id'], data['nom']
    if rid in games:
        leave_room('hall'); join_room(rid); sid_to_room[request.sid] = rid
        jeu = games[rid]; jeu.joueurs.append(Joueur(request.sid, nom))
        jeu.verifier_proprietaire()
        if not jeu.createur_sid: jeu.createur_sid = request.sid
        jeu.broadcast_etat(f"{nom} a rejoint")

@socketio.on('ajouter_bot')
def handle_add_bot():
    jeu = get_game(request.sid)
    if jeu and request.sid == jeu.createur_sid and jeu.etat == "ATTENTE":
        nb = len([j for j in jeu.joueurs if j.is_bot]) + 1
        fake = f"BOT_{jeu.id}_{nb}"
        jeu.joueurs.append(Joueur(fake, f"Bot {nb}", True))
        jeu.broadcast_etat(f"Bot {nb} ajouté !")

@socketio.on('disconnect')
def handle_disconnect():
    jeu = get_game(request.sid)
    if request.sid in admin_sids: admin_sids.remove(request.sid)
    if jeu:
        j = next((p for p in jeu.joueurs if p.sid == request.sid), None)
        if j:
            jeu.joueurs.remove(j)
            if jeu.createur_sid == request.sid:
                jeu.verifier_proprietaire()
                if jeu.createur_sid == request.sid:
                     del games[jeu.id]; broadcast_game_list(); return
            if jeu.etat != "ATTENTE" and jeu.etat != "FIN" and len(jeu.joueurs) > 0:
                if jeu.joueur_actuel_idx >= len(jeu.joueurs): jeu.joueur_actuel_idx = 0
                if j.nom == jeu.get_joueur_actuel().nom: jeu.passer_suivant()
            jeu.broadcast_etat(f"{j.nom} a quitté.")
    if request.sid in sid_to_room: del sid_to_room[request.sid]

# --- ADMIN PANEL ---
@socketio.on('admin_login')
def handle_admin_login(data):
    if data.get('password') == '12345':
        admin_sids.add(request.sid)
        emit('admin_success', {'msg': "Mode Admin Activé"})
        broadcast_game_list() 
        jeu = get_game(request.sid)
        if jeu: jeu.broadcast_etat()

@socketio.on('admin_kick')
def handle_admin_kick(data):
    if request.sid not in admin_sids: return
    target_sid = data.get('target_sid')
    jeu = get_game(request.sid)
    if jeu:
        target = next((p for p in jeu.joueurs if p.sid == target_sid), None)
        if target:
            jeu.joueurs.remove(target)
            jeu.broadcast_etat(f"ADMIN: {target.nom} a été exclu !")
            socketio.emit('force_quit', to=target_sid)
            jeu.verifier_proprietaire()
            if len(jeu.joueurs) > 0 and jeu.joueur_actuel_idx >= len(jeu.joueurs): jeu.joueur_actuel_idx = 0

@socketio.on('admin_delete_room')
def handle_admin_delete_room(data):
    if request.sid in admin_sids:
        rid = data.get('room_id')
        if rid in games:
            socketio.emit('force_quit', to=rid) 
            del games[rid]
            broadcast_game_list()

@socketio.on('fermer_salon')
def handle_close():
    jeu = get_game(request.sid)
    if jeu and (request.sid == jeu.createur_sid or request.sid in admin_sids): 
        socketio.emit('force_quit', to=jeu.id); del games[jeu.id]; broadcast_game_list()

# --- JEU ACTIONS ---
@socketio.on('demarrer_partie')
def handle_demarrer():
    jeu = get_game(request.sid)
    if jeu and len(jeu.joueurs) >= 2:
        jeu.etat = "ATTRIBUTION_PV"
        
        for j in jeu.joueurs:
            j.des_pv = [random.randint(1,6) for _ in range(5)]
            j.pv = sum(j.des_pv)
            j.est_pret = False 
        
        jeu.broadcast_etat("Initialisation des PV...")
        socketio.start_background_task(bot_validate_sequence, jeu)

@socketio.on('valider_pv')
def handle_valider_pv():
    jeu = get_game(request.sid)
    if jeu and jeu.etat == "ATTRIBUTION_PV":
        joueur = next((p for p in jeu.joueurs if p.sid == request.sid), None)
        if joueur:
            joueur.est_pret = True
            jeu.broadcast_etat() 
            check_start_real_game(jeu)

@socketio.on('valider_debut_tour')
def handle_val():
    jeu = get_game(request.sid)
    if jeu and request.sid == jeu.joueurs[jeu.joueur_actuel_idx].sid:
        jeu.etat, jeu.des_sur_table = "TOUR_CHOIX", [random.randint(1,6) for _ in range(5)]
        jeu.broadcast_etat("À toi de jouer !")

@socketio.on('action_garder')
def handle_garder(indices):
    jeu = get_game(request.sid)
    if not jeu or jeu.etat != "TOUR_CHOIX": return
    indices.sort(reverse=True)
    for i in indices: jeu.des_gardes.append(jeu.des_sur_table.pop(i))
    
    if len(jeu.des_gardes) == 5:
        s, j = sum(jeu.des_gardes), jeu.joueurs[jeu.joueur_actuel_idx]
        if 5<=s<=10: 
            jeu.valeur_killer, jeu.etat = 11-s, "ATTENTE_LANCER"
            emit('notification', {'msg': f"KILLER {jeu.valeur_killer}!", 'sound':'sword'}, to=jeu.id)
        elif s==11 or s==24: 
            jeu.etat, jeu.des_gardes, jeu.des_sur_table = "TOUR_REGEN", [], []
            emit('notification',{'msg':f"Score {s}: Régénération !"},to=jeu.id)
            jeu.broadcast_etat(); return
        elif 12<=s<=17: 
            p=s-11; j.pv-=p; emit('notification',{'msg':f"Score {s}: -{p} PV", 'sound':'oof'},to=jeu.id); jeu.passer_suivant(); return
        elif 18<=s<=23: 
            p=24-s; j.pv-=p; emit('notification',{'msg':f"Score {s}: -{p} PV", 'sound':'oof'},to=jeu.id); jeu.passer_suivant(); return
        elif 25<=s<=30: 
            jeu.valeur_killer, jeu.etat = s-24, "ATTENTE_LANCER"
            emit('notification', {'msg': f"KILLER {jeu.valeur_killer}!", 'sound':'sword'}, to=jeu.id)
        
        if jeu.etat == "ATTENTE_LANCER": 
            jeu.liste_victimes = [(jeu.joueur_actuel_idx + i)%len(jeu.joueurs) for i in range(1,len(jeu.joueurs))]
            jeu.victime_actuelle_idx = jeu.liste_victimes.pop(0); jeu.degats_accumules, jeu.des_gardes = 0, []
        jeu.broadcast_etat()
    else: 
        jeu.des_sur_table = [random.randint(1,6) for _ in range(5-len(jeu.des_gardes))]
        jeu.broadcast_etat("Relance...")

@socketio.on('action_lancer_regen')
def handle_regen_roll():
    jeu = get_game(request.sid)
    if jeu and jeu.etat == "TOUR_REGEN":
        v = random.randint(1,6); jeu.des_sur_table = [v]; jeu.joueurs[jeu.joueur_actuel_idx].pv += v
        jeu.etat = "RESULTAT_REGEN"
        emit('notification', {'msg': f"Régénération +{v} PV", 'sound':'dice'}, to=jeu.id)
        jeu.broadcast_etat(f"Gain de {v} PV !")

@socketio.on('action_fin_regen')
def handle_regen_end():
    jeu = get_game(request.sid)
    if jeu and jeu.etat == "RESULTAT_REGEN": jeu.passer_suivant()

@socketio.on('action_lancer_attaque')
def handle_atk():
    jeu = get_game(request.sid)
    if jeu:
        jeu.des_sur_table = [random.randint(1,6) for _ in range(5)]
        if jeu.valeur_killer in jeu.des_sur_table: jeu.etat = "TOUR_ATTAQUE"; jeu.broadcast_etat("Choisis tes dés !")
        else: jeu.etat = "ATTAQUE_RATEE"; jeu.broadcast_etat("Raté !")

@socketio.on('action_garder_attaque')
def handle_g_atk(indices):
    jeu = get_game(request.sid)
    if not jeu or jeu.etat != "TOUR_ATTAQUE": return
    indices.sort(reverse=True)
    for i in indices: v=jeu.des_sur_table.pop(i); jeu.des_gardes.append(v); jeu.degats_accumules+=v
    
    if len(jeu.des_gardes)==5:
        jeu.des_gardes=[]; emit('notification', {'msg': "FULL ! Relance 5 dés !", 'sound':'sword'}, to=jeu.id)
        jeu.des_sur_table=[random.randint(1,6) for _ in range(5)]
        if jeu.valeur_killer not in jeu.des_sur_table: jeu.etat = "FIN_ATTAQUE"
    else:
        jeu.des_sur_table=[random.randint(1,6) for _ in range(5-len(jeu.des_gardes))]
        if jeu.valeur_killer not in jeu.des_sur_table: jeu.etat = "FIN_ATTAQUE"
    jeu.broadcast_etat()

@socketio.on('action_terminer_attaque')
def handle_fin_atk():
    jeu = get_game(request.sid)
    if jeu and (jeu.etat == "FIN_ATTAQUE" or jeu.etat == "ATTAQUE_RATEE"):
        victime = jeu.joueurs[jeu.victime_actuelle_idx]
        if jeu.degats_accumules > 0:
            victime.pv -= jeu.degats_accumules
            emit('notification', {'msg': f"💥 -{jeu.degats_accumules} pour {victime.nom}", 'sound':'punch'}, to=jeu.id)
        else:
            emit('notification', {'msg': "Aucun dégât."}, to=jeu.id)
        
        jeu.preparer_prochaine_victime()

@socketio.on('action_suivant')
def handle_next():
    jeu = get_game(request.sid)
    if jeu.liste_victimes: jeu.victime_actuelle_idx=jeu.liste_victimes.pop(0); jeu.degats_accumules,jeu.des_gardes,jeu.des_sur_table,jeu.etat=0,[],[],"ATTENTE_LANCER"; jeu.broadcast_etat()
    else: jeu.passer_suivant()

@socketio.on('rejouer_partie')
def handle_replay():
    jeu = get_game(request.sid)
    if jeu and request.sid == jeu.createur_sid: jeu.reset_jeu(); handle_demarrer()

if __name__ == '__main__': socketio.run(app, debug=True)