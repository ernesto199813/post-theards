"""
Bot diario de Threads para Cotizados (https://cotizados.vercel.app)

Flujo:
  1. Lee las estadísticas de los posts de los últimos 14 días (vistas, likes, respuestas...)
     y elige el formato narrativo (noticia rápida, ¿cuánto es?, mercado en desarrollo, cómo
     funciona, detrás de cámaras, escena, brecha, truco) por sorteo ponderado según cómo le ha
     ido a cada uno.
     Los mejores posts se le pasan a la IA como referencia.
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

  El token necesita el permiso threads_manage_insights para leer estadísticas.
  python bot/post.py --stats  muestra el rendimiento por formato sin publicar nada.

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
            "Formato VARIACIÓN DEL DÍA (es el primer post de la mañana). La primera línea es la "
            "tasa del dólar BCV de hoy y cuánto subió o bajó respecto a ayer, con las cifras de "
            "los cálculos ya hechos (si no cambió, dilo). Puedes sumar el euro BCV si también se "
            "movió. Luego una línea de contexto práctico usando el contexto del mercado (ej. qué "
            "significa para quien cobra a BCV y le cobran a tasa euro). Cierra con una pregunta "
            "corta y fácil de responder."
        ),
        "link": f"{SITE}/cotizaciones",
        # Carrusel de 2: la tasa del día y su gráfica.
        "imagenes": 2,
        "vistas": ["cot_dolar", "cot_euro", "cot_resumen", "grafica_dolar", "grafica_euro"],
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
        "vistas": ["calculadora", "cot_brecha", "cot_binance", "cot_resumen"],
        "respuesta": "Calcula tu monto exacto aquí 👉 {link}",
        "datos": "tasas",
    },
    {
        "id": "mercado",
        "angulo": (
            "Formato EN DESARROLLO. Habla como el creador de la app contando, a grandes rasgos, que "
            "estás construyendo la sección Mercado: una comparativa de productos básicos del "
            "supermercado (harina, arroz, huevos, aceite…) para ver dónde sale más barato y armar "
            "tu lista de compras. Deja claro que todavía no está terminada y que la estás armando "
            "poco a poco. NO menciones precios, cifras, supermercados concretos ni funciones "
            "detalladas. Cierra pidiendo algo concreto: qué productos les gustaría poder comparar "
            "o qué les ayudaría más al hacer el mercado."
        ),
        "link": f"{SITE}/mercado",
        "espacio": 4,
        "vistas": ["mercado", "mercado_lista"],
        "respuesta": "Así va quedando, todavía en construcción 👉 {link}",
        "datos": "ninguno",
    },
    {
        "id": "recorrido",
        "angulo": (
            "Formato CÓMO FUNCIONA. Habla como el creador explicando UNA parte de la app (la de la "
            "idea de hoy) para que la gente entienda qué puede hacer con ella. Empieza por el "
            "problema que resuelve, no por el nombre de la sección. Luego cómo se usa, en 2 o 3 "
            "pasos o frases cortas. Cierra con una pregunta corta sobre si la conocían o cómo la "
            "usarían. Describe solo lo que dice la idea de hoy: no agregues pasos, botones, apps "
            "ni detalles que no estén ahí."
        ),
        # Una parte de la app por post, con la captura que la muestra (ver pick_idea).
        "ideas": [
            {"idea": "la Calculadora: escribes un monto y lo ves al instante en bolívares, dólares "
                     "BCV, USDT, euros y pesos colombianos; puedes añadir más tasas", "vistas": ["calculadora"]},
            {"idea": "la Tasa Libre de la calculadora: calcular con tu propia tasa, la que de verdad "
                     "te dieron, sin depender del BCV ni de Binance", "vistas": ["calculadora"]},
            {"idea": "la tarjeta de Brecha en Cotizaciones: cuánto se separan el BCV y el Binance "
                     "P2P en porcentaje y en bolívares", "vistas": ["cot_brecha", "cot_resumen"]},
            {"idea": "la gráfica anual de la Brecha (botón 'Ver gráfica anual' en Cotizaciones): ves "
                     "lo volátil que ha estado la brecha en lo que va de año, con la actual, el "
                     "promedio, la máxima y la mínima, por 1 mes, 3 meses, 6 meses o 1 año",
             "vistas": ["grafica_brecha"]},
            {"idea": "la vista de Inicio cuando inicias sesión: ahí llevas tus gastos del mes y "
                     "sabes en qué se te va la plata (gratis, solo necesitas crear tu cuenta)",
             "vistas": []},
            {"idea": "las tarjetas del dólar y el euro BCV en Cotizaciones: la tasa de hoy, la de "
                     "ayer y cuánto cambió", "vistas": ["cot_dolar", "cot_euro", "cot_resumen"]},
            {"idea": "el botón Compartir tasa en Cotizaciones, para mandarle la tasa del día a "
                     "quien te pregunte", "vistas": ["cot_resumen", "cot_brecha"]},
            {"idea": "la sección Mercado, que todavía estoy construyendo: una comparativa de "
                     "productos del supermercado para armar tu lista de compras (sin mencionar "
                     "precios)", "vistas": ["mercado", "mercado_lista"]},
            {"idea": "la sección Noticias: ahí anoto cada mejora que le hago a la app, en orden",
             "vistas": ["noticias"]},
            {"idea": "instalar Cotizados en la pantalla de inicio del teléfono desde el navegador, "
                     "sin tienda de apps y sin ocupar casi espacio", "vistas": ["calculadora", "cot_resumen"]},
            {"idea": "la sección Comentarios: cualquiera puede mandar una idea o reportar un error "
                     "y yo leo todos los mensajes", "vistas": ["cot_resumen", "calculadora"]},
        ],
        "link": SITE,
        "vistas": ["calculadora", "cot_resumen"],
        "respuesta": "Pruébala tú mismo 👉 {link}",
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
        # Es el formato que más vistas trae, pero las novedades cambian poco: como mucho una vez
        # cada 4 posts para no repetirlas.
        "espacio": 4,
        # Carrusel de 3, como el post manual que más vistas tuvo.
        "imagenes": 3,
        "vistas": ["noticias", "calculadora", "cot_resumen", "grafica_dolar", "grafica_brecha", "mercado"],
        "respuesta": "Aquí está todo lo nuevo 👉 {link}",
        "datos": "noticias",
    },
    {
        "id": "escena",
        "angulo": (
            "Formato SITUACIÓN REAL. Una situación concreta y reconocible del día a día con las "
            "tasas, sacada del contexto del mercado venezolano (ej. cobras tu sueldo a tasa BCV "
            "pero la tienda te cobra a tasa euro; preguntas '¿a qué tasa?' y cada negocio te da una "
            "distinta; el que vende en efectivo pone el dólar a como le conviene). 2-4 líneas "
            "cortas, contada en primera persona o como narración rápida. Cierra con una reflexión "
            "corta o una pregunta. La app, si aparece, en media línea al final."
        ),
        "link": f"{SITE}/calculator",
        "vistas": ["calculadora", "cot_euro", "cot_resumen", "cot_binance"],
        "respuesta": "Para la próxima, la calculadora 👉 {link}",
        "datos": "tasas",
    },
    {
        "id": "brecha",
        "angulo": (
            "Formato DATO QUE DUELE. Usa la brecha entre BCV y Binance P2P de los cálculos ya hechos. "
            "Elige UN enfoque: tradúcela a plata (cuántos bolívares de diferencia hay por cada "
            "100 $), o compárala con cómo ha sido en lo que va de año (actual vs promedio, máxima y "
            "mínima de los datos, para mostrar lo volátil que ha estado). Explica en una línea a "
            "quién le afecta (quien cobra a BCV y le cobran a tasa euro o a Binance). Cierra con "
            "una pregunta corta."
        ),
        "link": f"{SITE}/cotizaciones",
        "imagenes": 2,
        "vistas": ["cot_brecha", "grafica_brecha", "grafica_binance"],
        "respuesta": "La brecha actualizada aquí 👉 {link}",
        "datos": "tasas",
    },
    {
        "id": "truco",
        "angulo": (
            "Formato TRUCO ÚTIL. Un truco práctico y concreto para quien cobra o paga en bolívares y "
            "dólares (o para emprendedores que actualizan precios), de finanzas personales del día "
            "a día. Directo: 'Truco: ...'. Nada de sermones. Las funciones de la app tienen su "
            "propio formato; aquí la app, si aparece, va en media línea al final."
        ),
        "link": SITE,
        "vistas": ["calculadora", "cot_resumen", "grafica_dolar", "grafica_euro", "cot_euro"],
        "respuesta": "Aquí la tienes 👉 {link}",
        "datos": "tasas",
    },
]

# Capturas posibles para las imágenes del post. Cada una se toma en modo claro u oscuro (se
# alterna entre posts), así que una misma vista da dos imágenes distintas.
#   "tarjeta": índice de la tarjeta en /cotizaciones (0 brecha, 1 dólar BCV, 2 euro BCV, 3 Binance);
#              con "hasta_tarjeta" se recorta desde arriba hasta el final de esa tarjeta.
#   "hasta":   texto donde termina el recorte.
#   "grafica": índice de la tarjeta cuya "Ver gráfica anual" se abre; "rangos" son los periodos
#              posibles de la gráfica (se elige uno al azar).
#   "clic":    texto que se pulsa antes de capturar.
#   "pais":    la página elige el país por ubicación; se fuerza Venezuela en el selector.
RANGOS = {"1M": "el último mes", "3M": "los últimos 3 meses", "6M": "los últimos 6 meses", "1A": "el último año"}
VISTAS = {
    "cot_brecha": {"path": "/cotizaciones", "tarjeta": 0, "desc": "la brecha cambiaria entre el BCV y Binance P2P"},
    "cot_dolar": {"path": "/cotizaciones", "tarjeta": 1, "desc": "la tasa del dólar BCV de hoy y su cambio"},
    "cot_euro": {"path": "/cotizaciones", "tarjeta": 2, "desc": "la tasa del euro BCV de hoy y su cambio"},
    "cot_binance": {"path": "/cotizaciones", "tarjeta": 3, "desc": "el precio de compra y venta del USDT en Binance P2P"},
    "cot_resumen": {"path": "/cotizaciones", "hasta_tarjeta": 2, "desc": "las tasas del día: brecha, dólar BCV y euro BCV"},
    "calculadora": {"path": "/calculator", "hasta": "AÑADIR TASA", "desc": "la calculadora convirtiendo dólares BCV a bolívares"},
    "noticias": {"path": "/noticias", "alto": 495, "desc": "la sección Noticias con las últimas mejoras de la app"},
    "grafica_brecha": {"path": "/cotizaciones", "grafica": 0, "rangos": ["1A"],
                       "desc": "la gráfica de la brecha BCV vs Binance en {rango}, con actual, promedio, máxima y mínima"},
    "grafica_dolar": {"path": "/cotizaciones", "grafica": 1, "rangos": ["1M", "3M", "6M", "1A"],
                      "desc": "la gráfica del dólar BCV en {rango}"},
    "grafica_euro": {"path": "/cotizaciones", "grafica": 2, "rangos": ["1M", "3M", "6M", "1A"],
                     "desc": "la gráfica del euro BCV en {rango}"},
    "grafica_binance": {"path": "/cotizaciones", "grafica": 3, "rangos": ["1M", "3M", "6M", "1A"],
                        "desc": "la gráfica del USDT en Binance P2P en {rango}"},
    # Mercado no está terminado: se recorta antes de la lista de productos para no mostrar precios.
    "mercado": {"path": "/mercado", "pais": "VE", "alto": 440, "desc": "la sección Mercado (en construcción)"},
    "mercado_lista": {"path": "/mercado", "pais": "VE", "clic": "Armar mi lista", "alto": 600,
                      "desc": "la pestaña Armar mi lista de la sección Mercado (en construcción)"},
}

# Tema de Threads (topic tag) con el que se etiqueta cada post; reemplaza a los hashtags del texto.
TOPIC_TAG = os.getenv("THREADS_TOPIC_TAG") or "DólarBCV"
# Métricas de cada post que se leen de la API de Threads (permiso threads_manage_insights).
METRICAS = ["views", "likes", "replies", "reposts", "quotes", "shares"]

def load_context() -> str:
    """Contexto del mercado venezolano: la sección del README.md entre <!-- contexto:inicio -->
    y <!-- contexto:fin -->, para editarlo en un solo lugar."""
    m = re.search(
        r"<!-- contexto:inicio -->(.*?)<!-- contexto:fin -->",
        (ROOT / "README.md").read_text(encoding="utf-8"), re.DOTALL,
    )
    return m.group(1).strip() if m else ""


SYSTEM_PROMPT = f"""Eres la persona venezolana que creó Cotizados ({SITE}), una web/app gratuita para
ver la tasa del BCV (dólar y euro), el USDT en Binance P2P y convertir montos. Escribes en tu
cuenta personal de Threads, en primera persona, como una persona real (no como una marca ni un
community manager).

