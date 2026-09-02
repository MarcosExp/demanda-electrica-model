"""Captura diaria de la predicción horaria de AEMET para municipios seleccionados.

AEMET no publica histórico de predicciones pasadas: este script hay que
ejecutarlo todos los días para no perder la predicción de ese día.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

MUNICIPIOS = {
    "28079": "Madrid",
    "08019": "Barcelona",
    "46250": "Valencia",
    "41091": "Sevilla",
    "48020": "Bilbao",
    "50297": "Zaragoza",
}

DATA_DIR = Path("data/aemet_predicciones")


def _session() -> requests.Session:
    """Sesión con reintentos: un corte de red breve no debe costar el día."""
    retry = Retry(
        total=4,
        backoff_factor=5,  # 5s, 10s, 20s, 40s entre intentos
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def _validar(pred: dict) -> None:
    """Comprueba que la respuesta trae lo que el resto del pipeline espera."""
    dias = pred.get("prediccion", {}).get("dia", [])
    if len(dias) < 2:
        raise ValueError(f"se esperaban al menos 2 días (D, D+1), llegaron {len(dias)}")

    temp_d1 = dias[1].get("temperatura", [])
    if len(temp_d1) < 24:
        raise ValueError(f"D+1 trae {len(temp_d1)} horas de temperatura, se esperaban 24")


def fetch_prediccion_horaria(municipio: str, api_key: str, session: requests.Session) -> dict:
    """Predicción horaria (D..D+2) de AEMET para un municipio. Patrón de dos pasos."""
    url = f"https://opendata.aemet.es/opendata/api/prediccion/especifica/municipio/horaria/{municipio}"
    step1 = session.get(url, params={"api_key": api_key}, timeout=15).json()
    if step1.get("estado") != 200:
        raise RuntimeError(f"AEMET step1 falló para {municipio}: {step1}")

    resp2 = session.get(step1["datos"], timeout=15)
    # AEMET declara charset=ISO-8859-15 en el header pero en realidad manda iso-8859-1
    resp2.encoding = "iso-8859-1"
    pred = resp2.json()[0]

    _validar(pred)
    return pred


def capturar(api_key: str) -> tuple[dict, list[str]]:
    session = _session()
    predicciones = {}
    fallos = []

    for municipio in MUNICIPIOS:
        try:
            predicciones[municipio] = fetch_prediccion_horaria(municipio, api_key, session)
        except Exception as exc:
            print(f"error: fallo capturando {municipio}: {exc}", file=sys.stderr)
            predicciones[municipio] = {"error": str(exc)}
            fallos.append(municipio)

    captura = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "municipios": predicciones,
    }
    return captura, fallos


def main() -> None:
    load_dotenv()
    api_key = os.environ["AEMET_API_KEY"]

    captura, fallos = capturar(api_key)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fecha = datetime.now().date().isoformat()
    path = DATA_DIR / f"{fecha}.jsonl"

    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(captura, ensure_ascii=False) + "\n")

    if fallos:
        print(f"Guardado en {path} CON FALLOS en: {', '.join(fallos)}", file=sys.stderr)
        sys.exit(1)

    print(f"Guardado en {path}")


if __name__ == "__main__":
    main()
