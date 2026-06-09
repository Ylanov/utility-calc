#!/usr/bin/env bash
# Шлёт сводку одного сканера в платформу: POST /api/admin/security/report.
# Best-effort: НИКОГДА не валит джобу (нет токена / сеть / 4xx → просто warning).
#
# Использование: echo '<payload-json>' | bash .github/security/post-finding.sh
# Env: SECURITY_SYNC_TOKEN (обязателен — иначе skip), SECURITY_REPORT_URL
#      (по умолчанию https://asy-tk.ru).
set +e

TOKEN="${SECURITY_SYNC_TOKEN:-}"
URL="${SECURITY_REPORT_URL:-https://asy-tk.ru}"

if [ -z "$TOKEN" ]; then
  echo "[security-report] SECURITY_SYNC_TOKEN не задан — пропускаю отправку (не ошибка)"
  exit 0
fi

payload="$(cat)"
if [ -z "$payload" ]; then
  echo "[security-report] пустой payload — пропускаю"
  exit 0
fi

code=$(curl -sS --max-time 30 -o /tmp/sec_report_resp -w "%{http_code}" \
  -X POST "${URL%/}/api/admin/security/report" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d "$payload")

if [ "$code" = "200" ]; then
  echo "[security-report] отправлено (HTTP 200)"
else
  echo "[security-report] не отправлено (HTTP ${code:-000}) — не ошибка, пропускаю"
  head -c 300 /tmp/sec_report_resp 2>/dev/null || true
fi
exit 0
