#!/usr/bin/env python3
"""Decision layer (System One) with a self-degrading fallback chain.

Sanitised copy for publication: paths are configurable, no credentials inside.

Qué hace
--------
Responde preguntas tipadas (noul = sí/no, choice = elegir, score = puntuar) sobre un estado,
usando el primer nivel disponible de una cadena que degrada sola:

  1. codiv-openjev   → Codiv, openjev-latest (DiffusionGemma 26B). Máxima calidad, gratis.
  2. codiv-jevk5     → Codiv, jevk5-0.2 (Qwen3.5-4B). Otra caja GPU: si una cae, la otra vive.
  3. codiv-laya      → Codiv, laya-1.0 (encoder 421M). Ventana 1.024 tk; rapidísimo.
  4. local-ollama    → tu Ollama (qwen3.5:0.8b). Sin red, sin cuota, calidad baja y lento.
  5. deepseek        → API de pago. Solo con --permitir-pago. Último recurso con red.
  6. (ninguno)       → devuelve ok=false con el motivo: el llamador decide qué hacer.

Garantías
---------
- Interruptor por nivel: 2 fallos seguidos bloquean ese nivel 15 min; un error de cuota (401/402/403/429)
  lo bloquea 6 h. Los niveles sanos siguen funcionando.
- Cuota contada en local (JSONL): avisos al 65 %, 80 % y 95 % de los 100M tokens de Codiv.
- Nunca lanza excepción por falta de red: devuelve un resultado degradado y explicado.

Uso
---
  jev.py decide --json preguntas.json          # {"state": ..., "questions": {...}}
  jev.py decide --demo                         # petición de ejemplo
  jev.py decide --demo --solo codiv-laya       # forzar un nivel (pruebas)
  jev.py decide --demo --permitir-pago         # autorizar el último recurso
  jev.py estado                                # niveles, bloqueos y cuota
  jev.py reset                                 # limpia bloqueos (no borra el uso)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

CONF_DIR = os.path.expanduser("~/.config/agent-ops")
KEY_PATH = os.path.join(CONF_DIR, "api_key")
ESTADO_PATH = os.path.join(CONF_DIR, "estado.json")
USO_PATH = os.path.join(CONF_DIR, "uso.jsonl")

CUOTA_TOTAL = 100_000_000          # tokens de decisión del plan gratis de Codiv
UMBRALES = (0.65, 0.80, 0.95)
BLOQUEO_FALLOS = 15 * 60           # 2 fallos seguidos → 15 min fuera
BLOQUEO_CUOTA = 6 * 3600           # error de cuota/auth → 6 h fuera
OLLAMA_URL = "http://127.0.0.1:11434"

NIVELES: list[dict] = [
    {"id": "codiv-openjev", "tipo": "codiv", "modelo": "openjev-latest"},
    {"id": "codiv-jevk5", "tipo": "codiv", "modelo": "jevk5-0.2"},
    {"id": "codiv-laya", "tipo": "codiv", "modelo": "laya-1.0"},
    {"id": "local-ollama", "tipo": "ollama", "modelo": "qwen3.5:0.8b"},
    {"id": "deepseek", "tipo": "deepseek", "modelo": "deepseek-v4-flash", "pago": True},
]


# ─────────────────────────── estado persistente ───────────────────────────

def _leer_json(path: str, por_defecto: dict) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(por_defecto)


def cargar_estado() -> dict:
    est = _leer_json(ESTADO_PATH, {"niveles": {}, "cuota": {"tokens": 0, "avisados": []}})
    est.setdefault("niveles", {})
    est.setdefault("cuota", {"tokens": 0, "avisados": []})
    return est


def guardar_estado(est: dict) -> None:
    est["actualizado"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    tmp = ESTADO_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(est, fh, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, ESTADO_PATH)


def bloqueado(est: dict, nivel_id: str) -> str | None:
    info = est["niveles"].get(nivel_id) or {}
    hasta = info.get("bloqueado_hasta") or 0
    if hasta > time.time():
        return info.get("ultimo_error", "bloqueado")
    return None


def anotar(est: dict, nivel_id: str, ok: bool, error: str | None = None,
           cuenta_err: bool = False, bloqueo: int = 0) -> None:
    info = est["niveles"].setdefault(nivel_id, {})
    if ok:
        info["fallos"] = 0
        info.pop("bloqueado_hasta", None)
        info.pop("ultimo_error", None)
    else:
        info["fallos"] = (info.get("fallos") or 0) + 1
        info["ultimo_error"] = (error or "")[:200]
        info["ultimo_fallo"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if cuenta_err:
            info["bloqueado_hasta"] = time.time() + BLOQUEO_CUOTA
        elif info["fallos"] >= 2:
            info["bloqueado_hasta"] = time.time() + bloqueo
    info["ultima_llamada"] = datetime.now(timezone.utc).isoformat(timespec="seconds")


def registrar_uso(nivel: str, modelo: str, tokens: int, latencia: float, ok: bool) -> None:
    linea = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "nivel": nivel,
             "modelo": modelo, "tokens": tokens, "latencia": round(latencia, 3), "ok": ok}
    with open(USO_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(linea, ensure_ascii=False) + "\n")
    try:
        os.chmod(USO_PATH, 0o600)
    except OSError:
        pass


def sumar_cuota(est: dict, tokens: int) -> list[str]:
    """Suma tokens y devuelve la lista de avisos nuevos que hay que dar."""
    cuota = est["cuota"]
    cuota["tokens"] = (cuota.get("tokens") or 0) + max(tokens, 0)
    avisados = set(cuota.get("avisados") or [])
    nuevos: list[str] = []
    for umbral in UMBRALES:
        clave = f"{int(umbral*100)}"
        if cuota["tokens"] >= CUOTA_TOTAL * umbral and clave not in avisados:
            avisados.add(clave)
            nuevos.append(
                f"cuota Codiv al {clave}%: {cuota['tokens']:,} de {CUOTA_TOTAL:,} tokens usados"
            )
    cuota["avisados"] = sorted(avisados)
    return nuevos


# ─────────────────────────── clientes por nivel ───────────────────────────

class FalloNivel(Exception):
    def __init__(self, mensaje: str, cuota: bool = False):
        super().__init__(mensaje)
        self.cuota = cuota


def _clave_codiv() -> str:
    val = os.environ.get("TYPESAFE_API_KEY") or os.environ.get("CODIV_API_KEY")
    if val:
        return val.strip()
    try:
        with open(KEY_PATH, encoding="utf-8") as fh:
            return fh.read().strip()
    except FileNotFoundError:
        raise FalloNivel(f"falta la clave en {KEY_PATH}") from None


def _post(url: str, body: dict, headers: dict, timeout: int) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detalle = exc.read().decode("utf-8", "replace")[:200]
        # 401/402/403 = clave o saldo; 429 = límite de ritmo. Todos son "cuota".
        raise FalloNivel(f"HTTP {exc.code}: {detalle}", cuota=exc.code in (401, 402, 403, 429)) from None
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise FalloNivel(f"red: {type(exc).__name__} {exc}") from None


def _preguntas_a_texto(questions: dict) -> str:
    lineas = []
    for qid, q in questions.items():
        tipo = q.get("type")
        instru = q.get("instructions") or ""
        crit = q.get("criteria")
        if tipo == "choice":
            opciones = ", ".join(crit.keys()) if isinstance(crit, dict) else str(crit)
            lineas.append(f'- id="{qid}" tipo=choice | {instru} | opciones: {opciones}')
        elif tipo == "score":
            niveles = crit if isinstance(crit, list) else []
            etiquetas = ", ".join(f"{i}={n}" for i, n in enumerate(niveles))
            lineas.append(f'- id="{qid}" tipo=score | {instru} | niveles: {etiquetas}')
        else:
            crit_txt = f" (sí: {crit.get('true')} / no: {crit.get('false')})" if isinstance(crit, dict) else ""
            lineas.append(f'- id="{qid}" tipo=noul | {instru}{crit_txt} | responde si o no')
    return "\n".join(lineas)


def _normalizar_llm(texto: str, questions: dict) -> dict:
    """Convierte la respuesta de un LLM de texto al formato de respuestas de System One."""
    limpio = texto.strip()
    if limpio.startswith("```"):
        limpio = limpio.split("```")[1] if "```" in limpio[3:] else limpio.strip("`")
        limpio = limpio.removeprefix("json").strip()
    inicio, fin = limpio.find("{"), limpio.rfind("}")
    if inicio < 0 or fin < 0:
        raise FalloNivel(f"el respaldo de texto no devolvió JSON: {texto[:120]!r}")
    crudo = json.loads(limpio[inicio:fin + 1])
    answers = {}
    for qid, q in questions.items():
        valor = crudo.get(qid)
        if valor is None:
            raise FalloNivel(f"el respaldo no contestó la pregunta {qid}")
        if isinstance(valor, dict):
            valor = valor.get("valor") or valor.get("value") or next(iter(valor.values()))
        tipo = q.get("type")
        if tipo == "choice":
            answers[qid] = {"type": "choice", "choice": str(valor), "probabilities": {},
                            "confidence": None}
        elif tipo == "score":
            answers[qid] = {"type": "score", "score": float(valor), "probabilities": {},
                            "confidence": None}
        else:
            txt = str(valor).strip().lower()
            answers[qid] = {"type": "noul",
                            "noul": 1.0 if txt in ("si", "sí", "true", "1", "yes") else 0.0}
    return answers


def _nivel_codiv(nivel: dict, state, questions: dict) -> dict:
    resp = _post(
        "https://api.codiv.ai/v1/systemone",
        {"model": nivel["modelo"], "state": state, "questions": questions},
        {"Authorization": f"Bearer {_clave_codiv()}", "Content-Type": "application/json",
         "User-Agent": "hermes-decision-chain/1.0"},
        timeout=90,
    )
    uso = resp.get("usage") or {}
    return {"answers": resp.get("answers") or {}, "modelo_real": resp.get("model"),
            "tokens": uso.get("input_tokens") or 0}


def _nivel_ollama(nivel: dict, state, questions: dict) -> dict:
    """Respaldo local. Una pregunta por llamada: un 0.8B no acierta con varias en un JSON."""

    def _preguntar(texto_prompt: str) -> tuple[str, int]:
        # `think: false` es imprescindible: sin él un qwen3.5 se pone a razonar y pasa de 4 minutos.
        resp = _post(
            f"{OLLAMA_URL}/api/chat",
            {"model": nivel["modelo"], "messages": [{"role": "user", "content": texto_prompt}],
             "stream": False, "keep_alive": "10m", "think": False,
             "options": {"temperature": 0, "num_ctx": 4096, "num_predict": 40}},
            {"Content-Type": "application/json"},
            timeout=90,
        )
        return ((resp.get("message") or {}).get("content") or "").strip(), \
               resp.get("prompt_eval_count") or 0

    def _empareja(respuesta: str, opciones: list[str]) -> str:
        bajo = respuesta.lower().strip(" .,;:!\"'")
        for op in opciones:
            if op.lower() in bajo:
                return op
        import difflib
        cerca = difflib.get_close_matches(bajo, [o.lower() for o in opciones], n=1, cutoff=0.6)
        if cerca:
            return next(o for o in opciones if o.lower() == cerca[0])
        raise FalloNivel(f"el respaldo local no supo elegir entre {opciones}: {respuesta[:60]!r}")

    answers, tokens = {}, 0
    for qid, q in questions.items():
        tipo = q.get("type")
        cabecera = f"ESTADO:\n{state}\n\nPREGUNTA: {q.get('instructions') or ''}\n"
        if tipo == "choice":
            opciones = list((q.get("criteria") or {}).keys())
            cuerpo = ("OPCIONES: " + ", ".join(opciones) +
                      "\nContesta SOLO con una de las opciones, escrita igual.")
        elif tipo == "score":
            opciones = [str(i) for i in range(len(q.get("criteria") or []))]
            cuerpo = ("NIVELES: " + ", ".join(f"{i}={n}" for i, n in enumerate(q.get("criteria") or [])) +
                      "\nContesta SOLO con el número del nivel.")
        else:
            opciones = ["si", "no"]
            cuerpo = "Contesta SOLO con 'si' o 'no'."
        texto, tk = _preguntar(cabecera + cuerpo)
        tokens += tk
        if tipo == "choice":
            answers[qid] = {"type": "choice", "choice": _empareja(texto, opciones),
                            "probabilities": {}, "confidence": None}
        elif tipo == "score":
            answers[qid] = {"type": "score", "score": float(_empareja(texto, opciones)),
                            "probabilities": {}, "confidence": None}
        else:
            answers[qid] = {"type": "noul",
                            "noul": 1.0 if _empareja(texto, opciones) == "si" else 0.0}
    return {"answers": answers, "modelo_real": nivel["modelo"], "tokens": tokens}


def _nivel_deepseek(nivel: dict, state, questions: dict) -> dict:
    clave = _clave_deepseek()
    if not clave:
        raise FalloNivel("sin clave de DeepSeek")
    prompt = (
        "Clasifica el ESTADO y contesta SOLO un objeto JSON, con una clave por pregunta y el valor "
        f"elegido (para noul: si o no).\n\nESTADO:\n{state}\n\nPREGUNTAS:\n"
        f"{_preguntas_a_texto(questions)}\n\n"
        'Formato exacto: {"<id>": "<valor>"}'
    )
    resp = _post(
        "https://api.deepseek.com/chat/completions",
        {"model": nivel["modelo"], "messages": [{"role": "user", "content": prompt}],
         "response_format": {"type": "json_object"}, "temperature": 0},
        {"Authorization": f"Bearer {clave}", "Content-Type": "application/json"},
        timeout=120,
    )
    texto = resp["choices"][0]["message"]["content"]
    uso = resp.get("usage") or {}
    return {"answers": _normalizar_llm(texto, questions), "modelo_real": nivel["modelo"],
            "tokens": uso.get("prompt_tokens") or 0, "tokens_salida": uso.get("completion_tokens") or 0}


def _clave_deepseek() -> str | None:
    env_path = os.path.expanduser("~/.config/agent-ops/env")
    if os.path.exists(env_path):
        for linea in open(env_path, encoding="utf-8"):
            if linea.strip().startswith("DEEPSEEK_API_KEY"):
                return linea.split("=", 1)[1].strip().strip('"').strip("'")
    return os.environ.get("DEEPSEEK_API_KEY")


LLAMADORES = {"codiv": _nivel_codiv, "ollama": _nivel_ollama, "deepseek": _nivel_deepseek}


# ─────────────────────────── cadena de decisión ───────────────────────────

def decidir(state, questions: dict, permitir_pago: bool = False,
            solo: str | None = None) -> dict:
    est = cargar_estado()
    avisos: list[str] = []
    intentos: list[dict] = []
    niveles = [n for n in NIVELES if (solo is None or n["id"] == solo)
               and (permitir_pago or not n.get("pago"))]

    for nivel in niveles:
        motivo = bloqueado(est, nivel["id"])
        if motivo:
            intentos.append({"nivel": nivel["id"], "resultado": f"omitido: {motivo[:80]}"})
            continue
        t0 = time.time()
        try:
            salida = LLAMADORES[nivel["tipo"]](nivel, state, questions)
        except FalloNivel as exc:
            dt = time.time() - t0
            anotar(est, nivel["id"], ok=False, error=str(exc), cuenta_err=exc.cuota,
                   bloqueo=BLOQUEO_FALLOS)
            registrar_uso(nivel["id"], nivel["modelo"], 0, dt, ok=False)
            intentos.append({"nivel": nivel["id"], "resultado": f"fallo: {str(exc)[:80]}"})
            continue
        dt = time.time() - t0
        anotar(est, nivel["id"], ok=True)
        registrar_uso(nivel["id"], nivel["modelo"], salida.get("tokens") or 0, dt, ok=True)
        if nivel["tipo"] == "codiv":
            avisos += sumar_cuota(est, salida.get("tokens") or 0)
        guardar_estado(est)
        return {"ok": True, "nivel": nivel["id"], "modelo": salida.get("modelo_real") or nivel["modelo"],
                "answers": salida["answers"], "tokens": salida.get("tokens") or 0,
                "latencia_s": round(dt, 3), "degradado": nivel["id"] != niveles[0]["id"],
                "intentos": intentos, "avisos": avisos}

    guardar_estado(est)
    return {"ok": False, "nivel": None, "answers": {}, "tokens": 0, "latencia_s": 0.0,
            "degradado": True, "intentos": intentos, "avisos": avisos,
            "motivo": "ningún nivel disponible: usa el resultado determinista del llamador"}


def demo() -> dict:
    return {
        "state": ("Hola, el pedido 48213 llegó con la brida deformada y tenemos la inspección de "
                  "cliente el martes. Necesito saber si me lo cambiáis antes o anulo el pedido."),
        "questions": {
            "departamento": {"type": "choice",
                             "instructions": "¿Qué equipo debe atender este mensaje",
                             "criteria": {"facturacion": "Cobros o facturas",
                                          "tecnico": "Averías o defectos de fabricación",
                                          "comercial": "Precios o condiciones"}},
            "enfado": {"type": "score", "instructions": "Cuánto enfado muestra el cliente",
                       "criteria": ["Tranquilo", "Molesto pero correcto", "Muy enfadado"]},
            "urgente": {"type": "noul", "instructions": "El mensaje transmite urgencia"},
        },
    }


def _estado_legible(est: dict) -> str:
    lineas = [f"tokens de cuota usados: {est['cuota']['tokens']:,} / {CUOTA_TOTAL:,} "
              f"({100*est['cuota']['tokens']/CUOTA_TOTAL:.3f}%)",
              f"avisos dados: {', '.join(est['cuota'].get('avisados') or []) or 'ninguno'}", "", "niveles:"]
    ahora = time.time()
    for nivel in NIVELES:
        info = est["niveles"].get(nivel["id"]) or {}
        hasta = info.get("bloqueado_hasta") or 0
        if hasta > ahora:
            situacion = f"BLOQUEADO {int((hasta-ahora)/60)} min ({info.get('ultimo_error','')[:60]})"
        elif info.get("ultima_llamada"):
            situacion = f"ok la última vez ({info['ultima_llamada']}), fallos seguidos {info.get('fallos',0)}"
        else:
            situacion = "sin uso"
        lineas.append(f"  {nivel['id']:16s} {nivel['modelo']:20s} {situacion}")
    return "\n".join(lineas)


def main() -> int:
    ap = argparse.ArgumentParser(description="Capa de decisión con cadena de respaldo")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("decide", help="responde preguntas tipadas")
    d.add_argument("--json", metavar="FICHERO")
    d.add_argument("--demo", action="store_true")
    d.add_argument("--permitir-pago", action="store_true")
    d.add_argument("--solo", help="forzar un nivel (id)")
    d.add_argument("--raw", action="store_true")
    sub.add_parser("estado", help="niveles, bloqueos y cuota")
    sub.add_parser("reset", help="limpia bloqueos")
    args = ap.parse_args()

    if args.cmd == "estado":
        print(_estado_legible(cargar_estado()))
        return 0
    if args.cmd == "reset":
        est = cargar_estado()
        est["niveles"] = {}
        guardar_estado(est)
        print("bloqueos limpiados")
        return 0

    if args.json:
        carga = json.load(sys.stdin) if args.json == "-" else json.load(open(args.json, encoding="utf-8"))
    elif args.demo:
        carga = demo()
    else:
        ap.error("indica --json FICHERO/-, o --demo")

    res = decidir(carga["state"], carga["questions"], permitir_pago=args.permitir_pago, solo=args.solo)
    print(json.dumps(res, ensure_ascii=False, indent=2) if args.raw else _resumen(res))
    return 0 if res["ok"] else 1


def _resumen(res: dict) -> str:
    if not res["ok"]:
        texto = ["SIN RESPUESTA: " + res.get("motivo", "fallo")]
        texto += [f"  {i['nivel']}: {i['resultado']}" for i in res["intentos"]]
        return "\n".join(texto)
    cabecera = (f"nivel {res['nivel']} ({res['modelo']}) · {res['tokens']} tk · "
                f"{res['latencia_s']}s" + ("  [DEGRADADO]" if res["degradado"] else ""))
    lineas = [cabecera]
    for qid, a in res["answers"].items():
        if a.get("type") == "noul":
            lineas.append(f"  {qid}: sí/no -> {a.get('noul')}")
        elif a.get("type") == "choice":
            probs = ", ".join(f"{k}={v:.2f}" for k, v in (a.get("probabilities") or {}).items())
            lineas.append(f"  {qid}: {a.get('choice')} (conf {a.get('confidence')})"
                          + (f" [{probs}]" if probs else ""))
        else:
            lineas.append(f"  {qid}: {a.get('score')} (conf {a.get('confidence')})")
    lineas += [f"  intento descartado: {i['nivel']} -> {i['resultado']}" for i in res["intentos"]]
    lineas += [f"  AVISO: {a}" for a in res["avisos"]]
    return "\n".join(lineas)


if __name__ == "__main__":
    sys.exit(main())
