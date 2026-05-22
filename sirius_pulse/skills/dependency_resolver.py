"""Resolve and install missing dependencies for SKILL files.

Strategy:
1. Parse SKILL_META.dependencies (explicit list, preferred).
2. Fallback: scan top-level import statements in the SKILL source.
3. Filter out stdlib and known installed packages.
4. Install missing packages via ``uv pip install`` (subprocess).
"""

from __future__ import annotations

import ast
import importlib.util
import logging
import shutil
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# Packages that are part of sirius_pulse or always available at runtime.
_ALWAYS_AVAILABLE = frozenset({
    "sirius_pulse",
    "setuptools",
    "pip",
    "uv",
})

_IMPORT_TO_PACKAGE_NAME = {
    "PIL": "Pillow",
    "bs4": "beautifulsoup4",
    "cv2": "opencv-python",
    "yaml": "PyYAML",
    "skimage": "scikit-image",
    "sklearn": "scikit-learn",
}

_PACKAGE_IMPORT_PROBES = {
    "Pillow": ("PIL",),
    "beautifulsoup4": ("bs4",),
    "opencv-python": ("cv2",),
    "PyYAML": ("yaml",),
    "scikit-image": ("skimage",),
    "scikit-learn": ("sklearn",),
}


def resolve_skill_dependencies(
    skill_file: Path,
    *,
    auto_install: bool = True,
) -> list[str]:
    """Check a SKILL file for missing dependencies and optionally install them.

    Returns the list of packages that were successfully installed (may be empty).
    """
    declared = _extract_declared_dependencies(skill_file)
    imported = _extract_imported_packages(skill_file)
    candidates = declared | imported

    missing = _find_missing(candidates)
    if not missing:
        return []

    if not auto_install:
        logger.warning(
            "SKILL '%s' 存在未安装的依赖: %s（auto_install 已关闭）",
            skill_file.name,
            ", ".join(sorted(missing)),
        )
        return []

    return _install_packages(sorted(missing), skill_file.name)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _extract_declared_dependencies(skill_file: Path) -> set[str]:
    """Read ``SKILL_META["dependencies"]`` without executing the module."""
    try:
        source = skill_file.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(skill_file))
    except (SyntaxError, OSError) as exc:
        logger.debug("AST解析SKILL文件失败 (%s): %s", skill_file.name, exc)
        return set()

    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "SKILL_META":
                return _parse_dependencies_from_ast(node.value)
    return set()


def _parse_dependencies_from_ast(node: ast.expr) -> set[str]:
    """Extract the ``dependencies`` key from an AST dict literal."""
    if not isinstance(node, ast.Dict):
        return set()

    for key, value in zip(node.keys, node.values):
        if isinstance(key, ast.Constant) and key.value == "dependencies":
            if isinstance(value, ast.List):
                deps: set[str] = set()
                for elt in value.elts:
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                        deps.add(elt.value.strip())
                return deps
    return set()


def _extract_imported_packages(skill_file: Path) -> set[str]:
    """Scan all ``import X`` / ``from X import ...`` statements in the module."""
    try:
        source = skill_file.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(skill_file))
    except (SyntaxError, OSError):
        return set()

    packages: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                packages.add(_normalize_candidate(alias.name.split(".")[0]))
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                packages.add(_normalize_candidate(node.module.split(".")[0]))
    return packages


