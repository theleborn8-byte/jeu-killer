from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit
import random

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret_key_killer_game'
socketio = SocketIO(app)

# --- CLASSES ET LOGIQUE ---

class Joueur:
    def __init__(self, sid, nom):
        self.sid = sid
        self.nom = nom
        self.pv = 0

    def to_dict(self):
        return {'nom': self.nom, 'pv': self.pv, 'sid': self.sid}

class Partie:
    def __init__(self):
        self.reset()

    def reset(self):
        self.joueurs = []
        # ETATS : ATTENTE, INIT_PV, TRANSITION_TOUR, TOUR_CHOIX, ATTENTE_LANCER, TOUR_ATTAQUE, RESULTAT_ATTAQUE, FIN
        self.etat = "ATTENTE" 
        self.joueur_actuel_idx = 0
        self.des_sur_table = []
        self.des_gardes = [] 
        self.message = "En attente de joueurs..."
        self.vainqueur = None
        self.createur_sid = None 
        
        # Variables Killer
        self.valeur_killer = 0
        self.liste_victimes = [] 
        self.victime_actuelle_idx = -1
        self.degats_accumules = 0 

    def get_joueur_actuel(self):
        if not self.joueurs: return None
        return self.joueurs[self.joueur_actuel_idx]

    def verifier_victoire(self):
        # MODIFICATION ICI : On survit tant qu'on est >= 0.
        # On n'est "éliminé" (pour le calcul de fin) que si on est < 0.
        survivants = [j for j in self.joueurs if j.pv >= 0]
        
        if len(self.joueurs) > 1:
            if len(survivants) == 1:
                # Un seul joueur n'est pas négatif -> C'est le vainqueur
                self.vainqueur = survivants[0].nom
                self.etat = "FIN"
                self.message = f"🏆 VICTOIRE ! {self.vainqueur} est le dernier survivant !"
                self.broadcast_etat()
                return True
            elif len(survivants) == 0:
                # Tout le monde est négatif
                self.vainqueur = "Personne"
                self.etat = "FIN"
                self.message = "Tout le monde est dans le négatif... Match nul ?"
                self.broadcast_etat()
                return True
        return False

    def passer_au_joueur_suivant(self):
        if self.verifier_victoire(): return
        self.joueur_actuel_idx = (self.joueur_actuel_idx + 1) % len(self.joueurs)
        
        self.des_gardes = []
        self.des_sur_table = []
        
        self.etat = "TRANSITION_TOUR"
        self.broadcast_etat(f"Au tour de {self.get_joueur_actuel().nom} de jouer.")

    def lancer_des(self, nombre):
        self.des_sur_table = [random.randint(1, 6) for _ in range(nombre)]

    def broadcast_etat(self, msg_specifique=None):
        if msg_specifique: self.message = msg_specifique
        
        nom_victime = ""
        if self.victime_actuelle_idx != -1 and self.etat in ["ATTENTE_LANCER", "TOUR_ATTAQUE", "RESULTAT_ATTAQUE"]:
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
            'createur_sid': self.createur_sid
        }
        socketio.emit('update_jeu', data)

    # --- LOGIQUE PHASE KILLER ---

    def init_phase_attaque(self):
        self.liste_victimes = []
        nb_j = len(self.joueurs)
        for i in range(1, nb_j):
            cible_idx = (self.joueur_actuel_idx + i) % nb_j
            self.liste_victimes.append(cible_idx)
        
        self.preparer_prochaine_victime()

    def preparer_prochaine_victime(self):
        if not self.liste_victimes:
            socketio.emit('notification', {'msg': "Tour Killer terminé. Retour au jeu normal."})
            self.passer_au_joueur_suivant()
            return

        self.victime_actuelle_idx = self.liste_victimes.pop(0)
        self.degats_accumules = 0 
        self.des_gardes = []      
        self.des_sur_table = []   
        
        self.etat = "ATTENTE_LANCER"
        nom_cible = self.joueurs[self.victime_actuelle_idx].nom
        self.broadcast_etat(f"Prêt à attaquer {nom_cible} ?")

