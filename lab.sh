#!/usr/bin/env bash
# Cenários de falha do lab Zabbix 8 (alertas, APM e CEP).
# Nenhum cenário consome CPU: as falhas são simuladas (erro/lentidão no código ou container parado).
set -euo pipefail
cd "$(dirname "$0")"

ESTOQUE_URL="http://localhost:8002"

uso() {
  cat <<'EOF'
Uso: ./lab.sh <comando>

  caos erro          ~50% das reservas do "estoque" falham (HTTP 500 + exceção no span + log ERROR)
  caos lento         consultas do "estoque" levam ~3 s (pedidos fica lento junto)
  caos normal        volta o "estoque" ao normal
  estoque-fora       derruba o container "estoque" (pedidos passa a responder 503)
  estoque-volta      sobe o "estoque" de novo
  linux-fora         derruba o agente do linux-demo ("Zabbix agent is not available" em ~3 min)
  linux-volta        sobe o agente do linux-demo de novo
  normal             desfaz tudo
  status             mostra o estado atual e os links
EOF
}

caos() {
  local modo="${1:-}"
  [[ "$modo" =~ ^(erro|lento|normal)$ ]] || { uso; exit 1; }
  curl -fsS "$ESTOQUE_URL/caos/$modo" && echo
}

status() {
  echo "estoque:    $(curl -fsS --max-time 2 "$ESTOQUE_URL/health" 2>/dev/null || echo 'fora do ar')"
  echo "linux-demo: $(docker compose ps linux-demo --format '{{.Status}}' 2>/dev/null | grep . || echo 'parado')"
  cat <<'EOF'

Problemas:              http://localhost:8080/zabbix.php?action=problem.view
Event processing (CEP): http://localhost:8080/zabbix.php?action=ceprule.list
APM (traces):           http://localhost:8080/zabbix.php?action=apm.trace.list
Notificações:           http://localhost:8090
EOF
}

case "${1:-}" in
  caos)          caos "${2:-}" ;;
  estoque-fora)  docker compose stop estoque ;;
  estoque-volta) docker compose start estoque ;;
  linux-fora)    docker compose stop linux-demo ;;
  linux-volta)   docker compose start linux-demo ;;
  normal)        docker compose start estoque linux-demo >/dev/null; sleep 2; caos normal ;;
  status)        status ;;
  *)             uso ;;
esac
