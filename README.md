# kgraph — graphe de connaissances technique et fonctionnel d'un dépôt applicatif

Charge un dépôt (chemin local ou URL git), reconstruit un **graphe de connaissances** du front jusqu'à la base de données,
détecte les **règles métier**, et permet l'**analyse d'impact**, la **lignée de données**, la **documentation générée**
et des **questions en langage naturel** (FR/EN).

* Aucune dépendance externe : Python ≥ 3.10 (stdlib uniquement) + `git` pour les URL. UI en JS/SVG vanilla (fonctionne hors ligne).
* L'analyse est **statique** et **en lecture seule** : le code scanné n'est jamais exécuté, compilé ni installé.

## Démarrage rapide

```bash
cd code-knowledge-graph
python3 -m kgraph serve --scan samples/shop        # http://127.0.0.1:8765
python3 -m unittest discover -s tests              # tests (155)
```

CLI sans interface :

```bash
python3 -m kgraph scan <chemin|url-git> [--branch main] --out graph.json
python3 -m kgraph query determine_impact  --graph graph.json customers
python3 -m kgraph query show_call_chain   --graph graph.json /orders/new orders
python3 -m kgraph query find_data_lineage --graph graph.json orders status
python3 -m kgraph ask "quel est l'impact si je modifie la table customers ?" --graph graph.json
python3 -m kgraph docs --graph graph.json --out docs-generated
```

## Lancement : `launch.sh`

Un seul point d'entrée :

```bash
./launch.sh                          # appli web sur l'exemple (ouvre le navigateur, choisit un port libre)
./launch.sh web ~/work/app 9000      # sur votre dépôt ; options : --no-heuristics | --llm | --modernization --modules X | --yes | --test
./launch.sh desktop                  # application Electron (desktop dist = installateur)
./launch.sh status                   # serveurs kgraph en cours + leur URL
./launch.sh stop                     # arrête tous les serveurs kgraph (libère les ports)
./launch.sh scan <repo> [out.json]   # scan en terminal
./launch.sh neo4j <repo|graph.json> [--load] [--clear]
./launch.sh test                     # tests Python (+ tests du lanceur Electron si Node est installé)
```
`run.sh` ne rejoue plus les tests au démarrage (démarrage plus rapide) : ajoutez `--test` pour les lancer.

## Capacités

