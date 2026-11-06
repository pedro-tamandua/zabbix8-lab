"""Configura o Zabbix do lab pela API na primeira subida (idempotente).

- Aponta a interface do host "Zabbix server" para o container do agente
- Registra o proxy ativo "zabbix-proxy" com "APM data collection" habilitado
- Cria o host "linux-demo" com o template "Linux by Zabbix agent"
- Deriva o template "OpenTelemetry service by OTLP" (filtra por service.name)
- Cria os hosts de APM "pedidos" e "estoque" (monitorados pelo proxy)
- Cria o media type "Lab webhook", a mídia do Admin e a action de notificação
- Cria as regras CEP (Event processing) do cenário de falha

Só usa a biblioteca padrão do Python.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid

API = os.getenv("ZBX_API_URL", "http://zabbix-web:8080/api_jsonrpc.php")
USER = os.getenv("ZBX_USER", "Admin")
PASSWORD = os.getenv("ZBX_PASSWORD", "zabbix")
PROXY_NAME = os.getenv("ZBX_PROXY_NAME", "zabbix-proxy")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "http://notificacoes:8090/webhook")

LINUX_HOST = "linux-demo"
LINUX_TEMPLATE = "Linux by Zabbix agent"
APM_GROUP = "APM services"
OTEL_SRC_TEMPLATE = "Generic OpenTelemetry by OTLP"
OTEL_TEMPLATE = "OpenTelemetry service by OTLP"
# service.name -> macros extras do host. Na regra CEP de causa/sintoma o PRIMEIRO problema da
# janela vira a causa: o "estoque" (dependência) consulta a cada 30 s e o "pedidos" a cada 2 min,
# para a falha do estoque ser detectada antes do reflexo dela no pedidos.
APM_SERVICES = {
    "estoque": {"{$OTLP.FREQUENCY}": "30s"},
    # ~6 WARN/min de "sem estoque" é o normal do pedidos: limite de WARN acima disso por intervalo de 2 min
    "pedidos": {"{$OTLP.FREQUENCY}": "2m", "{$LOG.SEVERITY.WARN}": "30"},
}

token = None


def call(method, params):
    headers = {"Content-Type": "application/json-rpc"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": 1}).encode()
    with urllib.request.urlopen(urllib.request.Request(API, body, headers), timeout=60) as r:
        data = json.load(r)
    if "error" in data:
        raise RuntimeError(f"{method}: {data['error']}")
    return data["result"]


def wait_for_api():
    for _ in range(120):
        try:
            print(f"API do Zabbix {call('apiinfo.version', {})} disponível", flush=True)
            return
        except (urllib.error.URLError, OSError, RuntimeError, json.JSONDecodeError) as e:
            print(f"aguardando API ({e})...", flush=True)
            time.sleep(5)
    sys.exit("API do Zabbix não respondeu a tempo")


def ensure_group(name):
    found = call("hostgroup.get", {"filter": {"name": name}, "output": ["groupid"]})
    return found[0]["groupid"] if found else call("hostgroup.create", {"name": name})["groupids"][0]


# ---------------------------------------------------------------- infraestrutura

def fix_zabbix_server_interface():
    host = call("host.get", {"filter": {"host": "Zabbix server"}, "output": ["hostid"], "selectInterfaces": "extend"})[0]
    iface = host["interfaces"][0]
    if iface["useip"] == "0" and iface["dns"] == "zabbix-agent":
        print("host 'Zabbix server': interface já aponta para zabbix-agent")
        return
    # Zabbix 8.0 removeu hostinterface.update: interfaces são alteradas via host.update
    call("host.update", {"hostid": host["hostid"], "interfaces": [{
        "interfaceid": iface["interfaceid"], "type": iface["type"], "main": iface["main"], "port": iface["port"],
        "useip": 0, "dns": "zabbix-agent", "ip": "",
    }]})
    print("host 'Zabbix server': interface apontada para zabbix-agent")


def ensure_proxy():
    # "APM data collection" do proxy: sem isso o coletor OTLP (:4317) não abre a porta,
    # mesmo com StartAPMCollector=1 no proxy
    apm = {"data_collection_status": 1}
    found = call("proxy.get", {"filter": {"name": PROXY_NAME}, "output": ["proxyid"], "selectApm": ["data_collection_status"]})
    if found:
        if found[0]["apm"]["data_collection_status"] != "1":
            call("proxy.update", {"proxyid": found[0]["proxyid"], "apm": apm})
            print(f"proxy '{PROXY_NAME}': APM data collection habilitado")
        else:
            print(f"proxy '{PROXY_NAME}' já existe com APM habilitado")
        return found[0]["proxyid"]
    proxyid = call("proxy.create", {"name": PROXY_NAME, "operating_mode": 0, "apm": apm,
                                    "description": "Proxy ativo com coletor APM (OTLP gRPC :4317)"})["proxyids"][0]
    print(f"proxy '{PROXY_NAME}' criado (modo ativo, APM data collection habilitado)")
    return proxyid


def ensure_linux_host():
    groupid = ensure_group("Linux servers")
    if call("host.get", {"filter": {"host": LINUX_HOST}, "output": ["hostid"]}):
        print(f"host '{LINUX_HOST}' já existe")
        return groupid
    template = call("template.get", {"filter": {"host": LINUX_TEMPLATE}, "output": ["templateid"]})
    if not template:
        sys.exit(f"template '{LINUX_TEMPLATE}' não encontrado")
    call("host.create", {
        "host": LINUX_HOST,
        "name": "Linux demo (Ubuntu)",
        "groups": [{"groupid": groupid}],
        "templates": [{"templateid": template[0]["templateid"]}],
        "interfaces": [{"type": 1, "main": 1, "useip": 0, "ip": "", "dns": LINUX_HOST, "port": "10050"}],
        "tags": [{"tag": "lab", "value": "zabbix8"}, {"tag": "camada", "value": "infra"}],
    })
    print(f"host '{LINUX_HOST}' criado com o template '{LINUX_TEMPLATE}'")
    return groupid


# ---------------------------------------------------------------- APM

def ensure_otel_service_template():
    """Deriva um template por serviço a partir do 'Generic OpenTelemetry by OTLP'.

    O template original consulta TODA a telemetria do banco (não filtra por serviço).
    Aqui cada item "Telemetry query" ganha o filtro ServiceName = {$OTLP.SERVICE.NAME},
    as janelas de duração caem de 5m para 2m (o lab reage mais rápido) e entra um
    trigger novo para serviço que parou de mandar spans.
    """
    found = call("template.get", {"filter": {"host": OTEL_TEMPLATE}, "output": ["templateid"]})
    if found:
        print(f"template '{OTEL_TEMPLATE}' já existe")
        return found[0]["templateid"]
    src = call("template.get", {"filter": {"host": OTEL_SRC_TEMPLATE}, "output": ["templateid"]})
    if not src:
        sys.exit(f"template '{OTEL_SRC_TEMPLATE}' não encontrado")
    export = json.loads(call("configuration.export", {"format": "json", "options": {"templates": [src[0]["templateid"]]}}))
    tpl = export["zabbix_export"]["templates"][0]
    for item in tpl["items"]:
        if item["type"] == "TELEMETRY_QUERY":
            flt = item["query"].setdefault("filter", {"evaltype": "AND", "conditions": []})
            flt.setdefault("evaltype", "AND")
            flt.setdefault("conditions", []).append({"column": "ServiceName", "value": "{$OTLP.SERVICE.NAME}"})
    tpl["macros"].append({"macro": "{$OTLP.SERVICE.NAME}", "value": "",
                          "description": "service.name (OpenTelemetry) monitorado por este host"})
    tpl["description"] = ("Derivado do 'Generic OpenTelemetry by OTLP' pelo bootstrap do lab: filtra todos os itens "
                          "Telemetry query por ServiceName = {$OTLP.SERVICE.NAME}.\n\n" + tpl["description"])
    export["zabbix_export"]["triggers"] = export["zabbix_export"].get("triggers", []) + [{
        "uuid": "0",
        # 2 coletas seguidas sem nenhum span, num serviço que mandou spans nos últimos 30 min
        # (sem a 2ª parte, o host recém-criado dispara antes da primeira telemetria chegar)
        "expression": "(" + "+".join(f"max(/{OTEL_SRC_TEMPLATE}/otlp.span.status.{s},#2)" for s in ("ok", "error", "unset"))
                      + ")=0 and (" + "+".join(f"max(/{OTEL_SRC_TEMPLATE}/otlp.span.status.{s},30m)" for s in ("ok", "error", "unset"))
                      + ")>0",
        "name": "OpenTelemetry: serviço sem telemetria",
        "event_name": "OpenTelemetry: {$OTLP.SERVICE.NAME} parou de enviar spans",
        "priority": "HIGH",
        "description": "Nenhum span recebido no último intervalo: serviço fora do ar ou sem conseguir exportar telemetria.",
        "tags": [{"tag": "scope", "value": "availability"}],
    }]
    text = json.dumps(export).replace(OTEL_SRC_TEMPLATE, OTEL_TEMPLATE).replace(",5m)>", ",2m)>")
    text = re.sub(r'"uuid": "[0-9a-f]{0,32}"', lambda _: f'"uuid": "{uuid.uuid4().hex}"', text)
    rules = {k: {"createMissing": True, "updateExisting": True}
             for k in ("templates", "items", "triggers", "graphs", "templateDashboards", "valueMaps")}
    rules["template_groups"] = {"createMissing": True}
    call("configuration.import", {"format": "json", "source": text, "rules": rules})
    print(f"template '{OTEL_TEMPLATE}' criado a partir de '{OTEL_SRC_TEMPLATE}'")
    return call("template.get", {"filter": {"host": OTEL_TEMPLATE}, "output": ["templateid"]})[0]["templateid"]


def ensure_apm_hosts(proxyid, templateid):
    groupid = ensure_group(APM_GROUP)
    for service, extra_macros in APM_SERVICES.items():
        if call("host.get", {"filter": {"host": service}, "output": ["hostid"]}):
            print(f"host APM '{service}' já existe")
            continue
        macros = [{"macro": "{$OTLP.SERVICE.NAME}", "value": service}]
        macros += [{"macro": k, "value": v} for k, v in extra_macros.items()]
        call("host.create", {
            "host": service,
            "name": f"Serviço {service}",
            "groups": [{"groupid": groupid}],
            "templates": [{"templateid": templateid}],
            "monitored_by": 1, "proxyid": proxyid,   # Telemetry query roda no proxy
            "macros": macros,
            "tags": [{"tag": "lab", "value": "zabbix8"}, {"tag": "camada", "value": "apm"},
                     {"tag": "servico", "value": service}],
        })
        print(f"host APM '{service}' criado (monitorado pelo proxy)")
    return groupid


# ---------------------------------------------------------------- notificações

WEBHOOK_SCRIPT = r"""
var p = JSON.parse(value), url = p.URL;
delete p.URL;
var req = new HttpRequest();
req.addHeader('Content-Type: application/json');
req.post(url, JSON.stringify(p));
if (req.getStatus() !== 200) {
    throw 'webhook respondeu HTTP ' + req.getStatus();
}
return 'OK';
"""


def ensure_webhook_mediatype():
    found = call("mediatype.get", {"filter": {"name": "Lab webhook"}, "output": ["mediatypeid"]})
    if found:
        print("media type 'Lab webhook' já existe")
        return found[0]["mediatypeid"]
    params = {
        "URL": WEBHOOK_URL, "status": "{EVENT.STATUS}", "severity": "{EVENT.SEVERITY}",
        "host": "{HOST.NAME}", "event_name": "{EVENT.NAME}", "event_id": "{EVENT.ID}",
        "cause_event_id": "{EVENT.CAUSE.ID}", "tags": "{EVENT.TAGS}",
        "subject": "{ALERT.SUBJECT}", "message": "{ALERT.MESSAGE}",
    }
    mtid = call("mediatype.create", {
        "name": "Lab webhook", "type": 4, "status": 0, "timeout": "10s",
        "script": WEBHOOK_SCRIPT,
        "parameters": [{"name": k, "value": v} for k, v in params.items()],
        "description": "Envia os alertas do lab para o container 'notificacoes' (http://localhost:8090).",
        "message_templates": [
            {"eventsource": 0, "recovery": 0, "subject": "Problema: {EVENT.NAME}",
             "message": "Problema em {HOST.NAME}: {EVENT.NAME} ({EVENT.SEVERITY})"},
            {"eventsource": 0, "recovery": 1, "subject": "Resolvido: {EVENT.NAME}",
             "message": "Resolvido em {HOST.NAME}: {EVENT.NAME} após {EVENT.DURATION}"},
        ],
    })["mediatypeids"][0]
    print("media type 'Lab webhook' criado")
    return mtid


def ensure_admin_media(mediatypeid):
    user = call("user.get", {"filter": {"username": USER}, "output": ["userid"], "selectMedias": "extend"})[0]
    if any(m["mediatypeid"] == mediatypeid for m in user["medias"]):
        print(f"usuário '{USER}' já tem a mídia 'Lab webhook'")
        return user["userid"]
    # user.update substitui a lista inteira: mantém as mídias existentes
    medias = [{k: v for k, v in m.items() if k not in ("mediaid", "userid", "userdirectory_mediaid", "provisioned")}
              for m in user["medias"]]
    call("user.update", {"userid": user["userid"], "medias": medias + [{
        "mediatypeid": mediatypeid, "sendto": "lab", "active": 0, "severity": 63, "period": "1-7,00:00-24:00"}]})
    print(f"mídia 'Lab webhook' adicionada ao usuário '{USER}'")
    return user["userid"]


def ensure_action(mediatypeid, userid, groupids):
    if call("action.get", {"filter": {"name": "Lab: notificar webhook"}, "output": ["actionid"]}):
        print("action 'Lab: notificar webhook' já existe")
        return
    call("action.create", {
        "name": "Lab: notificar webhook", "eventsource": 0, "status": 0, "esc_period": "1h",
        "filter": {"evaltype": 2, "conditions": [{"conditiontype": 0, "operator": 0, "value": g} for g in groupids]},
        "operations": [{"operationtype": 0, "opmessage": {"default_msg": 1, "mediatypeid": mediatypeid},
                        "opmessage_usr": [{"userid": userid}]}],
        "recovery_operations": [{"operationtype": 11, "opmessage": {"default_msg": 1}}],
    })
    print("action 'Lab: notificar webhook' criada")


# ---------------------------------------------------------------- CEP (Event processing)

# Constantes de CCepRuleHelper / defines.inc.php do frontend 8.0
WIN_NONE, WIN_CAUSE_SYMPTOM, WIN_PATTERN = 0, 2, 4
WHEN_OCCURRED, WHEN_ADDED, WHEN_PATTERN = 0, 1, 4
OP_SET_NAME, OP_INCREASE_SEVERITY, OP_CLONE_FIRST, OP_ADD_TAG, OP_CLOSE_WINDOW = 1, 5, 9, 11, 18
COND_TAG_VALUE, COND_HOST_GROUP = 26, 37
OPCOND_SYMPTOM, OPCOND_COPIED = 33, 34
OPER_EQUAL, OPER_YES, OPER_NO = 0, 10, 11
SCOPE_CURRENT, SCOPE_ALL = 0, 2


def cond_filter(*conditions):
    # evaltype 0 = And/Or (formulaid só é aceito com evaltype 3, expressão customizada)
    return {"evaltype": 0, "conditions": list(conditions)}


APM_GROUP_FILTER = cond_filter({"type": COND_HOST_GROUP, "operator": OPER_EQUAL, "host_group_name": APM_GROUP})

PATTERN_SCRIPT = """// Dispara quando 3 ou mais problemas dos serviços APM acontecem dentro da janela
return cep_get_events().length >= 3;"""

CEP_RULES = [
    {
        "name": "Lab: enriquecer eventos dos serviços APM",
        "description": "Adiciona a tag 'time: plataforma' em todo evento dos hosts do grupo 'APM services'.",
        "window_type": WIN_NONE, "filter": APM_GROUP_FILTER,
        "operations": [{"execute_when": WHEN_OCCURRED, "type": OP_ADD_TAG, "tag": "time", "tag_value": "plataforma",
                        "scope": SCOPE_CURRENT}],
    },
    {
        "name": "Lab: pedidos é crítico para o negócio",
        "description": "Eventos do serviço 'pedidos' (checkout) sobem um nível de severidade e ganham a tag 'impacto: cliente'.",
        "window_type": WIN_NONE,
        "filter": cond_filter({"type": COND_TAG_VALUE, "operator": OPER_EQUAL, "tag": "servico", "tag_value": "pedidos"}),
        "operations": [
            {"execute_when": WHEN_OCCURRED, "type": OP_INCREASE_SEVERITY, "scope": SCOPE_CURRENT},
            {"execute_when": WHEN_OCCURRED, "type": OP_ADD_TAG, "tag": "impacto", "tag_value": "cliente", "scope": SCOPE_CURRENT},
        ],
    },
    {
        "name": "Lab: falha em cascata (causa e sintomas)",
        "description": "Agrupa os problemas dos serviços APM numa janela de 10 min: o primeiro vira a causa e os "
                       "seguintes viram sintomas dele. Marca cada um com a tag 'cep'.",
        "window_type": WIN_CAUSE_SYMPTOM, "filter": APM_GROUP_FILTER, "window": {"duration": "10m"},
        "operations": [
            {"execute_when": WHEN_ADDED, "type": OP_ADD_TAG, "tag": "cep", "tag_value": "sintoma", "scope": SCOPE_CURRENT,
             "filter": cond_filter({"type": OPCOND_SYMPTOM, "operator": OPER_YES})},
            {"execute_when": WHEN_ADDED, "type": OP_ADD_TAG, "tag": "cep", "tag_value": "causa", "scope": SCOPE_CURRENT,
             "filter": cond_filter({"type": OPCOND_SYMPTOM, "operator": OPER_NO})},
        ],
    },
    {
        "name": "Lab: incidente com vários alertas (pattern match)",
        "description": "Quando 3 ou mais problemas dos serviços APM acontecem em 5 min, cria um evento "
                       "'Incidente: degradação nos serviços APM' (cópia do primeiro evento da janela, que fica "
                       "aberta enquanto o problema original durar) e reinicia a janela.",
        "window_type": WIN_PATTERN, "filter": APM_GROUP_FILTER, "window": {"duration": "5m", "script": PATTERN_SCRIPT},
        "operations": [
            {"execute_when": WHEN_OCCURRED, "type": OP_SET_NAME, "event_name": "Incidente: degradação nos serviços APM",
             "scope": SCOPE_CURRENT, "filter": cond_filter({"type": OPCOND_COPIED, "operator": OPER_YES})},
            {"execute_when": WHEN_PATTERN, "type": OP_CLONE_FIRST, "scope": SCOPE_ALL},
            {"execute_when": WHEN_PATTERN, "type": OP_CLOSE_WINDOW, "scope": SCOPE_ALL},
        ],
    },
]


def ensure_cep_rules():
    existing = {r["name"] for r in call("ceprule.get", {"output": ["name"]})}
    for i, rule in enumerate(CEP_RULES, start=1):
        if rule["name"] in existing:
            print(f"regra CEP '{rule['name']}' já existe")
            continue
        ops = [dict(op, sortorder=n) for n, op in enumerate(rule["operations"], start=1)]
        call("ceprule.create", dict(rule, operations=ops, status=0, stop=0, sortorder=100 + i))
        print(f"regra CEP '{rule['name']}' criada")


def main():
    global token
    wait_for_api()
    token = call("user.login", {"username": USER, "password": PASSWORD})
    fix_zabbix_server_interface()
    proxyid = ensure_proxy()
    linux_groupid = ensure_linux_host()
    templateid = ensure_otel_service_template()
    apm_groupid = ensure_apm_hosts(proxyid, templateid)
    mediatypeid = ensure_webhook_mediatype()
    userid = ensure_admin_media(mediatypeid)
    ensure_action(mediatypeid, userid, [apm_groupid, linux_groupid])
    ensure_cep_rules()
    print("bootstrap concluído")


if __name__ == "__main__":
    main()
