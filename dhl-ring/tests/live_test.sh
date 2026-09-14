#!/bin/bash
# Live test: real processes, real HTTP, including an uplink outage.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
H=http://127.0.0.1:8500
C=http://127.0.0.1:8501
rm -f /tmp/live.db /tmp/live.db-wal /tmp/live.db-shm

cleanup(){ pkill -f "uvicorn hub:app" 2>/dev/null; pkill -f "uvicorn main:app" 2>/dev/null; }
trap cleanup EXIT

start_cloud(){
  (cd $ROOT/cloud && INGEST_TOKEN=live-token NOTIFY_GRACE_SEC=0 \
     PUBLIC_URL=https://orbweaver.roarfun.live \
     python3 -m uvicorn main:app --host 127.0.0.1 --port 8501 --log-level error \
     > /tmp/cl.log 2>&1 &)
}
push(){
  (cd $ROOT/booth && HUB_URL=$H CLOUD_URL=$C INGEST_TOKEN=live-token \
     SUBSCRIBER_SECONDS=0 LOG_FILE=/tmp/p.log \
     python3 pusher_hub.py --once --env /nonexistent 2>&1 | grep -E "pushed|synced|rejected|failed|unreachable|retrying")
}
api(){ curl -s -X POST "$1" -H "Content-Type: application/json" -d "$2"; }

(cd $ROOT/booth && HUB_DB=/tmp/live.db python3 -m uvicorn hub:app --host 127.0.0.1 --port 8500 --log-level error > /tmp/hub.log 2>&1 &)
start_cloud
sleep 5
echo "servers: hub=$(curl -s -o /dev/null -w '%{http_code}' $H/healthz) cloud=$(curl -s -o /dev/null -w '%{http_code}' $C/healthz)"

echo
echo "=== A. booth session ==="
V1=$(api $H/api/visitors '{"first_name":"David","surname":"P","company":"Vodafone","actor":"rig1"}' | python3 -c "import json,sys;print(json.load(sys.stdin)['id'])")
V2=$(api $H/api/visitors '{"first_name":"Tomas","surname":"B","company":"DBAG","actor":"rig2"}' | python3 -c "import json,sys;print(json.load(sys.stdin)['id'])")
api $H/api/runs "{\"visitor_id\":\"$V1\",\"lap_ms\":58412}" > /dev/null
api $H/api/runs "{\"visitor_id\":\"$V2\",\"lap_ms\":61200}" > /dev/null
api $H/api/consent "{\"visitor_id\":\"$V1\",\"phone\":\"+421900123456\",\"channel\":\"sms\",\"consent\":true}" > /dev/null
echo "  registered 2, 2 runs, 1 opt-in (D.P is P1)"
push | sed 's/^/  /'

echo
echo "=== B. D.P is overtaken — does a message fire? ==="
api $H/api/runs "{\"visitor_id\":\"$V2\",\"lap_ms\":54000}" | python3 -c "import json,sys;d=json.load(sys.stdin);print('  hub: T.B ->',d['best_lap'],'now P'+str(d['rank']))"
push | sed 's/^/  /'
curl -s $C/admin/notifications -H "Authorization: Bearer live-token" | python3 -c "
import json,sys;d=json.load(sys.stdin)
print('  messages:',len(d['messages']))
for m in d['messages']: print('   ',m['kind'],'->',m['to'],'|',m['body'][:96])"

echo
echo "=== C. uplink dies mid-show ==="
pkill -f "uvicorn main:app"; sleep 2
echo "  cloud stopped."
api $H/api/visitors '{"first_name":"Anna","surname":"K","company":"Siemens"}' > /tmp/v3.json
V3=$(python3 -c "import json;print(json.load(open('/tmp/v3.json'))['id'])")
api $H/api/runs "{\"visitor_id\":\"$V3\",\"lap_ms\":51000}" > /dev/null
echo "  booth kept working while offline:"
curl -s $H/api/leaderboard | python3 -c "
import json,sys;d=json.load(sys.stdin)
print('   ',d['count'],'entries, leader',d['leaderboard'][0]['name'],d['leaderboard'][0]['best_lap'])"
echo "  pusher behaviour while offline:"
push | sed 's/^/    /'

echo
echo "=== D. uplink returns ==="
start_cloud; sleep 5
push | sed 's/^/  /'
curl -s $C/v1/leaderboard.json | python3 -c "
import json,sys;d=json.load(sys.stdin)
print('  cloud caught up:',d['count'],'entries, leader',d['leaderboard'][0]['name'])"
curl -s $C/admin/notifications -H "Authorization: Bearer live-token" | python3 -c "
import json,sys;d=json.load(sys.stdin)
print('  subscribers restored:',len(d['subscribers']))
print('  messages after recovery:',len(d['messages']),'(a burst here would be a bug)')"

echo
echo "=== E. data integrity after all that ==="
curl -s $H/api/stats | python3 -c "
import json,sys;d=json.load(sys.stdin);print('  hub stats:',d)"
curl -s "$H/api/search?q=siemens" | python3 -c "
import json,sys;d=json.load(sys.stdin)
print('  search siemens:',[(r['first_name'],r['best_lap'],r['rank']) for r in d['results']])"
echo "  audit trail:"
python3 - <<'EOF'
import sqlite3
c=sqlite3.connect("/tmp/live.db"); c.row_factory=sqlite3.Row
for r in c.execute("SELECT action, COUNT(*) n FROM audit GROUP BY action"):
    print(f"    {r['action']:16} {r['n']}")
print("    phone numbers in audit:",
      c.execute("SELECT COUNT(*) FROM audit WHERE detail LIKE '%900123456%'").fetchone()[0])
EOF
