"""
Bot diario de Threads para Cotizados (https://cotizados.vercel.app)

Flujo:
  1. Elige el tema siguiente al del último post (rota por todos los formatos narrativos:
     noticia rápida, ¿cuánto es?, debate, detrás de cámaras, escena, brecha, truco).
  2. Lee las tasas reales del día: de COTIZADOS_API_URL si existe, o abriendo
     /cotizaciones en un navegador sin interfaz (la página carga los datos con JavaScript),
     y calcula en Python montos de ejemplo, variación y brecha (la IA no calcula cifras).
  3. Genera el texto con Gemini (Google AI Studio) y lo descarta si trae muletillas de
     plantilla (saludos, "en la imagen…", "pruébalo aquí abajo"…) o es demasiado largo.
  4. Captura una vista de la web según el tema (tarjetas de /cotizaciones, calculadora o
     noticias; ver VISTAS) como imagen en public/tasas.png y la sube al repo (debe ser público) para que Threads la descargue. Si falla, el post va sin imagen.
  5. Publica en Threads (crear contenedor -> esperar -> publicar) y responde al propio
     post con el enlace (los posts sin enlace externo suelen tener más alcance).
  6. Guarda el historial y, una vez por semana, renueva el token de Threads.

Variables de entorno:
  THREADS_ACCESS_TOKEN  (obligatoria) token de larga duración de Threads
  GEMINI_API_KEY        (obligatoria) clave de Google AI Studio
  COTIZADOS_API_URL     (opcional)    endpoint JSON con las tasas (si no, se lee la página)
  GEMINI_MODEL          (opcional)    por defecto gemini-3.5-flash-lite
  THREADS_TOPIC_TAG     (opcional)    tema de Threads de cada post, por defecto DólarBCV
  DRY_RUN=true          (opcional)    genera el post pero no lo publica
  FORCE_REFRESH=true    (opcional)    fuerza la renovación del token

  En local se pueden poner en un archivo .env en la raíz del repo (no se sube a GitHub).
"""

import datetime as dt
import json
import os
import pathlib
import random
import re
import subprocess
import sys
import time

import requests

ROOT = pathlib.Path(__file__).resolve().parent.parent


