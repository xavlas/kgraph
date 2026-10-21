"""Natural-language query: intent detection + entity linking (FR / EN), no LLM needed."""
from __future__ import annotations

import re
import unicodedata

from .graph import Graph, split_words
from .queries import QueryEngine, QueryError

# ontology questions: "which services write to orders", "quelles API appellent des services"
ONT_TYPES = {
    "Screen": r"screens?|ecrans?|pages?", "Component": r"components?|composants?", "Route": r"routes?",
    "API": r"apis?|endpoints?", "Controller": r"controllers?|controleurs?", "Service": r"services?",
    "BusinessRule": r"rules?|regles?", "Repository": r"repositor(?:y|ies)|referentiels?|daos?",
    "Entity": r"entit(?:y|ies)|entites?", "Table": r"tables?", "Topic": r"topics?|queues?",
    "Capability": r"capabilit(?:y|ies)|capacites?",
}
ONT_VERBS = [   # (verb pattern on the accent-less question, relationship)
    (r"\b(?:writes?|ecri(?:t|vent)|ecrire|alimente(?:nt)?|modifie(?:nt)?)\b", "WRITES"),
    (r"\b(?:reads?|lit|lisent|consulte(?:nt)?)\b", "READS"),
    (r"\b(?:calls?|appelle(?:nt)?)\b", "CALLS"),
    (r"\b(?:uses?|utilise(?:nt)?)\b", "USES"),
    (r"\b(?:executes?|execute(?:nt)?|applique(?:nt)?|enforces?)\b", "EXECUTES"),
    (r"\b(?:exposes?|expose(?:nt)?)\b", "EXPOSES"),
    (r"\b(?:impacts?|impacte(?:nt)?)\b", "IMPACTS"),
    (r"\b(?:depends?|dependent|depend)\b", "DEPENDS_ON"),
    (r"\b(?:implements?|implemente(?:nt)?)\b", "IMPLEMENTS"),
]

INTENTS = [
    ("explore_ontology", r"ontolog"),
    ("determine_impact", r"impact|casser|\bbreak|affect|cascade|blast|what if|si je (modifie|change|supprime|renomme|retire)|"
                         r"if i (change|modify|remove|delete|rename|drop)|que se passe"),
    ("find_data_lineage", r"lineage|lignee|provenance|data ?flow|flux de donn|alimente|who (writes|reads|updates)|"
                          r"qui (ecrit|lit|modifie)|(which|quels?|quelles?) (screens?|ecrans?|apis?) (write|read|update|ecri|lis|modifi)|"
                          r"ou est (ecrit|lu|stocke)|where is .* (written|read|stored)"),
    ("show_call_chain", r"call chain|chaine d.?appel|call path|appelle|\bcalls?\b|parcours|\btrace\b|chemin|"
                        r"from .+ to |\bde .+ (a|vers|jusqu)|front.*(jusqu|to).*(db|base)|end.to.end|bout en bout"),
    ("find_business_rules", r"\brules?\b|regles?|contrainte|constraint|validation|invariant|policy|politique"),
    ("show_capability", r"capabilit|capacite|fonctionnalite|feature|domaine|\bdomain\b|perimetre"),
    ("explain_screen", r"explain|explique|decri|describe|what does|que fait|c.est quoi|screen|ecran|\bpage\b|\broute\b"),
]
KIND_WORDS = {"validation": "validation", "guard": "guard", "authorization": "authorization", "autorisation": "authorization",
              "security": "authorization", "securite": "authorization", "trigger": "db-trigger", "declared": "declared",
              "existence": "existence", "lifecycle": "lifecycle", "decision": "decision", "unique": "db-unique",
              "check": "db-check"}
SYNONYMS = {"commande": "order", "commandes": "order", "client": "customer", "clients": "customer", "stock": "stock",
            "inventaire": "inventory", "produit": "product", "facture": "invoice", "paiement": "payment",
            "utilisateur": "user", "ecran": "screen", "creation": "create", "liste": "list", "detail": "detail",
            "annulation": "cancel", "annuler": "cancel", "compte": "account", "contrat": "contract", "sinistre": "claim"}
STOP = set("the a an of for to in on is are what which who does do how me my all any with and or by from that this it "
           "le la les un une des du de d l en pour sur est sont quel quels quelle quelles qui que quoi comment mon ma mes "
           "tout tous toute avec et ou par depuis ce cette cet il elle ecran screen page route table api service "
           "rule rules regle regles impact show affiche montre donne explain explique describe decris si je change "
           "modifie supprime modify delete remove what if i would then casser break lineage chain capability".split())
EXAMPLES = [
    "which services write to orders?",
    "explain the screen OrderCreateComponent",
    "what is the impact if I change the table customers?",
    "which screens write to orders.status?",
    "show the call chain from /orders/new to the database",
    "what business rules apply to POST /api/orders?",
    "show capability Orders",
    "quelles règles métier s'appliquent à l'écran de création de commande ?",
    "quel est l'impact si je modifie la table customers ?",
]


