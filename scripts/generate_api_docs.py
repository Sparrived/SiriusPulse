"""Python API 文档自动生成脚本."""

import ast
import importlib
import inspect
import sys
import json
from pathlib import Path
from typing import Any


def extract_docstring(node: ast.AST) -> str | None:
    """从 AST 节点提取 docstring."""
    return ast.get_docstring(node) or None


def extract_function_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> dict:
    """提取函数签名信息."""
    args = node.args
    params = []

    # 基础参数
    for arg in args.args:
        annotation = None
        if arg.annotation:
            try:
                annotation = ast.unparse(arg.annotation)
            except (AttributeError, ValueError):
                annotation = None
        params.append({
            "name": arg.arg,
            "annotation": annotation,
            "kind": "positional"
        })

    # Keyword-only 参数
    for arg in args.kwonlyargs:
        annotation = None
        if arg.annotation:
            try:
                annotation = ast.unparse(arg.annotation)
            except (AttributeError, ValueError):
                annotation = None
        params.append({
            "name": arg.arg,
            "annotation": annotation,
            "kind": "keyword-only"
        })

    # 返回类型
    return_type = None
    if node.returns:
        try:
            return_type = ast.unparse(node.returns)
        except (AttributeError, ValueError):
            return_type = None

    return {
        "name": node.name,
        "is_async": isinstance(node, ast.AsyncFunctionDef),
        "params": params,
        "return_type": return_type,
        "docstring": extract_docstring(node),
    }


def extract_runtime_function_signature(name: str, obj: Any) -> dict:
    """提取运行时函数签名信息，支持转发/重导出函数."""
    try:
        signature = inspect.signature(obj)
    except (TypeError, ValueError):
        signature = None

    params = []
    if signature is not None:
        for param in signature.parameters.values():
            annotation = None
            if param.annotation is not inspect._empty:
                annotation = str(param.annotation).replace("typing.", "")
            kind = "keyword-only" if param.kind == inspect.Parameter.KEYWORD_ONLY else "positional"
            params.append({
                "name": param.name,
                "annotation": annotation,
                "kind": kind,
            })

    return_type = None
    if signature is not None and signature.return_annotation is not inspect._empty:
        return_type = str(signature.return_annotation).replace("typing.", "")

    return {
        "name": name,
        "is_async": inspect.iscoroutinefunction(obj),
        "params": params,
        "return_type": return_type,
        "docstring": inspect.getdoc(obj),
    }


def extract_runtime_class_info(name: str, obj: type[Any]) -> dict:
    """提取运行时类信息，支持转发/重导出类."""
    methods = []
    for method_name, method in inspect.getmembers(obj, predicate=inspect.isfunction):
        if method_name.startswith("_"):
            continue
        methods.append(extract_runtime_function_signature(method_name, method))
    return {
        "name": name,
        "docstring": inspect.getdoc(obj),
        "methods": methods,
    }


def resolve_source_module(name: str, obj: Any) -> str | None:
    """解析符号的来源模块（去除 sirius_pulse. 前缀）."""
    mod = getattr(obj, "__module__", None)
    if mod and mod.startswith("sirius_pulse."):
        return mod[len("sirius_pulse."):]
    return mod


def collect_public_api() -> dict[str, dict]:
    """从 sirius_pulse.__init__.py 收集公开 API，按来源模块分组."""
    try:
        pkg = importlib.import_module("sirius_pulse")
    except Exception as exc:
        print(f"[FAIL] 无法导入 sirius_pulse: {exc}", file=sys.stderr)
        sys.exit(1)

    exported_names = getattr(pkg, "__all__", [])
    if not isinstance(exported_names, list):
        print("[WARN] sirius_pulse.__all__ 不是列表，使用 dir() 替代", file=sys.stderr)
        exported_names = [n for n in dir(pkg) if not n.startswith("_")]

    modules: dict[str, dict] = {}

    for name in exported_names:
        obj = getattr(pkg, name, None)
        if obj is None:
            continue

        src_mod = resolve_source_module(name, obj)
        if not src_mod:
            src_mod = "public"

        if src_mod not in modules:
            modules[src_mod] = {"functions": [], "classes": []}

        if inspect.isfunction(obj) or inspect.iscoroutinefunction(obj):
            modules[src_mod]["functions"].append(extract_runtime_function_signature(name, obj))
        elif inspect.isclass(obj):
            modules[src_mod]["classes"].append(extract_runtime_class_info(name, obj))

    return modules


def generate_markdown_doc(modules: dict[str, dict]) -> str:
    """生成 markdown 格式的 API 文档."""
    md = "# Python API\n\n"
    md += "自动生成的 Python API 参考文档（基于 `sirius_pulse` 顶层公开导出）。\n\n"

    if not modules:
        md += "（未找到公开 API 符号）\n"
        return md

    md += "## 模块索引\n\n"
    for mod_name in sorted(modules):
        md += f"- [{mod_name}](#{mod_name.replace('.', '-')})\n"

    md += "\n---\n\n"

    for mod_name in sorted(modules):
        data = modules[mod_name]
        if not data["functions"] and not data["classes"]:
            continue

        md += f"## {mod_name}\n\n"

        # 类文档
        if data["classes"]:
            md += "### Classes\n\n"
            for cls in data["classes"]:
                md += f"#### `{cls['name']}`\n\n"
                if cls.get("docstring"):
                    md += f"{cls['docstring']}\n\n"

                # 方法
                if cls.get("methods"):
                    md += "**方法：**\n\n"
                    for method in cls["methods"]:
                        params = ", ".join(
                            f"{p['name']}: {p['annotation']}" if p['annotation'] else p['name']
                            for p in method['params']
                        )
                        sig = f"{'async ' if method['is_async'] else ''}{method['name']}({params})"
                        if method['return_type']:
                            sig += f" -> {method['return_type']}"

                        md += f"- `{sig}`"
                        if method['docstring']:
                            first_line = method['docstring'].split('\n')[0]
                            md += f" - {first_line}"
                        md += "\n"
                    md += "\n"

        # 函数文档
        if data["functions"]:
            md += "### Functions\n\n"
            for func in data["functions"]:
                params = ", ".join(
                    f"{p['name']}: {p['annotation']}" if p['annotation'] else p['name']
                    for p in func['params']
                )
                sig = f"{'async ' if func['is_async'] else ''}{func['name']}({params})"
                if func['return_type']:
                    sig += f" -> {func['return_type']}"

                md += f"#### `{sig}`\n\n"
                if func['docstring']:
                    md += f"{func['docstring']}\n\n"

        md += "\n---\n\n"

    return md


def generate_json_doc(modules: dict[str, dict]) -> dict:
    """生成 JSON 格式的 API 文档."""
    return {
        "title": "Sirius Chat API Reference",
        "version": "1.0.0",
        "modules": modules
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/generate_api_docs.py <format> [<output_path>]")
        print("Formats: markdown, json")
        sys.exit(1)

    output_format = sys.argv[1]

    modules = collect_public_api()

    if output_format == "markdown":
        output_path = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("docs/reference/python-api.md")
        doc = generate_markdown_doc(modules)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(doc, encoding='utf-8')
        print(f"[OK] Markdown API 文档已生成: {output_path}")

    elif output_format == "json":
        output_path = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("docs/reference/python-api.json")
        doc = generate_json_doc(modules)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(doc, f, indent=2, ensure_ascii=False)
        print(f"[OK] JSON API 文档已生成: {output_path}")

    else:
        print(f"[FAIL] 不支持的格式: {output_format}")
        sys.exit(1)