def load_dotenv(path: pathlib.Path) -> None:
    """Carga un archivo .env (para pruebas locales) sin pisar variables ya definidas."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        if value:
            os.environ.setdefault(key.strip(), value)


load_dotenv(ROOT / ".env")
HISTORY_FILE = ROOT / "data" / "history.json"
IMAGE_FILE = ROOT / "public" / "tasas.png"
SITE = "https://cotizados.vercel.app"
GRAPH = "https://graph.threads.net/v1.0"
THREADS_MAX_CHARS = 500
TZ_VE = dt.timezone(dt.timedelta(hours=-4))

DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
FORCE_REFRESH = os.getenv("FORCE_REFRESH", "false").lower() == "true"
GEMINI_MODEL = os.getenv("GEMINI_MODEL") or "gemini-3.5-flash-lite"

# Cada tema es un formato narrativo distinto (no solo un producto distinto): el post habla de la
# vida del lector con dos monedas y la app aparece poco o nada; el enlace va en la respuesta.
# "angulo" guía a la IA, "link" es la sección del enlace, "vistas" las imágenes posibles (ver
# VISTAS), "respuesta" el texto de la respuesta con el enlace y "datos" qué se le pasa a la IA.
TEMAS = [
    {
        "id": "movimiento_dia",
        "angulo": (
            "Formato NOTICIA RÁPIDA. Cuenta cómo se movió el dólar BCV respecto a ayer, como lo "
            "contaría un amigo que siempre está pendiente de la tasa. La primera línea es la cifra "
            "de hoy y si subió o bajó. Luego una línea de contexto humano (qué significa para el "
            "bolsillo). Cierra con una pregunta corta y fácil de responder."
        ),
        "link": f"{SITE}/cotizaciones",
        "vistas": ["cot_dolar"],
        "respuesta": "Todas las tasas de hoy en un solo sitio 👉 {link}",
        "datos": "tasas",
    },
    {
        "id": "cuanto_es",
        "angulo": (
            "Formato ¿CUÁNTO ES? Aterriza la tasa en algo cotidiano: elige UN monto de los cálculos "
            "ya hechos (ej. una compra de 20 $) y di cuánto es hoy en bolívares a tasa BCV y a tasa "
            "Binance P2P, y cuánto es la diferencia. Que se sienta como algo que le pasa a cualquiera "
            "en el mercado, la panadería o la farmacia."
        ),
        "link": f"{SITE}/calculator",
        "vistas": ["calculadora", "cot_brecha"],
        "respuesta": "Calcula tu monto exacto aquí 👉 {link}",
        "datos": "tasas",
    },
    {
        "id": "debate",
        "angulo": (
            "Formato DEBATE. Lanza UNA pregunta que divida opiniones sobre el día a día con dos "
            "monedas (ej. ¿cobrar a BCV o a paralelo?, ¿vuelto en bolívares o en caramelos?, "
            "¿precios en $ o en Bs en los negocios?, ¿pago móvil o efectivo?). Toma una postura "
            "ligera y con humor para provocar que la gente responda. No hables de la app."
        ),
        "link": f"{SITE}/cotizaciones",
        "vistas": ["cot_brecha", "cot_binance"],
        "respuesta": "Por si quieres ver cómo están las tasas ahorita 👉 {link}",
        "datos": "tasas",
    },
    {
        "id": "novedades",
        "angulo": (
            "Formato DETRÁS DE CÁMARAS. Habla como el creador de la app contando en qué estuvo "
            "trabajando: 1 o 2 mejoras reales y recientes de los datos, explicadas por el problema "
            "que resuelven (no como lista de funciones). Cierra pidiendo una opinión concreta sobre "
            "esa mejora."
        ),
        "link": f"{SITE}/noticias",
        "vistas": ["noticias", "calculadora"],
        "respuesta": "Aquí está todo lo nuevo 👉 {link}",
        "datos": "noticias",
    },
    {
        "id": "escena",
        "angulo": (
            "Formato ESCENA. Una mini escena con humor, muy reconocible, de la vida diaria en "
            "Venezuela con las tasas (el vendedor que saca la calculadora del teléfono, el '¿a qué "
            "tasa?', el vuelto que nunca cuadra, el grupo de WhatsApp de la familia preguntando el "
            "dólar). 2-4 líneas cortas, estilo diálogo o narración rápida. Remata con un giro "
            "gracioso. La app, si aparece, en media línea al final."
        ),
        "link": f"{SITE}/calculator",
        "vistas": ["calculadora", "cot_dolar"],
        "respuesta": "Para la próxima, la calculadora 👉 {link}",
        "datos": "tasas",
    },
    {
        "id": "brecha",
        "angulo": (
            "Formato DATO QUE DUELE. Usa la brecha entre BCV y Binance P2P de los cálculos ya hechos "
            "y tradúcela a plata: cuántos bolívares de diferencia hay por cada 100 $. Explica en "
            "una línea a quién le afecta (quien cobra a BCV y compra a paralelo, o al revés). "
            "Cierra preguntando de qué lado está la gente."
        ),
        "link": f"{SITE}/cotizaciones",
        "vistas": ["cot_brecha"],
        "respuesta": "La brecha actualizada aquí 👉 {link}",
        "datos": "tasas",
    },
    {
        "id": "truco",
        "angulo": (
            "Formato TRUCO ÚTIL. Un truco práctico y concreto para quien cobra o paga en bolívares y "
            "dólares (o para emprendedores que actualizan precios). Puede ser de finanzas personales "
            "o de cómo usar Cotizados (agregarla a la pantalla de inicio sin descargar nada, la "
            "calculadora con varias monedas, la tasa libre). Directo: 'Truco: ...'. Nada de sermones."
        ),
        "link": SITE,
        "vistas": ["calculadora", "cot_dolar", "cot_euro"],
        "respuesta": "Aquí la tienes 👉 {link}",
        "datos": "tasas",
    },
]

# Capturas posibles para la imagen del post. "tarjeta" es el índice de la tarjeta en /cotizaciones
# (0 brecha, 1 dólar BCV, 2 euro BCV, 3 Binance); "hasta" es el texto donde termina el recorte.
VISTAS = {
    "cot_brecha": {"path": "/cotizaciones", "tarjeta": 0, "desc": "la brecha cambiaria entre el BCV y Binance P2P"},
    "cot_dolar": {"path": "/cotizaciones", "tarjeta": 1, "desc": "la tasa del dólar BCV de hoy y su cambio"},
    "cot_euro": {"path": "/cotizaciones", "tarjeta": 2, "desc": "la tasa del euro BCV de hoy y su cambio"},
    "cot_binance": {"path": "/cotizaciones", "tarjeta": 3, "desc": "el precio de compra y venta del USDT en Binance P2P"},
    "calculadora": {"path": "/calculator", "hasta": "AÑADIR TASA", "desc": "la calculadora convirtiendo dólares BCV a bolívares"},
    "noticias": {"path": "/noticias", "alto": 495, "desc": "la sección Noticias con las últimas mejoras de la app"},
}

# Tema de Threads (topic tag) con el que se etiqueta cada post; reemplaza a los hashtags del texto.
TOPIC_TAG = os.getenv("THREADS_TOPIC_TAG") or "DólarBCV"

SYSTEM_PROMPT = f"""Eres la persona venezolana que creó Cotizados ({SITE}), una web/app gratuita para
ver la tasa del BCV, el USDT en Binance P2P y convertir montos. Escribes en tu cuenta personal de
Threads, en primera persona, como una persona real (no como una marca ni un community manager).

