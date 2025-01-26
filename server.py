import os
import time
import random
import threading
import logging
from datetime import datetime

from flask import Flask, request, jsonify
from flask_cors import CORS
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure

# Configuration de la journalisation
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)  # Autorise les requêtes cross-origin

# Récupérer l'URI MongoDB depuis les variables d'environnement
MONGO_URI = os.getenv("MONGO_URI")
if not MONGO_URI:
    logger.error("La variable d'environnement MONGO_URI n'est pas définie.")
    exit(1)

try:
    client = MongoClient(MONGO_URI)
    client.admin.command('ping')  # Vérifie la connexion
    logger.info("Connecté à MongoDB!")
except ConnectionFailure as e:
    logger.error(f"Impossible de se connecter à MongoDB: {e}")
    exit(1)

db = client['traçabilité']
users_collection = db['users']
shipments_collection = db['shipments']

simulation_running = True

# Verrou pour gérer la concurrence
lock = threading.Lock()

# Flag pour s'assurer que simulation_loop est démarré seulement une fois
simulation_started = False

def generate_step_durations():
    """Génère les durées pour chaque étape de la livraison."""
    total_time = random.randint(55, 60)
    transit_prop = random.uniform(0.4, 0.5)
    transit_time = int(total_time * transit_prop)
    rest_time = total_time - transit_time

    partials = [random.random() for _ in range(6)]
    s = sum(partials)
    if s == 0:
        partials = [1] * 6
        s = 6
    partials = [int(p / s * rest_time) for p in partials]

    diff = rest_time - sum(partials)
    if diff != 0:
        imax = max(range(6), key=lambda i: partials[i])
        partials[imax] += diff

    durations = []
    for i in range(7):
        if i < 3:
            durations.append(partials[i])
        elif i == 3:
            durations.append(transit_time)
        else:
            durations.append(partials[i - 1])

    for i in range(7):
        if durations[i] < 1:
            durations[i] = 1

    sum_diff = total_time - sum(durations)
    if sum_diff > 0:
        imax = max(range(7), key=lambda i: durations[i])
        durations[imax] += sum_diff

    return durations

def handle_incident(tracking, days):
    """Gère les incidents potentiels pendant la livraison."""
    logger.info(f"Gestion d'un incident pour {tracking}, retard estimé de {days} jours.")
    time.sleep(days * 5)  # Simulation: chaque jour équivaut à 5 secondes
    chance = 10 + (days - 1) * 5
    chance = min(chance, 100)
    outcome = random.randint(1, 100) <= chance
    with lock:
        shipment = shipments_collection.find_one({"tracking": tracking})
        if not shipment:
            logger.warning(f"Aucune expédition trouvée pour le suivi {tracking} lors de la gestion de l'incident.")
            return
        if outcome:
            shipments_collection.update_one(
                {"tracking": tracking},
                {"$set": {"status": "Livraison annulée", "finished": True},
                 "$push": {"history": ("Livraison annulée", datetime.now().ctime())}}
            )
            logger.info(f"Livraison annulée pour le suivi {tracking}.")
        else:
            shipments_collection.update_one(
                {"tracking": tracking},
                {"$set": {"status": "En transit"},
                 "$push": {"history": ("En transit", datetime.now().ctime())},
                 "$unset": {"on_hold": ""}
                }
            )
            logger.info(f"Livraison reprise pour le suivi {tracking}.")

def simulation_loop():
    """Boucle de simulation qui met à jour les statuts des expéditions."""
    statuses = [
        "Commande confirmée",
        "Colis préparé",
        "Pris en charge par le transporteur",
        "En transit",
        "Arrivé au centre de distribution",
        "En cours de livraison",
        "Livré"
    ]
    last_time = time.time()
    logger.info("Boucle de simulation démarrée.")

    while simulation_running:
        time.sleep(1)
        now = time.time()
        delta = now - last_time
        last_time = now

        shipments = list(shipments_collection.find({"finished": False, "archived": False}))

        for shipment in shipments:
            tracking = shipment["tracking"]
            idx = shipment.get("current_step_index", 0)

            if idx >= len(statuses) - 1:
                shipments_collection.update_one(
                    {"tracking": tracking},
                    {"$set": {"finished": True}}
                )
                logger.info(f"Expédition {tracking} terminée.")
                continue

            if shipment.get("on_hold", False):
                continue

            # Mise à jour du temps dans l'étape
            time_in_step = shipment.get("time_in_step", 0) + delta
            dur_step = shipment["step_durations"][idx]

            if idx == 3 and not shipment.get("incident_checked", False) and shipment.get("incident_decision", False):
                days = random.randint(1, 9)
                shipments_collection.update_one(
                    {"tracking": tracking},
                    {"$set": {
                        "incident_checked": True,
                        "status": f"Incident signalé : Retard estimé : {days} jour{'s' if days >1 else ''}",
                        "on_hold": True
                    },
                     "$push": {"history": ("Incident signalé", datetime.now().ctime())}
                    }
                )
                threading.Thread(target=handle_incident, args=(tracking, days), daemon=True).start()
                logger.info(f"Incident déclenché pour l'expédition {tracking}.")
                continue

            if time_in_step >= dur_step:
                new_idx = idx + 1
                new_status = statuses[new_idx]
                update_fields = {
                    "current_step_index": new_idx,
                    "status": new_status,
                    "time_in_step": 0
                }
                if new_idx >= len(statuses) -1:
                    update_fields["finished"] = True
                shipments_collection.update_one(
                    {"tracking": tracking},
                    {"$set": update_fields,
                     "$push": {"history": (new_status, datetime.now().ctime())}}
                )
                logger.info(f"Expédition {tracking} mise à jour au statut: {new_status}")
            else:
                shipments_collection.update_one(
                    {"tracking": tracking},
                    {"$set": {"time_in_step": time_in_step}}
                )

