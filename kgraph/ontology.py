"""Ontology, capabilities and pipeline definition (single source of truth)."""

NODE_TYPES = [
    "Screen", "Component", "Route", "API", "Controller", "Service",
    "BusinessRule", "Repository", "Entity", "Table", "Topic", "Capability",
]

REL_TYPES = [
    "CALLS", "USES", "IMPLEMENTS", "READS", "WRITES",
    "IMPACTS", "DEPENDS_ON", "EXPOSES", "EXECUTES",
]

CAPABILITIES = [
    "scan_repository", "build_knowledge_graph", "detect_business_rules",
    "impact_analysis", "generate_documentation", "graph_visualization",
    "natural_language_query",
]

SUPPORTED_LANGUAGES = {
    "backend": ["java", "kotlin"],
    "frontend": ["angular", "react"],
    "database": ["sql", "postgresql", "oracle", "mysql"],
}

PIPELINE = [
    "scan_frontend", "extract_routes", "detect_api_calls", "scan_backend",
    "discover_controllers", "discover_services", "discover_repositories",
    "discover_entities", "discover_business_rules", "populate_graph",
]

QUERIES = [
    "explain_screen", "show_call_chain", "find_business_rules",
    "determine_impact", "show_capability", "find_data_lineage", "explore_ontology",
]

# Flow direction = runtime direction (user -> screen -> API -> service -> table).
# CALLS/USES/READS/WRITES/EXECUTES/DEPENDS_ON follow the declared direction,
# EXPOSES follows it for Route->Screen and Capability->*, but is reversed for
# Controller->API (a request enters through the API, then reaches the controller).
# IMPACTS and IMPLEMENTS are associative (membership / semantic links).
FLOW_RELS = {"CALLS", "USES", "READS", "WRITES", "EXECUTES", "DEPENDS_ON", "EXPOSES"}
ASSOC_RELS = {"IMPACTS", "IMPLEMENTS"}

# Left-to-right layer used by the graph visualisation.
LAYERS = {
    "Capability": 0, "Route": 1, "Screen": 2, "Component": 3, "API": 4,
    "Controller": 5, "Service": 6, "Topic": 7, "BusinessRule": 8,
    "Repository": 9, "Entity": 10, "Table": 11,
}

READ_PREFIXES = ("find", "get", "read", "count", "exists", "search", "query",
                 "stream", "select", "load", "list", "fetch", "is", "has")
WRITE_PREFIXES = ("save", "delete", "remove", "update", "insert", "persist",
                  "flush", "add", "create", "set", "merge", "store", "put",
                  "cancel", "mark", "clear", "reset", "increment")
