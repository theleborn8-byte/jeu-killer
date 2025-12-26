from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room, leave_room
import random
import string

app = Flask(__name__)
app.config['SECRET_KEY'] = 'killer_secret_key'
socketio = SocketIO(app)

# --- CLASSES ---
class Joueur:
    def __init__(self, sid, nom):
        self.sid = sid
        self.nom = nom
        self.pv = 0
    def to_dict(self):
        return {'nom': self.nom, 'pv': self.pv, 'sid': self.sid}

class Partie:
    def __init__(self, room_id, nom_salon):
        self.id = room_id
        self.nom_salon = nom_salon
        self.joueurs = []
        self.reset_jeu()

    def reset_jeu(self):
        # ETATS : ATTENTE, TRANSITION_TOUR, TOUR_CHOIX, TOUR_REGEN, RESULTAT_REGEN, 
        # ATTENTE_LANCER, TOUR_ATTAQUE, FIN_ATTAQUE, ATTAQUE_RATEE, RESULTAT_ATTAQUE, FIN
        self.etat = "ATTENTE" 
        self.joueur_actuel_idx = 0
        self.des_sur_table = []
        self.des_gardes = [] 
        self.message = "En attente..."
        self.vainqueur = None
        self.createur_sid = self.joueurs[0].sid if self.joueurs else None
        self.valeur_killer = 0
        self.liste_victimes = [] 
        self.victime_actuelle_idx = -1
        self.degats_accumules = 0 

    def get_info_publique(self):
        return {
            'id': self.id,
            'nom': self.nom_salon,
            'nb_joueurs': len(self.joueurs),
            'statut': "En cours" if self.etat not in ["ATTENTE", "FIN"] else "En attente"
        }

    def get_joueur_actuel(self):
        if not self.joueurs: return None
        return self.joueurs[self.joueur_actuel_idx]

    def broadcast_etat(self, msg_specifique=None):
        if msg_specifique: self.message = msg_specifique
        
        nom_victime = ""
        if self.victime_actuelle_idx != -1:
             nom_victime = self.joueurs[self.victime_actuelle_idx].nom

        data = {
            'joueurs': [j.to_dict() for j in self.joueurs],
            'etat': self.etat,
            'joueur_actuel': self.get_joueur_actuel().nom if self.joueurs else "",
            'joueur_actuel_sid': self.get_joueur_actuel().sid if self.joueurs else "",
            'des_table': self.des_sur_table,
            'des_gardes': self.des_gardes,
            'message': self.message,
            'valeur_killer': self.valeur_killer,
            'nom_victime': nom_victime,
            'degats_accumules': self.degats_accumules,
            'vainqueur': self.vainqueur,
            'createur_sid': self.createur_sid,
            'room_id': self.id,
            'nom_salon': self.nom_salon
        }
        socketio.emit('update_jeu', data, to=self.id)
        broadcast_game_list()

    def verifier_victoire(self):
        survivants = [j for j in self.joueurs if j.pv >= 0]
        if len(self.joueurs) > 1 and len(survivants) <= 1:
            self.vainqueur = survivants[0].nom if survivants else "Personne"
            self.etat = "FIN"
            self.message = f"🏆 VICTOIRE ! {self.vainqueur} gagne !"
            self.broadcast_etat()
            return True
        elif len(survivants) == 0:
            self.vainqueur = "Personne"
            self.etat = "FIN"
            self.message = "Match nul ?"
            self.broadcast_etat()
            return True
        return False

    def passer_au_joueur_suivant(self):
        if self.verifier_victoire(): return
        self.joueur_actuel_idx = (self.joueur_actuel_idx + 1) % len(self.joueurs)
        self.des_gardes = []
        self.des_sur_table = []
        self.etat = "TRANSITION_TOUR"
        self.broadcast_etat(f"Au tour de {self.get_joueur_actuel().nom}.")

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
            self.passer_au_joueur_suivant()
            return
        self.victime_actuelle_idx = self.liste_victimes.pop(0)
        self.degats_accumules = 0 
        self.des_gardes = []      
        self.des_sur_table = []   
        self.etat = "ATTENTE_LANCER"
        nom_cible = self.joueurs[self.victime_actuelle_idx].nom
        self.broadcast_etat(f"Prêt à attaquer {nom_cible} ?")

# --- GESTION DES SALONS ---
games = {}        
sid_to_room = {}  

def broadcast_game_list():
    socketio.emit('update_game_list', [g.get_info_publique() for g in games.values()], to='hall')

def get_game(sid):
    room_id = sid_to_room.get(sid)
    if room_id and room_id in games: return games[room_id]
    return None

@app.route('/')
def index(): return render_template('index.html', room_id=request.args.get('room', ""))

@socketio.on('join_hall')
def handle_join_hall(): join_room('hall'); broadcast_game_list()

@socketio.on('creer_salon')
def handle_creer_salon(data):
    rid = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
    games[rid] = Partie(rid, data.get('nom_salon', 'Salon'))
    emit('salon_cree', {'room_id': rid})
    broadcast_game_list()

