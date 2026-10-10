"""
Bot diario de Threads para Cotizados (https://cotizados.vercel.app)

Flujo:
  1. Elige un tema del día (rota por día del año, sin repetir el de ayer).
  2. Lee las tasas reales del día: de COTIZADOS_API_URL si existe, o abriendo
     /cotizaciones en un navegador sin interfaz (la página carga los datos con JavaScript).
  3. Genera el texto con IA (Gemini o Claude), evitando repetir los últimos posts.
  4. Publica en Threads (crear contenedor -> esperar -> publicar).
  5. Guarda el historial y, una vez por semana, renueva el token de Threads.

Variables de entorno:
  THREADS_ACCESS_TOKEN  (obligatoria) token de larga duración de Threads
  GEMINI_API_KEY        (obligatoria*) clave de Google AI Studio; se usa si existe
  ANTHROPIC_API_KEY     (alternativa*) clave de la API de Claude (si no hay GEMINI_API_KEY)
  COTIZADOS_API_URL     (opcional)    endpoint JSON con las tasas (si no, se lee la página)
  GEMINI_MODEL          (opcional)    por defecto gemini-3.5-flash-lite
  CLAUDE_MODEL          (opcional)    por defecto claude-haiku-5-5
  DRY_RUN=true          (opcional)    genera el post pero no lo publica
  FORCE_REFRESH=true    (opcional)    fuerza la renovación del token

  * Hace falta al menos una de las dos claves de IA.
"""

import datetime as dt
import json
import os
import pathlib
import re
import sys
import time

import requests

ROOT = pathlib.Path(__file__).resolve().parent.parent
HISTORY_FILE = ROOT / "data" / "history.json"
SITE = "https://cotizados.vercel.app"
GRAPH = "https://graph.threads.net/v1.0"
THREADS_MAX_CHARS = 500
TZ_VE = dt.timezone(dt.timedelta(hours=-4))

DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
FORCE_REFRESH = os.getenv("FORCE_REFRESH", "false").lower() == "true"
GEMINI_MODEL = os.getenv("GEMINI_MODEL") or "gemini-3.5-flash-lite"
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL") or "claude-haiku-5-5"
PROVIDER = "gemini" if os.getenv("GEMINI_API_KEY") else "claude"

# Cada tema define el ángulo del post y el enlace al que debe llevar.
TEMAS = [
    {
        "id": "tasa_bcv",
        "angulo": "Invita a consultar la tasa oficial del BCV de hoy en un solo lugar, rápido y sin anuncios molestos.",
        "link": f"{SITE}/cotizaciones",
    },
    {
        "id": "calculadora",
        "angulo": "Presenta la calculadora: convertir entre bolívares, dólares y USDT en segundos (ej. para pagar, cobrar o cotizar un producto).",
        "link": f"{SITE}/calculator",
    },
    {
        "id": "bcv_vs_usdt",
        "angulo": "Explica por qué conviene comparar la tasa BCV con la cotización del USDT antes de cambiar o fijar precios.",
        "link": f"{SITE}/cotizaciones",
    },
    {
        "id": "noticias",
        "angulo": "Invita a ponerse al día con las noticias económicas y del dólar en Venezuela dentro de la web.",
        "link": f"{SITE}/noticias",
    },
    {
        "id": "comunidad",
        "angulo": "Invita a la comunidad a dejar sus comentarios y sugerencias para mejorar la app. Tono cercano, pide opinión.",
        "link": f"{SITE}/comentarios",
    },
    {
        "id": "app_movil",
        "angulo": "Cuenta que Cotizados se puede agregar a la pantalla de inicio del teléfono y usarse como una app, sin descargar nada de la tienda.",
        "link": SITE,
    },
    {
        "id": "tip_finanzas",
        "angulo": "Da un tip práctico y breve de finanzas personales para quien cobra o paga en bolívares y dólares, y cierra recomendando Cotizados.",
        "link": f"{SITE}/calculator",
    },
    {
        "id": "comerciantes",
        "angulo": "Habla a emprendedores y comerciantes: actualizar precios en bolívares según la tasa del día con la calculadora.",
        "link": f"{SITE}/calculator",
    },
]