Tu objetivo NO es vender la app: es que la gente se detenga a leer y RESPONDA. La gente sigue
cuentas que le informan o le dan algo útil. La app se gana su lugar sola.

Contexto del mercado venezolano (tenlo presente al redactar; úsalo para que lo que digas sea
realista, pero no lo repitas entero en cada post):
{load_context()}

Cómo escribir:
- Español de Venezuela, natural, como se escribe en redes. Frases cortas y claras. Alguna
  expresión venezolana con moderación (de pana, echarle un ojo, real), sin forzarla.
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
    r"^\W*cansad[oa] de",
    r"hacerte la vida",
    r"al vuelo",
    # Clichés que el público ya no quiere leer (ver el contexto en README.md).
    r"caramel",
    r"malabares",
    r"se esfuma",
    r"\bt[ií]o\b",
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
        json.dumps(history[-150:], ensure_ascii=False, indent=2), encoding="utf-8"
    )


def post_age_hours(h: dict) -> float:
    return (dt.datetime.now(TZ_VE) - dt.datetime.fromisoformat(h["fecha"])).total_seconds() / 3600


def update_stats(history: list, token: str) -> None:
    """Lee de la API de Threads las estadísticas de los posts de los últimos 14 días y las
    guarda en cada entrada del historial ("stats"). Requiere el permiso threads_manage_insights;
    si el token no lo tiene, avisa y sigue sin estadísticas."""
    actualizados = 0
    for h in history:
        if not h.get("post_id") or post_age_hours(h) > 14 * 24:
            continue
        try:
            data = threads_request(
                "GET", f"/{h['post_id']}/insights", token, metric=",".join(METRICAS)
            )
        except RuntimeError as e:
            log(f"Aviso: no se pudieron leer las estadísticas ({e}).")
            return
        h["stats"] = {m["name"]: m["values"][0]["value"] for m in data.get("data", [])}
        # La respuesta con el enlace la publica el propio bot: no cuenta como respuesta del público.
        if h.get("reply_id") and h["stats"].get("replies"):
            h["stats"]["replies"] -= 1
        h["stats_fecha"] = dt.datetime.now(TZ_VE).isoformat(timespec="minutes")
        actualizados += 1
    log(f"Estadísticas actualizadas de {actualizados} posts.")


