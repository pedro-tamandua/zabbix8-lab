// Serviço "estoque" (Node.js/Express).
// Reserva itens para o serviço "pedidos". Telemetria gerada pela auto-instrumentação
// do OpenTelemetry (--require @opentelemetry/auto-instrumentations-node/register)
// e enviada via OTLP gRPC para o coletor APM do Zabbix proxy.
//
// Modo caos (para testar alertas e regras CEP):
//   GET /caos/erro    -> ~50% das reservas falham com HTTP 500 (exceção no span + log ERROR)
//   GET /caos/lento   -> consultas ao "banco" levam 2,5-3,5 s
//   GET /caos/normal  -> volta ao normal
// OTel JS 3.x: a API de logs faz parte do @opentelemetry/api (o pacote @opentelemetry/api-logs é legado)
const express = require('express');
const { trace, metrics, logs, SeverityNumber, SpanStatusCode } = require('@opentelemetry/api');

const app = express();
app.use(express.json());

const tracer = trace.getTracer('estoque');
const meter = metrics.getMeter('estoque');
const logger = logs.getLogger('estoque');
const reservas = meter.createCounter('reservas', { description: 'Reservas de estoque por resultado' });

const PRECOS = { teclado: 199.9, mouse: 89.9, monitor: 1299.0, headset: 349.9, webcam: 259.9 };
const MODOS = ['normal', 'erro', 'lento'];
let modo = 'normal';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function emitLog(severityNumber, severityText, body, attributes) {
  console.log(`[${severityText}] ${body}`);
  logger.emit({ severityNumber, severityText, body, attributes });
}

app.get('/health', (_req, res) => res.json({ status: 'ok', modo }));

app.get('/caos/:modo', (req, res) => {
  if (!MODOS.includes(req.params.modo)) {
    return res.status(400).json({ erro: `modo inválido, use: ${MODOS.join(', ')}` });
  }
  modo = req.params.modo;
  emitLog(SeverityNumber.WARN, 'WARN', `modo caos alterado para "${modo}"`, { modo });
  res.json({ modo });
});

app.post('/reservar', async (req, res) => {
  const { produto, quantidade } = req.body;

  const falhou = await tracer.startActiveSpan('consultar_banco_estoque', async (span) => {
    span.setAttribute('db.system', 'simulado');
    span.setAttribute('estoque.produto', produto);
    span.setAttribute('lab.modo_caos', modo);
    try {
      if (modo === 'lento') {
        await sleep(2500 + Math.random() * 1000);
      } else {
        // ~5% das consultas ficam lentas, para aparecerem no APM
        await sleep(Math.random() < 0.05 ? 400 + Math.random() * 600 : 10 + Math.random() * 40);
      }
      if (modo === 'erro' && Math.random() < 0.5) {
        throw new Error('timeout de conexão com o banco de estoque (pool esgotado)');
      }
      return false;
    } catch (err) {
      span.recordException(err);
      span.setStatus({ code: SpanStatusCode.ERROR, message: err.message });
      emitLog(SeverityNumber.ERROR, 'ERROR', `falha ao consultar estoque de ${produto}: ${err.message}`, { produto });
      return true;
    } finally {
      span.end();
    }
  });

  if (falhou) {
    reservas.add(1, { produto, resultado: 'erro' });
    return res.status(500).json({ erro: 'falha interna ao consultar estoque' });
  }

  // ~10% das reservas falham por falta de estoque (regra de negócio, não é erro)
  if (Math.random() < 0.1) {
    reservas.add(1, { produto, resultado: 'sem_estoque' });
    emitLog(SeverityNumber.WARN, 'WARN', `sem estoque para ${produto} x${quantidade}`, { produto });
    return res.status(409).json({ erro: 'sem estoque' });
  }

  reservas.add(1, { produto, resultado: 'ok' });
  emitLog(SeverityNumber.INFO, 'INFO', `reservado ${produto} x${quantidade}`, { produto });
  res.json({ produto, quantidade, preco_unitario: PRECOS[produto] ?? 100 });
});

app.listen(8002, () => console.log('estoque ouvindo na porta 8002'));
