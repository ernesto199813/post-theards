"""
¿Toca publicar ahora? GitHub dispara los cron programados cuando quiere: aunque el workflow está
programado cada 30 minutos, a veces pasan 5-6 horas entre ejecuciones. Por eso:

- Toca publicar si hoy ya pasaron más HORARIOS que posts publicados. Así se recuperan todos los
  horarios perdidos del día, dejando al menos ESPACIO_MIN minutos entre posts.
- Con --esperar, si todavía no toca pero el próximo horario llega en menos de ESPERA_MAX minutos,
  la ejecución espera dormida hasta esa hora y publica puntual (una ejecución que GitHub dispara
  a las 7:46 publica a las 8:47). En repos públicos los minutos de Actions son gratis.

Solo usa la librería estándar, para correr antes de instalar dependencias:
  python bot/schedule.py [--esperar]   escribe due=true/false en $GITHUB_OUTPUT (y lo imprime)
"""

import datetime as dt
import json
import os
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
HISTORY_FILE = ROOT / "data" / "history.json"
TZ_VE = dt.timezone(dt.timedelta(hours=-4))
# Horas de publicación en Venezuela (hora, minuto).
HORARIOS = [(8, 47), (13, 17), (19, 47)]
# Minutos mínimos entre dos posts al recuperar horarios perdidos.
ESPACIO_MIN = 45
# Máximo que una ejecución espera al próximo horario (el job tiene un límite de 6 h).
ESPERA_MAX = 330


def load_history() -> list:
    return json.loads(HISTORY_FILE.read_text(encoding="utf-8")) if HISTORY_FILE.exists() else []


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


def next_due(history: list, now: dt.datetime) -> dt.datetime | None:
    """El próximo momento de hoy en que tocará publicar (now si ya toca), o None."""
    if is_due(history, now):
        return now
    candidatos = [now.replace(hour=h, minute=m, second=0, microsecond=0) for h, m in HORARIOS]
    hoy = posts_today(history, now)
    if hoy:
        candidatos.append(hoy[-1] + dt.timedelta(minutes=ESPACIO_MIN))
    return min((c for c in candidatos if c > now and is_due(history, c)), default=None)


def main() -> None:
    history = load_history()
    now = dt.datetime.now(TZ_VE)
    due = is_due(history, now)
    print(f"{now:%Y-%m-%d %H:%M} VE · horarios pasados: {slots_passed(now)} · posts hoy: "
          f"{len(posts_today(history, now))} · {'toca publicar' if due else 'nada que publicar ahora'}")
    if not due and "--esperar" in sys.argv:
        proximo = next_due(history, now)
        if proximo and proximo - now <= dt.timedelta(minutes=ESPERA_MAX):
            print(f"Esperando hasta las {proximo:%H:%M} para publicar puntual…", flush=True)
            time.sleep(max(0, (proximo - dt.datetime.now(TZ_VE)).total_seconds()))
            # Mientras esperaba pudo publicarse algo (otra ejecución o en local): se revisa de nuevo.
            subprocess.run(["git", "pull", "-q", "--rebase"], cwd=ROOT, check=False)
            due = is_due(load_history())
            print(f"{dt.datetime.now(TZ_VE):%H:%M} VE · {'toca publicar' if due else 'ya se publicó'}")
        elif proximo:
            print(f"El próximo horario ({proximo:%H:%M}) está a más de {ESPERA_MAX} min: no se espera.")
    if os.getenv("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write(f"due={'true' if due else 'false'}\n")


if __name__ == "__main__":
    main()
