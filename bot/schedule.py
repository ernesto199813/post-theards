"""
¿Toca publicar ahora? El workflow corre cada 30 minutos (GitHub retrasa o se salta a veces los
cron programados) y este chequeo decide si hay que publicar: toca si hoy ya pasaron más HORARIOS
que posts publicados. Así se recuperan todos los horarios perdidos del día (los 3 posts salen
aunque GitHub se salte ejecuciones), dejando al menos ESPACIO_MIN minutos entre posts.

Solo usa la librería estándar, para correr antes de instalar dependencias:
  python bot/schedule.py   escribe due=true/false en $GITHUB_OUTPUT (y lo imprime)
"""

import datetime as dt
import json
import os
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
HISTORY_FILE = ROOT / "data" / "history.json"
TZ_VE = dt.timezone(dt.timedelta(hours=-4))
# Horas de publicación en Venezuela (hora, minuto).
HORARIOS = [(8, 47), (13, 17), (19, 47)]
# Minutos mínimos entre dos posts al recuperar horarios perdidos.
ESPACIO_MIN = 45


def slots_passed(now: dt.datetime) -> int:
    """Cuántos horarios de hoy ya pasaron."""
    return sum(now.replace(hour=h, minute=m, second=0, microsecond=0) <= now for h, m in HORARIOS)


def posts_today(history: list, now: dt.datetime) -> list[dt.datetime]:
    fechas = [dt.datetime.fromisoformat(h["fecha"]) for h in history if h.get("fecha")]
    return sorted(f for f in fechas if f.astimezone(TZ_VE).date() == now.date())


def is_due(history: list, now: dt.datetime | None = None) -> bool:
    now = now or dt.datetime.now(TZ_VE)
    hoy = posts_today(history, now)
    if len(hoy) >= slots_passed(now):
        return False
    return not hoy or now - hoy[-1] >= dt.timedelta(minutes=ESPACIO_MIN)


def main() -> None:
    history = json.loads(HISTORY_FILE.read_text(encoding="utf-8")) if HISTORY_FILE.exists() else []
    now = dt.datetime.now(TZ_VE)
    due = is_due(history, now)
    print(f"{now:%Y-%m-%d %H:%M} VE · horarios pasados: {slots_passed(now)} · posts hoy: "
          f"{len(posts_today(history, now))} · {'toca publicar' if due else 'nada que publicar'}")
    if os.getenv("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write(f"due={'true' if due else 'false'}\n")


if __name__ == "__main__":
    main()
