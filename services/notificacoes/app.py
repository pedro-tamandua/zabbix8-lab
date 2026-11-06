"""Receptor de webhooks do Zabbix (lab).

POST /webhook  -> recebe a notificação enviada pelo media type "Lab webhook"
GET  /         -> página com as últimas notificações (atualiza sozinha)
GET  /api      -> as mesmas notificações em JSON

Só usa a biblioteca padrão do Python.
"""
import html
import json
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

NOTIFICACOES = deque(maxlen=300)

SEV_CORES = {
    "Not classified": "#97aab3", "Information": "#7499ff", "Warning": "#ffc859",
    "Average": "#ffa059", "High": "#e97659", "Disaster": "#e45959",
}

PAGINA = """<!doctype html><html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="5">
<title>Notificações do Zabbix (lab)</title>
<style>
body{{font:14px system-ui,sans-serif;margin:0;background:#f4f6f8;color:#1f2c33}}
header{{background:#1f2c33;color:#fff;padding:14px 20px}} h1{{margin:0;font-size:18px}}
p.sub{{margin:4px 0 0;color:#b7c4cc;font-size:13px}} main{{padding:16px 20px}}
table{{width:100%;border-collapse:collapse;background:#fff;box-shadow:0 1px 2px #0002}}
th,td{{padding:8px 10px;border-bottom:1px solid #e3e8eb;text-align:left;vertical-align:top}}
th{{background:#eef2f4;font-size:12px;text-transform:uppercase;color:#56656d}}
.sev{{display:inline-block;padding:2px 8px;border-radius:3px;font-size:12px;color:#1f2c33}}
.ok{{background:#86cc89}} .tags{{color:#56656d;font-size:12px}} .vazio{{padding:30px;text-align:center;color:#56656d}}
</style></head><body><header><h1>Notificações recebidas do Zabbix</h1>
<p class="sub">{total} notificação(ões) · a página atualiza a cada 5 s · JSON em <a style="color:#9cd" href="/api">/api</a></p></header>
<main><table><tr><th>Recebida (UTC)</th><th>Status</th><th>Severidade</th><th>Host</th><th>Evento</th><th>Causa / sintoma</th><th>Tags</th></tr>
{linhas}</table></main></body></html>"""


def linha(n):
    e = lambda k: html.escape(str(n.get(k, "")))
    resolvido = n.get("status") == "RESOLVED"
    sev = n.get("severity", "")
    cor = "" if resolvido else f' style="background:{SEV_CORES.get(sev, "#ddd")}"'
    cls = "sev ok" if resolvido else "sev"
    # {EVENT.CAUSE.ID} só é resolvido em sintomas; em eventos causa chega "*UNKNOWN*"
    cause = str(n.get("cause_event_id", ""))
    causa = f"sintoma de #{html.escape(cause)}" if cause.isdigit() and cause != "0" else "causa"
    return (f"<tr><td>{e('_recebida')}</td><td><span class='{cls}'{cor}>{e('status')}</span></td>"
            f"<td>{e('severity')}</td><td>{e('host')}</td><td><b>{e('event_name')}</b><br>"
            f"<span class='tags'>evento #{e('event_id')}</span></td><td>{causa}</td><td class='tags'>{e('tags')}</td></tr>")


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path != "/webhook":
            return self._send(404, '{"erro":"use POST /webhook"}', "application/json")
        tamanho = int(self.headers.get("Content-Length", 0))
        try:
            n = json.loads(self.rfile.read(tamanho) or b"{}")
        except json.JSONDecodeError:
            return self._send(400, '{"erro":"JSON inválido"}', "application/json")
        n["_recebida"] = time.strftime("%H:%M:%S")
        NOTIFICACOES.appendleft(n)
        print(f"[webhook] {n.get('status')} {n.get('severity')} {n.get('host')}: {n.get('event_name')}", flush=True)
        self._send(200, '{"ok":true}', "application/json")

    def do_GET(self):
        if self.path == "/api":
            return self._send(200, json.dumps(list(NOTIFICACOES), ensure_ascii=False, indent=1), "application/json")
        if self.path == "/health":
            return self._send(200, '{"status":"ok"}', "application/json")
        linhas = "".join(linha(n) for n in NOTIFICACOES) or "<tr><td colspan=7 class=vazio>Nenhuma notificação ainda. Rode <code>./lab.sh caos erro</code>.</td></tr>"
        self._send(200, PAGINA.format(total=len(NOTIFICACOES), linhas=linhas), "text/html; charset=utf-8")

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    print("receptor de notificações ouvindo na porta 8090", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 8090), Handler).serve_forever()
