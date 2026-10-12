"""
Bot diario de Threads para Cotizados (https://cotizados.vercel.app)

Flujo:
  1. Elige el tema siguiente al del último post (rota por todos los temas, sin repetir).
  2. Lee las tasas reales del día: de COTIZADOS_API_URL si existe, o abriendo
     /cotizaciones en un navegador sin interfaz (la página carga los datos con JavaScript).
  3. Genera el texto con Gemini (Google AI Studio), evitando repetir los últimos posts.
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
# Texto de la respuesta con el enlace; se elige uno al azar para no repetir siempre lo mismo.
REPLY_TEMPLATES = [
    "Aquí lo tienes 👉 {link}",
    "Te dejo el enlace: {link}",
    "Pruébalo aquí 👇\n{link}",
    "Entra aquí: {link}",
]
TZ_VE = dt.timezone(dt.timedelta(hours=-4))

DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
FORCE_REFRESH = os.getenv("FORCE_REFRESH", "false").lower() == "true"
GEMINI_MODEL = os.getenv("GEMINI_MODEL") or "gemini-3.5-flash-lite"

# Cada tema define el ángulo del post, el enlace que se publica en la respuesta y las vistas
# de la web que pueden ir como imagen (ver VISTAS).
TEMAS = [
    {
        "id": "tasa_bcv",
        "angulo": "Invita a consultar la tasa oficial del BCV de hoy en un solo lugar, rápido y sin anuncios molestos.",
        "link": f"{SITE}/cotizaciones",
        "vistas": ["cot_dolar", "cot_brecha"],
    },
    {
        "id": "calculadora",
        "angulo": "Presenta la calculadora: convertir entre bolívares, dólares y USDT en segundos (ej. para pagar, cobrar o cotizar un producto).",
        "link": f"{SITE}/calculator",
        "vistas": ["calculadora"],
    },
    {
        "id": "bcv_vs_usdt",
        "angulo": "Explica por qué conviene comparar la tasa BCV con la cotización del USDT antes de cambiar o fijar precios.",
        "link": f"{SITE}/cotizaciones",
        "vistas": ["cot_brecha", "cot_binance"],
    },
    {
        "id": "noticias",
        "angulo": "Cuenta las últimas novedades y mejoras de la app (la sección Noticias es el registro de cambios de Cotizados, no noticias económicas).",
        "link": f"{SITE}/noticias",
        "vistas": ["noticias"],
    },
    {
        "id": "comunidad",
        "angulo": "Invita a la comunidad a dejar sus comentarios y sugerencias para mejorar la app. Tono cercano, pide opinión.",
        "link": f"{SITE}/comentarios",
        "vistas": ["cot_brecha", "cot_dolar", "cot_euro", "cot_binance", "calculadora"],
    },
    {
        "id": "app_movil",
        "angulo": "Cuenta que Cotizados se puede agregar a la pantalla de inicio del teléfono y usarse como una app, sin descargar nada de la tienda.",
        "link": SITE,
        "vistas": ["calculadora", "cot_brecha", "cot_dolar"],
    },
    {
        "id": "tip_finanzas",
        "angulo": "Da un tip práctico y breve de finanzas personales para quien cobra o paga en bolívares y dólares, y cierra recomendando Cotizados.",
        "link": f"{SITE}/calculator",
        "vistas": ["cot_euro", "cot_binance", "cot_dolar", "calculadora"],
    },
    {
        "id": "comerciantes",
        "angulo": "Habla a emprendedores y comerciantes: actualizar precios en bolívares según la tasa del día con la calculadora.",
        "link": f"{SITE}/calculator",
        "vistas": ["calculadora", "cot_dolar"],
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

SYSTEM_PROMPT = f"""Eres el community manager de Cotizados ({SITE}), una web/app gratuita venezolana
para consultar la tasa del BCV, la cotización del USDT, convertir montos con una calculadora y ver
las novedades de la app.

Escribes UN post para Threads en español de Venezuela, natural y cercano (sin sonar a anuncio
corporativo).

Reglas estrictas:
- Máximo 450 caracteres en total.
- Empieza con un gancho en la primera línea (pregunta, dato o situación cotidiana).
- NO incluyas ningún enlace ni URL: el enlace se publica aparte, en una respuesta al post.
  Puedes cerrar invitando a ver el enlace "aquí abajo" o "en la respuesta".
- Máximo 1-2 emojis y máximo 2 hashtags (por ejemplo #DólarBCV #Venezuela), o ninguno.
- "Datos reales del día" es el texto de la página de cotizaciones de Cotizados. Si trae tasas,
  aprovéchalas: cita 1 o 2 cifras (por ejemplo BCV y USDT) copiándolas exactamente como aparecen,
  con su etiqueta correcta. Si no estás seguro de qué representa un número, no lo uses.
- Si los "Datos reales del día" son las novedades de la app (sección Noticias), comenta 1 o 2
  mejoras recientes tal como se describen ahí, contadas como "estuvimos mejorando…". No inventes
  funciones que no aparezcan en esos datos.
- NUNCA inventes ni calcules cifras de tasas, precios o porcentajes. Si no hay datos, no
  menciones cifras.
- Si se indica qué muestra la imagen del post, puedes referirte a ella ("en la imagen…").
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
    """Texto de /noticias (registro de mejoras de la app) para el tema "noticias"."""
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


def generate_post(topic: dict, history: list, real_data: str | None, view_id: str) -> str:
    recientes = "\n---\n".join(h["texto"] for h in history[-10:]) or "(ninguno todavía)"
    hoy = dt.datetime.now(TZ_VE).strftime("%A %d/%m/%Y, %I:%M %p")

    user_msg = (
        f"Fecha y hora: {hoy} (adapta el saludo o el gancho al momento del día)\n"
        f"Tema: {topic['angulo']}\n"
        f"Sección que se promociona (su enlace va en la respuesta, no lo escribas): {topic['link']}\n"
        f"Datos reales del día: {real_data or 'no disponibles'}\n"
        f"La imagen del post muestra: {VISTAS[view_id]['desc']}\n\n"
        f"Posts recientes (no repitas sus ganchos, frases ni estructura):\n{recientes}"
    )

    for intento in range(3):
        text = ask_gemini(SYSTEM_PROMPT, user_msg).strip().strip('"')
        # Por si la IA incluye algún enlace igualmente: el enlace va solo en la respuesta.
        text = re.sub(r"[ \t]*https?://\S+", "", text).strip()
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


def publish_to_threads(
    text: str, token: str, reply_to_id: str | None = None, image_url: str | None = None
) -> str:
    params = {"media_type": "TEXT", "text": text}
    if image_url:
        params.update(media_type="IMAGE", image_url=image_url)
    if reply_to_id:
        params["reply_to_id"] = reply_to_id
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
    # En el tema "noticias" los datos son las últimas mejoras de la app, no las tasas.
    data = fetch_news() if topic["id"] == "noticias" else fetch_real_data()
    text = generate_post(topic, history, data, view_id)
    log(f"Post generado ({len(text)} caracteres):\n{text}\n")
    reply_text = random.choice(REPLY_TEMPLATES).format(link=topic["link"])
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
