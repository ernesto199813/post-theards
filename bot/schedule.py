"""
¿Toca publicar ahora? El workflow corre cada 30 minutos (GitHub retrasa o se salta a veces los
cron programados) y este chequeo decide si hay que publicar: toca si ya pasó la hora de alguno de
los HORARIOS de hoy y todavía no hay un post desde esa hora. Así un cron perdido se recupera en la
siguiente ejecución, sin publicar dos veces el mismo horario.

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


def last_slot(now: dt.datetime) -> dt.datetime | None:
    """La hora de publicación más reciente de hoy que ya pasó (None antes de la primera)."""
    pasados = [now.replace(hour=h, minute=m, second=0, microsecond=0) for h, m in HORARIOS]
    pasados = [s for s in pasados if s <= now]
    return pasados[-1] if pasados else None


def is_due(history: list, now: dt.datetime | None = None) -> bool:
    now = now or dt.datetime.now(TZ_VE)
    slot = last_slot(now)
    if not slot:
        return False
    return not any(dt.datetime.fromisoformat(h["fecha"]) >= slot for h in history if h.get("fecha"))


def main() -> None:
    history = json.loads(HISTORY_FILE.read_text(encoding="utf-8")) if HISTORY_FILE.exists() else []
    now = dt.datetime.now(TZ_VE)
    due = is_due(history, now)
    print(f"{now:%Y-%m-%d %H:%M} VE · último horario: {last_slot(now) or 'ninguno aún'} · "
          f"{'toca publicar' if due else 'nada que publicar'}")
    if os.getenv("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write(f"due={'true' if due else 'false'}\n")


if __name__ == "__main__":
    main()