SYSTEM_PROMPT = f"""Eres el community manager de Cotizados ({SITE}), una web/app gratuita venezolana
para consultar la tasa del BCV, la cotización del USDT, convertir montos con una calculadora y leer
noticias económicas.

Escribes UN post para Threads en español de Venezuela, natural y cercano (sin sonar a anuncio
corporativo).

Reglas estrictas:
- Máximo 450 caracteres en total, enlace incluido.
- Empieza con un gancho en la primera línea (pregunta, dato o situación cotidiana).
- Incluye el enlace exactamente como se te da, una sola vez.
- Máximo 1-2 emojis y máximo 2 hashtags (por ejemplo #DólarBCV #Venezuela), o ninguno.
- "Datos reales del día" es el texto de la página de cotizaciones de Cotizados. Si trae tasas,
  aprovéchalas: cita 1 o 2 cifras (por ejemplo BCV y USDT) copiándolas exactamente como aparecen,
  con su etiqueta correcta. Si no estás seguro de qué representa un número, no lo uses.
- NUNCA inventes ni calcules cifras de tasas, precios o porcentajes. Si no hay datos, no
  menciones cifras.
- No prometas ganancias ni des consejos de inversión.
- Devuelve solo el texto del post, sin comillas ni explicaciones."""


def log(msg: str) -> None:
    print(f"[{dt.datetime.now(TZ_VE):%Y-%m-%d %H:%M}] {msg}", flush=True)


def load_history() -> list:
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log("Historial dañado; se empieza uno nuevo.")
    return []


def save_history(history: list) -> None:
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_FILE.write_text(
        json.dumps(history[-60:], ensure_ascii=False, indent=2), encoding="utf-8"
    )


def pick_topic(history: list) -> dict:
    today = dt.datetime.now(TZ_VE).date()
    idx = today.timetuple().tm_yday % len(TEMAS)
    topic = TEMAS[idx]
    # Evita repetir el tema del último post (por ejemplo, tras una ejecución manual).
    if history and history[-1].get("tema") == topic["id"]:
        topic = TEMAS[(idx + 1) % len(TEMAS)]
    return topic


def fetch_from_api(url: str) -> str | None:
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    return json.dumps(r.json(), ensure_ascii=False)[:2000]


def fetch_from_page() -> str | None:
    """Abre /cotizaciones en un Chromium sin interfaz, espera a que el JavaScript
    pinte las tasas y devuelve el texto visible de la página."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(locale="es-VE")
        page.goto(f"{SITE}/cotizaciones", wait_until="networkidle", timeout=45000)
        # Espera a que aparezca algún número tipo tasa (ej. 36,52 o 36.52).
        try:
            page.wait_for_function(
                r"() => /\d+[.,]\d{2}/.test(document.body.innerText)", timeout=20000
            )
        except Exception:  # noqa: BLE001
            log("Aviso: la página cargó pero no aparecieron cifras.")
        text = page.inner_text("main") if page.query_selector("main") else page.inner_text("body")
        browser.close()

    # Limpia líneas vacías y repetidas (menús, botones).
    lines, seen = [], set()
    for line in (l.strip() for l in text.splitlines()):
        if line and line not in seen:
            seen.add(line)
            lines.append(line)
    clean = "\n".join(lines)
    return clean[:2500] if re.search(r"\d+[.,]\d{2}", clean) else None


def fetch_real_data() -> str | None:
    """Tasas reales del día: primero el endpoint JSON (si existe), si no, la página."""
    url = os.getenv("COTIZADOS_API_URL", "").strip()
    try:
        data = fetch_from_api(url) if url else fetch_from_page()
        if data:
            log(f"Datos reales obtenidos:\n{data}\n")
            return data
    except Exception as e:  # noqa: BLE001
        log(f"Aviso: no se pudieron leer las tasas ({e}).")
    log("Se genera el post sin cifras.")
    return None


def ask_gemini(system: str, user_msg: str) -> str:
    """Llama a la API de Gemini (Google AI Studio) por REST."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user_msg}]}],
        "generationConfig": {"temperature": 0.9, "maxOutputTokens": 2048},
    }
    for intento in range(3):
        r = requests.post(
            url, json=body, timeout=60,
            headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]},
        )
        # 429 = límite de la capa gratuita; 5xx = error temporal de Google.
        if r.status_code in (429, 500, 503) and intento < 2:
            log(f"Gemini respondió {r.status_code}, reintentando...")
            time.sleep(20 * (intento + 1))
            continue
        if not r.ok:
            raise RuntimeError(f"Gemini API {r.status_code}: {r.text}")
        data = r.json()
        parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        if text.strip():
            return text
        raise RuntimeError(f"Gemini no devolvió texto: {json.dumps(data)[:500]}")
    raise RuntimeError("Gemini no respondió.")


def ask_claude(system: str, user_msg: str) -> str:
    from anthropic import Anthropic

    resp = Anthropic().messages.create(
        model=CLAUDE_MODEL,
        max_tokens=500,
        system=system,
        messages=[{"role": "user", "content": user_msg}],
    )
    return "".join(b.text for b in resp.content if b.type == "text")