def _strip_accents(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c)).replace("’", "'")


def _norm(s: str) -> str:
    return _strip_accents(s.lower())


def _tokens(s: str) -> list[str]:
    toks = []
    for w in split_words(_strip_accents(s)):   # camelCase split must happen before lower-casing
        w = SYNONYMS.get(w, w)
        toks.append(w[:-1] if len(w) > 3 and w.endswith("s") else w)
    return toks


class NaturalLanguage:
    def __init__(self, graph: Graph, engine=None):
        self.g = graph
        self.engine = engine or QueryEngine(graph)
        self._index = None

    def _build(self):
        idx = []
        for n in self.g.nodes.values():
            if n["type"] in ("BusinessRule",):
                continue
            toks = [t for t in _tokens(n["name"]) if t not in ("get", "post", "put", "delete", "patch", "api", "component")] \
                or _tokens(n["name"])
            idx.append((n, _norm(n["name"]), set(toks)))
        self._index = idx

    def mentions(self, question: str, prefer: tuple[str, ...] = ()) -> list[dict]:
        if self._index is None:
            self._build()
        q = _norm(question)
        qtoks = {t for t in _tokens(question) if t not in STOP}
        scored = []
        for n, name_l, toks in self._index:
            m = re.search(r"(?<![\w/])%s(?![\w])" % re.escape(name_l), q) if len(name_l) >= 3 else None
            pos = m.start() if m else -1
            span = (m.start(), m.end()) if m else None   # reset on every node
            if m:
                score = 100 + len(name_l)
            else:
                core = toks - STOP
                if not core:
                    continue
                inter = core & qtoks
                if not inter:
                    continue
                ratio = len(inter) / len(core)
                if ratio < 0.99 and not (len(inter) >= 2 and ratio >= 0.6):
                    continue
                score = 10 * ratio + len(inter) * 3 - 0.1 * len(core)
                pos = min((q.find(t) for t in inter if q.find(t) >= 0), default=0)
            if n["type"] in prefer:
                score += 25 - 5 * prefer.index(n["type"])
            scored.append((score, pos, n, span))
        scored.sort(key=lambda x: -x[0])
        out, seen = [], set()
        spans = [sp for _, _, _, sp in scored if sp]
        for score, pos, n, span in scored[:20]:
            if span and any(o != span and o[0] <= span[0] and span[1] <= o[1] for o in spans):
                continue   # contained in a longer exact mention (/orders inside /orders/new)
            if n["id"] not in seen:
                seen.add(n["id"])
                out.append({**n, "_pos": pos, "_score": score})
        return out[:12]

    def detect_intent(self, question: str) -> str | None:
        q = _norm(question)
        for name, pattern in INTENTS:
            if re.search(pattern, q):
                return name
        return None

    def ask(self, question: str) -> dict:
        intent = self.detect_intent(question)
        pref = {"determine_impact": ("Table", "Entity", "API", "Service", "Screen", "Controller", "Repository", "Component", "Topic", "BusinessRule"),
                "find_data_lineage": ("Table", "Entity", "Screen", "API"),
                "show_call_chain": ("Screen", "Route", "API", "Service", "Table", "Controller"),
                "find_business_rules": ("Screen", "Route", "API", "Service", "Controller", "Table", "Entity", "Capability"),
                "show_capability": ("Capability",),
                "explain_screen": ("Screen", "Route", "Component")}.get(intent or "", ())
        ments = self.mentions(question, pref)
        top = [m for m in ments if m["_score"] >= 10]
        try:
            lead = re.match(r"\s*(?:which|what|quels?|quelles?|list|liste|show|montre|donne)\b", _norm(question))
            if intent is None or (lead and intent in ("show_call_chain", "explain_screen")):
                onto = self._ontology_question(question, top)          # "which services write to orders?" -> free ontology query
                if onto is not None:
                    onto["intent"], onto["question"] = "explore_ontology", question
                    onto.setdefault("engine", getattr(self.engine, "engine_name", "memory"))
                    return onto
            if intent is None:
                intent = "explain_screen" if any(m["type"] == "Screen" for m in top) else (
                    "show_capability" if any(m["type"] == "Capability" for m in top) else None)
            if intent is None:
                return self._help(question, "I could not tell what you want to know.")
            result = self._dispatch(intent, question, top)
        except QueryError as ex:
            return self._help(question, str(ex), ex.suggestions, intent)
        result["intent"] = intent
        result["question"] = question
        result.setdefault("engine", getattr(self.engine, "engine_name", "memory"))
        return result

    def _ontology_question(self, question: str, top: list[dict]):
        """`<source type> <verb> <target type | named element>`  ->  explore_ontology, or None if it does not look like one."""
        q = _norm(question)
        verb = next(((m, rel) for rx, rel in ONT_VERBS for m in [re.search(rx, q)] if m), None)
        if not verb:
            return None
        m, rel = verb
        first_type = lambda text: next((t for t, rx in ONT_TYPES.items() if re.search(r"\b(?:%s)\b" % rx, text)), None)
        before, after = q[:m.start()], q[m.end():]
        src = first_type(before)
        if not src:
            return None
        tgt_type = first_type(after)
        named = next((x for x in top if x["_pos"] >= len(before) and x["_score"] >= 10 and x["type"] != "Capability"), None) \
            if not tgt_type or re.search(r"\b(?:to|dans|sur|the table|la table|table|de|d')\b", after) else None
        if named and tgt_type and named["type"] != tgt_type:
            named = None
        if not tgt_type and not named:
            return None
        res = self.engine.explore_ontology(source_type=src, relation=rel, target_type=tgt_type if not named else named["type"],
                                           target=named["id"] if named else None)
        res["data"]["interpreted"] = {"source_type": src, "relation": rel, "target": named["name"] if named else None, "target_type": tgt_type}
        return res

    def _dispatch(self, intent: str, question: str, top: list[dict]) -> dict:
        e = self.engine
        q = _norm(question)
        if intent == "explore_ontology":
            res = self._ontology_question(question, top)
            if res is None:
                raise QueryError("Say it as: <type> <relation> <type or name>, e.g. \"which services write to orders\".",
                                 ["which services write to orders", "quelles API appellent des services", "which screens call APIs"])
            return res
        if intent == "show_capability":
            cap = next((m for m in top if m["type"] == "Capability"), None)
            return e.show_capability(cap["name"] if cap else None)
        if intent == "find_business_rules":
            kind = next((v for k, v in KIND_WORDS.items() if re.search(r"\b%s" % k, q)), None)
            scope = next((m for m in top if m["type"] != "Capability" or True), None)
            text = None
            if not scope:
                text = " ".join(t for t in re.split(r"\W+", q) if t and t not in STOP and t not in KIND_WORDS) or None
            return e.find_business_rules(text=text, scope=scope["id"] if scope else None, kind=kind)
        if not top:
            raise QueryError("I could not find any element of the graph in your question.",
                             [n["name"] for n in self.g.of_type("Screen", "Table", "API")][:10])
        if intent == "explain_screen":
            m = next((m for m in top if m["type"] in ("Screen", "Route", "Component")), top[0])
            return e.explain_screen(m["id"])
        if intent == "determine_impact":
            direction = "downstream" if re.search(r"depend|relie|utilise|uses\b|needs", q) else "upstream"
            return e.determine_impact(top[0]["id"], direction)
        if intent == "find_data_lineage":
            m = next((m for m in top if m["type"] in ("Table", "Entity", "Screen", "API")), top[0])
            col = None
            cm = re.search(r"\b(?:column|colonne)\s+(\w+)", q) or re.search(r"\b%s\.(\w+)" % re.escape(_norm(m["name"])), q)
            if cm:
                col = cm.group(1)
            elif m["type"] in ("Table", "Entity"):
                cols = m["props"].get("columns") or []
                names = {(c.get("name") or c.get("column") or c.get("field")): c for c in cols}
                qt = set(re.split(r"\W+", q))
                col = next((n for n in names if n and n.lower() in qt and n.lower() not in ("id",)), None)
            return e.find_data_lineage(m["id"], col)
        if intent == "show_call_chain":
            exact = [m for m in top if m["_score"] >= 100][:2]
            pair = sorted(exact, key=lambda m: m["_pos"])
            if len(pair) == 2 and re.search(r"from .+ to |\bde .+ (a|vers|jusqu)|\bto\b|\ba\b|vers|jusqu", q):
                return e.show_call_chain(pair[0]["id"], pair[1]["id"])
            m = next((m for m in top if m["type"] in ("Screen", "Route", "API", "Controller", "Service")), top[0])
            tgt = None
            if m["type"] in ("Screen", "Route") and re.search(r"database|base de donn|\bdb\b|bdd|table", q):
                tgt = next((x["id"] for x in top if x["type"] == "Table" and x["_score"] >= 100), None)
            src = m["id"]
            if m["type"] == "Route":
                src = e.explain_screen(m["id"])["target"]["id"]
            return e.show_call_chain(src, tgt)
        raise QueryError(f"Unsupported intent {intent}")

    def _help(self, question: str, reason: str, suggestions=None, intent=None) -> dict:
        lines = [reason, "", "Examples you can ask:"] + [f"- {x}" for x in EXAMPLES]
        if suggestions:
            lines += ["", "Did you mean: " + ", ".join(f"`{s}`" for s in suggestions[:10])]
        return {"query": None, "intent": intent, "question": question, "title": "I need more precision",
                "summary": "\n".join(lines), "data": {}, "graph": {"nodes": [], "edges": [], "focus": None}, "error": True}
