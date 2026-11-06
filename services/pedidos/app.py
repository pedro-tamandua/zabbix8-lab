"""Serviço "pedidos" (Python/Flask).

Recebe pedidos e consulta o serviço "estoque" (Node.js) para reservar itens.
Toda a telemetria (traces, métricas e logs) é gerada pela auto-instrumentação
do OpenTelemetry (`opentelemetry-instrument`) e enviada via OTLP gRPC para o
coletor APM do Zabbix proxy. Os spans/métricas manuais abaixo só enriquecem o demo.
"""
import logging
import os
import random
import time

import requests
from flask import Flask, jsonify
from opentelemetry import metrics, trace
from opentelemetry.trace import StatusCode

ESTOQUE_URL = os.getenv("ESTOQUE_URL", "http://estoque:8002")

app = Flask(__name__)
log = logging.getLogger("pedidos")
logging.basicConfig(level=logging.INFO)

tracer = trace.get_tracer("pedidos")
meter = metrics.get_meter("pedidos")
pedidos_total = meter.create_counter("pedidos_criados", description="Pedidos criados com sucesso")
valor_pedido = meter.create_histogram("pedido_valor", unit="BRL", description="Valor dos pedidos")

PRODUTOS = ["teclado", "mouse", "monitor", "headset", "webcam"]


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/pedidos")
@app.get("/pedidos/novo")
def criar_pedido():
    produto = random.choice(PRODUTOS)
    quantidade = random.randint(1, 5)

    with tracer.start_as_current_span("validar_pedido") as span:
        span.set_attribute("pedido.produto", produto)
        span.set_attribute("pedido.quantidade", quantidade)
        time.sleep(random.uniform(0.005, 0.03))

    # Chamada HTTP instrumentada: o contexto do trace é propagado para o "estoque"
    try:
        r = requests.post(f"{ESTOQUE_URL}/reservar", json={"produto": produto, "quantidade": quantidade}, timeout=5)
    except requests.RequestException as e:
        span = trace.get_current_span()
        span.record_exception(e)
        span.set_status(StatusCode.ERROR, "estoque indisponível")
        log.error("estoque indisponível ao reservar %s x%d: %s", produto, quantidade, e.__class__.__name__)
        return jsonify(erro="estoque indisponível"), 503

    if r.status_code == 409:
        # regra de negócio, não é erro de sistema
        log.warning("sem estoque para %s x%d", produto, quantidade)
        return jsonify(erro="sem estoque", produto=produto), 409
    if r.status_code != 200:
        log.error("falha ao reservar %s x%d: estoque respondeu HTTP %d", produto, quantidade, r.status_code)
        return jsonify(erro="falha no estoque", produto=produto), 502

    with tracer.start_as_current_span("calcular_frete"):
        time.sleep(random.uniform(0.01, 0.08))

    valor = round(r.json()["preco_unitario"] * quantidade, 2)
    pedidos_total.add(1, {"produto": produto})
    valor_pedido.record(valor, {"produto": produto})
    log.info("pedido criado: %s x%d = R$ %.2f", produto, quantidade, valor)
    return jsonify(produto=produto, quantidade=quantidade, valor=valor)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8001)