def engagement(h: dict) -> float:
    """Puntaje de un post: reproducciones más interacciones (una respuesta vale por 10 vistas)."""
    s = h.get("stats") or {}
    return (s.get("views", 0) + 5 * s.get("likes", 0)
            + 10 * (s.get("replies", 0) + s.get("reposts", 0) + s.get("quotes", 0) + s.get("shares", 0)))


def measured(history: list) -> list:
    """Posts con estadísticas y al menos 24 h publicados (antes no han terminado de moverse)."""
    return [h for h in history if h.get("stats") and post_age_hours(h) >= 24]


def stats_report(history: list) -> str:
    """Promedios por formato, para el log de cada ejecución."""
    lines = []
    for t in TEMAS:
        posts = [h for h in measured(history) if h.get("tema") == t["id"]]
        if posts:
            views = sum(h["stats"].get("views", 0) for h in posts) / len(posts)
            replies = sum(h["stats"].get("replies", 0) for h in posts) / len(posts)
            lines.append(f"  {t['id']:<15} {len(posts):>2} posts | {views:6.0f} vistas | "
                         f"{replies:4.1f} respuestas | puntaje {sum(map(engagement, posts)) / len(posts):6.0f}")
        else:
            lines.append(f"  {t['id']:<15}  sin datos")
    return "\n".join(lines)


