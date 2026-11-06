#!/usr/bin/env bash
# Cenários de falha do lab Zabbix 8 (alertas, APM e CEP).
set -euo pipefail
cd "$(dirname "$0")"

ESTOQUE_URL="http://localhost:8002"
LOOP='while :; do :; done'

uso() {
  cat <<'EOF'
Uso: ./lab.sh <comando>

  caos erro          ~50% das reservas do "estoque" falham (HTTP 500 + exceção no span + log ERROR)
  caos lento         consultas do "estoque" levam ~3 s (pedidos fica lento junto)
  caos normal        volta o "estoque" ao normal
  estoque-fora       derruba o container "estoque" (pedidos passa a responder 503)
  estoque-volta      sobe o "estoque" de novo
  stress-linux [s]   CPU a 100% no linux-demo por [s] segundos (padrão: 420)
  stress-parar       para o stress do linux-demo
  normal             desfaz tudo (caos normal + estoque de volta + para o stress)
  status             mostra o estado atual e os links
EOF
}

caos() {
  local modo="${1:-}"
  [[ "$modo" =~ ^(erro|lento|normal)$ ]] || { uso; exit 1; }
  curl -fsS "$ESTOQUE_URL/caos/$modo" && echo
}

stress_linux() {
  local segundos="${1:-420}"
  local cpus
  cpus=$(docker compose exec -T linux-demo nproc)
  for _ in $(seq "$cpus"); do
    docker compose exec -d linux-demo timeout "$segundos" sh -c "$LOOP"
  done
  echo "stress: $cpus CPU(s) a 100% por ${segundos}s no linux-demo (o trigger de CPU alta precisa de ~5 min)"
}

stress_parar() {
  docker compose exec -T linux-demo pkill -f "$LOOP" >/dev/null 2>&1 || true
  echo "stress parado"
}

status() {
  echo "estoque: $(curl -fsS --max-time 2 "$ESTOQUE_URL/health" 2>/dev/null || echo 'fora do ar')"
  local stress
  stress=$(docker compose exec -T linux-demo pgrep -fc "$LOOP" 2>/dev/null || true)
  echo "stress no linux-demo: ${stress:-0} processo(s)"
  cat <<'EOF'

Problemas:      http://localhost:8080/zabbix.php?action=problem.view
Event processing (CEP): http://localhost:8080/zabbix.php?action=ceprule.list
APM (traces):   http://localhost:8080/zabbix.php?action=apm.trace.list
Notificações:   http://localhost:8090
EOF
}

case "${1:-}" in
  caos)          caos "${2:-}" ;;
  estoque-fora)  docker compose stop estoque ;;
  estoque-volta) docker compose start estoque ;;
  stress-linux)  stress_linux "${2:-420}" ;;
  stress-parar)  stress_parar ;;
  normal)        docker compose start estoque >/dev/null; sleep 2; caos normal; stress_parar ;;
  status)        status ;;
  *)             uso ;;
esac