def _is_stdlib(name: str) -> bool:
    """Check whether *name* belongs to the Python standard library."""
    if name in _ALWAYS_AVAILABLE:
        return False  # handled separately

    if sys.version_info >= (3, 10):
        # Python 3.10+ has sys.stdlib_module_names
        return name in sys.stdlib_module_names  # type: ignore[attr-defined]

    # Fallback: a conservative static list for 3.12
    _STDLIB_FALLBACK = {
        "__future__", "abc", "aifc", "argparse", "array", "ast", "asyncio",
        "atexit", "base64", "bdb", "binascii", "binhex", "bisect",
        "builtins", "bz2", "calendar", "cgi", "cgitb", "chunk", "cmath",
        "cmd", "code", "codecs", "codeop", "collections", "colorsys",
        "compileall", "concurrent", "configparser", "contextlib",
        "contextvars", "copy", "copyreg", "cProfile", "crypt", "csv",
        "ctypes", "curses", "dataclasses", "datetime", "dbm", "decimal",
        "difflib", "dis", "distutils", "doctest", "email", "encodings",
        "enum", "errno", "faulthandler", "fcntl", "filecmp", "fileinput",
        "fnmatch", "fractions", "ftplib", "functools", "gc", "getopt",
        "getpass", "gettext", "glob", "graphlib", "grp", "gzip",
        "hashlib", "heapq", "hmac", "html", "http", "idlelib", "imaplib",
        "imghdr", "imp", "importlib", "inspect", "io", "ipaddress",
        "itertools", "json", "keyword", "lib2to3", "linecache", "locale",
        "logging", "lzma", "mailbox", "mailcap", "marshal", "math",
        "mimetypes", "mmap", "modulefinder", "multiprocessing", "netrc",
        "nis", "nntplib", "numbers", "operator", "optparse", "os",
        "ossaudiodev", "pathlib", "pdb", "pickle", "pickletools", "pipes",
        "pkgutil", "platform", "plistlib", "poplib", "posix", "posixpath",
        "pprint", "profile", "pstats", "pty", "pwd", "py_compile",
        "pyclbr", "pydoc", "queue", "quopri", "random", "re",
        "readline", "reprlib", "resource", "rlcompleter", "runpy",
        "sched", "secrets", "select", "selectors", "shelve", "shlex",
        "shutil", "signal", "site", "smtpd", "smtplib", "sndhdr",
        "socket", "socketserver", "spwd", "sqlite3", "sre_compile",
        "sre_constants", "sre_parse", "ssl", "stat", "statistics",
        "string", "stringprep", "struct", "subprocess", "sunau",
        "symtable", "sys", "sysconfig", "syslog", "tabnanny", "tarfile",
        "telnetlib", "tempfile", "termios", "test", "textwrap",
        "threading", "time", "timeit", "tkinter", "token", "tokenize",
        "tomllib", "trace", "traceback", "tracemalloc", "tty", "turtle",
        "turtledemo", "types", "typing", "unicodedata", "unittest",
        "urllib", "uu", "uuid", "venv", "warnings", "wave", "weakref",
        "webbrowser", "winreg", "winsound", "wsgiref", "xdrlib", "xml",
        "xmlrpc", "zipapp", "zipfile", "zipimport", "zlib",
        "_thread", "typing_extensions",
    }
    return name in _STDLIB_FALLBACK


def _find_missing(candidates: set[str]) -> set[str]:
    """Filter *candidates* to only those that are not importable."""
    missing: set[str] = set()
    for pkg in candidates:
        if _is_stdlib(pkg) or pkg in _ALWAYS_AVAILABLE:
            continue
        if _package_is_importable(pkg):
            continue
        missing.add(pkg)
    return missing


def _normalize_candidate(name: str) -> str:
    value = str(name or "").strip()
    if not value:
        return value
    return _IMPORT_TO_PACKAGE_NAME.get(value, value)


def _package_is_importable(package_name: str) -> bool:
    for probe_name in _package_import_names(package_name):
        if importlib.util.find_spec(probe_name) is not None:
            return True
    return False


def _package_import_names(package_name: str) -> tuple[str, ...]:
    return _PACKAGE_IMPORT_PROBES.get(package_name, (package_name,))


def _install_packages(packages: list[str], skill_name: str) -> list[str]:
    """Install *packages* using ``uv pip install``, falling back to ``pip``.

    Returns the list of packages that were successfully installed.
    """
    if not packages:
        return []

    installer, cmd_base = _pick_installer()
    cmd = [*cmd_base, *packages]

    logger.info(
        "正在为 '%s' 准备环境，安装 %s（使用 %s）",
        skill_name,
        ", ".join(packages),
        installer,
    )

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0:
            logger.info("'%s' 的依赖 %s 已经装好了", skill_name, ", ".join(packages))
            # Invalidate import caches so the newly installed packages are visible
            importlib.invalidate_caches()
            return packages
        else:
            logger.error(
                "SKILL '%s': 依赖安装失败 (exit %d): %s",
                skill_name,
                result.returncode,
                result.stderr.strip()[:500],
            )
            return []
    except subprocess.TimeoutExpired:
        logger.error("SKILL '%s': 依赖安装超时", skill_name)
        return []
    except FileNotFoundError:
        logger.error("SKILL '%s': 找不到包管理器 (%s)", skill_name, installer)
        return []


def _pick_installer() -> tuple[str, list[str]]:
    """Return (label, command_prefix) — prefer ``uv``, fallback ``pip``."""
    uv_path = shutil.which("uv")
    if uv_path:
        # --python 确保 uv 把包装到当前解释器对应的环境，
        # 避免 uv 自动探测到父目录的 .venv 而装错地方。
        return "uv", [uv_path, "pip", "install", "--quiet", "--python", sys.executable]
    # Fallback to pip via current interpreter
    return "pip", [sys.executable, "-m", "pip", "install", "--quiet"]