def pick_topic(history: list) -> dict:
    """Elige el formato por sorteo ponderado según su puntaje medio, sin repetir los de los
    últimos posts (2, o "espacio" si el tema lo define). Los formatos con menos de 3 posts medidos
    reciben el mejor puntaje para que se sigan probando, y un 20 % de las veces se elige al azar
    para no encasillarse. El peso es la raíz del puntaje: un post viral no acapara la rotación.
    De martes a viernes, el primer post del día es siempre la variación de la tasa."""
    hoy = dt.datetime.now(TZ_VE)
    if hoy.weekday() in (1, 2, 3, 4) and not any(
        h.get("fecha", "").startswith(hoy.strftime("%Y-%m-%d")) for h in history
    ):
        return next(t for t in TEMAS if t["id"] == "movimiento_dia")

    def libre(t: dict) -> bool:
        return t["id"] not in {h.get("tema") for h in history[-t.get("espacio", 2):]}

    opciones = [t for t in TEMAS if libre(t)] or TEMAS
    datos = measured(history)
    medias = {}
    for t in TEMAS:
        posts = [h for h in datos if h.get("tema") == t["id"]]
        if len(posts) >= 3:
            medias[t["id"]] = sum(map(engagement, posts[-10:])) / len(posts[-10:])
    if not medias or random.random() < 0.2:
        return random.choice(opciones)
    mejor = max(medias.values())
    pesos = [max(medias.get(t["id"], mejor), 1) ** 0.5 for t in opciones]
    return random.choices(opciones, weights=pesos)[0]


