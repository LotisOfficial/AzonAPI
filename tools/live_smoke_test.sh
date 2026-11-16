#!/bin/bash
# Live smoke test of the AZON API against a scratch copy of a repository.
# Usage: run_live_test.sh <AzonAPI checkout> ; server must NOT already be on port 5299.
set -u
API_DIR="$1"
S="$(cd "$(dirname "$0")" && pwd)"
PY="$API_DIR/.venv/Scripts/python"
PORT=5299
BASE="http://127.0.0.1:$PORT"

# Throwaway test tokens (scratch only).
"$PY" - "$S" <<'PYEOF'
import hashlib, json, secrets, sys, pathlib
s = pathlib.Path(sys.argv[1])
toks = {r: secrets.token_urlsafe(32) for r in ("read", "write", "admin")}
(s / "secrets" / "api_tokens.json").write_text(json.dumps(
    [{"name": r + "-test", "role": r, "sha256": hashlib.sha256(t.encode()).hexdigest()} for r, t in toks.items()]))
(s / "secrets" / "plain_test_tokens.env").write_text("".join(f"{r.upper()}={t}\n" for r, t in toks.items()))
PYEOF
. "$S/secrets/plain_test_tokens.env"

: "${JAVA_HOME:?set JAVA_HOME to the Java runtime to test with}"
export REPO_ROOT="$S/repositories" PPRJ_FILE="$S/repositories/Arthur/Arthur.pprj"
export CONFIG_FILE="$S/config/active.json" AZON_API_TOKENS_FILE="$S/secrets/api_tokens.json"
rm -f "$CONFIG_FILE"

cd "$API_DIR"
"$PY" -m waitress --listen=127.0.0.1:$PORT --threads=4 wsgi:app > "$S/server.log" 2>&1 &
SERVER=$!
for i in $(seq 1 90); do
  curl -s -o /dev/null "$BASE/health" && break
  sleep 2
done

t() { # label expected method path [extra curl args]
  local label="$1" want="$2" method="$3" path="$4"; shift 4
  local got; got=$(curl -s -o "$S/last_body.txt" -w '%{http_code}' -X "$method" "$BASE$path" "$@")
  if [ "$got" = "$want" ]; then echo "PASS $got $label"; else echo "FAIL got $got want $want :: $label :: $(head -c 200 "$S/last_body.txt")"; fi
}
J='Content-Type: application/json'

echo "health: $(curl -s $BASE/health)"
t "health open"                      200 GET  /health
t "classes no token"                 401 GET  /api/classes/
t "classes bad token"                401 GET  /api/classes/ -H "Authorization: Bearer nope"
t "classes read token"               200 GET  /api/classes/ -H "Authorization: Bearer $READ"
t "instances read token"             200 GET  "/api/classes/Currency/instances?slots=name&count=3" -H "Authorization: Bearer $READ"
echo "  sample: $(head -c 160 "$S/last_body.txt")"
t "create with read token"           403 POST /api/instances -H "Authorization: Bearer $READ" -H "$J" -d '{"className":"Currency","name":"ZZ Test Dollar"}'
t "create text/plain"                415 POST /api/instances -H "Authorization: Bearer $WRITE" -H 'Content-Type: text/plain' -d '{"className":"Currency","name":"ZZ Test Dollar"}'
t "create with write token"          201 POST /api/instances -H "Authorization: Bearer $WRITE" -H "$J" -d '{"className":"Currency","name":"ZZ Test Dollar"}'
ID=$("$PY" -c "import json,sys;d=json.load(open(sys.argv[1]));print((d.get('instance') or d).get('id',''))" "$S/last_body.txt")
echo "  created id: $ID"
t "update with write token"          200 PATCH "/api/instances/$ID" -H "Authorization: Bearer $WRITE" -H "$J" -d '{"className":"Currency","description":"live test"}'
t "delete no token"                  401 DELETE "/api/instances/$ID"
t "delete with write token"          200 DELETE "/api/instances/$ID" -H "Authorization: Bearer $WRITE"
t "repositories read token"          200 GET  /api/repositories -H "Authorization: Bearer $READ"
echo "  repos: $(cat "$S/last_body.txt" | tr -d '\n ' | head -c 300)"
t "switch with write token"          403 POST /api/repository -H "Authorization: Bearer $WRITE" -H "$J" -d '{"id":"ArthurCopy/Arthur.pprj"}'
t "switch traversal"                 400 POST /api/repository -H "Authorization: Bearer $ADMIN" -H "$J" -d '{"id":"../secrets/x.pprj"}'
t "switch legacy path field"         400 POST /api/repository -H "Authorization: Bearer $ADMIN" -H "$J" -d "{\"path\":\"$S/repositories/Arthur/Arthur.pprj\"}"
t "switch listed id (admin)"         200 POST /api/repository -H "Authorization: Bearer $ADMIN" -H "$J" -d '{"id":"ArthurCopy/Arthur.pprj"}'
echo "  now: $(cat "$S/last_body.txt" | tr -d '\n')"
t "classes after switch"             200 GET  /api/classes/ -H "Authorization: Bearer $READ"
t "publish GET"                      405 GET  "/api/publish?url=http://x&user=a&pwd=b" -H "Authorization: Bearer $ADMIN"
t "publish unconfigured"             503 POST /api/publish -H "Authorization: Bearer $ADMIN" -H "$J" -d '{"target":"local"}'
t "swagger off"                      404 GET  /api/
t "cors preflight (unauthenticated)" 401 OPTIONS /api/instances -H 'Origin: https://evil.example' -H 'Access-Control-Request-Method: POST'
echo "  ACAO header count: $(curl -s -D - -o /dev/null -X OPTIONS $BASE/api/instances -H 'Origin: https://evil.example' -H 'Access-Control-Request-Method: POST' | grep -ci access-control)"
echo "listeners: $(netstat -ano | grep ":$PORT " | grep LISTENING | awk '{print $2}' | sort -u | tr '\n' ' ')"

kill $SERVER 2>/dev/null; sleep 1
taskkill //F //PID $(netstat -ano | grep ":$PORT " | grep LISTENING | awk '{print $5}' | head -1) >/dev/null 2>&1

# Fail-closed check: no token file -> process must exit non-zero.
unset AZON_API_TOKENS_FILE
"$PY" -c "import wsgi" > "$S/failclosed.log" 2>&1; echo "start without token file: exit=$? ($(tail -1 "$S/failclosed.log" | cut -c1-110))"
rm -f "$S/secrets/plain_test_tokens.env"
echo "--- server log tail"; tail -15 "$S/server.log" | cut -c1-200
