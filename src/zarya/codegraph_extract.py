"""Static structure only: never execute code or retain source text/literal values."""

import ast
import re
from dataclasses import asdict, dataclass, field
from pathlib import PurePosixPath
from typing import Any


@dataclass(frozen=True)
class Node:
    id: str
    kind: str
    name: str
    path: str
    line: int
    end_line: int
    file_hash: str


@dataclass(frozen=True)
class Edge:
    source: str
    target: str
    kind: str
    path: str
    line: int
    file_hash: str
    confidence: str = "syntax"


@dataclass
class Structure:
    nodes: dict[str, Node] = field(default_factory=dict)
    edges: set[Edge] = field(default_factory=set)
    diagnostics: list[dict[str, Any]] = field(default_factory=list)
    unresolved: list[dict[str, Any]] = field(default_factory=list)
    unresolved_total: int = 0

    def edge(
        self, source: str, target: str, kind: str, path: str, line: int, confidence: str = "syntax"
    ) -> None:
        self.edges.add(
            Edge(source, target, kind, path, line, self.nodes[file_id(path)].file_hash, confidence)
        )

    def missing(self, path: str, line: int, reason: str) -> None:
        self.unresolved_total += 1
        if len(self.unresolved) < 200:
            # No expression, exception message, source line or literal in diagnostics.
            self.unresolved.append({"path": path, "line": line, "reason": reason})

    def document(self) -> dict[str, Any]:
        return {
            "nodes": [asdict(n) for n in sorted(self.nodes.values(), key=lambda n: n.id)],
            "edges": [
                asdict(e)
                for e in sorted(
                    self.edges,
                    key=lambda e: (e.source, e.target, e.kind, e.path, e.line, e.confidence),
                )
            ],
            "diagnostics": self.diagnostics,
            "unresolved": self.unresolved,
            "unresolved_total": self.unresolved_total,
        }


def file_id(path: str) -> str:
    return "file:" + path


def dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted(node.value)
        return base + "." + node.attr if base else None
    return None


def module_name(path: str) -> str:
    parts = list(PurePosixPath(path).with_suffix("").parts)
    if parts[0] == "src":
        parts.pop(0)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


@dataclass
class Scope:
    qual: str
    owner: str
    kind: str
    parent: str | None
    class_qual: str | None
    blocked: set[str] = field(default_factory=set)
    imports: dict[str, list[tuple[str, bool]]] = field(default_factory=dict)


