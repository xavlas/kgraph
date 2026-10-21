"""generate_documentation: Markdown + Mermaid documentation from the graph."""
from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path

from .graph import Graph
from .ontology import NODE_TYPES, REL_TYPES
from .queries import QueryEngine

FOOTER = ("\n---\n*Generated automatically by static analysis (heuristic). Review and validate before sharing "
          "outside the team; business-rule descriptions are derived from code and may need functional confirmation.*\n")


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-").lower() or "item"


def _cell(s) -> str:
    return str(s if s is not None else "").replace("|", "\\|").replace("\n", " ")


def _mermaid_type(t: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "", (t or "text").split("(")[0]) or "text"


def generate_documentation(g: Graph) -> dict[str, str]:
    q = QueryEngine(g)
    files: dict[str, str] = {}
    meta = g.meta
    stats = g.stats()

    idx = [f"# Knowledge graph — {meta.get('repository', 'repository')}\n",
           f"- Source: `{meta.get('origin', '')}`" + (f" @ `{meta['commit']}`" if meta.get("commit") else ""),
           f"- Scanned at: {meta.get('scanned_at', '')}",
           f"- Frameworks detected: {', '.join(meta.get('frameworks') or []) or 'n/a'}",
           f"- Business rules source: {(meta.get('rules_source') or {}).get('engine', 'n/a')}"
           + (f" — command `{meta['rules_source']['command']}`, run {meta['rules_source']['started_at']} "
              f"({meta['rules_source']['duration_s']} s), rules file sha256 `{meta['rules_source']['rules_sha256']}`"
              if (meta.get('rules_source') or {}).get('command') else "") + "\n",
           "## Graph size\n", "| Node type | Count |", "|---|---|"]
    idx += [f"| {t} | {stats['by_type'].get(t, 0)} |" for t in NODE_TYPES]
    idx += ["", "| Relationship | Count |", "|---|---|"] + [f"| {r} | {stats['by_rel'].get(r, 0)} |" for r in REL_TYPES]
    caps = q.show_capability()["data"]["capabilities"]
    idx += ["", "## Capabilities\n", "| Capability | Screens | APIs | Services | Tables | Rules |", "|---|---|---|---|---|---|"]
    idx += [f"| {c['capability']['name']} | {c['screens']} | {c['apis']} | {c['services']} | {c['tables']} | {c['rules']} |" for c in caps]
    idx += ["", "## Contents\n", "- [Capabilities](capabilities.md)", "- [Screens](screens/)", "- [APIs](apis.md)",
            "- [Business rules](business-rules.md)", "- [Data model](data-model.md)"]
    if g.of_type("Topic"):
        idx.append("- [Messaging topics](topics.md)")
    files["index.md"] = "\n".join(idx) + FOOTER

    # capabilities
    out = ["# Capabilities\n"]
    for c in g.of_type("Capability"):
        r = q.show_capability(c["name"])
        out += [f"## {c['name']}\n", r["summary"].split("\n", 1)[1], ""]
    files["capabilities.md"] = "\n".join(out) + FOOTER

    # screens
    screens = sorted(g.of_type("Screen"), key=lambda n: n["name"])
    for s in screens:
        r = q.explain_screen(s["id"])
        chain = q.show_call_chain(s["id"])
        lin = q.find_data_lineage(s["id"])
        rules = q.find_business_rules(scope=s["id"])
        md = [f"# Screen {s['name']}\n", f"`{s['props'].get('file')}:{s['props'].get('line')}`\n", "## Overview\n", r["summary"], ""]
        md += ["## Call chains\n", "```mermaid", chain["data"]["mermaid"], "```", ""]
        md += ["## Data touched\n", lin["summary"], ""]
        md += ["## Business rules\n", rules["summary"], ""]
        files[f"screens/{_slug(s['name'])}.md"] = "\n".join(md) + FOOTER
    files["screens/README.md"] = "# Screens\n\n" + "\n".join(
        f"- [{s['name']}]({_slug(s['name'])}.md)" for s in screens) + "\n" + FOOTER

    # APIs
    rows = ["# APIs\n", "| API | Handler | Called by | Reads | Writes | Rules |", "|---|---|---|---|---|---|"]
    for a in sorted(g.of_type("API"), key=lambda n: n["name"]):
        callers = sorted({g.nodes[e["src"]]["name"] for e in g.in_edges(a["id"]) if e["rel"] == "CALLS"
                          and g.nodes[e["src"]]["type"] == "Screen"})
        rd = sorted({g.nodes[e["dst"]]["name"] for e in g.out_edges(a["id"]) if e["rel"] == "READS" and e["props"].get("derived")})
        wr = sorted({g.nodes[e["dst"]]["name"] for e in g.out_edges(a["id"]) if e["rel"] == "WRITES" and e["props"].get("derived")})
        nr = sum(1 for e in g.out_edges(a["id"]) if e["rel"] == "EXECUTES")
        flag = " ⚠ no backend match" if a["props"].get("unresolved") else ""
        rows.append(f"| `{_cell(a['name'])}`{flag} | {_cell(a['props'].get('handler'))} | {_cell(', '.join(callers))} | "
                    f"{_cell(', '.join(rd))} | {_cell(', '.join(wr))} | {nr} |")
    files["apis.md"] = "\n".join(rows) + FOOTER

    # rules
    allr = q.find_business_rules()
    out = [f"# Business rules\n", f"{len(allr['data']['rules'])} rule(s) detected. Confidence reflects how explicit the evidence is "
           "(declared marker 1.0 > guard/validation 0.9 > existence 0.8 > decision 0.5).\n"]
    by: dict[str, list] = {}
    for r in allr["data"]["rules"]:
        by.setdefault(r["kind"], []).append(r)
    for kind, items in sorted(by.items()):
        out += [f"## {kind} ({len(items)})\n", "| Rule | Business wording | Location | Conf. | Impacts | Enforced by |", "|---|---|---|---|---|---|"]
        out += [f"| {_cell(r['description'])} | {_cell(r['business_description'])}{' (Claude)' if r['source'] == 'llm' else ''} | "
                f"`{_cell(r['location'])}` | {r['confidence']} | {_cell(', '.join(r['impacts']))} | "
                f"{_cell(', '.join(r['enforced_by'][:4]))} |" for r in items]
        out.append("")
    files["business-rules.md"] = "\n".join(out) + FOOTER

    # data model
    out = ["# Data model\n"]
    er = ["erDiagram"]
    tables = sorted(g.of_type("Table"), key=lambda n: n["name"])
    for t in tables:
        cols = t["props"].get("columns") or []
        er.append(f"  {_slug(t['name']).replace('-', '_')} {{")
        for c in cols[:25]:
            er.append(f"    {_mermaid_type(c.get('type'))} {_slug(c.get('name') or 'col').replace('-', '_')}" + (" PK" if c.get("pk") else ""))
        er.append("  }")
    for e in g.edges.values():
        if e["rel"] == "DEPENDS_ON" and g.nodes[e["src"]]["type"] == "Table":
            a, b = (_slug(g.nodes[e["src"]]["name"]).replace("-", "_"), _slug(g.nodes[e["dst"]]["name"]).replace("-", "_"))
            er.append(f'  {b} ||--o{{ {a} : "FK"')
    out += ["```mermaid", "\n".join(er), "```", ""]
    for t in tables:
        lin = q.find_data_lineage(t["id"])
        out += [f"## {t['name']}\n", f"- Entity: {t['props'].get('entity') or 'n/a'} · dialect: {t['props'].get('dialect') or 'n/a'}"
                + (f" · `{t['props'].get('file')}:{t['props'].get('line')}`" if t["props"].get("file") else " · (not declared in a DDL script)"), "",
                "| Column | Type | PK | Nullable |", "|---|---|---|---|"]
        out += [f"| {_cell(c.get('name'))} | {_cell(c.get('type'))} | {'✓' if c.get('pk') else ''} | {'' if c.get('nullable') is False else 'yes'} |"
                for c in (t["props"].get("columns") or [])]
        out += ["", lin["summary"], ""]
    files["data-model.md"] = "\n".join(out) + FOOTER

    # topics
    topics = g.of_type("Topic")
    if topics:
        out = ["# Messaging topics\n", "| Topic | Producers | Consumers |", "|---|---|---|"]
        for tp in topics:
            prod = sorted(g.nodes[e["src"]]["name"] for e in g.in_edges(tp["id"]) if e["rel"] == "WRITES")
            cons = sorted(g.nodes[e["dst"]]["name"] for e in g.out_edges(tp["id"]) if e["rel"] == "CALLS")
            out.append(f"| `{_cell(tp['name'])}` | {_cell(', '.join(prod))} | {_cell(', '.join(cons))} |")
        files["topics.md"] = "\n".join(out) + FOOTER
    return files


def write_documentation(g: Graph, outdir: str | Path) -> list[Path]:
    outdir = Path(outdir)
    written = []
    for rel, content in generate_documentation(g).items():
        p = outdir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        written.append(p)
    return written


def documentation_zip(g: Graph) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for rel, content in generate_documentation(g).items():
            z.writestr(rel, content)
    return buf.getvalue()