| Capacité | Où |
|---|---|
| `scan_repository` | `source.py`, `pipeline.scan_repository` (dossier local ou `git clone --depth 1`) |
| `build_knowledge_graph` | `pipeline.py`, `graph.py` |
| `detect_business_rules` | `rules.py`, `sqlscan.py` |
| `impact_analysis` | `queries.determine_impact` |
| `generate_documentation` | `docs.py` (Markdown + Mermaid, export .zip) |
| `graph_visualization` | `static/` (graphe en couches, zoom/pan, filtres, vue « résultat ») |
| `natural_language_query` | `nlq.py` (intentions + liaison d'entités, FR/EN, sans LLM) |

## Technologies reconnues

* **Backend** Java et Kotlin : Spring MVC (`@RestController`, `@RequestMapping`, `@GetMapping`… y compris `RequestMethod`, chemins multiples,
  constantes, `${placeholders}`, `server.servlet.context-path`), JAX-RS de base, Spring Data/JPA (`JpaRepository<E,ID>`, `@Query` JPQL/natif,
  `@Modifying`, requêtes dérivées), DAO `@Repository` + `JdbcTemplate`, entités JPA (`@Table`, `@Column`, relations), injection par constructeur
  / champ / Lombok / constructeur primaire Kotlin, interface → implémentation, Kafka/Rabbit/JMS (`@KafkaListener`, `kafkaTemplate.send`).
* **Frontend** Angular (composants, templates, `selector`, DI, `HttpClient`, routes imbriquées et `loadChildren`/`loadComponent`)
  et React (composants fonction, hooks, `react-router` JSX et `createBrowserRouter`, `fetch`/`axios` y compris instances).
* **Base de données** `CREATE/ALTER TABLE`, FK, `CHECK`, `UNIQUE`, triggers pour PostgreSQL, Oracle, MySQL et SQL générique (dialecte détecté).

## Ontologie

Nœuds : `Screen Component Route API Controller Service BusinessRule Repository Entity Table Topic Capability`
Relations : `CALLS USES IMPLEMENTS READS WRITES IMPACTS DEPENDS_ON EXPOSES EXECUTES`

Sens d'une arête : **sens d'exécution** (utilisateur → écran → API → service → table).
`EXPOSES` suit ce sens, sauf `Controller → API` (la requête entre par l'API puis atteint le contrôleur). `IMPACTS` et `IMPLEMENTS` sont
associatives (appartenance à une capacité, règle ↔ entité). L'analyse d'impact parcourt le graphe **à rebours**.

Deux niveaux d'arêtes coexistent :

* **structurelles** (niveau classe) : ce que la classe *peut* appeler ;
* **exactes** (`derived: true`, niveau méthode) : ce qu'une API/écran atteint réellement via sa fermeture d'appels
  (tables lues/écrites, règles exécutées, colonnes touchées). Les requêtes `explain_screen`, `find_business_rules` et
  `find_data_lineage` s'appuient dessus pour éviter la sur-approximation.

## Pipeline

`scan_frontend → extract_routes → detect_api_calls → scan_backend → discover_controllers → discover_services →
discover_repositories → discover_entities → discover_business_rules → populate_graph`

## Requêtes

| Requête | Paramètres | Résultat |
|---|---|---|
| `explain_screen` | écran ou route | routes, composants, API appelées → services → tables lues/écrites, règles, topics, capacité |
| `show_call_chain` | source, cible (optionnelle) | chaînes écran → … → table, diagramme Mermaid |
| `find_business_rules` | périmètre, texte, nature, confiance min. | règles avec emplacement `fichier:ligne`, confiance, entités/champs impactés |
| `determine_impact` | élément, sens (`upstream`/`downstream`) | éléments impactés par type (écrans, API, services, règles…), chemins, niveau de risque |
| `show_capability` | capacité (vide = liste) | membres de la capacité et dépendances partagées avec les autres |
| `find_data_lineage` | table / entité / écran / API, colonne | écrivains et lecteurs de bout en bout, FK, triggers, règles liées |
| `explore_ontology` | type de départ, relation, sens, type d'arrivée, profondeur, filtres de nom, liens (tous / structurels / exacts) | requête libre sur l'ontologie : `Service ─WRITES→ Table`, `Screen ─CALLS→ Service` (profondeur 2)… avec chemins et méthodes concernées |

API REST (utilisée par l'UI) : `POST /api/scan`, `GET /api/status|graph|node|search|meta|ontology|docs|docs.zip|neo4j.cypher`, `POST /api/query|ask|rules/analyze|cancel`.

**Explorer par ontologie** : panneau *Queries → explore_ontology* (listes alimentées par l'ontologie, aperçu du nombre de liens de cette forme dans le graphe), ou en langage naturel : « which services write to orders? », « quelles API appellent des services ? », « which repositories read orders ». Le sens est celui écrit dans l'ontologie (`out` : A ─REL→ B, `in` : A ←REL─ B), pas le sens d'exécution. CLI : `python3 -m kgraph query explore_ontology --graph g.json Service WRITES Table [profondeur]` (`-` = indifférent). `GET /api/ontology` renvoie le schéma réel (nombre de nœuds par type, de liens par relation et par triplet type–relation–type).

## Règles métier détectées

Chaque règle porte `kind`, `file:line` et une `confidence` : `declared` (marqueur `// BR-12: …`, `@BusinessRule`) 1.0 ·
`guard` / `validation` / `authorization` 0.9 · `existence` 0.8 · `lifecycle` 0.7 · `decision` (seuils/statuts) 0.5 ·
règles de base (`db-check`, `db-unique`, `db-trigger`) 1.0.

## Enrichissement des règles métier par Claude (optionnel)

Désactivé par défaut. Il **envoie le code source** des classes service/contrôleur/entité à Anthropic : à n'utiliser que si la politique de
données du client/projet l'autorise (l'UI demande confirmation).

```bash
python3 -m kgraph scan <repo> --llm            # auto : CLI `claude` si présent, sinon ANTHROPIC_API_KEY
python3 -m kgraph scan <repo> --llm cli --llm-max-classes 20 --llm-model <modèle>
./run.sh                                       # puis cocher « Enrich rules with Claude » dans l'UI
```

* Claude **reformule** les règles détectées en langage métier (`business_description`) et en **découvre** que les heuristiques manquent
  (calculs, workflows, seuils) : `source: llm`, confiance plafonnée à 0.8.
* Anti-hallucination : chaque règle doit citer un `evidence` **présent à l'identique** dans le code, sinon elle est rejetée (comptée dans les logs).
* Le code scanné est présenté comme *donnée non fiable* (délimiteur dédié), le CLI tourne sans aucun outil et sans session persistante ;
  il ne peut retourner que des descriptions de règles.
* Réponses mises en cache dans `.kgraph/llm-cache.json` (un re-scan ne renvoie ni ne repaie les classes inchangées).
* Limite : non vérifié contre un vrai modèle dans l'environnement de développement (session `claude` imbriquée non authentifiée) ;
  le comportement est couvert par des tests avec un faux fournisseur. À essayer sur 2-3 classes avant un scan complet.

## Règles métier via le plugin officiel `code-modernization` (sans heuristiques)

Mode alternatif : l'application **appelle elle-même** le plugin Claude Code et n'utilise **aucune** heuristique de règle
(ni garde-fous, ni validations, ni contraintes SQL) ; la structure (écrans, API, tables, FK…) reste extraite statiquement.

Installation unique (dans un terminal) : `claude plugin install code-modernization@claude-plugins-official`
(les 7 options de configuration du plugin sont facultatives).

**Bouton « Analyze business rules »** (en-tête de l'UI et onglet *Rules*) : lance l'analyse **à la demande** sur le dépôt déjà scanné,
séparément du scan de structure.

```bash
./run.sh --no-heuristics samples/shop     # structure seule (aucune règle), puis cliquer « Analyze business rules »
```
Onglet *Rules* : champ **modules** (recommandé : un seul au début), case **reuse the last plugin result** (réimporte le dernier
`BUSINESS_RULES.md` sans relancer le plugin), bouton **Cancel** (arrête le run), journal d'activité en direct et rappel de provenance
(commande, durée, empreinte, transcript). Le bouton est grisé tant qu'aucun dépôt n'est scanné ou que le plugin est absent ; une confirmation
(coût, envoi du code) est demandée.

**Avancement en direct** (onglet *Rules* + badge d'en-tête) : l'application lit le flux `stream-json` de Claude Code
(`--forward-subagent-text`) et affiche la phase, le temps écoulé, le nombre d'**agents** lancés / attendus (~12 par module, d'après le README du plugin),
les **appels d'outils**, le nombre de **modules** (`extract-rules.modules.json`), les erreurs d'outils, le coût final, le dernier message
et un flux d'événements (outils, agents, messages des sous-agents). La barre est *indicative* : elle devient proportionnelle seulement quand
le nombre de modules est connu ; sinon elle est animée (le plugin ne publie pas de pourcentage). Le transcript complet est écrit en direct
dans `<workdir>/modernization/<nom>/analysis/<nom>/claude-output.log` (`tail -f` possible).

**Voir les règles** : onglet **Rules list** au-dessus du graphe (compteur de règles) : toutes les règles dans un tableau triable
(ID, nature, priorité, confiance, classe/méthode, écrans qui les déclenchent, entités impactées, défauts suspects, question métier),
filtres (texte, nature, priorité, « défauts suspects », « à valider par un expert »), détail déplié par clic (formulation métier, Given/When/Then,
paramètres, notes, sources `fichier:ligne`, API concernées). Boutons **Show in graph** (une règle), **Rules in graph** (toutes) et
**Show these rules in the graph** (selon les filtres) : le graphe affiche les règles avec ce qui les applique (service, API, entité, table)
et les écrans concernés ; les règles portent leur priorité et un ⚠ si un défaut est suspecté. L'analyse terminée, le graphe s'ouvre directement
sur les règles.

**Format du plugin** : la sortie du plugin est générée par un modèle et sa mise en page **varie d'un run à l'autre** (deux variantes réelles
sont des jeux de test : cartes `### RULE-001: Nom` + champs en gras, ou `### RULE-001 — Nom` + puces `Statement/Source/Notes` avec catégorie
dans un tableau récapitulatif). L'import fusionne cartes, tableau récapitulatif, registre de défauts (`DEFECT-03` → `RULE-015`) et « Open questions »
(références `RULE-013, RULE-005`). Les noms de fichiers seuls et les chemins abrégés (`…/order/X.java`) sont résolus dans le dépôt ; une fiche dont
la source est introuvable est **conservée et signalée** (jamais ignorée en silence). La priorité (P0-P2) n'existe que si le plugin la fournit.

Autres façons de lancer :
```bash
./run.sh --modernization --modules order ~/work/mon-app      # analyse dès le démarrage
python3 -m kgraph scan <repo> --rules modernization --modernization-pattern order --out graph.json
python3 -m kgraph scan <repo> --rules none --out graph.json  # structure seule
```
API : `POST /api/rules/analyze {module_pattern, reuse}`, `POST /api/cancel`.

Déroulement : (1) création de `<workdir>/modernization/<nom>/legacy/<nom>` → lien vers le dépôt (rien n'est copié) ;
(2) `claude -p` lance `/code-modernization:modernize-extract-rules <nom> [modules]` dans ce dossier ;
(3) lecture de `analysis/<nom>/BUSINESS_RULES.md` (fiches `### RULE-NNN`) ; (4) import comme nœuds `BusinessRule`
(`source: modernization`, priorité P0-P2, spécification Given/When/Then, paramètres, défaut suspect, question pour l'expert métier) ;
(5) rattachement à la classe/méthode d'après le `fichier:ligne` cité, puis aux écrans/API qui l'atteignent. Une fiche dont le fichier
ou la ligne n'existe pas dans le dépôt est rejetée (comptée dans les logs).

À savoir avant de lancer :
* **Coût et durée** : ~10-15 agents par module (~16k tokens chacun) ; minutes à heures. Confirmation demandée (UI, CLI, `run.sh`).
* **Confidentialité** : le code du dépôt est envoyé à Anthropic par le plugin.
* **Exécution sans interaction** : l'application autorise `Read/Write/Edit/Glob/Grep/Agent/Workflow` et `Bash(python3|readlink|ls)` et
  indique au plugin de ne poser aucune question ; ne l'utilisez que sur des dépôts de confiance (`KGRAPH_CLAUDE_ALLOWED_TOOLS` pour ajuster).
* **Non vérifié contre le vrai plugin** : il n'était pas installable dans l'environnement de développement. L'intégration est testée
  avec un faux `claude` qui reproduit le contrat documenté par le plugin (entrée, dossier de travail, format de `BUSINESS_RULES.md`).
  Premier essai conseillé sur un seul module ; si le plugin change de format ou de permissions, l'erreur est affichée telle quelle.
* La revue humaine du plugin (`modernize-review`) reste à faire dans Claude Code ; les fiches « Medium/Low » sont signalées par `sme_question`.
* Exclusif avec `--llm` (enrichissement par ma propre passe Claude).

## Neo4j

Le graphe s'exporte tel quel dans Neo4j (4.4+ / 5) : **un label par type de l'ontologie** (+ `:KGNode`), **un type de relation par relation**
(`CALLS`, `READS`, `EXECUTES`…), propriétés conservées (les structures imbriquées sont stockées en JSON dans `<clé>_json`), liens exacts marqués `derived: true`.

```bash
python3 -m kgraph neo4j --graph graph.json --out graph.cypher        # script Cypher (contraintes + noeuds + relations)
cypher-shell -u neo4j -f graph.cypher                                # ... ou coller le fichier dans Neo4j Browser
python3 -m kgraph neo4j --graph graph.json --load                    # charge directement (http://localhost:7474, mot de passe demandé)
python3 -m kgraph neo4j --repo samples/shop --load --uri http://localhost:7474 --user neo4j --database neo4j --clear
```
**Neo4j local en une commande (Podman ou Docker)** :

```bash
./launch.sh neo4j-up                    # démarre le conteneur, charge le dernier graphe (ou l'exemple), ouvre Neo4j Browser
./launch.sh neo4j-up ~/work/mon-app     # ... en scannant ce dépôt
./launch.sh all ~/work/mon-app          # Neo4j + application web
./launch.sh neo4j-down [--purge]        # arrête et supprime le conteneur (--purge : aussi le volume et le mot de passe)
```
Podman est utilisé s'il est présent, sinon Docker (`KGRAPH_CONTAINER=docker` pour forcer). Sur macOS/Windows, la machine Podman doit tourner
(`podman machine init` une fois, puis `podman machine start`). Image `docker.io/library/neo4j:5` (`KGRAPH_NEO4J_IMAGE` pour en changer), ports **liés à
127.0.0.1**, données dans le volume `kgraph-neo4j-data`. Le mot de passe est `NEO4J_PASSWORD` s'il est défini, sinon généré aléatoirement et conservé dans
`.kgraph/neo4j-password` (mode 600, ignoré par git) : il est transmis au moteur par variable d'environnement, jamais dans la ligne de commande.
*Vérifié* avec un faux moteur de conteneurs (commande lancée, ports, mot de passe absent des arguments, droits du fichier) et, pour le démarrage réel, par vous
(Podman + Neo4j 5) ; le chargement a révélé et fait corriger une erreur (schéma après écriture).

Un exemple prêt à charger est fourni : `samples/shop-graph.cypher`. L'interface propose aussi **Documentation → Download Neo4j script**.
Requêtes d'exemple (dans le pied du script) : chaîne d'un écran vers les tables, impact d'une table, qui écrit une table, règles à défaut suspect,
ontologie telle que présente dans les données.

Sécurité : le mot de passe vient de `NEO4J_PASSWORD` ou d'une saisie masquée (jamais de l'interface web ; la seule exception est le conteneur jetable de `launch.sh neo4j-up`, voir ci-dessous) ; refus d'envoyer
des identifiants en clair vers un hôte distant (http n'est accepté que vers localhost, sinon https) ; toutes les données sont échappées dans des littéraux
(un nom de classe hostile ne peut pas devenir une instruction) ; `--clear` ne supprime que les nœuds `:KGNode` de **ce** dépôt.
*Vérifié* contre un vrai Neo4j 5.26 Community (chargement, requêtes, parité avec le moteur en mémoire) ; non testé sur d'autres versions (cible : 4.4+ / 5).

## Neo4j comme moteur de requêtes

Avec Neo4j configuré, **les requêtes sont exécutées par Neo4j**, plus par le graphe en mémoire.

```bash
./launch.sh all [dépôt]          # Neo4j (conteneur) + application web, Neo4j = moteur
./launch.sh full [dépôt]         # idem avec l'application Electron
NEO4J_PASSWORD=... [NEO4J_URI=http://localhost:7474 NEO4J_USER=neo4j NEO4J_DATABASE=neo4j] ./launch.sh web   # Neo4j déjà installé
python3 -m kgraph query determine_impact --engine neo4j --graph g.json customers     # CLI (graphe déjà chargé : neo4j --load)
```
`KGRAPH_ENGINE` = `auto` (défaut : Neo4j si `NEO4J_PASSWORD` est défini), `neo4j` ou `memory` (force le moteur en mémoire). La pastille « Neo4j » de la barre du haut
indique le moteur actif ; chaque réponse affiche le moteur et la durée (`Answered by Neo4j in 120 ms`).

**Fonctionnement** : après chaque scan, le graphe est synchronisé dans Neo4j (rechargé seulement s'il a changé : empreinte versionnée, marqueur `KGMeta` écrit en dernier
= chargement complet), puis le moteur Neo4j remplace le moteur en mémoire. Modèle : un label par type de l'ontologie, les relations d'origine, et un lien matérialisé
**`FLOW`** dans le sens d'exécution (le `EXPOSES` Controller→API y est inversé), ce qui permet d'écrire l'impact et les chaînes d'appels comme de simples motifs
`(a)-[:FLOW*]->(b)`. Les propriétés complètes sont conservées (`props_json`), l'ordre de création des nœuds aussi (`ord`).

| Requête | Exécution |
|---|---|
| `explore_ontology`, `ontology_schema` | **une instruction Cypher** (chemins les plus courts par paire, filtres, profondeur) |
| `determine_impact` | **un parcours** `shortestPath` (`FLOW`\|`IMPACTS` en amont, `FLOW` structurel en aval) |
| `show_call_chain` | **une énumération de chemins** par passe (arrêt à la première table, écrans limités à leurs API exactes) |
| `explain_screen`, `find_business_rules`, `show_capability`, `find_data_lineage` | même code de composition que le moteur en mémoire, au-dessus d'une API de graphe en lecture seule dont **chaque voisinage est une requête Cypher** (mis en cache) : mêmes réponses par construction, un aller-retour par lecture |

**Sécurité** : le texte de l'utilisateur ne va **jamais** dans le texte Cypher, seulement en paramètres ; les seuls éléments interpolés sont des identifiants issus de listes
blanches (types, relations) et des entiers bornés ; le mot de passe n'est lu que dans l'environnement.

**Si Neo4j tombe** : l'application continue en mémoire (pastille « unavailable », note dans la réponse). Une réponse en cours qui échoue est refaite en mémoire.

**Garanties et limites**
* 9 tests de **parité** comparent les deux moteurs sur une vraie base : mêmes ensembles, distances, scores, règles, lignées (`KGRAPH_NEO4J_TEST=1 NEO4J_PASSWORD=... python3 -m unittest tests.test_neo4j_engine` ;
  ils n'écrivent que sous `kg_repo='kgparity'` et nettoient). Seule différence admise : entre deux chemins **aussi courts**, les moteurs peuvent choisir l'un ou l'autre.
* Les quatre requêtes « composées » font plusieurs dizaines d'aller-retour (100-400 ms sur l'exemple) ; sur de très gros graphes elles seront plus lentes que les trois requêtes
  poussées en Cypher : ce sont les prochaines à pousser.
* L'affichage du graphe entier dans l'interface et la reconnaissance des noms en langage naturel utilisent encore le résultat du scan (en mémoire) ; Neo4j exécute les requêtes.
* Plusieurs dépôts peuvent coexister dans une base (`kg_repo` = nom du dossier) ; deux dossiers de même nom se remplaceraient.

## Serveur MCP (consommer le graphe depuis un assistant IA)

`python3 -m kgraph mcp` démarre un serveur [MCP](https://modelcontextprotocol.io) en **stdio**, **lecture seule** : il ne scanne rien, n'écrit rien et n'envoie rien à l'extérieur. Il sert le dernier graphe scanné (`.kgraph/last-graph.json`, relu automatiquement quand il change) ou un fichier précis (`--graph graph.json`). `--engine neo4j` répond depuis Neo4j (`NEO4J_PASSWORD` dans l'environnement).

Outils : `graph_overview`, `ontology_schema`, `search_nodes`, `explain_screen`, `show_call_chain`, `find_business_rules`, `determine_impact`, `show_capability`, `find_data_lineage`, `explore_ontology`, `ask` (langage naturel EN/FR). Chaque réponse contient un résumé lisible et le détail JSON (`structuredContent`).

Déclaration dans un client (Claude Code : `.mcp.json` ou `claude mcp add`) :

```json
{ "mcpServers": { "kgraph": {
    "command": "python3",
    "args": ["-m", "kgraph", "mcp", "--graph", "/chemin/vers/graph.json"],
    "cwd": "/chemin/vers/code-knowledge-graph" } } }
```

## Application de bureau (Electron)

```bash
./run-desktop.sh          # première fois : npm install (réseau requis), puis ouvre la fenêtre
./run-desktop.sh dist     # génère un installateur dans electron/dist (dmg / nsis / AppImage)
```
**Electron + Neo4j en une commande** : `./run-desktop-neo4j.sh [dépôt]` (ou `./launch.sh full [dépôt]`) démarre Neo4j (Podman/Docker), y charge le graphe,
puis ouvre l'application de bureau qui scanne **le même dépôt** (variable `KGRAPH_SCAN`). `./launch.sh neo4j-down` arrête Neo4j. Fermez d'abord une fenêtre
kgraph déjà ouverte : l'application n'autorise qu'une instance à la fois.

Prérequis : Node.js 18+ et Python 3.10+ (le moteur d'analyse reste en Python, bibliothèque standard seulement ; l'installateur **n'embarque pas** Python).
L'application choisit un port libre, lance `python -m kgraph serve` en local (127.0.0.1), attend qu'il réponde, affiche l'interface et l'arrête à la
fermeture. Espace de travail : dossier de données de l'application (menu *kgraph → Open the workspace folder*). Sécurité : `contextIsolation`,
`sandbox`, pas de Node dans la page, navigation limitée à l'application (liens externes ouverts dans le navigateur), permissions web refusées,
deux seules fonctions exposées à la page (choisir un dossier, chemin de l'exemple). Dans la fenêtre, le champ « Repository » reçoit un bouton **Browse…**.
Tests du lanceur : `cd electron && npm test` (démarre le vrai serveur).
*Non vérifié ici* : l'environnement de développement n'avait pas accès à npm, donc la fenêtre Electron et l'installateur n'ont pas été lancés ;
la logique de démarrage du serveur est testée, pas l'affichage Electron lui-même.

## Interface

Design « tableau de bord » : barre latérale (Repository, Ask, Queries, Business rules, Filters, Documentation), barre du haut (titre, recherche
`⌘/Ctrl + K`, statut, cloche d'activité), onglets **Graph / Network / Rules list**, cartes de chiffres clés (écrans, API, tables, règles avec
défauts suspects) et panneau Result/Node. L'onglet **Network** est un graphe à forces façon Obsidian : taille des nœuds selon les liens, survol qui
met en évidence les voisins, glisser-déposer, zoom/pan, recherche, *local graph* (double-clic, profondeur réglable), curseurs de forces, légende
cliquable, mode orphelins ; il suit la vue courante (filtres, résultat d'une requête, « Rules in graph »).

## Limites connues (à lire avant d'utiliser les résultats)

* Analyse **heuristique** sans AST complet : surtout fiable sur les conventions Spring/JPA/Angular/React courantes ;
  la réflexion, la configuration dynamique, les URLs construites à l'exécution ou les mappers génériques échappent à l'analyse.
* Les règles métier sont **déduites du code** (garde-fous, annotations) : elles décrivent *ce que le code fait*, pas l'intention fonctionnelle ;
  à faire valider par un expert métier. Les règles `decision` (confiance 0.5) sont volontairement bruyantes.
* L'accès aux **colonnes** (lignée fine) est une heuristique (getters/setters, requêtes dérivées, `SET` JPQL) ; l'UI/les résumés le signalent.
* Résolution des appels : variables locales typées explicitement, champs injectés, appels statiques et même classe. Les appels sur des
  types déduits (`var x = repo.find()`) ne sont pas suivis.
* Non couvert : MyBatis XML, procédures stockées appelées depuis le code, GraphQL, Next.js (routage par fichiers), Vue.
* Un fichier non analysable est ignoré avec un avertissement ; il n'interrompt pas le scan.

## Sécurité

* Le serveur écoute sur `127.0.0.1` uniquement ; les `POST` exigent l'en-tête `X-KG-Request` et les en-têtes `Host`/`Origin` doivent être locaux
  (protection CSRF / DNS-rebinding).
* URL git limitées à `https://`, `http://`, `ssh://`, `git@host:` (pas de `ext::`, pas d'option injectée) ; clone superficiel.
* Les liens symboliques ne sont pas suivis ; fichiers > 1,5 Mo ignorés. Tout texte issu du dépôt est échappé avant affichage dans l'UI.
* Ne scannez pas de dépôts contenant des données soumises à réglementation (RGPD, etc.) sans avis préalable du DPO ; le graphe et la documentation
  générée reprennent des noms de classes, tables et colonnes. **Relisez et validez la documentation générée avant tout partage externe.**

## Étendre

* Nouveau framework : ajouter un extracteur qui alimente `FEModel` / `ClassInfo` puis le brancher dans `pipeline.STAGES`.
* Nouvelle règle de détection : `rules.py` (retourne des dicts de règle ; la liaison au graphe est faite dans `populate_graph`).
* Nouvelle requête : méthode de `QueryEngine` + entrée dans `QueryEngine.run`, `ontology.QUERIES` et `nlq.INTENTS`.