jeu = Partie()

# --- ROUTES & EVENTS SOCKET ---

@app.route('/')
def index(): return render_template('index.html')

@socketio.on('disconnect')
def handle_disconnect():
    joueur_parti = None
    for j in jeu.joueurs:
        if j.sid == request.sid:
            joueur_parti = j
            break
    
    if joueur_parti:
        jeu.joueurs.remove(joueur_parti)
        
        if jeu.createur_sid == request.sid:
            if len(jeu.joueurs) > 0:
                jeu.createur_sid = jeu.joueurs[0].sid
            else:
                jeu.createur_sid = None
                jeu.reset()
                return

        jeu.broadcast_etat(f"{joueur_parti.nom} a quitté la partie.")

@socketio.on('rejoindre')
def handle_rejoindre(nom):
    if jeu.etat != "ATTENTE": return
    for j in jeu.joueurs:
        if j.sid == request.sid: return
    
    nouveau = Joueur(request.sid, nom)
    jeu.joueurs.append(nouveau)
    
    if jeu.createur_sid is None:
        jeu.createur_sid = nouveau.sid
        
    jeu.broadcast_etat(f"{nom} a rejoint.")

@socketio.on('demarrer_partie')
def handle_demarrer():
    if len(jeu.joueurs) < 2: return
    jeu.etat = "INIT_PV"
    min_pv = 1000
    starter_idx = 0
    log = []
    
    for idx, j in enumerate(jeu.joueurs):
        lancer = [random.randint(1, 6) for _ in range(5)]
        total = sum(lancer)
        j.pv = total
        log.append(f"{j.nom}: {total}")
        if total < min_pv:
            min_pv = total
            starter_idx = idx
            
    jeu.joueurs = jeu.joueurs[starter_idx:] + jeu.joueurs[:starter_idx]
    socketio.emit('notification', {'msg': "PV Initiaux : " + ", ".join(log)})
    
    jeu.joueur_actuel_idx = 0
    jeu.etat = "TRANSITION_TOUR"
    jeu.broadcast_etat(f"La partie commence ! Au tour de {jeu.joueurs[0].nom}.")

@socketio.on('valider_debut_tour')
def handle_valider_debut_tour():
    joueur = jeu.get_joueur_actuel()
    if request.sid != joueur.sid or jeu.etat != "TRANSITION_TOUR": return
    
    jeu.etat = "TOUR_CHOIX"
    jeu.lancer_des(5)
    jeu.broadcast_etat("C'est parti !")

@socketio.on('action_garder')
def handle_garder(indices):
    if jeu.etat != "TOUR_CHOIX": return
    if not indices: 
        emit('erreur', "Garde au moins un dé !")
        return
    
    indices.sort(reverse=True)
    for i in indices: jeu.des_gardes.append(jeu.des_sur_table.pop(i))
    
    if len(jeu.des_gardes) == 5:
        score = sum(jeu.des_gardes)
        joueur = jeu.get_joueur_actuel()
        msg = f"Score : {score}. "
        
        if score >= 25:
            jeu.valeur_killer = score - 24
            joueur.pv += jeu.valeur_killer
            msg += f"KILLER {jeu.valeur_killer} ! Tu gagnes {jeu.valeur_killer} PV. À l'attaque !"
            socketio.emit('notification', {'msg': msg})
            jeu.init_phase_attaque() 
        elif score == 24:
            socketio.emit('notification', {'msg': msg + "Rien ne se passe."})
            jeu.passer_au_joueur_suivant()
        else:
            perte = 24 - score
            joueur.pv -= perte
            socketio.emit('notification', {'msg': msg + f"Échec. -{perte} PV."})
            jeu.passer_au_joueur_suivant()
    else:
        jeu.lancer_des(5 - len(jeu.des_gardes))
        jeu.broadcast_etat("Relance des dés restants...")