def pick_idea(topic: dict, history: list) -> dict | None:
    """Para temas con lista de "ideas", elige una que no se haya usado en los últimos 30 posts.
    Cada idea es {"idea": texto, "vistas": capturas que la muestran ([] = post sin imagen)}."""
    if not topic.get("ideas"):
        return None
    usadas = {h.get("idea") for h in history[-30:]}
    return random.choice([i for i in topic["ideas"] if i["idea"] not in usadas] or topic["ideas"])


def best_posts(history: list, n: int = 3) -> str:
    """Los posts con mejor puntaje, para que la IA vea qué conecta con el público."""
    # Solo formatos actuales y sin muletillas, para no premiar posts de la plantilla antigua.
    ids = {t["id"] for t in TEMAS}
    candidatos = [h for h in measured(history) if h.get("tema") in ids and not post_problems(h["texto"])]
    top = sorted(candidatos, key=engagement, reverse=True)[:n]
    return "\n---\n".join(
        f"[{h['stats'].get('views', 0)} vistas, {h['stats'].get('replies', 0)} respuestas]\n{h['texto']}"
        for h in top
    )


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
        anual = None
        try:
            # La gráfica anual de la brecha (primera tarjeta) trae actual, promedio, máxima y mínima.
            page.get_by_text("Ver gráfica anual").first.click()
            page.wait_for_selector("[role=dialog]", timeout=10000)
            page.wait_for_timeout(1500)
            anual = page.inner_text("[role=dialog]")
        except Exception as e:  # noqa: BLE001
            log(f"Aviso: no se pudo leer la gráfica anual de la brecha ({e}).")
        browser.close()

    # Limpia líneas vacías y repetidas (menús, botones). Las cifras no se quitan aunque se
    # repitan: si la tasa no cambió, la de ayer es igual a la de hoy y hace falta para comparar.
    lines, seen = [], set()
    for line in (l.strip() for l in text.splitlines()):
        if line and (line not in seen or re.fullmatch(r"[\d.,]+", line)):
            seen.add(line)
            lines.append(line)
    clean = "\n".join(lines)
    if anual:
        stats = dict(re.findall(r"(ACTUAL|PROMEDIO|M[ÁA]XIMA|M[ÍI]NIMA)\s+([\d.,]+%)", anual))
        if stats:
            clean += "\nBrecha BCV vs Binance en el último año: " + ", ".join(
                f"{k.lower()} {v}" for k, v in stats.items()
            )
    return clean[:3000] if re.search(r"\d+[.,]\d{2}", clean) else None


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


def select_country(page, code: str) -> None:
    """En /mercado el país sale por ubicación (desde GitHub Actions no es Venezuela). La opción
    puede venir marcada sin aplicarse, así que se cambia a otra y luego a la pedida."""
    otro = "MX" if code != "MX" else "VE"
    page.select_option("main select", otro)
    page.wait_for_timeout(300)
    page.select_option("main select", code)
    page.wait_for_timeout(2500)  # carga los precios del país


def used_views(h: dict) -> list:
    """Vistas usadas en un post del historial (los posts antiguos guardaban solo "vista")."""
    return h.get("vistas") or ([h["vista"]] if h.get("vista") else [])


def plan_images(topic: dict, history: list) -> tuple[list[dict], str]:
    """Elige las imágenes del post: "imagenes" vistas distintas del tema (1 si no lo define),
    priorizando las que no salieron en los últimos 6 posts y, si no alcanza, las usadas hace más
    tiempo. Cada imagen es {"vista", "rango"} (el rango solo en gráficas). El modo (claro u
    oscuro) se alterna con el del post anterior, para que la misma vista no se vea igual."""
    estilo = "light" if (history[-1].get("estilo") if history else "dark") == "dark" else "dark"
    if not topic["vistas"]:
        return [], estilo

    def last_use(v: str) -> int:
        for i, h in enumerate(reversed(history)):
            if v in used_views(h):
                return i
        return len(history) + 1

    vistas = list(topic["vistas"])
    random.shuffle(vistas)
    frescas = [v for v in vistas if last_use(v) >= 6]
    usadas = sorted((v for v in vistas if last_use(v) < 6), key=last_use, reverse=True)
    elegidas = (frescas + usadas)[: topic.get("imagenes", 1)]
    shots = []
    for v in elegidas:
        shot = {"vista": v}
        if VISTAS[v].get("rangos"):
            shot["rango"] = random.choice(VISTAS[v]["rangos"])
        shots.append(shot)
    return shots, estilo


