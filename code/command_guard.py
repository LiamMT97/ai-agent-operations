"""
Global Agent Guardrails — plugin de Hermes.

Bloquea comandos de terminal catastróficos antes de ejecutarlos.
Lee la denylist compartida ~/.agents/hooks/dangerous-patterns.txt
(regex POSIX ERE, una por línea, # = comentario).

El hook es fail-open en excepciones (un plugin roto nunca debe
bloquear TODO el terminal del agente). Devuelve {"action": "block"}
solo cuando hay coincidencia real con un patrón del denylist.
"""

import os
import re
import pathlib

PATTERNS_FILE = pathlib.Path.home() / ".agents" / "hooks" / "dangerous-patterns.txt"

# Cache de patrones compilados (se recompilan si cambia el mtime)
_cache = {"mtime": None, "patterns": []}


def _load_patterns():
    """Carga y compila los patrones del denylist, con caché por mtime."""
    try:
        mtime = PATTERNS_FILE.stat().st_mtime
    except OSError:
        return []
    if _cache["mtime"] == mtime and _cache["patterns"]:
        return _cache["patterns"]
    patterns = []
    try:
        for line in PATTERNS_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                # POSIX ERE usa [[:space:]]; Python usa \s
                py_re = line.replace("[[:space:]]", r"\s")
                patterns.append(re.compile(py_re))
            except re.error:
                continue  # patrón roto → ignorar, no romper el guard
    except OSError:
        return []
    _cache["mtime"] = mtime
    _cache["patterns"] = patterns
    return patterns


def _extract_command(args):
    """Extrae el comando del payload, según la forma que llegue."""
    if not isinstance(args, dict):
        return ""
    cmd = args.get("command", "") or ""
    if isinstance(cmd, str):
        return cmd
    # Algunos adaptadores pasan la lista de args
    if isinstance(cmd, list):
        return " ".join(str(a) for a in cmd)
    return str(cmd)


def on_pre_tool_call(*, tool_name: str = "", args=None, **_: object):
    """Hook pre_tool_call: bloquea comandos peligrosos del terminal."""
    if tool_name != "terminal":
        return None
    cmd = _extract_command(args)
    if not cmd:
        return None

    # Normalizar: tabs → espacios, colapsar espacios
    norm = " ".join(cmd.split())

    for pattern in _load_patterns():
        if pattern.search(norm):
            return {
                "action": "block",
                "message": (
                    "🚫 BLOQUEADO por guardrails: el comando coincide con un "
                    "patrón peligroso de la denylist. Si es realmente necesario, "
                    "pide al usuario que lo revise y apruebe explícitamente."
                ),
            }
    return None


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
