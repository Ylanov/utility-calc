# ZAP traditional JSON (-J) → SARIF 2.1.0 для GitHub Code Scanning (Security tab).
# DAST-находки (web-URL, не файлы репо) — code scanning принимает их как алерты
# без привязки к строке кода. Запускается: jq -f этот_файл zap-report.json.
#
# riskcode: 3=High 2=Medium 1=Low 0=Info → SARIF level error/warning/note/none.
# security-severity (число) — чтобы GitHub раскрасил severity в Security tab.

def lvl(rc): if rc=="3" then "error" elif rc=="2" then "warning" elif rc=="1" then "note" else "none" end;
def sev(rc): if rc=="3" then "8.0" elif rc=="2" then "5.0" elif rc=="1" then "3.0" else "0.0" end;
def strip: (. // "") | gsub("<[^>]*>"; " ") | gsub("[[:space:]]+"; " ") | gsub("^ +| +$"; "");
# GitHub Code Scanning отвергает URI со схемой http (ждёт относительный путь
# файла). DAST-находки — про runtime-URL, файла нет → маппим web-URL в
# синтетический относительный путь DAST/<host><path> (без схемы/запроса).
# Реальный URL остаётся в тексте сообщения.
def relpath(u): "DAST/" + ((u // "/")
  | sub("^[a-zA-Z][a-zA-Z0-9+.-]*://"; "")
  | sub("[?#].*$"; "")
  | sub("/$"; "")
  | (if . == "" then "root" else . end));

{
  "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
  "version": "2.1.0",
  "runs": [
    {
      "tool": {
        "driver": {
          "name": "OWASP ZAP",
          "informationUri": "https://www.zaproxy.org/",
          "version": "baseline",
          "rules": (
            [ (.site // [])[].alerts[]? ]
            | group_by(.pluginid)
            | map( .[0] as $a | {
                "id": ($a.pluginid // "0" | tostring),
                "name": ($a.name // "ZAP Alert"),
                "shortDescription": { "text": ($a.name // "ZAP Alert") },
                "fullDescription": { "text": ($a.desc | strip) },
                "helpUri": "https://www.zaproxy.org/docs/alerts/",
                "help": { "text": (($a.solution // "") | strip) },
                "properties": {
                  "tags": ([ "security", "dast" ] + (if (($a.cweid // "") | tostring) != "" then [ "CWE-" + ($a.cweid | tostring) ] else [] end)),
                  "security-severity": sev($a.riskcode | tostring)
                }
              } )
          )
        }
      },
      "results": (
        [ (.site // [])[].alerts[]? ]
        | map( . as $a
            | ($a.instances // [ { "uri": "/" } ])
            | map( {
                "ruleId": ($a.pluginid // "0" | tostring),
                "level": lvl($a.riskcode | tostring),
                "message": { "text": (($a.alert // $a.name // "ZAP Alert") + " — " + ($a.desc | strip)) },
                "locations": [ { "physicalLocation": {
                  "artifactLocation": { "uri": relpath(.uri) },
                  "region": { "startLine": 1 }
                } } ],
                "properties": { "url": (.uri // "/") }
              } )
          )
        | flatten
      )
    }
  ]
}
