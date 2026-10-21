"""Local web server (stdlib only): REST API + static single-page UI."""
from __future__ import annotations

import json
import mimetypes
import os
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .docs import documentation_zip, generate_documentation
from .graph import Graph
from .llm import LlmError, RuleEnricher, make_provider, provider_available
from .modernization import (INSTALL_HINT, ModernizationError, Modernizer, auth_message, available as claude_available,
                            logged_in, plugin_installed)
from .neo4j_engine import Neo4jEngine, Neo4jError, synced as neo4j_synced
from .neo4j_export import Neo4jClient, fingerprint as neo4j_fingerprint, load as neo4j_load, to_cypher
from .nlq import NaturalLanguage
from .ontology import CAPABILITIES, LAYERS, NODE_TYPES, PIPELINE, QUERIES, REL_TYPES, SUPPORTED_LANGUAGES
from .pipeline import scan_repository
from .queries import QueryEngine, QueryError
from .source import RepoError

STATIC = Path(__file__).parent / "static"
PLUGIN_MISSING = ("The code-modernization plugin is not installed. Run once in a terminal: "
                  "claude plugin install code-modernization@claude-plugins-official")
MAX_BODY = 1_000_000
LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]"}


class State:
    def __init__(self, workdir: Path):
        self.workdir = workdir
        self.lock = threading.Lock()
        self.graph: Graph | None = None
        self.engine: QueryEngine | None = None
        self.nlq: NaturalLanguage | None = None
        self.status = {"state": "idle", "stage": None, "log": [], "error": None}
        self.cache = workdir / "last-graph.json"
        self._plugin_cache = (0.0, False, None)
        self.last: dict = {}            # source / branch of the last successful scan
        self.thread = None
        self.cancel_hook = None
        # query engine: in memory, or Neo4j when NEO4J_PASSWORD is set (NEO4J_URI / NEO4J_USER / NEO4J_DATABASE optional)
        self.mem: QueryEngine | None = None
        self.nlq_mem: NaturalLanguage | None = None
        self.engine_pref = os.environ.get("KGRAPH_ENGINE", "auto")      # auto | neo4j | memory
        self.neo: Neo4jClient | None = None
        self.neo_state: dict = {"state": "off", "error": None}
        if self.engine_pref != "memory":
            try:
                self.neo = Neo4jClient.from_env()
            except Neo4jError as ex:
                self.neo_state = {"state": "error", "error": str(ex)}
        if self.neo is not None:
            self.neo_state = {"state": "pending", "error": None, "uri": self.neo.base, "database": self.neo.database}

    def _cli_state(self) -> tuple[bool, bool | None]:
        """(plugin installed, logged in?) — `claude` calls are slow: cached for 30 s."""
        ts, ready, auth = self._plugin_cache
        if time.time() - ts > 30:
            ready = claude_available() and plugin_installed()
            auth = logged_in() if ready else None
            self._plugin_cache = (time.time(), ready, auth)
        return ready, auth

    def modernization_ready(self) -> bool:
        return self._cli_state()[0]

    def claude_logged_in(self) -> bool | None:
        return self._cli_state()[1]

    def modernization_problem(self) -> str | None:
        ready, auth = self._cli_state()
        if not ready:
            return PLUGIN_MISSING
        return auth_message() if auth is False else None

    def set_graph(self, g: Graph, sync: bool = False):
        """Install a graph. With Neo4j configured it is synchronised first (inline if `sync`, else in the background);
        until then — and whenever Neo4j is unavailable — queries are answered in memory."""
        with self.lock:
            self.mem = QueryEngine(g)
            self.nlq_mem = NaturalLanguage(g, self.mem)
            self.graph, self.engine, self.nlq = g, self.mem, self.nlq_mem
        if self.neo is not None:
            if sync:
                self._sync(g)
            else:
                threading.Thread(target=self._sync, args=(g,), daemon=True).start()

    def _sync(self, g: Graph):
        repo = str(g.meta.get("repository") or "repo")
        fp = neo4j_fingerprint(g)
        self.neo_state = {**self.neo_state, "state": "syncing", "error": None, "repo": repo}
        try:
            marker = neo4j_synced(self.neo, repo)
            if not (marker and marker.get("fingerprint") == fp):          # already loaded and unchanged: nothing to push
                neo4j_load(g, clear=True, wait=30, client=self.neo)
                marker = neo4j_synced(self.neo, repo)
            engine = Neo4jEngine(self.neo, repo)
            with self.lock:
                if self.graph is g:                                       # a newer scan may have replaced it meanwhile
                    self.engine, self.nlq = engine, NaturalLanguage(g, engine)
            self.neo_state = {**self.neo_state, "state": "ready", "error": None,
                              "synced_at": (marker or {}).get("synced_at"), "nodes": (marker or {}).get("nodes")}
        except Neo4jError as ex:
            self.neo_state = {**self.neo_state, "state": "error", "error": str(ex)}

    def engine_info(self) -> dict:
        return {"preferred": self.engine_pref, "active": getattr(self.engine, "engine_name", "memory"),
                "neo4j": None if self.neo is None and self.neo_state["state"] == "off" else self.neo_state}

    def _with_fallback(self, call):
        """Run `call(engine, nlq)` on the active engine; if Neo4j fails mid-request, answer from memory and say so."""
        t0 = time.time()
        eng, nlq = self.engine, self.nlq
        try:
            res = call(eng, nlq)
        except Neo4jError as ex:
            if eng is self.mem:
                raise
            res = call(self.mem, self.nlq_mem)
            res["engine_note"] = f"Neo4j unavailable, answered in memory: {ex}"
            self.neo_state = {**self.neo_state, "state": "error", "error": str(ex)}
        if isinstance(res, dict):
            res["elapsed_ms"] = int((time.time() - t0) * 1000)
        return res

    def run_query(self, query: str, params: dict) -> dict:
        return self._with_fallback(lambda eng, nlq: eng.run(query, **params))

    def ask(self, question: str) -> dict:
        return self._with_fallback(lambda eng, nlq: nlq.ask(question))

    def load_cache(self):
        if self.cache.exists():
            try:
                self.set_graph(Graph.from_dict(json.loads(self.cache.read_text(encoding="utf-8"))))
                self.status["state"] = "done"
            except (OSError, ValueError, KeyError):
                pass

    def scan(self, source: str, branch: str | None, llm_mode: str | None = None, rules_engine: str = "heuristic",
             pattern: str | None = None, reuse: bool = False):
        def progress(stage, msg):
            self.status["stage"] = stage
            self.status["log"].append(f"[{stage}] {msg}")
        try:
            enricher = modernizer = None
            if rules_engine == "modernization":
                modernizer = Modernizer(self.workdir, pattern, reuse_existing=reuse)
                self.cancel_hook = modernizer.cancel
                modernizer.on_progress = lambda snap: self.status.update(progress=snap)
            elif llm_mode:
                enricher = RuleEnricher(make_provider(llm_mode), self.workdir / "llm-cache.json")
            g = scan_repository(source, self.workdir / "workspaces", branch, progress, llm=enricher, modernizer=modernizer,
                                rules_engine=rules_engine if rules_engine == "none" else "heuristic")
            self.last = {"source": source, "branch": branch}
            self.workdir.mkdir(parents=True, exist_ok=True)
            self.cache.write_text(g.to_json(), encoding="utf-8")
            self.set_graph(g, sync=True)                                  # Neo4j (if configured) is ready when the scan ends
            prog = self.status.get("progress")
            self.status.update(state="done", stage=None, error=None, progress={**prog, "phase": "done"} if prog else None)
        except (RepoError, LlmError, ModernizationError, ValueError) as ex:
            self.status.update(state="error", error=str(ex))
        except Exception as ex:   # surfaced to the UI instead of killing the thread silently
            traceback.print_exc()
            self.status.update(state="error", error=f"{type(ex).__name__}: {ex}")

    def start_scan(self, source: str, branch: str | None, llm_mode: str | None = None, rules_engine: str = "heuristic",
                   pattern: str | None = None, reuse: bool = False) -> bool:
        if self.status["state"] == "running":
            return False
        self.status = {"state": "running", "stage": "scan_repository", "log": [], "error": None, "progress": None}
        self.cancel_hook = None
        self.thread = threading.Thread(target=self.scan, args=(source, branch, llm_mode, rules_engine, pattern, reuse), daemon=True)
        self.thread.start()
        return True

    def cancel(self) -> bool:
        if self.status["state"] == "running" and self.cancel_hook:
            self.cancel_hook()
            return True
        return False