Tu objetivo NO es vender la app: es que la gente se detenga a leer y RESPONDA. La gente sigue
cuentas que le informan, le hacen reír o le dan algo útil. La app se gana su lugar sola.

Cómo escribir:
- Español de Venezuela, natural, como se escribe en redes. Frases cortas. Puedes usar
  expresiones venezolanas con moderación (chamo, de pana, ladilla, echarle un ojo, real).
- La PRIMERA LÍNEA lo es todo: corta (máx. ~70 caracteres), que se entienda sola y genere
  curiosidad, sorpresa o identificación. Puede ser una cifra, una afirmación rotunda, una escena
  o una opinión. Luego un salto de línea.
- Entre 120 y 320 caracteres en total. Nunca más de 420. Párrafos de 1-2 líneas.
- 0 o 1 emoji, solo si suma.
- Sigue el formato que se te indica; cada formato tiene una estructura distinta.

Prohibido (hace que todos los posts suenen iguales y la gente los ignora):
- Saludos al inicio ("Buenas tardes", "Buenos días", "Hola", "por aquí").
- Empezar con preguntas retóricas genéricas ("¿Sabías que…?", "¿Cansado de…?", "¿Sacando
  cuentas…?", "¿Revisando…?").
- Describir la imagen ("En la imagen ves…"). La imagen acompaña, no se explica.
- Llamados a la acción de anuncio ("Pruébalo aquí abajo", "Te dejo el enlace", "Entra ya").
  Si hace falta, como mucho una mención casual al final ("lo tengo en la app", "link abajo").
- Hashtags y enlaces (el enlace se publica aparte, en una respuesta).
- Frases de relleno corporativas ("hacerte la vida más fácil", "sin complicaciones", "al vuelo").

Datos:
- Usa SOLO las cifras de "Cálculos ya hechos" o de "Datos reales del día", copiadas exactamente.
  NUNCA inventes ni calcules cifras por tu cuenta. Si no hay datos, no menciones cifras.
- Si los datos son las novedades de la app (sección Noticias), habla solo de mejoras que aparezcan
  ahí; no inventes funciones.
- No prometas ganancias ni des consejos de inversión.

Devuelve solo el texto del post, sin comillas ni explicaciones."""

# Muletillas que delatan el post plantilla; si aparecen, se pide otra versión a la IA.
FRASES_PROHIBIDAS = [
    r"^\W*(buen[oa]s|hola|feliz)\b",
    r"por aqu[ií]",
    r"en la imagen",
    r"pru[eé]balo",
    r"aqu[ií] abajo",
    r"te dejo el enlace",
    r"^\W*¿sab[ií]as que",
    r"^\W*¿cansad[oa] de",
    r"hacerte la vida",
    r"al vuelo",
]


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
    """Toma el tema siguiente al del último post, así varios posts al día no repiten tema."""
    ids = [t["id"] for t in TEMAS]
    last = history[-1].get("tema") if history else None
    idx = (ids.index(last) + 1) % len(TEMAS) if last in ids else 0
    return TEMAS[idx]


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


def fetch_news() -> str | None:
    """Texto de /noticias (registro de mejoras de la app) para el tema "novedades"."""
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(locale="es-VE")
            page.goto(f"{SITE}/noticias", wait_until="networkidle", timeout=45000)
            text = page.inner_text("main") if page.query_selector("main") else page.inner_text("body")
            browser.close()
        clean = "\n".join(l.strip() for l in text.splitlines() if l.strip())
        log(f"Novedades leídas:\n{clean[:1500]}\n")
        return clean[:1500]
    except Exception as e:  # noqa: BLE001
        log(f"Aviso: no se pudieron leer las novedades ({e}).")
        return None


def pick_view(topic: dict, history: list) -> str:
    """Elige una vista del tema, evitando repetir la imagen del post anterior."""
    last = history[-1].get("vista") if history else None
    opciones = [v for v in topic["vistas"] if v != last] or topic["vistas"]
    return random.choice(opciones)


def capture_view(view_id: str) -> pathlib.Path | None:
    """Captura una vista de la web (ver VISTAS) como PNG en public/tasas.png."""
    from playwright.sync_api import sync_playwright

    view = VISTAS[view_id]
    out = IMAGE_FILE
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            # Ventana alta para que la barra de navegación fija no tape las tarjetas de abajo.
            page = browser.new_page(
                locale="es-VE", viewport={"width": 430, "height": 1600},
                device_scale_factor=2, color_scheme="dark",
            )
            page.goto(f"{SITE}{view['path']}", wait_until="networkidle", timeout=45000)
            try:
                page.wait_for_function(
                    r"() => /\d+[.,]\d{2}/.test(document.body.innerText)", timeout=15000
                )
            except Exception:  # noqa: BLE001
                pass  # /noticias no tiene cifras
            page.wait_for_timeout(1500)  # deja terminar las animaciones de las cifras

            if "tarjeta" in view:
                box = page.evaluate(
                    """(i) => {
                        const c = document.querySelectorAll('main [class*="rounded-[28px]"]')[i];
                        if (!c) return null;
                        const r = c.getBoundingClientRect();
                        return {top: r.top + scrollY, bottom: r.bottom + scrollY};
                    }""",
                    view["tarjeta"],
                )
                if not box:
                    raise RuntimeError(f"no se encontró la tarjeta {view['tarjeta']}")
                # La primera tarjeta incluye el aviso "Actualizado: ..." que tiene encima.
                top = 0 if view["tarjeta"] == 0 else max(0, box["top"] - 16)
                clip = {"x": 0, "y": top, "width": 430, "height": box["bottom"] + 20 - top}
            elif "hasta" in view:
                bottom = page.evaluate(
                    """(t) => {
                        const e = [...document.querySelectorAll('button, a, span')]
                            .find(e => e.innerText && e.innerText.trim().toUpperCase() === t);
                        return e ? e.getBoundingClientRect().bottom + scrollY : 460;
                    }""",
                    view["hasta"],
                )
                clip = {"x": 0, "y": 0, "width": 430, "height": bottom + 30}
            else:
                clip = {"x": 0, "y": 0, "width": 430, "height": view["alto"]}

            page.screenshot(path=str(out), clip=clip, full_page=True)
            browser.close()
        log(f"Imagen generada ({view_id}): {out}")
        return out
    except Exception as e:  # noqa: BLE001
        log(f"Aviso: no se pudo generar la imagen {view_id} ({e}).")
        return None


def git(*args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=cotizados-bot",
         "-c", "user.email=cotizados-bot@users.noreply.github.com", *args],
        cwd=ROOT, check=True, capture_output=True, text=True,
    ).stdout.strip()


def publish_image(path: pathlib.Path) -> str:
    """Sube la imagen al repo (public/tasas.png) y devuelve su URL pública en
    raw.githubusercontent.com: la API de Threads solo acepta imágenes desde una URL.
    Requiere que el repo sea público. La URL lleva el commit para evitar la caché de GitHub."""
    rel = path.relative_to(ROOT).as_posix()
    git("add", rel)
    git("commit", "-m", f"chore: imagen de tasas {dt.datetime.now(TZ_VE):%F}", "--", rel)
    git("push")
    sha = git("rev-parse", "HEAD")
    repo = os.getenv("GITHUB_REPOSITORY") or re.sub(
        r"^.*github\.com[:/]|\.git$", "", git("remote", "get-url", "origin")
    )
    url = f"https://raw.githubusercontent.com/{repo}/{sha}/{rel}"
    # Espera a que GitHub sirva el archivo antes de pasárselo a Threads.
    for _ in range(12):
        if requests.head(url, timeout=15).status_code == 200:
            return url
        time.sleep(5)
    raise RuntimeError(f"la imagen no está accesible en {url} (¿el repo es público?)")


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


def parse_num(s: str) -> float:
    """'1.005,71' -> 1005.71"""
    return float(s.replace(".", "").replace(",", "."))


def fmt_bs(x: float) -> str:
    """1005.714 -> '1.005,71'"""
    return f"{x:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")


def rate_calcs(real_data: str | None) -> str | None:
    """Saca BCV, BCV de ayer y Binance P2P del texto de /cotizaciones y hace las cuentas en
    Python (montos en bolívares, variación, brecha), para que la IA no tenga que calcular."""
    if not real_data:
        return None
    num = r"(\d{1,3}(?:\.\d{3})*,\d{2})"
    bcv = re.search(r"BCV Oficial\s+" + num, real_data)
    p2p = re.search(r"Binance P2P\s+" + num, real_data)
    if not bcv:
        return None
    bcv = parse_num(bcv.group(1))
    lines = [f"Dólar BCV hoy: {fmt_bs(bcv)} Bs"]
    ayer = re.search(r"Hace 1 d\S*\s+" + num, real_data)
    if ayer:
        ayer = parse_num(ayer.group(1))
        diff = bcv - ayer
        if abs(diff) < 0.005:
            lines.append(f"Respecto a ayer ({fmt_bs(ayer)} Bs): sin cambios")
        else:
            verbo = "subió" if diff > 0 else "bajó"
            lines.append(
                f"Respecto a ayer ({fmt_bs(ayer)} Bs): {verbo} {fmt_bs(abs(diff))} Bs "
                f"({fmt_bs(abs(diff) / ayer * 100)}%)"
            )
    if p2p:
        p2p = parse_num(p2p.group(1))
        lines.append(f"USDT Binance P2P: {fmt_bs(p2p)} Bs")
        lines.append(f"Brecha BCV vs P2P: {fmt_bs((p2p - bcv) / bcv * 100)}%")
    for usd in (1, 5, 10, 20, 50, 100):
        line = f"{usd} $ = {fmt_bs(usd * bcv)} Bs a tasa BCV"
        if p2p:
            line += (f" | {fmt_bs(usd * p2p)} Bs a tasa P2P"
                     f" | diferencia {fmt_bs(usd * (p2p - bcv))} Bs")
        lines.append(line)
    return "\n".join(lines)


def ask_gemini(system: str, user_msg: str) -> str:
    """Llama a la API de Gemini (Google AI Studio) por REST."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user_msg}]}],
        "generationConfig": {"temperature": 1.0, "maxOutputTokens": 2048},
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


def post_problems(text: str) -> list[str]:
    """Muletillas prohibidas o largo excesivo; lista vacía si el post está bien."""
    problems = [p for p in FRASES_PROHIBIDAS if re.search(p, text, re.IGNORECASE | re.MULTILINE)]
    if len(text) > 420:
        problems.append(f"demasiado largo ({len(text)} caracteres, máximo 420)")
    return problems


def generate_post(topic: dict, history: list, real_data: str | None, view_id: str) -> str:
    # Solo las primeras líneas: si la IA ve los posts completos tiende a copiar su estructura.
    ganchos = "\n".join(
        f"- {h['texto'].strip().splitlines()[0][:90]}" for h in history[-12:] if h.get("texto")
    ) or "(ninguno todavía)"
    hoy = dt.datetime.now(TZ_VE).strftime("%A %d/%m/%Y, %I:%M %p")
    calcs = rate_calcs(real_data) if topic["datos"] == "tasas" else None

    user_msg = (
        f"Fecha y hora en Venezuela: {hoy}\n\n"
        f"{topic['angulo']}\n\n"
        f"Cálculos ya hechos (cifras exactas, úsalas tal cual): {calcs or 'no disponibles'}\n\n"
        f"Datos reales del día:\n{real_data or 'no disponibles'}\n\n"
        f"El post lleva una imagen de {VISTAS[view_id]['desc']} (no la describas).\n\n"
        f"Primeras líneas de los últimos posts (escribe algo que no se parezca a ninguna):\n{ganchos}"
    )

    text = ""
    for intento in range(3):
        text = ask_gemini(SYSTEM_PROMPT, user_msg).strip().strip('"')
        # El enlace va solo en la respuesta y el tema se marca con topic_tag, no con hashtags.
        text = re.sub(r"[ \t]*https?://\S+", "", text)
        text = re.sub(r"[ \t]*#\w+", "", text).strip()
        problems = post_problems(text)
        if not problems:
            return text
        log(f"Post descartado ({'; '.join(problems)}), reintentando ({intento + 1}/3):\n{text}\n")
        user_msg += (
            f"\n\nTu versión anterior no sirve por: {'; '.join(problems)}. "
            "Escribe otra distinta que cumpla todas las reglas."
        )

    if len(text) <= THREADS_MAX_CHARS:
        log("Aviso: se publica la última versión aunque no cumple todas las reglas.")
        return text
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


def publish_to_threads(
    text: str, token: str, reply_to_id: str | None = None, image_url: str | None = None
) -> str:
    params = {"media_type": "TEXT", "text": text}
    if image_url:
        params.update(media_type="IMAGE", image_url=image_url)
    if reply_to_id:
        params["reply_to_id"] = reply_to_id
    else:
        params["topic_tag"] = TOPIC_TAG
    try:
        container = threads_request("POST", "/me/threads", token, **params)
    except RuntimeError as e:
        if "topic_tag" not in params:
            raise
        log(f"Aviso: Threads rechazó el topic_tag ({e}); se publica sin él.")
        params.pop("topic_tag")
        container = threads_request("POST", "/me/threads", token, **params)
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
    """Renueva el token de larga duración (válido 60 días) una vez por semana, en el primer
    post del lunes. El nuevo token se deja en $RUNNER_TEMP/new_token para que el workflow lo
    guarde como secreto."""
    now = dt.datetime.now(TZ_VE)
    first_monday_post = now.weekday() == 0 and now.hour < 12
    if not (first_monday_post or FORCE_REFRESH):
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
    if not os.getenv("GEMINI_API_KEY"):
        log("Falta GEMINI_API_KEY.")
        return 1
    log(f"IA: Gemini ({GEMINI_MODEL})")

    history = load_history()
    topic = pick_topic(history)
    log(f"Tema del día: {topic['id']}")

    view_id = pick_view(topic, history)
    log(f"Vista para la imagen: {view_id}")
    # En el tema "novedades" los datos son las últimas mejoras de la app, no las tasas.
    data = fetch_news() if topic["datos"] == "noticias" else fetch_real_data()
    text = generate_post(topic, history, data, view_id)
    log(f"Post generado ({len(text)} caracteres):\n{text}\n")
    reply_text = topic["respuesta"].format(link=topic["link"])
    log(f"Respuesta con el enlace:\n{reply_text}\n")
    image = capture_view(view_id)

    if DRY_RUN:
        log("DRY_RUN activo: no se publica ni se guarda historial.")
        return 0

    post_id = None
    if image:
        try:
            image_url = publish_image(image)
            log(f"Imagen publicada: {image_url}")
            post_id = publish_to_threads(text, token, image_url=image_url)
        except Exception as e:  # noqa: BLE001
            log(f"Aviso: no se pudo publicar con imagen ({e}); se publica solo el texto.")
    if not post_id:
        post_id = publish_to_threads(text, token)
    log(f"Publicado en Threads. ID: {post_id}")

    reply_id = None
    try:
        reply_id = publish_to_threads(reply_text, token, reply_to_id=post_id)
        log(f"Respuesta con el enlace publicada. ID: {reply_id}")
    except Exception as e:  # noqa: BLE001
        log(f"Aviso: el post salió, pero no se pudo publicar la respuesta con el enlace ({e}).")

    history.append(
        {
            "fecha": dt.datetime.now(TZ_VE).isoformat(timespec="minutes"),
            "tema": topic["id"],
            "post_id": post_id,
            "reply_id": reply_id,
            "vista": view_id,
            "texto": text,
        }
    )
    save_history(history)
    maybe_refresh_token(token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
