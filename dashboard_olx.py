#!/usr/bin/env python3
"""API Flask simples para consultar anúncios da tabela ClickHouse olx_carros.

Instalação:
    pip install flask clickhouse-connect

Execução:
    flask --app api_olx_carros run --host 0.0.0.0 --port 5000
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import clickhouse_connect
from flask import Flask, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException


app = Flask(__name__)


def identifier(value: str) -> str:
    """Valida database/tabela antes de usar o identificador na SQL."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError(f"Identificador ClickHouse inválido: {value!r}")
    return f"`{value}`"


DATABASE = identifier(os.getenv("CLICKHOUSE_DATABASE", "default"))
TABLE = identifier(os.getenv("CLICKHOUSE_TABLE", "olx_carros"))
FULL_TABLE = f"{DATABASE}.{TABLE}"

client = clickhouse_connect.get_client(
    host=os.getenv("CLICKHOUSE_HOST", "localhost"),
    port=int(os.getenv("CLICKHOUSE_PORT", "8123")),
    username=os.getenv("CLICKHOUSE_USER", "default"),
    password=os.getenv("CLICKHOUSE_PASSWORD", ""),
    database=os.getenv("CLICKHOUSE_DATABASE", "default"),
    secure=os.getenv("CLICKHOUSE_SECURE", "false").casefold() in {"1", "true", "yes"},
    # O dashboard consulta /carros e /estatisticas em paralelo. Sem um session_id
    # compartilhado, o ClickHouse pode executar ambas simultaneamente.
    autogenerate_session_id=False,
)


def json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def query_dicts(sql: str, parameters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    result = client.query(sql, parameters=parameters or {})
    return [
        {column: json_value(value) for column, value in zip(result.column_names, row)}
        for row in result.result_rows
    ]


def integer_arg(name: str, default: int | None = None) -> int | None:
    raw = request.args.get(name)
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except ValueError as error:
        raise ValueError(f"O parâmetro '{name}' precisa ser um número inteiro.") from error


def decimal_arg(name: str) -> str | None:
    raw = request.args.get(name)
    if raw in (None, ""):
        return None
    try:
        return str(Decimal(raw.replace(".", "").replace(",", "."))) if "," in raw else str(Decimal(raw))
    except Exception as error:
        raise ValueError(f"O parâmetro '{name}' precisa ser numérico.") from error


def filters() -> tuple[list[str], dict[str, Any]]:
    clauses = ["1 = 1"]
    params: dict[str, Any] = {}

    string_filters = {
        "modelo": "modelo_carro",
        "cor": "cor",
    }
    for argument, column in string_filters.items():
        value = request.args.get(argument)
        if value:
            clauses.append(f"{column} = {{{argument}:String}}")
            params[argument] = value

    numeric_filters = (
        ("ano_min", "ano_veiculo", ">=", "UInt16", integer_arg),
        ("ano_max", "ano_veiculo", "<=", "UInt16", integer_arg),
        ("km_min", "quilometragem", ">=", "UInt32", integer_arg),
        ("km_max", "quilometragem", "<=", "UInt32", integer_arg),
        ("valor_min", "valor", ">=", "Decimal", decimal_arg),
        ("valor_max", "valor", "<=", "Decimal", decimal_arg),
    )
    for argument, column, operator, ch_type, parser in numeric_filters:
        value = parser(argument)
        if value is not None:
            if ch_type == "Decimal":
                clauses.append(f"{column} {operator} toDecimal64({{{argument}:String}}, 2)")
            else:
                clauses.append(f"{column} {operator} {{{argument}:{ch_type}}}")
            params[argument] = value

    return clauses, params


@app.errorhandler(ValueError)
def invalid_parameter(error: ValueError):
    return jsonify({"erro": str(error)}), 400


@app.errorhandler(Exception)
def unexpected_error(error: Exception):
    # Mantém 404, 405 e demais respostas HTTP com seus códigos originais.
    if isinstance(error, HTTPException):
        return error
    app.logger.exception("Erro ao consultar o ClickHouse")
    return jsonify({"erro": "Erro interno ao consultar o banco de dados."}), 500


@app.get("/health")
def health():
    client.command("SELECT 1")
    return jsonify({"status": "ok", "banco": "clickhouse"})


@app.get("/")
def dashboard():
    return send_from_directory(Path(__file__).parent, "dashboard_olx.html")


@app.get("/carros")
def list_cars():
    """Lista anúncios com filtros e paginação."""
    clauses, params = filters()
    limit = min(max(integer_arg("limit", 50) or 50, 1), 500)
    offset = max(integer_arg("offset", 0) or 0, 0)

    sort_columns = {
        "valor": "valor",
        "ano": "ano_veiculo",
        "km": "quilometragem",
        "data": "data_anuncio",
        "coleta": "coletado_em",
    }
    order_by = sort_columns.get(request.args.get("ordenar_por", "coleta"), "coletado_em")
    direction = "ASC" if request.args.get("ordem", "desc").casefold() == "asc" else "DESC"
    params.update({"limit": limit, "offset": offset})

    where = " AND ".join(clauses)
    sql = f"""
        SELECT
            modelo_carro,
            nome_anuncio,
            quilometragem,
            valor,
            cor,
            data_anuncio,
            data_anuncio_raw,
            link_anuncio,
            ano_veiculo,
            coletado_em
        FROM {FULL_TABLE} FINAL
        WHERE {where}
        ORDER BY {order_by} {direction}
        LIMIT {{limit:UInt32}}
        OFFSET {{offset:UInt32}}
    """
    count_sql = f"SELECT count() AS total FROM {FULL_TABLE} FINAL WHERE {where}"

    items = query_dicts(sql, params)
    count_params = {key: value for key, value in params.items() if key not in {"limit", "offset"}}
    total = query_dicts(count_sql, count_params)[0]["total"]
    return jsonify({"total": total, "limit": limit, "offset": offset, "dados": items})


@app.get("/estatisticas")
def statistics():
    """Resume preço e quilometragem, agrupando por modelo."""
    clauses, params = filters()
    where = " AND ".join(clauses)
    sql = f"""
        SELECT
            modelo_carro,
            count() AS quantidade,
            round(avg(valor), 2) AS valor_medio,
            median(valor) AS valor_mediano,
            min(valor) AS menor_valor,
            max(valor) AS maior_valor,
            round(avg(quilometragem), 0) AS km_media,
            min(ano_veiculo) AS menor_ano,
            max(ano_veiculo) AS maior_ano
        FROM {FULL_TABLE} FINAL
        WHERE {where}
        GROUP BY modelo_carro
        ORDER BY modelo_carro
    """
    return jsonify({"dados": query_dicts(sql, params)})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)
