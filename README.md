# 🗺️ Carnet de Voyage

Une application de journal de voyage moderne, légère et ultra-rapide, contenue dans **un seul fichier Python**, sans aucune dépendance externe obligatoire. 

L'application sauvegarde vos récits, gère vos dépenses, suit vos humeurs au fil des jours et génère des graphiques de statistiques directement dans votre interface.

---

## ✨ Fonctionnalités

| Catégorie | Description |
| :--- | :--- |
| **Interface Moderne** | Mode clair/sombre automatique, design responsive adapté aux ordinateurs et mobiles. |
| **Gestion des Récits** | Ajout de notes par voyage avec gestion des dates, lieux, humeurs (étoiles) et tags. |
| **Suivi Financier** | Enregistrement des dépenses par jour avec conversion et calculs statistiques automatiques. |
| **Moteur de Recherche** | Recherche plein texte (FTS5) ultra-rapide, insensible aux accents, pour retrouver un souvenir instantanément. |
| **Zéro Dépendance** | Utilise uniquement la bibliothèque standard de Python et la base de données intégrée SQLite. |
| **Export Complet** | Exportez toutes vos données de voyage en un clic au format **Markdown** ou **JSON**. |

---

## 🚀 Comment lancer l'application

Le projet intègre nativement trois modes d'utilisation différents selon vos préférences :

### 🪟 Sur Windows (Le plus simple)
Double-cliquez simplement sur le fichier `lancer.bat`. Un menu interactif s'ouvrira dans la console pour vous proposer :
1. **Mode Fenêtre** (Application autonome semblable à un logiciel natif).
2. **Mode Navigateur** (Ouvre le carnet dans votre navigateur internet habituel).

### 💻 Via le Terminal (Toutes plateformes)

Ouvrez votre terminal dans le dossier du projet et utilisez l'une des commandes suivantes :

```bash
# 1. Mode Application (Fenêtre dédiée native sans bordures de navigateur)
python carnet.py gui

# 2. Mode Web (Serveur local accessible sur http://127.0.0.1:8000)
python carnet.py web

# 3. Mode Ligne de commande (Pour les experts du terminal)
python carnet.py --help
```

---

## 📦 Créer un fichier exécutable (.exe autonomes)

Si vous souhaitez utiliser l'application sur un ordinateur sans installer Python, ou la partager avec des proches, vous pouvez compiler le projet en un unique fichier `.exe` autonome :

1. Placez un fichier d'icône nommé `carnet.ico` dans le dossier si vous souhaitez personnaliser le logo (optionnel).
2. Double-cliquez sur le script `build_exe.bat`.
3. Le script va installer automatiquement l'outil `PyInstaller` et compiler votre application.
4. Récupérez votre logiciel prêt à l'emploi dans le dossier nouvellement créé : **`dist/Carnet.exe`**.

---

## 💾 Stockage et Technique

* **Base de données :** Vos données sont stockées de façon sécurisée en local dans votre dossier utilisateur (`~/.carnet_voyage.db`). Vous ne risquez pas de perdre vos textes en mettant à jour l'application.
* **Optimisations :** SQLite est configuré en mode **WAL** (Write-Ahead Logging) avec indexation composite pour garantir une écriture instantanée et une intégrité parfaite de vos souvenirs.