def shot_desc(shot: dict) -> str:
    return VISTAS[shot["vista"]]["desc"].format(rango=RANGOS.get(shot.get("rango"), ""))


def capture_shot(page, shot: dict) -> dict:
    """Prepara la página para una captura y devuelve el recorte (clip) a fotografiar."""
    view = VISTAS[shot["vista"]]
    page.goto(f"{SITE}{view['path']}", wait_until="networkidle", timeout=45000)
    try:
        page.wait_for_function(r"() => /\d+[.,]\d{2}/.test(document.body.innerText)", timeout=15000)
    except Exception:  # noqa: BLE001
        pass  # /noticias y /mercado no siempre tienen cifras
    if "pais" in view:
        select_country(page, view["pais"])
    if "clic" in view:
        page.get_by_text(view["clic"]).first.click()
    page.wait_for_timeout(1500)  # deja terminar las animaciones de las cifras

    if "tarjeta" in view or "hasta_tarjeta" in view:
        i = view.get("tarjeta", view.get("hasta_tarjeta"))
        box = page.evaluate(
            """(i) => {
                const c = document.querySelectorAll('main [class*="rounded-[28px]"]')[i];
                if (!c) return null;
                const r = c.getBoundingClientRect();
                return {top: r.top + scrollY, bottom: r.bottom + scrollY};
            }""",
            i,
        )
        if not box:
            raise RuntimeError(f"no se encontró la tarjeta {i}")
        # La primera tarjeta (y el resumen) incluyen el aviso "Actualizado: ..." de arriba.
        top = 0 if i == 0 or "hasta_tarjeta" in view else max(0, box["top"] - 16)
        return {"x": 0, "y": top, "width": 430, "height": box["bottom"] + 20 - top}
    if "grafica" in view:
        page.get_by_text("Ver gráfica anual").nth(view["grafica"]).click()
        page.wait_for_selector("[role=dialog]", timeout=10000)
        page.wait_for_timeout(1500)
        if shot.get("rango"):
            page.locator("[role=dialog]").get_by_text(shot["rango"], exact=True).click()
            page.wait_for_timeout(1500)
        # Del borde superior del panel hasta el eje de fechas de la gráfica.
        box = page.evaluate(
            """() => {
                const d = document.querySelector('[role=dialog]');
                const r = d.getBoundingClientRect();
                const svg = [...d.querySelectorAll('svg')].sort((a, b) =>
                    b.getBoundingClientRect().height - a.getBoundingClientRect().height)[0];
                const n = [...d.querySelectorAll('p, span, div')]
                    .filter(e => /^Basado en el hist/.test((e.innerText || '').trim())).pop();
                const bottom = n ? n.getBoundingClientRect().bottom + 16
                    : svg ? svg.getBoundingClientRect().bottom + 24 : r.top + 980;
                return {top: r.top, bottom: Math.min(bottom, r.bottom)};
            }"""
        )
        return {"x": 0, "y": box["top"], "width": 430, "height": box["bottom"] - box["top"]}
    if "hasta" in view:
        bottom = page.evaluate(
            """(t) => {
                const e = [...document.querySelectorAll('button, a, span')]
                    .find(e => e.innerText && e.innerText.trim().toUpperCase() === t);
                return e ? e.getBoundingClientRect().bottom + scrollY : 460;
            }""",
            view["hasta"],
        )
        return {"x": 0, "y": 0, "width": 430, "height": bottom + 30}
    return {"x": 0, "y": 0, "width": 430, "height": view["alto"]}