class PythonFile(ast.NodeVisitor):
    def __init__(self, path: str, raw: bytes, graph: Structure):
        self.path, self.graph, self.module = path, graph, module_name(path)
        self.scope = ""
        self.conditional = False
        self.scopes = {"": Scope("", file_id(path), "module", None, None)}
        self.defs: dict[str, list[str]] = {}
        self.conditional_defs: set[str] = set()
        self.calls: list[tuple[str, ast.Call]] = []
        self.bases: list[tuple[str, str, ast.expr]] = []
        self.imports: list[tuple[str, str, int, bool]] = []
        tree = ast.parse(raw, filename=path)
        self.visit(tree)

    def definition(self, node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        parent = self.scopes[self.scope]
        qual = self.scope + "." + node.name if self.scope else node.name
        if len(qual) > 512:
            self.graph.diagnostics.append({"path": self.path, "reason": "identifier_size_limit"})
            return
        if self.conditional:
            self.conditional_defs.add(qual)
        if isinstance(node, ast.ClassDef):
            kind = "class"
        elif node.name.startswith("test_") and self.path.startswith("tests/"):
            kind = "test"
        else:
            kind = "method" if parent.kind == "class" else "function"
        id_ = f"symbol:{self.path}:{qual}:{node.lineno}"
        self.graph.nodes[id_] = Node(
            id_,
            kind,
            qual,
            self.path,
            node.lineno,
            node.end_lineno or node.lineno,
            self.graph.nodes[file_id(self.path)].file_hash,
        )
        self.graph.edge(parent.owner, id_, "contains", self.path, node.lineno)
        self.defs.setdefault(qual, []).append(id_)
        # Repeated lexical definitions are kept but cannot resolve uniquely.
        if qual in self.scopes:
            self.scopes[qual].blocked.add(node.name)
        scope = Scope(qual, id_, kind, self.scope, qual if kind == "class" else parent.class_qual)
        if not isinstance(node, ast.ClassDef):
            args = node.args
            scope.blocked.update(a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs))
            if args.vararg:
                scope.blocked.add(args.vararg.arg)
            if args.kwarg:
                scope.blocked.add(args.kwarg.arg)
        else:
            self.bases.extend((id_, self.scope, base) for base in node.bases)
        for decorator in node.decorator_list:
            self.visit(decorator)
        previous = self.scope
        self.scopes[qual] = scope
        self.scope = qual
        for statement in node.body:
            self.visit(statement)
        self.scope = previous

    visit_ClassDef = definition
    visit_FunctionDef = definition
    visit_AsyncFunctionDef = definition

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.scopes[self.scope].blocked.add(node.id)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self.scopes[self.scope].blocked.add(node.name)
        self.generic_visit(node)

    def visit_Global(self, node: ast.Global | ast.Nonlocal) -> None:
        self.scopes[self.scope].blocked.update(node.names)

    visit_Nonlocal = visit_Global

    def visit_MatchAs(self, node: ast.MatchAs | ast.MatchStar) -> None:
        if node.name:
            self.scopes[self.scope].blocked.add(node.name)
        self.generic_visit(node)

    visit_MatchStar = visit_MatchAs

    def visit_MatchMapping(self, node: ast.MatchMapping) -> None:
        if node.rest:
            self.scopes[self.scope].blocked.add(node.rest)
        self.generic_visit(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        # Lambda/comprehension scopes are not modelled. Never invent lexical call bindings.
        for call in ast.walk(node):
            if isinstance(call, ast.Call):
                self.graph.missing(self.path, call.lineno, "unsupported_scope")

    def visit_ListComp(
        self, node: ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp
    ) -> None:
        for call in ast.walk(node):
            if isinstance(call, ast.Call):
                self.graph.missing(self.path, call.lineno, "unsupported_scope")
            elif isinstance(call, ast.NamedExpr) and isinstance(call.target, ast.Name):
                # A comprehension's walrus binds in the containing lexical scope.
                self.scopes[self.scope].blocked.add(call.target.id)

    visit_SetComp = visit_ListComp
    visit_DictComp = visit_ListComp
    visit_GeneratorExp = visit_ListComp

    def visit_Call(self, node: ast.Call) -> None:
        self.calls.append((self.scope, node))
        self.generic_visit(node)

    def conditional_body(self, node: ast.AST) -> None:
        previous = self.conditional
        self.conditional = True
        self.generic_visit(node)
        self.conditional = previous

    visit_If = conditional_body
    visit_Try = conditional_body
    visit_TryStar = conditional_body
    visit_For = conditional_body
    visit_AsyncFor = conditional_body
    visit_While = conditional_body
    visit_With = conditional_body
    visit_AsyncWith = conditional_body
    visit_Match = conditional_body

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            local = alias.asname or alias.name.split(".")[0]
            bound = alias.name if alias.asname else local
            self.scopes[self.scope].imports.setdefault(local, []).append((bound, self.conditional))
            self.imports.append((self.scope, alias.name, node.lineno, self.conditional))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        base = self.module if self.path.endswith("/__init__.py") else self.module.rpartition(".")[0]
        if node.level:
            parts = base.split(".")
            prefix = ".".join(parts[: len(parts) - node.level + 1])
            base = ".".join(v for v in (prefix, node.module) if v)
        else:
            base = node.module or ""
        for alias in node.names:
            if alias.name == "*":
                self.scopes[self.scope].blocked.add("*")
                self.graph.missing(self.path, node.lineno, "star_import")
                continue
            target = base + "." + alias.name if base else alias.name
            self.scopes[self.scope].imports.setdefault(alias.asname or alias.name, []).append(
                (target, self.conditional)
            )
            self.imports.append((self.scope, target, node.lineno, self.conditional))

    def reference(
        self, expression: ast.AST, scope: str, files: dict[str, "PythonFile"]
    ) -> tuple[str | None, str]:
        if any(len(v) > 1 for v in self.defs.values()):
            return None, "ambiguous_definition"
        name = dotted(expression)
        if not name:
            return None, "dynamic_expression"
        root, _, tail = name.partition(".")
        current: str | None = scope
        if root in {"self", "cls"} and tail and "." not in tail:
            cls = self.scopes[scope].class_qual
            targets = self.defs.get(f"{cls}.{tail}", [])
            return (
                (targets[0], "declared_method")
                if len(targets) == 1
                else (None, "dynamic_attribute")
            )
        while current is not None:
            local = self.scopes[current]
            if root in local.blocked or "*" in local.blocked:
                return None, "shadowed_binding"
            candidates = local.imports.get(root, [])
            if candidates:
                binding = current + "." + root if current else root
                if binding in self.defs:
                    return None, "import_definition_conflict"
                if len(candidates) != 1 or candidates[0][1]:
                    return None, "conditional_or_ambiguous_import"
                target = candidates[0][0] + ("." + tail if tail else "")
                return lookup(target, files), "import_binding"
            qual = current + "." + name if current else name
            found = self.defs.get(qual, [])
            if found:
                if qual in self.conditional_defs:
                    return None, "conditional_definition"
                return (
                    (found[0], "lexical_binding")
                    if len(found) == 1
                    else (None, "ambiguous_definition")
                )
            current = local.parent
            # A method does not close over class scope for bare-name lookup.
            if current is not None and self.scopes[current].kind == "class":
                current = self.scopes[current].parent
        return None, "external_or_dynamic"


def lookup(target: str, files: dict[str, PythonFile]) -> str | None:
    for mod in sorted(files, key=len, reverse=True):
        if target == mod:
            return file_id(files[mod].path)
        if target.startswith(mod + "."):
            qual = target[len(mod) + 1 :]
            file = files[mod]
            parts = qual.split(".")
            for count in range(1, len(parts) + 1):
                prefix = ".".join(parts[:count])
                owner = ".".join(parts[: count - 1])
                name = parts[count - 1]
                if (
                    len(file.defs.get(prefix, [])) != 1
                    or prefix in file.conditional_defs
                    or owner not in file.scopes
                    or name in file.scopes[owner].blocked
                    or "*" in file.scopes[owner].blocked
                    or name in file.scopes[owner].imports
                ):
                    return None
            names = file.defs.get(qual, [])
            return names[0] if len(names) == 1 else None
    return None


def module_target(target: str, files: dict[str, PythonFile]) -> str | None:
    for mod in sorted(files, key=len, reverse=True):
        if target == mod or target.startswith(mod + "."):
            return file_id(files[mod].path)
    return None


JS_TOKENS = re.compile(
    r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|'
    r"'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`|[A-Za-z_$][\w$]*|[^\s]"
)


def frontend(path: str, text: str, graph: Structure) -> None:
    tokens = [
        (m.group(), text.count("\n", 0, m.start()) + 1)
        for m in JS_TOKENS.finditer(text)
        if not m.group().startswith(("//", "/*"))
    ]
    for i, (word, line) in enumerate(tokens):
        if word not in {"import", "export"}:
            continue
        following = tokens[i + 1 : i + 128]
        if word == "export" and len(following) >= 2:
            kind, name = following[0][0], following[1][0]
            if kind in {"function", "class", "type", "interface", "const"} and re.fullmatch(
                r"[A-Za-z_$][\w$]*", name
            ):
                id_ = f"symbol:{path}:{name}:{line}"
                graph.nodes[id_] = Node(
                    id_,
                    "frontend_symbol",
                    name,
                    path,
                    line,
                    line,
                    graph.nodes[file_id(path)].file_hash,
                )
                graph.edge(file_id(path), id_, "contains", path, line, "hint")
        for j, (item, _) in enumerate(following):
            if item == ";":
                break
            if (j == 0 or following[j - 1][0] == "from") and item.startswith(("'", '"')):
                target = item[1:-1]
                if not target.startswith(".") or "\\" in target:
                    break
                parts = list(PurePosixPath(path).parent.parts)
                for part in target.split("/"):
                    if part == "..":
                        if parts:
                            parts.pop()
                    elif part != ".":
                        parts.append(part)
                base = "/".join(parts)
                targets = [
                    base,
                    *(base + ext for ext in (".ts", ".tsx", ".css", ".js")),
                    base + "/index.ts",
                    base + "/index.tsx",
                ]
                found = next((p for p in targets if file_id(p) in graph.nodes), None)
                if found:
                    graph.edge(file_id(path), file_id(found), "import_hint", path, line, "hint")
                break


def extract(sources: dict[str, bytes], hashes: dict[str, str]) -> Structure:
    graph = Structure()
    files: dict[str, PythonFile] = {}
    for path, raw in sorted(sources.items()):
        kind = (
            "document" if path.endswith(".md") else "migration" if path.endswith(".sql") else "file"
        )
        graph.nodes[file_id(path)] = Node(
            file_id(path), kind, path, path, 1, raw.count(b"\n") + 1, hashes[path]
        )
    for path, raw in sorted(sources.items()):
        if not path.endswith(".py"):
            continue
        try:
            file = PythonFile(path, raw, graph)
            files[file.module] = file
            if path.startswith("tests/") and "/" not in path[len("tests/") :]:
                files.setdefault(PurePosixPath(path).stem, file)
        except (SyntaxError, UnicodeError, ValueError):
            graph.diagnostics.append({"path": path, "reason": "python_parse_error"})
    for file in {f.path: f for f in files.values()}.values():
        for _scope, target, line, conditional in file.imports:
            found = module_target(target, files)
            if found:
                graph.edge(
                    file_id(file.path),
                    found,
                    "imports",
                    file.path,
                    line,
                    "conditional" if conditional else "syntax",
                )
        for scope, call in file.calls:
            found, confidence = file.reference(call.func, scope, files)
            if found and graph.nodes[found].kind != "file":
                kind = "calls_candidate" if confidence == "declared_method" else "calls"
                graph.edge(
                    file.scopes[scope].owner, found, kind, file.path, call.lineno, confidence
                )
            else:
                graph.missing(file.path, call.lineno, confidence)
        for source, scope, base in file.bases:
            found, confidence = file.reference(base, scope, files)
            if found:
                graph.edge(source, found, "inherits", file.path, base.lineno, confidence)
    for path, raw in sorted(sources.items()):
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeError:
            if not path.endswith(".py"):
                graph.diagnostics.append({"path": path, "reason": "text_decode_error"})
            continue
        if path.endswith((".ts", ".tsx", ".js", ".cjs")):
            frontend(path, text, graph)
        elif path.endswith(".sql"):
            # Mask comments and literal strings before recognising schema identifiers.
            masked = re.sub(
                r"--[^\n]*|/\*[\s\S]*?\*/|'(?:''|[^'])*'",
                lambda m: "".join("\n" if c == "\n" else " " for c in m.group()),
                text,
            )
            pattern = (
                r"\b(CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?|ALTER\s+TABLE|REFERENCES)"
                r"\s+([A-Za-z_][A-Za-z0-9_]*)"
            )
            for match in re.finditer(pattern, masked, re.I):
                name, line = match[2], masked.count("\n", 0, match.start()) + 1
                id_ = "table:" + name
                graph.nodes.setdefault(
                    id_, Node(id_, "sql_table", name, path, line, line, hashes[path])
                )
                kind = (
                    "declares_table"
                    if match[1].upper().startswith("CREATE")
                    else (
                        "alters_table"
                        if match[1].upper().startswith("ALTER")
                        else "references_table"
                    )
                )
                graph.edge(file_id(path), id_, kind, path, line)
        elif path.endswith(".md"):
            # Only links to indexed paths, no prose/headings/content are retained.
            for target in sources:
                if target == path:
                    continue
                rel = (
                    target[len("docs/") :]
                    if path.startswith("docs/") and target.startswith("docs/")
                    else target
                )
                for match in re.finditer(r"(?<![\w/])" + re.escape(rel) + r"(?![\w/])", text):
                    graph.edge(
                        file_id(path),
                        file_id(target),
                        "document_links_to",
                        path,
                        text.count("\n", 0, match.start()) + 1,
                        "text_reference",
                    )
    return graph
