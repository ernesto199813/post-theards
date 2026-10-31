# Bot diario de Threads — Cotizados

Publica **3 posts al día (8:47 a. m., 1:17 p. m. y 7:47 p. m., hora de Venezuela)** en Threads promocionando
[cotizados.vercel.app](https://cotizados.vercel.app). El texto lo redacta Gemini en primera persona,
como el creador de la app, rotando formatos narrativos (noticia rápida, ¿cuánto es?, mercado en
desarrollo, cómo funciona, tus cuentas, detrás de cámaras, situación real, brecha, truco). Los formatos que
mejor funcionan según las estadísticas de Threads salen más seguido. De martes a viernes, el
primer post del día siempre es la variación de la tasa. Corre gratis en GitHub Actions.

```
GitHub Actions (cron) ─▶ bot/post.py ─▶ IA: Gemini (redacta) ─▶ Threads API (publica)
                                      └▶ data/history.json (historial y estadísticas)
```

## Contexto del mercado venezolano

El bot lee esta sección y se la pasa a la IA en cada post, para que redacte entendiendo cómo se
usan las tasas en la calle. Edítala aquí para cambiar lo que la IA tiene presente (solo el texto
entre los dos comentarios).

<!-- contexto:inicio -->
- Los sueldos y pagos de los empleados del día a día se calculan en dólares a **tasa BCV** (el
  dólar oficial). Es la referencia de lo que la gente cobra.
- Muchos negocios ponen sus precios a **tasa euro BCV**, que es más alta que la del dólar BCV. Así,
  el mismo producto marcado en "dólares" sale más caro en bolívares. Quien cobra a dólar BCV y
  paga a tasa euro pierde en esa diferencia.
- En la práctica, a veces la que manda en el mercado es la **tasa Binance P2P (USDT)** o la
  **tasa euro**, no la del dólar BCV.
- El **dólar en efectivo** no tiene una tasa fija: cada comercio o persona lo pone como le
  conviene. Por eso conviene saber las tasas antes de pagar, cobrar o cambiar.
- Cotizados existe para eso: ver todas las tasas juntas (dólar BCV, euro BCV, Binance P2P), la
  brecha entre ellas y cómo ha variado en el año, y calcular con la tasa que de verdad te estén
  aplicando (Tasa Libre).
- Con una cuenta gratis, Cotizados también sirve para **llevar tus cuentas**: en Inicio ves el
  balance del mes con ingresos y gastos, y anotas cada movimiento en USDT, bolívares o efectivo
  (la app muestra su equivalente en bolívares), con de dónde vino (sueldo, pago de cliente,
  venta, transferencia). Útil para quien cobra en varias monedas y no sabe en qué se le va la plata.
- Tono: cercano e informado, como alguien que entiende cómo se mueve el mercado y lo explica
  claro. Respetuoso con la situación de la gente: se habla de plata que cuesta ganar. Humor solo
  si sale natural, nunca burlón.
- **Nunca decir "paralelo"** (dólar paralelo, mercado paralelo) ni "dólar negro": está prohibido
  en Venezuela. El tipo de cambio más alto se nombra como **USDT** (Binance P2P), y la brecha es
  "entre el BCV y el USDT".
- Evitar clichés gastados: el vuelto en caramelos, el tío del grupo de WhatsApp, "hacer
  malabares", "la quincena se esfuma".
<!-- contexto:fin -->

## Calendario

- **Horarios:** 8:47 a. m., 1:17 p. m. y 7:47 p. m. (Venezuela), en `HORARIOS` de
  `bot/schedule.py`. El workflow está programado cada 30 minutos, pero GitHub solo dispara
  algunas ejecuciones (a veces con 5-6 horas entre una y otra). Por eso `bot/schedule.py`:
  - publica si hoy ya pasaron más horarios que posts publicados, así que se recuperan los
    horarios perdidos (con al menos 45 minutos entre posts);
  - si todavía no toca pero el próximo horario llega en menos de 5 h 30 min, la ejecución espera
    dormida y publica a la hora exacta (una ejecución de las 7:46 publica a las 8:47). En repos
    públicos los minutos de GitHub Actions son gratis.
- **Martes a viernes:** el primer post del día es la variación de la tasa BCV respecto a ayer.
- **Tasas en todos los posts:** cada post nombra el dólar BCV, el euro BCV y el USDT de Binance
  P2P de ese momento; si la IA no copia las tres cifras, se le pide otra versión.
- **Frases nuevas:** la IA ve los ganchos y las preguntas de cierre de los últimos posts para no
  repetirlos, no puede abrir con "Dólar BCV en…", no repite el argumento "cobras a BCV y pagas a
  tasa euro" si salió en los últimos 3 posts, y en "detrás de cámaras" no repite mejoras
  contadas en los últimos 14 días.
- **Resto de posts:** sorteo ponderado entre los formatos según su rendimiento (vistas e
  interacciones de los últimos 14 días), sin repetir los formatos de los últimos 2 posts.
  "Detrás de cámaras" sale como mucho cada 4 posts; "Cómo funciona" y "Mercado", cada 6.

## 1. Crear la app de Threads y obtener el token

1. Entra a <https://developers.facebook.com/apps> → **Crear app** → caso de uso
   **"Acceder a la API de Threads"**.
2. En **Casos de uso → Personalizar**, activa los permisos `threads_basic`,
   `threads_content_publish` y `threads_manage_insights` (para leer las estadísticas de cada post).
3. En **Roles de la app → Roles → Probadores de Threads**, agrega tu cuenta de Threads y acepta
   la invitación desde la app de Threads (*Configuración → Cuenta → Permisos de sitios web*).
4. Abre el **Graph API Explorer**, cambia el selector de host a **threads.net**, elige tu app y
   pulsa **Generar token** (dura 1 hora).
5. Cámbialo por uno de **60 días**:
   ```bash
   curl "https://graph.threads.net/access_token?grant_type=th_exchange_token&client_secret=TU_APP_SECRET&access_token=TOKEN_CORTO"
   ```
   El `access_token` de la respuesta es tu `THREADS_ACCESS_TOKEN`.

> Mientras la app esté en modo desarrollo, solo publica en cuentas añadidas como probadoras.
> Para tu propia cuenta eso es suficiente; no necesitas revisión de Meta.

## 2. Clave de IA (Google AI Studio, gratis)

Crea una clave en <https://aistudio.google.com/apikey> → **Create API key**. La capa gratuita
alcanza de sobra para un post al día. El modelo por defecto es `gemini-3.5-flash-lite`; puedes
cambiarlo con la variable `GEMINI_MODEL`.

## 3. Subir el repo y configurar secretos

```bash
git init && git add . && git commit -m "Bot de Threads para Cotizados"
gh repo create cotizados-threads-bot --public --source=. --push
```

En **Settings → Secrets and variables → Actions**:

| Tipo     | Nombre                 | Valor |
|----------|------------------------|-------|
| Secret   | `THREADS_ACCESS_TOKEN` | token de 60 días del paso 1 |
| Secret   | `GEMINI_API_KEY`       | clave de Google AI Studio del paso 2 |
| Secret   | `GH_PAT`               | token *fine-grained* de GitHub con permiso **Secrets: Read and write** sobre este repo (permite renovar solo el token de Threads) |
| Variable | `COTIZADOS_API_URL`    | *(opcional)* endpoint JSON con las tasas; si no existe, el bot lee la página `/cotizaciones` |
| Variable | `GEMINI_MODEL`         | *(opcional)* por defecto `gemini-3.5-flash-lite` |

## 4. Probar

**Actions → Post diario en Threads → Run workflow** con *dry_run* marcado: verás el post generado
en el log sin publicarlo. Desmárcalo para publicar de verdad. Desde ahí corre solo, 3 veces al día.

Local:
```bash
pip install -r requirements.txt
python -m playwright install chromium
# Rellena el archivo .env de la raíz (GEMINI_API_KEY, THREADS_ACCESS_TOKEN, DRY_RUN=true)
python bot/post.py
```

## Cómo funciona

- **Datos reales:** cada día el bot abre `/cotizaciones` en un Chromium sin interfaz, espera a que
  el JavaScript cargue las tasas y le pasa ese texto a Gemini, que cita 1 o 2 cifras tal cual
  aparecen (BCV, USDT). Si defines `COTIZADOS_API_URL`, usa ese JSON en vez de la página (más
  rápido y estable). Si no logra leer cifras, publica igual pero sin números: nunca los inventa.
  El log de cada ejecución muestra el texto leído, para que verifiques que es correcto.
- **Imágenes del post:** cada tema tiene varias vistas posibles de la web (tarjetas y resumen de
  `/cotizaciones`, gráficas anuales del dólar, euro, Binance y la brecha con distintos rangos,
  la calculadora, `/noticias` y `/mercado`; ver `VISTAS` y `"vistas"` en `TEMAS`). El bot prioriza
  las que no salieron en los últimos 6 posts y alterna modo claro y oscuro. Algunos temas
  publican un carrusel (`"imagenes"` en `TEMAS`).
- **Videos (apagados):** rindieron menos que las fotos, así que todos los posts salen con
  imágenes. Para volver a probarlos, pon `USAR_VIDEOS = True` en `bot/post.py`. Encendidos, los
  temas con `"videos"` en `TEMAS` pueden salir con un video corto (10-15 s) que
  graba la app en tamaño de teléfono: el recorrido por Cotizaciones, una gráfica anual pasando de
  1 mes a 1 año o las Noticias (guiones en `GUIONES`). Se convierte a MP4 con ffmpeg
  (`imageio-ffmpeg`) y se sube como `public/video.mp4`. Los primeros videos rindieron menos que
  las imágenes, así que solo el 20 % de esos posts sale en video; cuando hay al menos 3 posts
  medidos en video y 3 con imagen, la probabilidad sigue al puntaje de cada uno (entre 15 % y
  75 %). Si el video falla, el post sale con imágenes. Las pantallas que necesitan iniciar sesión
  (Inicio y Nuevo Movimiento) son capturas fijas en `public/app/`; para actualizarlas, reemplaza
  esos archivos. Las capturas van a `public/tasas.png`,
  `tasas2.png`…, se suben al repo y Threads las descarga de `raw.githubusercontent.com`. Por eso
  **el repo debe ser público** (las claves siguen en los secretos). Si el carrusel falla se
  publica con una imagen, y si las imágenes fallan, solo con texto.
- **Cálculos:** las cifras (variación del dólar y el euro BCV, brecha, montos de ejemplo a tasa
  BCV, euro y P2P, y la brecha del último año de la gráfica anual) las calcula Python; la IA solo
  las copia.
- **Novedades:** en "detrás de cámaras" el bot lee `/noticias` (registro de mejoras de la app) y
  comenta 1 o 2 mejoras reales en vez de citar tasas. "Mercado" se describe a grandes rasgos y
  sin precios, porque aún está en construcción.
- **Estadísticas:** antes de cada post el bot lee las vistas, likes y respuestas de los posts de
  los últimos 14 días y las guarda en el historial. `python bot/post.py --stats` muestra el
  rendimiento por formato y por medio (video, imagen o texto) sin publicar nada.
- **Enlace en la respuesta:** el post principal sale sin enlace (Threads suele dar menos
  alcance a los posts con enlaces externos) y justo después el bot responde a su propio post con
  el enlace de la sección. El texto de esa respuesta es `"respuesta"` en cada tema de `TEMAS`.
- **Token:** cada lunes el bot renueva el token de Threads (dura 60 días) y lo guarda solo en el
  secreto usando `GH_PAT`. Si falta `GH_PAT`, verás un aviso y tendrás que renovarlo a mano.
- **Historial:** `data/history.json` guarda los últimos 150 posts con sus estadísticas; se
  commitea tras cada publicación.
- **Personalizar:** el contexto del mercado se edita arriba en este README; los formatos en la
  lista `TEMAS` y las reglas de redacción en `SYSTEM_PROMPT` de `bot/post.py`; las horas de publicación
  en `HORARIOS` de `bot/schedule.py` (hora de Venezuela; agrega o quita horarios para publicar más
  o menos veces).

## Límites a tener en cuenta

- Threads permite hasta 250 publicaciones por día y 500 caracteres por post; el bot valida el largo.
- GitHub puede retrasar los cron horas (o saltarse alguno) y desactiva los workflows programados tras 60 días
  sin actividad en el repo; los commits diarios del historial lo mantienen activo.