def make_handler(state: State, port: int):
    class Handler(BaseHTTPRequestHandler):
        server_version = "kgraph/1.0"

        def log_message(self, fmt, *args):
            pass

        # -- helpers -------------------------------------------------------
        def _send(self, code, body: bytes, ctype="application/json; charset=utf-8", extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, ensure_ascii=False, default=list).encode())

        def _err(self, code, msg, suggestions=None):
            self._json({"error": msg, "suggestions": suggestions or []}, code)

        def _guard(self, write: bool) -> bool:
            """DNS-rebinding / CSRF protection for a local-only tool."""
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            if host not in LOCAL_HOSTS:
                self._err(403, "forbidden host")
                return False
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).hostname not in {"localhost", "127.0.0.1", "::1"}:
                self._err(403, "forbidden origin")
                return False
            if write and self.headers.get("X-KG-Request") != "1":
                self._err(403, "missing X-KG-Request header")
                return False
            return True

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise ValueError("request too large")
            return json.loads(self.rfile.read(n) or b"{}") if n else {}

        def _need_graph(self) -> bool:
            if state.graph is None:
                self._err(409, "No repository scanned yet. Use POST /api/scan first.")
                return False
            return True

        # -- routes --------------------------------------------------------
        def do_GET(self):
            if not self._guard(False):
                return
            url = urlparse(self.path)
            qs = {k: v[0] for k, v in parse_qs(url.query).items()}
            path = url.path
            try:
                if path in ("/", "/index.html"):
                    return self._static("index.html")
                if path.startswith("/static/"):
                    return self._static(path[len("/static/"):])
                if path == "/api/meta":
                    return self._json({"node_types": NODE_TYPES, "relationships": REL_TYPES, "capabilities": CAPABILITIES,
                                       "pipeline": PIPELINE, "queries": QUERIES, "layers": LAYERS,
                                       "languages": SUPPORTED_LANGUAGES})
                if path == "/api/status":
                    s = dict(state.status)
                    s["log"] = s["log"][-40:]
                    s["has_graph"] = state.graph is not None
                    s["engine"] = state.engine_info()
                    s["llm_available"] = provider_available()
                    s["modernization_ready"] = state.modernization_ready()
                    s["claude_logged_in"] = state.claude_logged_in()
                    s["source"] = state.last.get("source")
                    s["cancellable"] = state.status["state"] == "running" and state.cancel_hook is not None
                    s["modernization_previous"] = bool(state.graph and Modernizer.previous_rules(state.workdir, state.graph.meta.get("repository", "")))
                    s["meta"] = state.graph.meta if state.graph else None
                    return self._json(s)
                if not self._need_graph():
                    return
                g = state.graph
                if path == "/api/graph":
                    return self._send(200, g.to_json().encode())
                if path == "/api/node":
                    n = g.nodes.get(qs.get("id", ""))
                    if not n:
                        return self._err(404, "unknown node")
                    return self._json({"node": n, "out": g.out_edges(n["id"]), "in": g.in_edges(n["id"])})
                if path == "/api/search":
                    types = qs["types"].split(",") if qs.get("types") else None
                    return self._json([{"id": n["id"], "type": n["type"], "name": n["name"]}
                                       for n in g.find(qs.get("q", ""), types, limit=30)])
                if path == "/api/ontology":
                    return self._json(state._with_fallback(lambda eng, nlq: eng.ontology_schema()))
                if path == "/api/docs":
                    return self._json({"files": generate_documentation(g)})
                if path == "/api/neo4j.cypher":
                    return self._send(200, to_cypher(g).encode(), "text/plain; charset=utf-8",
                                      {"Content-Disposition": 'attachment; filename="graph.cypher"'})
                if path == "/api/docs.zip":
                    return self._send(200, documentation_zip(g), "application/zip",
                                      {"Content-Disposition": 'attachment; filename="knowledge-graph-docs.zip"'})
                self._err(404, "not found")
            except QueryError as ex:
                self._err(404, str(ex), ex.suggestions)
            except Exception as ex:
                traceback.print_exc()
                self._err(500, f"{type(ex).__name__}: {ex}")

        def do_POST(self):
            if not self._guard(True):
                return
            path = urlparse(self.path).path
            try:
                body = self._body()
                if path == "/api/scan":
                    src = (body.get("source") or "").strip()
                    if not src:
                        return self._err(400, "'source' (local path or git URL) is required")
                    if body.get("llm") and not provider_available():
                        return self._err(400, "Claude enrichment requested but neither the `claude` CLI nor ANTHROPIC_API_KEY is available")
                    engine = body.get("rules_engine") or "heuristic"
                    if engine not in ("heuristic", "modernization", "none"):
                        return self._err(400, "rules_engine must be 'heuristic', 'modernization' or 'none'")
                    if engine == "modernization" and state.modernization_problem():
                        return self._err(400, state.modernization_problem())
                    if not state.start_scan(src, (body.get("branch") or "").strip() or None, "auto" if body.get("llm") else None,
                                            engine, (body.get("module_pattern") or "").strip() or None):
                        return self._err(409, "a scan is already running")
                    return self._json({"started": True}, 202)
                if path == "/api/cancel":
                    return self._json({"cancelled": True}) if state.cancel() else self._err(409, "nothing to cancel")
                if not self._need_graph():
                    return
                if path == "/api/rules/analyze":
                    reuse = bool(body.get("reuse"))
                    if not state.last.get("source"):
                        return self._err(409, "scan a repository first")
                    if reuse and not Modernizer.previous_rules(state.workdir, state.graph.meta.get("repository", "")):
                        return self._err(400, "no previous plugin result to reuse: run the analysis once first")
                    if not reuse and state.modernization_problem():
                        return self._err(400, state.modernization_problem())
                    if not state.start_scan(state.last["source"], state.last.get("branch"), None, "modernization",
                                            (body.get("module_pattern") or "").strip() or None, reuse):
                        return self._err(409, "a scan or analysis is already running")
                    return self._json({"started": True}, 202)
                if path == "/api/query":
                    return self._json(state.run_query(body.get("query", ""), body.get("params") or {}))
                if path == "/api/ask":
                    return self._json(state.ask(body.get("question", "")))
                self._err(404, "not found")
            except QueryError as ex:
                self._err(404, str(ex), ex.suggestions)
            except Neo4jError as ex:
                self._err(502, f"Neo4j: {ex}")
            except (ValueError, json.JSONDecodeError) as ex:
                self._err(400, str(ex))
            except Exception as ex:
                traceback.print_exc()
                self._err(500, f"{type(ex).__name__}: {ex}")

        def _static(self, rel: str):
            p = (STATIC / rel).resolve()
            if STATIC.resolve() not in p.parents or not p.is_file():
                return self._err(404, "not found")
            ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            self._send(200, p.read_bytes(), ctype)

    return Handler


def serve(host: str = "127.0.0.1", port: int = 8765, workdir: str = ".kgraph", scan: str | None = None, llm: str | None = None,
          rules_engine: str = "heuristic", pattern: str | None = None):
    state = State(Path(workdir))
    state.load_cache()
    if scan:
        state.start_scan(scan, None, llm, rules_engine, pattern)
    httpd = ThreadingHTTPServer((host, port), make_handler(state, port))
    print(f"Knowledge-graph explorer on http://{host}:{port}  (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