@socketio.on('action_lancer_attaque')
def handle_lancer_attaque():
    joueur = jeu.get_joueur_actuel()
    if request.sid != joueur.sid or jeu.etat != "ATTENTE_LANCER": return

    jeu.lancer_des(5)
    
    if jeu.valeur_killer not in jeu.des_sur_table:
        jeu.etat = "RESULTAT_ATTAQUE"
        jeu.message = "Aucun dé Killer ! Attaque ratée."
        socketio.emit('notification', {'msg': f"Attaque ratée sur {jeu.joueurs[jeu.victime_actuelle_idx].nom}."})
        jeu.broadcast_etat()
    else:
        jeu.etat = "TOUR_ATTAQUE"
        jeu.broadcast_etat("Choisis tes dés d'attaque !")

@socketio.on('action_garder_attaque')
def handle_garder_attaque(indices):
    if jeu.etat != "TOUR_ATTAQUE": return
    if not indices:
        emit('erreur', "Sélectionne les dés Killer !")
        return

    indices.sort(reverse=True)
    for i in indices:
        if jeu.des_sur_table[i] != jeu.valeur_killer:
            emit('erreur', f"Tu ne peux prendre que des {jeu.valeur_killer} !")
            return

    nb_pris = 0
    for i in indices:
        val = jeu.des_sur_table.pop(i)
        jeu.des_gardes.append(val)
        jeu.degats_accumules += val
        nb_pris += 1

    socketio.emit('notification', {'msg': f"Tu gardes {nb_pris} dés. Dégâts : {jeu.degats_accumules}."})

    des_restants = 5 - len(jeu.des_gardes)

    if des_restants == 0:
        jeu.des_gardes = [] 
        socketio.emit('notification', {'msg': "🔥 FULL ! 5 dés gardés ! Tu gagnes 5 nouveaux dés ! 🔥"})
        jeu.lancer_des(5) 
        
        if jeu.valeur_killer not in jeu.des_sur_table:
            msg = f"Relance BONUS ratée (aucun {jeu.valeur_killer}). Fin de l'attaque."
            socketio.emit('notification', {'msg': msg})
            finir_victime()
        else:
            jeu.broadcast_etat("BONUS ! Encore des touches ! Continue...")
            
    else:
        jeu.lancer_des(des_restants)
        
        if jeu.valeur_killer not in jeu.des_sur_table:
            msg = f"Relance : Aucun {jeu.valeur_killer}. Fin de l'attaque."
            socketio.emit('notification', {'msg': msg})
            finir_victime()
        else:
            jeu.broadcast_etat("Encore des touches ! Valider et Relancer...")

def finir_victime():
    victime = jeu.joueurs[jeu.victime_actuelle_idx]
    if jeu.degats_accumules > 0:
        victime.pv -= jeu.degats_accumules
        socketio.emit('notification', {'msg': f"💥 BOOM ! {jeu.degats_accumules} dégâts infligés à {victime.nom} !"})
    
    jeu.etat = "RESULTAT_ATTAQUE"
    jeu.broadcast_etat(f"Attaque terminée. Total dégâts : {jeu.degats_accumules}.")

@socketio.on('action_suivant')
def handle_suivant():
    joueur = jeu.get_joueur_actuel()
    if request.sid != joueur.sid or jeu.etat != "RESULTAT_ATTAQUE": return
    jeu.preparer_prochaine_victime()

@socketio.on('reset_partie')
def handle_reset():
    if jeu.createur_sid != request.sid:
        emit('erreur', "Seul le créateur de la partie peut arrêter le jeu !")
        return
    
    jeu.reset()
    socketio.emit('force_reset')

if __name__ == '__main__':
    socketio.run(app, debug=True)