def capture_images(shots: list[dict], estilo: str) -> list[pathlib.Path]:
    """Captura las imágenes del post en public/tasas.png, tasas2.png, ... Si una falla, se
    publica con las demás."""
    from playwright.sync_api import sync_playwright

    paths = []
    if not shots:
        return paths
    IMAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for n, shot in enumerate(shots, start=1):
            out = IMAGE_FILE if n == 1 else IMAGE_FILE.with_name(f"tasas{n}.png")
            try:
                # Ventana alta para que la barra de navegación fija no tape las tarjetas de abajo.
                page = browser.new_page(
                    locale="es-VE", viewport={"width": 430, "height": 1600},
                    device_scale_factor=2, color_scheme=estilo,
                )
                clip = capture_shot(page, shot)
                page.screenshot(path=str(out), clip=clip, full_page=True)
                page.close()
                paths.append(out)
                log(f"Imagen generada ({shot['vista']} {shot.get('rango', '')} {estilo}): {out}")
            except Exception as e:  # noqa: BLE001
                log(f"Aviso: no se pudo generar la imagen {shot['vista']} ({e}).")
        browser.close()
    return paths


def git(*args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=cotizados-bot",
         "-c", "user.email=cotizados-bot@users.noreply.github.com", *args],
        cwd=ROOT, check=True, capture_output=True, text=True,
    ).stdout.strip()


def publish_images(paths: list[pathlib.Path]) -> list[str]:
    """Sube las imágenes al repo (public/) y devuelve sus URLs públicas en
    raw.githubusercontent.com: la API de Threads solo acepta imágenes desde una URL.
    Requiere que el repo sea público. Las URLs llevan el commit para evitar la caché de GitHub."""
    rels = [p.relative_to(ROOT).as_posix() for p in paths]
    git("add", *rels)
    # Si las imágenes son idénticas a las ya subidas no hay nada que commitear.
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT).returncode:
        git("commit", "-m", f"chore: imagen de tasas {dt.datetime.now(TZ_VE):%F}", "--", *rels)
        git("push")
    sha = git("rev-parse", "HEAD")
    repo = os.getenv("GITHUB_REPOSITORY") or re.sub(
        r"^.*github\.com[:/]|\.git$", "", git("remote", "get-url", "origin")
    )
    urls = [f"https://raw.githubusercontent.com/{repo}/{sha}/{rel}" for rel in rels]
    # Espera a que GitHub sirva los archivos antes de pasárselos a Threads.
    for url in urls:
        for _ in range(12):
            if requests.head(url, timeout=15).status_code == 200:
                break
            time.sleep(5)
        else:
            raise RuntimeError(f"la imagen no está accesible en {url} (¿el repo es público?)")
    return urls


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
    # En la página la cifra del euro viene partida en dos líneas ("993" y ",09"), seguida de la de ayer.
    eur = re.search(r"Euro BCV\s+(\d{1,3}(?:\.\d{3})*)\s*(,\d{2})\s+" + num + "?", real_data)
    if eur:
        eur_ayer = parse_num(eur.group(3)) if eur.group(3) else None
        eur = parse_num(eur.group(1) + eur.group(2))
        line = f"Euro BCV hoy: {fmt_bs(eur)} Bs"
        if eur_ayer and abs(eur - eur_ayer) >= 0.005:
            verbo = "subió" if eur > eur_ayer else "bajó"
            line += f" ({verbo} {fmt_bs(abs(eur - eur_ayer))} Bs respecto a ayer)"
        elif eur_ayer:
            line += " (sin cambios respecto a ayer)"
        lines.append(line)
    if p2p:
        p2p = parse_num(p2p.group(1))
        lines.append(f"USDT Binance P2P: {fmt_bs(p2p)} Bs")
        lines.append(f"Brecha BCV vs P2P: {fmt_bs((p2p - bcv) / bcv * 100)}%")
    for usd in (1, 5, 10, 20, 50, 100):
        line = f"{usd} $ = {fmt_bs(usd * bcv)} Bs a tasa BCV"
        if eur:
            line += (f" | {fmt_bs(usd * eur)} Bs si el negocio cobra a tasa euro BCV"
                     f" (diferencia con tasa BCV {fmt_bs(usd * (eur - bcv))} Bs)")
        if p2p:
            line += (f" | {fmt_bs(usd * p2p)} Bs a tasa P2P"
                     f" | diferencia BCV vs P2P {fmt_bs(usd * (p2p - bcv))} Bs")
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


