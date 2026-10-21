"""Command line: python -m kgraph <scan|serve|query|ask|docs> ..."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .docs import write_documentation
from .graph import Graph
from .llm import LlmError, RuleEnricher, make_provider
from .modernization import ModernizationError, Modernizer
from .neo4j_engine import Neo4jEngine, synced as neo4j_synced
from .neo4j_export import Neo4jClient, Neo4jError, load as neo4j_load, to_cypher
from .nlq import NaturalLanguage
from .ontology import QUERIES
from .pipeline import scan_repository
from .queries import QueryEngine, QueryError
from .source import RepoError


def _load(args) -> Graph:
    if args.graph:
        return Graph.from_dict(json.loads(Path(args.graph).read_text(encoding="utf-8")))
    if args.repo:
        return scan_repository(args.repo, args.workdir + "/workspaces", None)
    sys.exit("error: pass --graph graph.json or --repo <path|git-url>")


def _rules_args(sp):
    sp.add_argument("--rules", choices=["heuristic", "modernization", "none"], default="heuristic",
                    help="business-rule source: built-in heuristics (default), the Claude Code `code-modernization` plugin, "
                         "or none (structure only; add rules later with the UI button)")
    sp.add_argument("--modernization-pattern", metavar="MODULES", help="limit the plugin to matching modules (recommended: start with one)")
    sp.add_argument("--yes", action="store_true", help="skip the cost / data confirmation of --rules modernization")


def _confirm_modernization(args):
    if args.rules != "modernization" or args.yes:
        return
    print("The code-modernization plugin starts MANY Claude agents (about 10-15 per module, ~16k tokens each) and sends the\n"
          "source code of the repository to Anthropic. Start with one module via --modernization-pattern.", file=sys.stderr)
    if input("Proceed? [y/N] ").strip().lower() != "y":
        sys.exit("aborted")


def _engine(args, g):
    """QueryEngine over `g`, or the Neo4j engine when --engine neo4j (credentials from the environment only)."""
    if getattr(args, "engine", "memory") != "neo4j":
        return QueryEngine(g)
    client = Neo4jClient.from_env()
    if client is None:
        sys.exit("error: --engine neo4j needs NEO4J_PASSWORD (and optionally NEO4J_URI, NEO4J_USER, NEO4J_DATABASE) in the environment")
    repo = str(g.meta.get("repository") or "repo")
    if neo4j_synced(client, repo) is None:
        sys.exit(f"error: '{repo}' is not loaded in Neo4j yet: run  python3 -m kgraph neo4j --graph <graph.json> --load")
    return Neo4jEngine(client, repo)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="kgraph", description="Technical & functional knowledge graph of an application repository")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("scan", help="scan a repository (local path or git URL) and write graph.json")
    sp.add_argument("source")
    sp.add_argument("--branch")
    sp.add_argument("--out", default="graph.json")
    sp.add_argument("--workdir", default=".kgraph")
    sp.add_argument("--quiet", action="store_true")
    sp.add_argument("--llm", nargs="?", const="auto", choices=["auto", "cli", "api"],
                    help="enrich business rules with Claude (sends business-class source code to Anthropic)")
    sp.add_argument("--llm-model")
    sp.add_argument("--llm-max-classes", type=int, default=60)
    _rules_args(sp)

    sp = sub.add_parser("serve", help="start the web UI")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8765)
    sp.add_argument("--workdir", default=".kgraph")
    sp.add_argument("--scan", metavar="SOURCE", help="scan this repository at startup")
    sp.add_argument("--llm", nargs="?", const="auto", choices=["auto", "cli", "api"], help="enrich rules with Claude for the startup scan")
    _rules_args(sp)

    for name in ("query", "ask", "docs", "neo4j"):
        sp = sub.add_parser(name)
        sp.add_argument("--graph", help="graph.json produced by `scan`")
        sp.add_argument("--repo", help="scan this repository on the fly")
        sp.add_argument("--workdir", default=".kgraph")
        if name in ("query", "ask"):
            sp.add_argument("--engine", choices=["memory", "neo4j"], default="memory",
                            help="answer from memory (default) or from Neo4j (NEO4J_PASSWORD / NEO4J_URI in the environment; "
                                 "the graph must have been loaded: `neo4j --load`)")
        if name == "query":
            sp.add_argument("query", choices=QUERIES)
            sp.add_argument("args", nargs="*", help="main argument(s): name | source [target] | target [column]")
            sp.add_argument("--json", action="store_true")
        elif name == "ask":
            sp.add_argument("question")
        elif name == "docs":
            sp.add_argument("--out", default="docs-generated")
        else:
            sp.add_argument("--out", help="write the Cypher script here (default graph.cypher unless only --load is given)")
            sp.add_argument("--load", action="store_true", help="push the graph into a running Neo4j (HTTP endpoint)")
            sp.add_argument("--uri", default="http://localhost:7474", help="Neo4j HTTP endpoint (default %(default)s)")
            sp.add_argument("--user", default="neo4j")
            sp.add_argument("--database", default="neo4j")
            sp.add_argument("--wait", type=int, default=0, metavar="SECONDS", help="with --load: wait up to this long for the database to accept queries")
            sp.add_argument("--clear", action="store_true", help="first delete the nodes previously exported for this repository (:KGNode only)")
            sp.add_argument("--yes", action="store_true", help="do not ask for confirmation before --clear")

    args = ap.parse_args(argv)
    try:
        if args.cmd == "scan":
            _confirm_modernization(args)
            modernizer = Modernizer(Path(args.workdir), args.modernization_pattern) if args.rules == "modernization" else None
            if modernizer and args.llm:
                sys.exit("error: --llm and --rules modernization are mutually exclusive")
            enricher = None
            if args.llm:
                enricher = RuleEnricher(make_provider(args.llm, args.llm_model), Path(args.workdir) / "llm-cache.json",
                                        args.llm_max_classes)
            g = scan_repository(args.source, args.workdir + "/workspaces", args.branch,
                                (lambda s, m: None) if args.quiet else (lambda s, m: print(f"[{s}] {m}", file=sys.stderr)),
                                llm=enricher, modernizer=modernizer, rules_engine="none" if args.rules == "none" else "heuristic")
            Path(args.out).write_text(g.to_json(indent=1), encoding="utf-8")
            print(json.dumps(g.stats(), indent=1))
            print(f"graph written to {args.out}")
            src = g.meta.get("rules_source") or {}
            if src.get("command"):
                print(f"business rules produced by: {src['command']}  (plugin: {src['plugin']})\n"
                      f"  run {src['started_at']}, {src['duration_s']} s, {src['imported']}/{src['cards']} cards imported\n"
                      f"  proof: {src['rules_file']}  sha256 {src['rules_sha256']}\n  transcript: {src['transcript']}")
        elif args.cmd == "serve":
            from .server import serve
            if args.scan and args.rules == "modernization":
                _confirm_modernization(args)
            serve(args.host, args.port, args.workdir, args.scan, args.llm, args.rules, args.modernization_pattern)
        elif args.cmd == "query":
            eng = _engine(args, _load(args))
            a = args.args
            params = {
                "explain_screen": lambda: {"name": a[0]},
                "show_call_chain": lambda: {"source": a[0], "target": a[1] if len(a) > 1 else None},
                "find_business_rules": lambda: {"scope": a[0] if a else None, "text": a[1] if len(a) > 1 else None},
                "determine_impact": lambda: {"name": a[0]},
                "show_capability": lambda: {"name": a[0] if a else None},
                "find_data_lineage": lambda: {"target": a[0], "column": a[1] if len(a) > 1 else None},
                # explore_ontology SOURCE_TYPE RELATION TARGET_TYPE [DEPTH]   ("-" = any)
                "explore_ontology": lambda: {k: (a[i] if len(a) > i and a[i] not in ("-", "*", "any") else None)
                                             for i, k in enumerate(("source_type", "relation", "target_type", "depth"))},
            }[args.query]()
            res = eng.run(args.query, **params)
            print(json.dumps(res, indent=1, ensure_ascii=False) if args.json else res["summary"])
        elif args.cmd == "ask":
            g = _load(args)
            print(NaturalLanguage(g, _engine(args, g)).ask(args.question)["summary"])
        elif args.cmd == "neo4j":
            g = _load(args)
            if args.out or not args.load:
                out = Path(args.out or "graph.cypher")
                out.write_text(to_cypher(g), encoding="utf-8")
                print(f"Cypher script written to {out}  ({g.stats()['nodes']} nodes, {g.stats()['edges']} relationships)")
            if args.load:
                import getpass
                import os
                password = os.environ.get("NEO4J_PASSWORD") or getpass.getpass(f"Neo4j password for {args.user}: ")
                if args.clear and not args.yes and input(f"Delete the previously exported nodes of '{g.meta.get('repository')}' first? [y/N] ").strip().lower() != "y":
                    sys.exit("aborted")
                r = neo4j_load(g, args.uri, args.user, password, args.database, args.clear, wait=args.wait)
                print(f"Loaded into Neo4j ({r['database']}): {r['nodes']} nodes, {r['relationships']} relationships in {r['statements']} statements.")
        elif args.cmd == "docs":
            files = write_documentation(_load(args), args.out)
            print(f"{len(files)} files written to {args.out}/")
    except (RepoError, QueryError, LlmError, ModernizationError, Neo4jError) as ex:
        sys.exit(f"error: {ex}" + (f"\nsuggestions: {', '.join(ex.suggestions)}" if getattr(ex, "suggestions", None) else ""))
    except IndexError:
        sys.exit("error: missing argument for this query")


if __name__ == "__main__":
    main()