@socketio.on('rejoindre')
def handle_rejoindre(data):
    rid, nom = data['room_id'], data['nom']
    if rid in games:
        leave_room('hall')
        join_room(rid); sid_to_room[request.sid] = rid
        jeu = games[rid]; jeu.joueurs.append(Joueur(request.sid, nom))
        if not jeu.createur_sid: jeu.createur_sid = request.sid
        jeu.broadcast_etat(f"{nom} a rejoint")

@socketio.on('disconnect')
def handle_disconnect():
    jeu = get_game(request.sid)
    if jeu:
        j = next((p for p in jeu.joueurs if p.sid == request.sid), None)
        if j:
            jeu.joueurs.remove(j)
            if jeu.createur_sid == request.sid:
                if jeu.joueurs: jeu.createur_sid = jeu.joueurs[0].sid
                else: del games[jeu.id]; broadcast_game_list(); return
            jeu.broadcast_etat(f"{j.nom} a quitté.")
    if request.sid in sid_to_room: del sid_to_room[request.sid]

# --- ACTIONS JEU ---

@socketio.on('demarrer_partie')
def handle_demarrer():
    jeu = get_game(request.sid)
    if jeu and len(jeu.joueurs) >= 2:
        for j in jeu.joueurs: 
            j.pv = sum([random.randint(1,6) for _ in range(5)])
        jeu.joueurs.sort(key=lambda p: p.pv)
        jeu.joueur_actuel_idx = 0
        jeu.etat = "TRANSITION_TOUR"
        noms_ordonnes = " > ".join([p.nom for p in jeu.joueurs])
        socketio.emit('notification', {'msg': f"Ordre : {noms_ordonnes}"}, to=jeu.id)
        jeu.broadcast_etat("La partie commence !")

@socketio.on('valider_debut_tour')
def handle_valider_debut_tour():
    jeu = get_game(request.sid)
    if jeu and request.sid == jeu.get_joueur_actuel().sid:
        jeu.etat = "TOUR_CHOIX"
        jeu.lancer_des(5)
        jeu.broadcast_etat("C'est parti !")

@socketio.on('action_garder')
def handle_garder(indices):
    jeu = get_game(request.sid)
    if not jeu or jeu.etat != "TOUR_CHOIX": return
    indices.sort(reverse=True)
    for i in indices: jeu.des_gardes.append(jeu.des_sur_table.pop(i))
    
    if len(jeu.des_gardes) == 5:
        score = sum(jeu.des_gardes)
        joueur = jeu.get_joueur_actuel()
        msg = f"Score : {score}. "

        if 5 <= score <= 10:
            jeu.valeur_killer = 11 - score 
            msg += f"🔥 KILLER (Force {jeu.valeur_killer}) !"
            socketio.emit('notification', {'msg': msg, 'sound': 'sword'}, to=jeu.id)
            jeu.init_phase_attaque()

        elif score == 11 or score == 24:
            jeu.etat = "TOUR_REGEN"
            jeu.des_gardes = [] 
            jeu.des_sur_table = []
            socketio.emit('notification', {'msg': f"Score {score} : Régénération !", 'sound': 'dice'}, to=jeu.id)
            jeu.broadcast_etat("Phase de Régénération...")
            return 

        elif 12 <= score <= 17:
            perte = score - 11
            joueur.pv -= perte
            msg += f"⚠️ Echec : -{perte} PV."
            socketio.emit('notification', {'msg': msg, 'sound': 'oof'}, to=jeu.id)
            jeu.passer_au_joueur_suivant()

        elif 18 <= score <= 23:
            perte = 24 - score
            joueur.pv -= perte
            msg += f"⚠️ Echec : -{perte} PV."
            socketio.emit('notification', {'msg': msg, 'sound': 'oof'}, to=jeu.id)
            jeu.passer_au_joueur_suivant()

        elif 25 <= score <= 30:
            jeu.valeur_killer = score - 24
            msg += f"⚔️ KILLER (Force {jeu.valeur_killer}) !"
            socketio.emit('notification', {'msg': msg, 'sound': 'sword'}, to=jeu.id)
            jeu.init_phase_attaque()
        else:
            jeu.passer_au_joueur_suivant()
    else:
        jeu.lancer_des(5 - len(jeu.des_gardes))
        jeu.broadcast_etat("Relance...")

@socketio.on('action_lancer_regen')
def handle_lancer_regen():
    jeu = get_game(request.sid)
    if jeu and jeu.etat == "TOUR_REGEN" and request.sid == jeu.get_joueur_actuel().sid:
        val = random.randint(1, 6)
        jeu.des_sur_table = [val]
        jeu.joueurs[jeu.joueur_actuel_idx].pv += val
        jeu.etat = "RESULTAT_REGEN"
        socketio.emit('notification', {'msg': f"Régénération : +{val} PV !", 'sound': 'dice'}, to=jeu.id)
        jeu.broadcast_etat(f"Gain de {val} PV !")