def generate_post(topic: dict, history: list, real_data: str | None, shots: list[dict]) -> str:
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
        + (f"El post lleva {'una imagen' if len(shots) == 1 else 'un carrusel de imágenes'}: "
           f"{'; '.join(map(shot_desc, shots))} (no las describas).\n\n"
           if shots else "El post va sin imagen.\n\n") +
        f"Primeras líneas de los últimos posts (escribe algo que no se parezca a ninguna):\n{ganchos}"
    )
    mejores = best_posts(history)
    if mejores:
        user_msg += (
            "\n\nLos posts que mejor han funcionado con el público. Fíjate en qué los hizo "
            f"conectar (tono, gancho, largo), pero no copies sus frases:\n{mejores}"
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


def wait_container(creation_id: str, token: str) -> None:
    """Meta recomienda esperar a que el contenedor esté listo antes de usarlo."""
    for _ in range(10):
        time.sleep(6)
        status = threads_request("GET", f"/{creation_id}", token, fields="status,error_message")
        if status.get("status") == "FINISHED":
            return
        if status.get("status") in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Contenedor con error: {status}")


def publish_to_threads(
    text: str, token: str, reply_to_id: str | None = None, image_urls: list[str] | None = None
) -> str:
    """Publica texto, texto con una imagen o, con varias imágenes, un carrusel."""
    params = {"media_type": "TEXT", "text": text}
    if image_urls and len(image_urls) == 1:
        params.update(media_type="IMAGE", image_url=image_urls[0])
    elif image_urls:
        # Carrusel: primero un contenedor por imagen y luego el del carrusel con sus hijos.
        children = []
        for url in image_urls:
            child = threads_request("POST", "/me/threads", token, media_type="IMAGE",
                                    image_url=url, is_carousel_item="true")["id"]
            wait_container(child, token)
            children.append(child)
        params.update(media_type="CAROUSEL", children=",".join(children))
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
    wait_container(creation_id, token)
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
    if token:
        update_stats(history, token)
    log(f"Rendimiento por formato (posts con más de 24 h):\n{stats_report(history)}")
    topic = pick_topic(history)
    idea = pick_idea(topic, history)
    log(f"Tema del día: {topic['id']}" + (f" (idea: {idea['idea']})" if idea else ""))
    if idea:
        topic = {**topic, "angulo": f"{topic['angulo']}\nIdea de hoy: {idea['idea']}."}
        if "vistas" in idea:  # [] = sin imagen
            topic["vistas"] = idea["vistas"]

    shots, estilo = plan_images(topic, history)
    log(f"Imágenes ({estilo}): {shots or 'ninguna'}")
    # Según el tema, los datos son las tasas, las mejoras de la app o los precios del mercado.
    fetchers = {"noticias": fetch_news, "tasas": fetch_real_data, "ninguno": lambda: None}
    data = fetchers[topic["datos"]]()
    text = generate_post(topic, history, data, shots)
    log(f"Post generado ({len(text)} caracteres):\n{text}\n")
    reply_text = topic["respuesta"].format(link=topic["link"])
    log(f"Respuesta con el enlace:\n{reply_text}\n")
    images = capture_images(shots, estilo)

    if DRY_RUN:
        log("DRY_RUN activo: no se publica ni se guarda historial.")
        return 0

    post_id = None
    if images:
        try:
            image_urls = publish_images(images)
            log(f"Imágenes publicadas: {image_urls}")
            try:
                post_id = publish_to_threads(text, token, image_urls=image_urls)
            except Exception as e:  # noqa: BLE001
                if len(image_urls) == 1:
                    raise
                log(f"Aviso: no se pudo publicar el carrusel ({e}); se publica con una imagen.")
                post_id = publish_to_threads(text, token, image_urls=image_urls[:1])
        except Exception as e:  # noqa: BLE001
            log(f"Aviso: no se pudo publicar con imágenes ({e}); se publica solo el texto.")
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
            "vistas": [s["vista"] for s in shots],
            "rangos": [s.get("rango") for s in shots],
            "estilo": estilo,
            "idea": idea["idea"] if idea else None,
            "texto": text,
        }
    )
    save_history(history)
    maybe_refresh_token(token)
    return 0


def print_stats() -> int:
    """python bot/post.py --stats: actualiza las estadísticas, imprime el informe y los mejores
    posts, y guarda el historial. No publica nada."""
    token = os.getenv("THREADS_ACCESS_TOKEN", "").strip()
    if not token:
        log("Falta THREADS_ACCESS_TOKEN.")
        return 1
    history = load_history()
    update_stats(history, token)
    save_history(history)
    print(f"\nRendimiento por formato (posts con más de 24 h):\n{stats_report(history)}")
    print(f"\nMejores posts:\n{best_posts(history, 5) or '(aún no hay)'}")
    return 0


if __name__ == "__main__":
    sys.exit(print_stats() if "--stats" in sys.argv else main())