def generate_post(topic: dict, history: list, real_data: str | None) -> str:
    ask = ask_gemini if PROVIDER == "gemini" else ask_claude
    recientes = "\n---\n".join(h["texto"] for h in history[-10:]) or "(ninguno todavía)"
    hoy = dt.datetime.now(TZ_VE).strftime("%A %d/%m/%Y")

    user_msg = (
        f"Fecha: {hoy}\n"
        f"Tema de hoy: {topic['angulo']}\n"
        f"Enlace a usar: {topic['link']}\n"
        f"Datos reales del día: {real_data or 'no disponibles'}\n\n"
        f"Posts recientes (no repitas sus ganchos, frases ni estructura):\n{recientes}"
    )

    for intento in range(3):
        text = ask(SYSTEM_PROMPT, user_msg).strip().strip('"')
        if topic["link"] not in text:
            text = f"{text}\n\n{topic['link']}"
        if len(text) <= THREADS_MAX_CHARS:
            return text
        log(f"Post demasiado largo ({len(text)} caracteres), reintentando ({intento + 1}/3)...")
        user_msg += "\n\nIMPORTANTE: el anterior fue demasiado largo. Hazlo más corto."

    raise RuntimeError("No se logró un post dentro del límite de 500 caracteres.")


def threads_request(method: str, path: str, token: str, **params) -> dict:
    params["access_token"] = token
    for intento in range(3):
        r = requests.request(method, f"{GRAPH}{path}", params=params, timeout=30)
        if r.ok:
            return r.json()
        if r.status_code >= 500 and intento < 2:
            time.sleep(10 * (intento + 1))
            continue
        raise RuntimeError(f"Threads API {r.status_code}: {r.text}")
    raise RuntimeError("Threads API no respondió.")


def publish_to_threads(text: str, token: str) -> str:
    container = threads_request("POST", "/me/threads", token, media_type="TEXT", text=text)
    creation_id = container["id"]
    log(f"Contenedor creado: {creation_id}")

    # Meta recomienda esperar a que el contenedor esté listo antes de publicar.
    for _ in range(10):
        time.sleep(6)
        status = threads_request("GET", f"/{creation_id}", token, fields="status,error_message")
        if status.get("status") == "FINISHED":
            break
        if status.get("status") in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Contenedor con error: {status}")

    result = threads_request("POST", "/me/threads_publish", token, creation_id=creation_id)
    return result["id"]


def maybe_refresh_token(token: str) -> None:
    """Renueva el token de larga duración (válido 60 días) una vez por semana.
    El nuevo token se deja en $RUNNER_TEMP/new_token para que el workflow lo guarde como secreto."""
    is_monday = dt.datetime.now(TZ_VE).weekday() == 0
    if not (is_monday or FORCE_REFRESH):
        return
    try:
        r = requests.get(
            "https://graph.threads.net/refresh_access_token",
            params={"grant_type": "th_refresh_token", "access_token": token},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        new_token = data["access_token"]
        days = int(data.get("expires_in", 0)) // 86400
        out_dir = pathlib.Path(os.getenv("RUNNER_TEMP", "/tmp"))
        (out_dir / "new_token").write_text(new_token, encoding="utf-8")
        if os.getenv("GITHUB_ACTIONS"):
            print(f"::add-mask::{new_token}")
        log(f"Token renovado (válido ~{days} días).")
    except Exception as e:  # noqa: BLE001
        log(f"Aviso: no se pudo renovar el token ({e}). Se reintentará la próxima semana.")


def main() -> int:
    token = os.getenv("THREADS_ACCESS_TOKEN", "").strip()
    if not token and not DRY_RUN:
        log("Falta THREADS_ACCESS_TOKEN.")
        return 1
    if not (os.getenv("GEMINI_API_KEY") or os.getenv("ANTHROPIC_API_KEY")):
        log("Falta GEMINI_API_KEY (o ANTHROPIC_API_KEY).")
        return 1
    log(f"IA: {PROVIDER} ({GEMINI_MODEL if PROVIDER == 'gemini' else CLAUDE_MODEL})")

    history = load_history()
    topic = pick_topic(history)
    log(f"Tema del día: {topic['id']}")

    text = generate_post(topic, history, fetch_real_data())
    log(f"Post generado ({len(text)} caracteres):\n{text}\n")

    if DRY_RUN:
        log("DRY_RUN activo: no se publica ni se guarda historial.")
        return 0

    post_id = publish_to_threads(text, token)
    log(f"Publicado en Threads. ID: {post_id}")

    history.append(
        {
            "fecha": dt.datetime.now(TZ_VE).isoformat(timespec="minutes"),
            "tema": topic["id"],
            "post_id": post_id,
            "texto": text,
        }
    )
    save_history(history)
    maybe_refresh_token(token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
