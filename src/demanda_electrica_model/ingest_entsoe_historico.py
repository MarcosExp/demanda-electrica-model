"""Backfill del histórico de demanda eléctrica de ENTSO-E para España.

Sustituye a REE (`apidatos.ree.es`) como fuente del target, descartada por no
poder auditarse (ver README, sección «REE — descartada»). ENTSO-E sí se puede
verificar: ámbito geográfico explícito (código de área por país, sin filtros
que se ignoran en silencio), huecos que aparecen como filas ausentes contra
una rejilla temporal completa (igual que REE, hay que comprobarlo así — no
hay nulos explícitos), y un endpoint de previsión independiente
(`query_load_forecast`, la previsión propia del operador) que sirve de
Baseline 2.

Como `ingest_aemet_historico.py`, es un backfill puntual (no cron): se puede
relanzar más adelante para extender el rango a fechas más recientes.

Notas de lo aprendido explorando en vivo contra la API (2026-08-27):
- Resolución: horaria (24 valores/día) hasta 2022-05-22, cuarto-horaria
  (96 valores/día, PT15M) desde 2022-05-23 en adelante. El script no
  homogeniza esto — se deja tal cual devuelve la API y se documenta aquí;
  homogenizar (p.ej. agregar a horario) es decisión de la fase de features.
- No hace falta trocear el rango en ventanas: una petición de 4 años
  (2022-2026) funciona en una sola llamada (el cliente `entsoe-py` gestiona
  la paginación internamente). El cliente además reintenta solo
  (`retry_count`, `retry_delay` en el constructor).
- Huecos reales confirmados en 2022-2025 (`Actual Load`): un valor suelto
  faltante cada 1 de enero (2023, 2024, 2025) y en 2023-11-21, y un hueco
  grande el 2025-04-28 (~46 de 96 valores) que coincide con el apagón
  ibérico de esa fecha — es un hueco real de la fuente, no un fallo de
  ingesta. Sin duplicados, sin nulos explícitos.
- `query_load_forecast` (previsión día-adelanto del operador, `process_type`
  por defecto "A01") tiene la misma resolución y cobertura que la real.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from entsoe import EntsoePandasClient
from entsoe.exceptions import NoMatchingDataError

PAIS = "ES"
ZONA_HORARIA = "Europe/Madrid"

FECHA_INICIO_DEFECTO = "2022-01-01"

DATA_DIR = Path("data/entsoe")
OUTPUT_PATH = DATA_DIR / "demanda_historica.csv"


def _cliente() -> EntsoePandasClient:
    load_dotenv()
    # retry_count/retry_delay por defecto de la librería (3 intentos, 10s):
    # cubre cortes de red breves sin tirar el backfill entero.
    return EntsoePandasClient(retry_count=4, retry_delay=10)


def backfill(fechaini: str, fechafin: str) -> tuple[pd.DataFrame, list[str]]:
    client = _cliente()
    start = pd.Timestamp(fechaini, tz=ZONA_HORARIA)
    # el extremo final de query_load es exclusivo; para incluir fechafin
    # completo se pide hasta el día siguiente a medianoche.
    end = pd.Timestamp(fechafin, tz=ZONA_HORARIA) + pd.Timedelta(days=1)

    fallos = []
    series = {}

    for nombre, columna, fetch in (
        ("real", "actual_load_mw", lambda: client.query_load(PAIS, start=start, end=end)),
        (
            "previsión",
            "forecast_load_mw",
            lambda: client.query_load_forecast(PAIS, start=start, end=end),
        ),
    ):
        try:
            df = fetch()
            df.columns = [columna]
            series[columna] = df[columna]
            print(f"{nombre}: {len(df)} registros ({df.index.min()} -> {df.index.max()})")
        except NoMatchingDataError:
            print(f"error: ENTSO-E no devolvió datos de {nombre} para el rango pedido", file=sys.stderr)
            fallos.append(nombre)
        except Exception as exc:
            print(f"error: fallo capturando {nombre}: {exc}", file=sys.stderr)
            fallos.append(nombre)

    if not series:
        return pd.DataFrame(), fallos

    # outer join: real y previsión pueden traer huecos en fechas distintas,
    # no se descarta ninguna fila por eso.
    df = pd.concat(series.values(), axis=1, keys=series.keys())
    df.columns = list(series.keys())
    df.index.name = "timestamp"
    return df.sort_index(), fallos


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fechaini", default=FECHA_INICIO_DEFECTO)
    parser.add_argument("--fechafin", default=date.today().isoformat())
    args = parser.parse_args()

    df, fallos = backfill(args.fechaini, args.fechafin)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUTPUT_PATH, encoding="utf-8")

    if fallos:
        print(f"Guardado en {OUTPUT_PATH} CON FALLOS en: {', '.join(fallos)}", file=sys.stderr)
        sys.exit(1)

    print(f"Guardado en {OUTPUT_PATH} ({len(df)} registros)")


if __name__ == "__main__":
    main()
