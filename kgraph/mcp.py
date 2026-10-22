"""MCP server (Model Context Protocol, stdio transport) exposing the knowledge graph to AI clients.

Read-only: every tool queries an already-scanned graph, nothing is scanned, written or sent anywhere.
JSON-RPC 2.0, one message per line on stdin/stdout; stdout carries protocol messages only (logs go to stderr).
The graph file is re-read when it changes, so a scan made from the UI/CLI is picked up without restarting.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .graph import Graph
from .neo4j_engine import Neo4jEngine, synced as neo4j_synced
from .neo4j_export import Neo4jClient
from .nlq import NaturalLanguage
from .ontology import NODE_TYPES, REL_TYPES
from .queries import QueryEngine, QueryError

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
MAX_JSON_CHARS = 60_000

_S = {"type": "string"}


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or [], "additionalProperties": False}


TOOLS = [
    ("graph_overview", "Size of the loaded graph (nodes/edges by type), repository, scan date and query engine. Start here.",
     _obj({}), lambda e, a: None),
    ("ontology_schema", "The ontology: node types, relationship types and which relations really exist between which types in this graph.",
     _obj({}), lambda e, a: None),
    ("search_nodes", "Find nodes by name (fuzzy). Use it to get the exact name of a screen, API, service, table, rule... before other tools.",
     _obj({"text": {**_S, "description": "name or part of a name"},
           "types": {"type": "array", "items": {"type": "string", "enum": NODE_TYPES}, "description": "restrict to these node types"},
           "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 15}}, ["text"]), lambda e, a: None),
    ("explain_screen", "Explain a screen: routes, components, API calls, business capabilities and the back-end it reaches down to the database.",
     _obj({"name": {**_S, "description": "screen / component / route name"}}, ["name"]), lambda e, a: ("explain_screen", {"name": a["name"]})),
    ("show_call_chain", "End-to-end call chains (front -> API -> controller -> service -> repository -> table) from a node, optionally to a target.",
     _obj({"source": _S, "target": {**_S, "description": "optional: only chains reaching this node"}}, ["source"]),
     lambda e, a: ("show_call_chain", {"source": a["source"], "target": a.get("target")})),
    ("find_business_rules", "Business rules (with evidence and confidence), optionally scoped to a screen/API/service/table or filtered by text/kind.",
     _obj({"scope": {**_S, "description": "node whose rules to list"}, "text": {**_S, "description": "free-text filter"},
           "kind": _S, "min_confidence": {"type": "number", "minimum": 0, "maximum": 1}}),
     lambda e, a: ("find_business_rules", {k: a.get(k) for k in ("scope", "text", "kind", "min_confidence")})),
    ("determine_impact", "Impact analysis: what is affected if this node changes. upstream = who depends on it (front side), downstream = what it depends on (database side).",
     _obj({"name": {**_S, "description": "table, entity, service, API, rule, screen..."},
           "direction": {"type": "string", "enum": ["upstream", "downstream"], "default": "upstream"}}, ["name"]),
     lambda e, a: ("determine_impact", {"name": a["name"], "direction": a.get("direction")})),
    ("show_capability", "Business capabilities and the screens, APIs, services, rules and tables that implement them (all capabilities when no name is given).",
     _obj({"name": _S}), lambda e, a: ("show_capability", {"name": a.get("name")})),
    ("find_data_lineage", "Data lineage of a table or column: which screens/APIs/services read or write it and through which path.",
     _obj({"target": {**_S, "description": "table or entity"}, "column": _S}, ["target"]),
     lambda e, a: ("find_data_lineage", {"target": a["target"], "column": a.get("column")})),
    ("explore_ontology", "Free query on the ontology: source_type -[relation]-> target_type (e.g. Service -WRITES-> Table). Omit a field for 'any'.",
     _obj({"source_type": {"type": "string", "enum": NODE_TYPES}, "relation": {"type": "string", "enum": REL_TYPES},
           "target_type": {"type": "string", "enum": NODE_TYPES}, "direction": {"type": "string", "enum": ["out", "in"], "default": "out"},
           "depth": {"type": "integer", "minimum": 1, "maximum": 6, "default": 1},
           "source_text": {**_S, "description": "filter source nodes by name"}, "target_text": {**_S, "description": "filter target nodes by name"}}),
     lambda e, a: ("explore_ontology", {k: a.get(k) for k in ("source_type", "relation", "target_type", "direction", "depth",
                                                                "source_text", "target_text")})),
    ("ask", "Ask a question in natural language (English or French), e.g. 'What is the impact if I change the table customers?'.",
     _obj({"question": _S}, ["question"]), lambda e, a: None),
]
TOOL_NAMES = {t[0] for t in TOOLS}


class GraphProvider:
    """Loads the graph lazily and reloads it when the file changes (or when Neo4j is the engine, per repository)."""

    def __init__(self, graph_path: str | None, workdir: str = ".kgraph", engine: str = "memory"):
        self.path = Path(graph_path) if graph_path else Path(workdir) / "last-graph.json"
        self.engine_kind = engine
        self._mtime = None
        self._graph: Graph | None = None
        self._engine = None

    def get(self) -> tuple[Graph, QueryEngine]:
        try:
            mtime = self.path.stat().st_mtime_ns
        except OSError:
            raise QueryError(f"No graph at {self.path}. Scan a repository first (`python3 -m kgraph scan <source>` or the web UI), "
                             "or start the server with --graph <graph.json>.")
        if self._graph is None or mtime != self._mtime:
            self._graph = Graph.from_dict(json.loads(self.path.read_text(encoding="utf-8")))
            self._mtime, self._engine = mtime, None
        if self._engine is None:
            self._engine = self._make_engine(self._graph)
        return self._graph, self._engine

    def _make_engine(self, g: Graph):
        if self.engine_kind != "neo4j":
            return QueryEngine(g)
        client = Neo4jClient.from_env()
        repo = str(g.meta.get("repository") or "repo")
        if client is None or neo4j_synced(client, repo) is None:
            raise QueryError("--engine neo4j needs NEO4J_PASSWORD in the environment and the graph loaded in Neo4j "
                             "(`python3 -m kgraph neo4j --graph <graph.json> --load`).")
        return Neo4jEngine(client, repo)


def _trim(result: dict) -> dict:
    out = {k: v for k, v in result.items() if k != "graph"}    # drawing payload: useless to an agent
    if len(json.dumps(out, ensure_ascii=False, default=str)) > MAX_JSON_CHARS:
        out = {k: v for k, v in out.items() if k in ("query", "summary", "engine", "suggestions")}
        out["truncated"] = "result too large; narrow the question (scope, target, depth) for the full detail"
    return out


class McpServer:
    def __init__(self, provider: GraphProvider):
        self.provider = provider
        self.initialized = False

    # ------------------------------------------------------------------ tools
    def call_tool(self, name: str, args: dict) -> dict:
        if name not in TOOL_NAMES:
            raise QueryError(f"Unknown tool '{name}'")
        g, eng = self.provider.get()
        if name == "graph_overview":
            return {"summary": f"{g.meta.get('repository', 'repository')}: {g.stats()['nodes']} nodes, {g.stats()['edges']} edges",
                    "meta": {k: g.meta.get(k) for k in ("repository", "scanned_at", "commit", "branch", "languages", "rules_source") if k in g.meta},
                    "stats": g.stats(), "engine": getattr(eng, "engine_name", "memory")}
        if name == "ontology_schema":
            return eng.ontology_schema()
        if name == "search_nodes":
            limit = max(1, min(int(args.get("limit") or 15), 50))
            hits = g.find(args["text"], args.get("types") or None, limit=limit)
            return {"summary": f"{len(hits)} node(s) match '{args['text']}'",
                    "nodes": [{"id": n["id"], "type": n["type"], "name": n["name"]} for n in hits]}
        if name == "ask":
            return NaturalLanguage(g, eng).ask(args["question"])
        query, params = next(t[3] for t in TOOLS if t[0] == name)(eng, args)
        return eng.run(query, **{k: v for k, v in params.items() if v is not None})

    def tools_list(self) -> list[dict]:
        return [{"name": n, "description": d, "inputSchema": s, "annotations": {"readOnlyHint": True, "openWorldHint": False}}
                for n, d, s, _ in TOOLS]

    # ------------------------------------------------------------------ protocol
    def handle(self, msg: dict) -> dict | None:
        mid, method = msg.get("id"), msg.get("method")
        is_request = "id" in msg and method is not None

        def ok(result):
            return {"jsonrpc": "2.0", "id": mid, "result": result}

        def err(code, text):
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": text}}

        if not is_request:
            return None                         # notifications (initialized, cancelled...) and stray responses
        params = msg.get("params") or {}
        if method == "initialize":
            wanted = params.get("protocolVersion")
            self.initialized = True
            return ok({"protocolVersion": wanted if wanted in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                       "capabilities": {"tools": {"listChanged": False}},
                       "serverInfo": {"name": "kgraph", "version": "1.0.0"},
                       "instructions": "Read-only technical & functional knowledge graph of a scanned application repository. "
                                       "Call graph_overview first, search_nodes to get exact names, then the query tools or ask."})
        if method == "ping":
            return ok({})
        if method == "tools/list":
            return ok({"tools": self.tools_list()})
        if method == "tools/call":
            name, args = params.get("name"), params.get("arguments") or {}
            if name not in TOOL_NAMES:
                return err(-32602, f"Unknown tool: {name}")
            if not isinstance(args, dict):
                return err(-32602, "arguments must be an object")
            try:
                res = _trim(self.call_tool(name, args))
            except QueryError as ex:
                text = str(ex) + (f"\nSuggestions: {', '.join(ex.suggestions)}" if getattr(ex, "suggestions", None) else "")
                return ok({"content": [{"type": "text", "text": text}], "isError": True})
            except KeyError as ex:
                return ok({"content": [{"type": "text", "text": f"Missing argument {ex}"}], "isError": True})
            except Exception as ex:             # never let a query kill the server
                print(f"kgraph-mcp: {name} failed: {ex!r}", file=sys.stderr)
                return ok({"content": [{"type": "text", "text": f"Internal error: {ex}"}], "isError": True})
            body = json.dumps(res, ensure_ascii=False, default=str)
            return ok({"content": [{"type": "text", "text": f"{res.get('summary', '')}\n\n{body}".strip()}],
                       "structuredContent": json.loads(body), "isError": False})
        return err(-32601, f"Method not found: {method}")

    def serve(self, stdin=None, stdout=None):
        stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
        print("kgraph-mcp: ready (stdio)", file=sys.stderr)
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                out = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
            else:
                if isinstance(msg, list) or not isinstance(msg, dict):
                    out = {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request"}}
                else:
                    out = self.handle(msg)
            if out is not None:
                stdout.write(json.dumps(out, ensure_ascii=True) + "\n")
                stdout.flush()


def serve_stdio(graph: str | None = None, workdir: str = ".kgraph", engine: str = "memory"):
    McpServer(GraphProvider(graph, workdir, engine)).serve()
