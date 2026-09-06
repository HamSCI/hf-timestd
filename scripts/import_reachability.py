"""Import-reachability graph for hf_timestd (docs/design/RESIDUE_AUDIT_2026-09-04.md §1).

Run from the repo root:

    python3 scripts/import_reachability.py \
      hf_timestd.cli,hf_timestd.core.core_recorder_v2,hf_timestd.core.metrology_service,\
hf_timestd.core.multi_broadcast_fusion,hf_timestd.core.l2_calibration_service,hf_timestd.quota_manager \
      scripts/live_vtec.py,scripts/monitor_radiod_health.py

Writes importgraph.json beside the invocation and prints the unreachable set.
 Every ast Import/ImportFrom node counts,
including those inside functions, plus the _LAZY maps in package __init__ files."""
import ast, os, sys, json, collections
ROOT = "src"
PKG = "hf_timestd"

def mod_name(path):
    rel = os.path.relpath(path, ROOT)[:-3].replace(os.sep, ".")
    return rel[:-9] if rel.endswith(".__init__") else rel

modules = {}
for dp, dn, fn in os.walk(os.path.join(ROOT, PKG)):
    for f in fn:
        if f.endswith(".py"):
            p = os.path.join(dp, f); modules[mod_name(p)] = p
is_pkg = {m for m, p in modules.items() if p.endswith("__init__.py")}

lazy = {}
for m in is_pkg:
    tree = ast.parse(open(modules[m]).read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "_LAZY" for t in node.targets):
            try:
                d = ast.literal_eval(node.value)
            except Exception as e:
                print("lazy parse fail", m, e, file=sys.stderr); continue
            lazy[m] = {}
            for name, (mod, attr) in d.items():
                lazy[m][name] = (m + mod) if mod.startswith(".") else mod

def resolve_rel(cur, level, module):
    base = cur if cur in is_pkg else cur.rsplit(".", 1)[0]
    parts = base.split(".")
    if level > 1: parts = parts[: len(parts) - (level - 1)]
    return ".".join(parts + ([module] if module else []))

def edges_for(path, cur):
    """Return set of hf_timestd module names imported by file at path (cur = its module name or None)."""
    out = set()
    try:
        tree = ast.parse(open(path).read())
    except SyntaxError as e:
        print("syntax", path, e, file=sys.stderr); return out
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.startswith(PKG): out.add(a.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level and cur:
                base = resolve_rel(cur, node.level, node.module)
            elif node.module and node.module.startswith(PKG):
                base = node.module
            else:
                continue
            out.add(base)
            for a in node.names:
                cand = base + "." + a.name
                if cand in modules: out.add(cand)
                elif base in lazy and a.name in lazy[base]: out.add(lazy[base][a.name])
    # keep only names that resolve to real modules (or their parent packages)
    res = set()
    for e in out:
        while e and e not in modules: e = e.rsplit(".", 1)[0] if "." in e else ""
        if e: res.add(e)
    return res

graph = {m: edges_for(p, m) for m, p in modules.items()}
# a submodule import implies its parent packages import (parent __init__ executes)
for m in list(graph):
    parts = m.split(".")
    for i in range(1, len(parts)):
        graph[m].add(".".join(parts[:i]))

roots = sys.argv[1].split(",")
extra_files = sys.argv[2].split(",") if len(sys.argv) > 2 and sys.argv[2] else []
reach = set(); stack = list(roots)
for f in extra_files:
    stack += list(edges_for(f, None))
while stack:
    m = stack.pop()
    if m in reach or m not in graph: continue
    reach.add(m); stack += list(graph[m])

importers = collections.defaultdict(set)
for m, es in graph.items():
    for e in es:
        if e != m: importers[e].add(m)

# tests + scripts as separate importer classes
def scan_dir(d):
    imp = collections.defaultdict(set)
    for dp, dn, fn in os.walk(d):
        for f in fn:
            if f.endswith(".py"):
                p = os.path.join(dp, f)
                for e in edges_for(p, None): imp[e].add(p)
    return imp
timp = scan_dir("tests"); simp = scan_dir("scripts")

rows = []
for m, p in sorted(modules.items()):
    n = sum(1 for _ in open(p, errors="replace"))
    rows.append(dict(module=m, lines=n, reachable=m in reach,
                     src_importers=sorted(importers.get(m, [])),
                     test_files=len(timp.get(m, [])), script_files=len(simp.get(m, []))))
json.dump(rows, open("importgraph.json", "w"), indent=1)
print(f"modules={len(modules)} reachable_from_services={sum(r['reachable'] for r in rows)} "
      f"unreachable={sum(not r['reachable'] for r in rows)}")
print(f"lines reachable={sum(r['lines'] for r in rows if r['reachable'])} unreachable={sum(r['lines'] for r in rows if not r['reachable'])}")
print("\n== UNREACHABLE from service+cli roots (module, lines, src_importers, tests, scripts, web) ==")
for r in rows:
    if not r["reachable"]:
        print(f"{r['module']:60s} {r['lines']:5d}  src<-{len(r['src_importers'])} t{r['test_files']} s{r['script_files']} w{r['web_files']}  {','.join(x.split('.')[-1] for x in r['src_importers'])[:80]}")