@socketio.on('action_fin_regen')
def handle_fin_regen():
    jeu = get_game(request.sid)
    if jeu and jeu.etat == "RESULTAT_REGEN" and request.sid == jeu.get_joueur_actuel().sid:
        jeu.passer_au_joueur_suivant()

@socketio.on('action_lancer_attaque')
def handle_lancer_attaque():
    jeu = get_game(request.sid)
    if jeu:
        jeu.lancer_des(5)
        # Premier lancer de l'attaque
        if jeu.valeur_killer in jeu.des_sur_table: 
            jeu.etat = "TOUR_ATTAQUE"
            jeu.broadcast_etat("Choisis tes dés d'attaque !")
        else: 
            # Raté direct
            jeu.etat = "ATTAQUE_RATEE"
            socketio.emit('notification', {'msg': "Attaque ratée (Aucun Killer)."}, to=jeu.id)
            jeu.broadcast_etat("Raté !")

@socketio.on('action_garder_attaque')
def handle_garder_attaque(indices):
    jeu = get_game(request.sid)
    if not jeu or jeu.etat != "TOUR_ATTAQUE": return
    
    indices.sort(reverse=True)
    for i in indices:
        v = jeu.des_sur_table.pop(i)
        jeu.des_gardes.append(v)
        jeu.degats_accumules += v
    
    nb_restant = 5 - len(jeu.des_gardes)
    
    if nb_restant == 0:
        # FULL
        jeu.des_gardes = [] 
        socketio.emit('notification', {'msg': "🔥 FULL ! 5 dés ! Tu continues avec 5 nouveaux dés !"}, to=jeu.id)
        jeu.lancer_des(5)
        
        if jeu.valeur_killer not in jeu.des_sur_table:
            # Full suivi d'un échec
            jeu.etat = "FIN_ATTAQUE"
            socketio.emit('notification', {'msg': "Pas de Killer sur la relance. Fin de série."}, to=jeu.id)
            jeu.broadcast_etat("Fin de série. Frapper ?")
        else:
            jeu.etat = "TOUR_ATTAQUE"
            jeu.broadcast_etat("BONUS FULL ! Continue !")
            
    else:
        # Relance normale
        jeu.lancer_des(nb_restant)
        
        if jeu.valeur_killer not in jeu.des_sur_table:
            # Plus de Killer
            jeu.etat = "FIN_ATTAQUE"
            socketio.emit('notification', {'msg': "Plus de Killer. Tu dois frapper."}, to=jeu.id)
            jeu.broadcast_etat("Plus de dés. Frapper ?")
        else:
            # Encore des Killer
            jeu.etat = "TOUR_ATTAQUE"
            jeu.broadcast_etat("Encore des touches possibles...")

@socketio.on('action_terminer_attaque')
def handle_fin_atk():
    jeu = get_game(request.sid)
    if jeu and (jeu.etat == "FIN_ATTAQUE" or jeu.etat == "ATTAQUE_RATEE"):
        victime = jeu.joueurs[jeu.victime_actuelle_idx]
        if jeu.degats_accumules > 0:
            victime.pv -= jeu.degats_accumules
            socketio.emit('notification', {'msg': f"💥 BOOM ! -{jeu.degats_accumules} PV pour {victime.nom}", 'sound':'punch'}, to=jeu.id)
        else:
            emit('notification', {'msg': "Attaque terminée sans dégâts."}, to=jeu.id)
        
        jeu.etat = "RESULTAT_ATTAQUE"
        jeu.broadcast_etat("Attaque terminée.")

def FinAttaque(jeu):
    victime = jeu.joueurs[jeu.victime_actuelle_idx]
    if jeu.degats_accumules > 0:
        victime.pv -= jeu.degats_accumules
        socketio.emit('notification', {'msg': f"💥 BOOM ! -{jeu.degats_accumules} PV pour {victime.nom}", 'sound': 'punch'}, to=jeu.id)
    else:
        socketio.emit('notification', {'msg': "Attaque terminée sans dégâts.", 'sound': 'oof'}, to=jeu.id)
        
    jeu.etat = "RESULTAT_ATTAQUE"
    jeu.broadcast_etat("Attaque terminée.")

@socketio.on('action_suivant')
def handle_suivant():
    jeu = get_game(request.sid)
    jeu.preparer_prochaine_victime()

@socketio.on('rejouer_partie')
def handle_rejouer():
    jeu = get_game(request.sid)
    if jeu and request.sid == jeu.createur_sid: jeu.reset_jeu(); handle_demarrer()

@socketio.on('fermer_salon')
def handle_fermer():
    jeu = get_game(request.sid)
    if jeu and request.sid == jeu.createur_sid: 
        socketio.emit('force_quit', to=jeu.id); del games[jeu.id]; broadcast_game_list()

if __name__ == '__main__': socketio.run(app, debug=True)