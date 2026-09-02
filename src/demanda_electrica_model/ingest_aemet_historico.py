"""Backfill del histórico de climatología diaria de AEMET para las estaciones
seleccionadas.

A diferencia de `ingest_aemet.py` (captura diaria incremental, sin backfill
posible), este script es un backfill puntual sobre un rango fijo: se puede
relanzar más adelante para extender el rango a fechas más recientes.

Notas de lo aprendido explorando en `notebooks/ingest.ipynb`:
- El endpoint limita el rango por petición a 6 meses exactos, hay que
  trocear en ventanas de esa duración.
- El producto diario se publica con unos días de retraso (control de
  calidad); pedir hasta "hoy" siempre deja unos días sin datos al final,
  no es un fallo.
- Las estaciones climatológicas (`idema`) no son los municipios de
  predicción: se han confirmado aparte contra `maestro/estaciones`,
  priorizando la estación con núcleo urbano salvo cuando devolvía huecos
  en el reporte reciente (caso Barcelona: Port Olímpic descartada por eso,
  se usa Aeropuerto).
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ESTACIONES = {
    "Madrid": "3129",
    "Barcelona": "0076",
    "Valencia": "8414A",
    "Sevilla": "5790Y",
    "Bilbao": "1082",
    "Zaragoza": "9434P",
}

FECHA_INICIO_DEFECTO = "2022-01-01"
RANGO_MAX_DIAS = 182  # el endpoint no admite más de 6 meses por petición

DATA_DIR = Path("data/aemet_historico")
OUTPUT_PATH = DATA_DIR / "temperaturas_diarias.csv"


def _session() -> requests.Session:
    """Sesión con reintentos: un corte de red breve no debe tirar el backfill entero."""
    retry = Retry(
        total=4,
        backoff_factor=5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def ventanas(fechaini: str, fechafin: str):
    """Trocea [fechaini, fechafin] en ventanas de como mucho RANGO_MAX_DIAS días."""
    inicio = datetime.strptime(fechaini, "%Y-%m-%d").date()
    fin = datetime.strptime(fechafin, "%Y-%m-%d").date()
    while inicio <= fin:
        cierre = min(inicio + timedelta(days=RANGO_MAX_DIAS), fin)
        yield inicio.isoformat(), cierre.isoformat()
        inicio = cierre + timedelta(days=1)


def fetch_historico_estacion(
    idema: str, fechaini: str, fechafin: str, api_key: str, session: requests.Session
) -> list[dict]:
    """Histórico diario de una estación en [fechaini, fechafin]. Patrón de dos pasos por ventana."""
    registros = []
    for ini, fin in ventanas(fechaini, fechafin):
        url = (
            "https://opendata.aemet.es/opendata/api/valores/climatologicos/diarios/datos/"
            f"fechaini/{ini}T00:00:00UTC/fechafin/{fin}T23:59:59UTC/estacion/{idema}"
        )
        step1 = session.get(url, params={"api_key": api_key}, timeout=15).json()
        if step1.get("estado") != 200:
            raise RuntimeError(f"AEMET step1 falló para {idema} {ini}->{fin}: {step1}")

        resp2 = session.get(step1["datos"], timeout=15)
        # AEMET declara charset=ISO-8859-15 en el header pero manda iso-8859-1 de verdad
        resp2.encoding = "iso-8859-1"
        registros.extend(resp2.json())
    return registros


def backfill(fechaini: str, fechafin: str, api_key: str) -> tuple[pd.DataFrame, list[str]]:
    session = _session()
    todos_los_registros = []
    fallos = []

    for ciudad, idema in ESTACIONES.items():
        try:
            registros = fetch_historico_estacion(idema, fechaini, fechafin, api_key, session)
        except Exception as exc:
            print(f"error: fallo capturando {ciudad} ({idema}): {exc}", file=sys.stderr)
            fallos.append(ciudad)
            continue

        for r in registros:
            r["ciudad"] = ciudad
        todos_los_registros.extend(registros)
        print(f"{ciudad} ({idema}): {len(registros)} registros")

    return pd.DataFrame(todos_los_registros), fallos


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fechaini", default=FECHA_INICIO_DEFECTO)
    parser.add_argument("--fechafin", default=date.today().isoformat())
    args = parser.parse_args()

    load_dotenv()
    api_key = os.environ["AEMET_API_KEY"]

    df, fallos = backfill(args.fechaini, args.fechafin, api_key)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUTPUT_PATH, index=False, encoding="utf-8")

    if fallos:
        print(f"Guardado en {OUTPUT_PATH} CON FALLOS en: {', '.join(fallos)}", file=sys.stderr)
        sys.exit(1)

    print(f"Guardado en {OUTPUT_PATH} ({len(df)} registros)")


if __name__ == "__main__":
    main()