def generate_tracking_for(username, quantity, destination):
    """Crée une nouvelle expédition."""
    tracking = str(random.randint(10000000, 99999999))
    durations = generate_step_durations()
    incident = (random.randint(1, 100) <= 15)
    shipment = {
        "tracking": tracking,
        "status": "Commande confirmée",
        "history": [("Commande confirmée", datetime.now().ctime())],
        "client": username,
        "quantity": quantity,
        "destination": destination,
        "creation_timestamp": datetime.now(),
        "current_step_index": 0,
        "finished": False,
        "on_hold": False,
        "step_durations": durations,
        "time_in_step": 0,
        "incident_decision": incident,
        "incident_checked": False,
        "archived": False
    }
    shipments_collection.insert_one(shipment)
    logger.info(f"Nouvelle expédition créée: {tracking} pour l'utilisateur {username}.")
    return tracking

# Routes API

@app.route("/")
def index_root():
    return "Bienvenue sur l'API Traçabilité"

@app.route("/signup", methods=["POST"])
def signup():
    js = request.json
    username = js.get("username", "").strip()
    password = js.get("password", "").strip()
    if not username or not password:
        return jsonify({"error": "Champs manquants"}), 400
    if users_collection.find_one({"username": username}):
        return jsonify({"error": "Identifiant déjà existant"}), 400
    users_collection.insert_one({"username": username, "password": password})
    logger.info(f"Nouvel utilisateur inscrit: {username}.")
    return jsonify({"success": True}), 200

@app.route("/login", methods=["POST"])
def login():
    js = request.json
    username = js.get("username", "").strip()
    password = js.get("password", "").strip()
    user = users_collection.find_one({"username": username, "password": password})
    if user:
        role = "admin" if username == "BUT3MLT" else "client"
        logger.info(f"Utilisateur connecté: {username} en tant que {role}.")
        return jsonify({"success": True, "role": role}), 200
    logger.warning(f"Tentative de connexion échouée pour l'utilisateur: {username}.")
    return jsonify({"error": "Identifiants invalides"}), 401

@app.route("/shipments", methods=["GET"])
def get_shipments():
    shipments = list(shipments_collection.find())
    retour = []
    for s in shipments:
        retour.append({
            "tracking": s["tracking"],
            "client": s["client"],
            "quantity": s["quantity"],
            "destination": s["destination"],
            "status": s["status"],
            "archived": s.get("archived", False),
            "current_step_index": s.get("current_step_index", 0),
            "history": s.get("history", [])
        })
    return jsonify(retour), 200

@app.route("/shipments", methods=["POST"])
def passer_commande():
    js = request.json
    username = js.get("username", "").strip()
    qty = js.get("quantity", 0)
    dest = js.get("destination", "").strip()
    if not username or not dest:
        return jsonify({"error": "Champs manquants"}), 400
    try:
        qty = int(qty)
    except ValueError:
        return jsonify({"error": "Quantité invalide"}), 400
    user = users_collection.find_one({"username": username})
    if not user:
        return jsonify({"error": "Utilisateur inexistant"}), 400
    tracking = generate_tracking_for(username, qty, dest)
    return jsonify({"success": True, "tracking": tracking}), 200

@app.route("/shipments/<tracking>", methods=["GET"])
def get_one_shipment(tracking):
    shipment = shipments_collection.find_one({"tracking": tracking})
    if not shipment:
        return jsonify({"error": "Invalide"}), 404
    response = {
        "tracking": shipment["tracking"],
        "client": shipment["client"],
        "quantity": shipment["quantity"],
        "destination": shipment["destination"],
        "status": shipment["status"],
        "history": shipment.get("history", []),
        "archived": shipment.get("archived", False),
        "current_step_index": shipment.get("current_step_index", 0)
    }
    return jsonify(response), 200

@app.route("/shipments/<tracking>/archive", methods=["POST"])
def archive_shipment(tracking):
    shipment = shipments_collection.find_one({"tracking": tracking})
    if not shipment:
        return jsonify({"error": "Invalide"}), 404
    shipments_collection.update_one(
        {"tracking": tracking},
        {"$set": {"archived": True}}
    )
    logger.info(f"Expédition {tracking} archivée.")
    return jsonify({"success": True}), 200

# Démarrage de la simulation

def start_simulation():
    """Démarre la boucle de simulation dans un thread séparé."""
    global simulation_started
    if not simulation_started:
        th = threading.Thread(target=simulation_loop, daemon=True)
        th.start()
        simulation_started = True
        logger.info("Thread de simulation lancé.")

# Démarrer la simulation
start_simulation()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
