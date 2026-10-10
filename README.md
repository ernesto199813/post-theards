# Bot diario de Threads — Cotizados

Publica todos los días a las **8:47 a. m. (hora de Venezuela)** un post en Threads promocionando
[cotizados.vercel.app](https://cotizados.vercel.app). El texto lo redacta Claude, rotando 8 temas
(tasa BCV, calculadora, BCV vs USDT, noticias, comunidad, app móvil, tips, comerciantes) y sin
repetir los últimos posts. Corre gratis en GitHub Actions.

```
GitHub Actions (cron) ─▶ bot/post.py ─▶ Claude (redacta) ─▶ Threads API (publica)
                                      └▶ data/history.json (evita repetir)
```

## 1. Crear la app de Threads y obtener el token

1. Entra a <https://developers.facebook.com/apps> → **Crear app** → caso de uso
   **"Acceder a la API de Threads"**.
2. En **Casos de uso → Personalizar**, activa los permisos `threads_basic` y
   `threads_content_publish`.
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

## 2. Clave de Claude

Crea una API key en <https://console.anthropic.com> → **API Keys**. Un post al día con Haiku
cuesta centavos al mes.

## 3. Subir el repo y configurar secretos

```bash
git init && git add . && git commit -m "Bot de Threads para Cotizados"
gh repo create cotizados-threads-bot --private --source=. --push
```

En **Settings → Secrets and variables → Actions**:

| Tipo     | Nombre                 | Valor |
|----------|------------------------|-------|
| Secret   | `THREADS_ACCESS_TOKEN` | token de 60 días del paso 1 |
| Secret   | `ANTHROPIC_API_KEY`    | clave del paso 2 |
| Secret   | `GH_PAT`               | token *fine-grained* de GitHub con permiso **Secrets: Read and write** sobre este repo (permite renovar solo el token de Threads) |
| Variable | `COTIZADOS_API_URL`    | *(opcional)* endpoint JSON con las tasas; si no existe, el bot lee la página `/cotizaciones` |
| Variable | `CLAUDE_MODEL`         | *(opcional)* por defecto `claude-haiku-5-5` |

## 4. Probar

**Actions → Post diario en Threads → Run workflow** con *dry_run* marcado: verás el post generado
en el log sin publicarlo. Desmárcalo para publicar de verdad. Desde ahí corre solo cada día.

Local:
```bash
pip install -r requirements.txt
python -m playwright install chromium
export ANTHROPIC_API_KEY=... THREADS_ACCESS_TOKEN=...
DRY_RUN=true python bot/post.py
```

## Cómo funciona

- **Datos reales:** cada día el bot abre `/cotizaciones` en un Chromium sin interfaz, espera a que
  el JavaScript cargue las tasas y le pasa ese texto a Claude, que cita 1 o 2 cifras tal cual
  aparecen (BCV, USDT). Si defines `COTIZADOS_API_URL`, usa ese JSON en vez de la página (más
  rápido y estable). Si no logra leer cifras, publica igual pero sin números: nunca los inventa.
  El log de cada ejecución muestra el texto leído, para que verifiques que es correcto.
- **Token:** cada lunes el bot renueva el token de Threads (dura 60 días) y lo guarda solo en el
  secreto usando `GH_PAT`. Si falta `GH_PAT`, verás un aviso y tendrás que renovarlo a mano.
- **Historial:** `data/history.json` guarda los últimos 60 posts; se commitea tras cada publicación.
- **Personalizar:** edita la lista `TEMAS` y el `SYSTEM_PROMPT` en `bot/post.py`; cambia la hora en
  el `cron` del workflow (está en UTC).

## Límites a tener en cuenta

- Threads permite hasta 250 publicaciones por día y 500 caracteres por post; el bot valida el largo.
- GitHub puede retrasar los cron unos minutos y desactiva los workflows programados tras 60 días
  sin actividad en el repo; los commits diarios del historial lo mantienen activo.
