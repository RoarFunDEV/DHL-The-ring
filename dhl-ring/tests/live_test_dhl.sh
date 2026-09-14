#!/bin/bash
# Live DHL test: real hub + real cloud over HTTP, manual sends, cooldown, outage.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
H=http://127.0.0.1:8800
C=http://127.0.0.1:8801
DB=/tmp/dhl_live.db
BK=/tmp/dhl_live_backup.csv
rm -f $DB $DB-wal $DB-shm $BK

cleanup(){ pkill -f "uvicorn hub:app" 2>/dev/null; pkill -f "uvicorn main:app" 2>/dev/null; }
trap cleanup EXIT

start_cloud(){
  (cd $ROOT/cloud && INGEST_TOKEN=dhl-token PUBLIC_URL=https://dhl.roarfun.live \
     EVENT_ID=dhl-the-ring \
     python3 -m uvicorn main:app --host 127.0.0.1 --port 8801 --log-level error \
     > /tmp/dhl_cl.log 2>&1 &)
}
push(){
  (cd $ROOT/booth && HUB_URL=$H CLOUD_URL=$C INGEST_TOKEN=dhl-token \
     SUBSCRIBER_SECONDS=0 LOG_FILE=/tmp/dhl_p.log \
     python3 pusher_hub.py --once --env /nonexistent 2>&1 \
     | grep -E "pushed|synced|rejected|failed|unreachable|retrying")
}
api(){ curl -s -X POST "$1" -H "Content-Type: application/json" -d "$2"; }

(cd $ROOT/booth && HUB_DB=$DB BACKUP_CSV=$BK EVENT_ID=dhl-the-ring \
   python3 -m uvicorn hub:app --host 127.0.0.1 --port 8800 --log-level error \
   > /tmp/dhl_hub.log 2>&1 &)
start_cloud
sleep 5
echo "servers: hub=$(curl -s -o /dev/null -w '%{http_code}' $H/healthz) cloud=$(curl -s -o /dev/null -w '%{http_code}' $C/healthz)"

echo
echo "=== A. registration with full name + country ==="
V1=$(api $H/api/visitors '{"first_name":"David","surname":"Pecl","company":"Germany","actor":"rig1"}' | python3 -c "import json,sys;d=json.load(sys.stdin);print(d['id']);import sys as s" )
api $H/api/visitors '{"first_name":"Anna","surname":"Klein","company":"Austria","actor":"rig2"}' > /dev/null
api $H/api/runs "{\"visitor_id\":\"$V1\",\"lap_ms\":54203}" > /dev/null
curl -s $H/api/leaderboard | python3 -c "
import json,sys;d=json.load(sys.stdin)
e=d['leaderboard'][0]
print(f\"  board: {e['name']}  |  country: {e.get('team')}  |  {e['best_lap']}\")"

echo
echo "=== B. physical CSV backup on disk ==="
python3 - <<PY
import csv
rows=list(csv.DictReader(open("$BK")))
print(f"  {len(rows)} rows written")
for r in rows: print(f"   {r['kind']:9} {r['first_name'] or r['visitor_id'][:8]:10} {r['country']:8} {r['lap']}")
PY

echo
echo "=== C. wall display served locally (offline capable) ==="
printf "  /wall: HTTP %s   " "$(curl -s -o /dev/null -w '%{http_code}' $H/wall)"
printf "reads local feed: %s   " "$(curl -s $H/wall | grep -c '/api/leaderboard')"
printf "cloud refs: %s\n" "$(curl -s $H/wall | grep -c 'roarfun.live')"
printf "  DHL title: %s   yellow: %s   fullscreen btn: %s\n" \
  "$(curl -s $H/wall | grep -c 'DHL The Ring')" \
  "$(curl -s $H/wall | grep -c 'FFCC00')" \
  "$(curl -s $H/wall | grep -c 'requestFullscreen')"

echo
echo "=== D. mirror to cloud ==="
push | sed 's/^/  /'

echo
echo "=== E. opt-in, then MANUAL send (no auto-send) ==="
api $H/api/consent "{\"visitor_id\":\"$V1\",\"phone\":\"+421900123456\",\"channel\":\"sms\",\"consent\":true}" > /dev/null
push | sed 's/^/  /'
echo "  messages after a new lap (must be 0 — manual only):"
api $H/api/runs "{\"visitor_id\":\"$V1\",\"lap_ms\":51000}" > /dev/null
push > /dev/null
curl -s $C/admin/notifications -H "Authorization: Bearer dhl-token" | python3 -c "
import json,sys;d=json.load(sys.stdin);print('   ',len(d['messages']),'messages')"

echo "  operator presses Send position update:"
curl -s -X POST $C/admin/broadcast -H "Authorization: Bearer dhl-token" \
  -H "Content-Type: application/json" -d '{"kind":"position"}' | sed 's/^/    /'
echo "  immediate second press (cooldown):"
curl -s -X POST $C/admin/broadcast -H "Authorization: Bearer dhl-token" \
  -H "Content-Type: application/json" -d '{"kind":"position"}' | sed 's/^/    /'
echo
curl -s $C/admin/notifications -H "Authorization: Bearer dhl-token" | python3 -c "
import json,sys;d=json.load(sys.stdin)
for m in d['messages'][::-1]: print(f\"    [{m['kind']}] {m['to']}: {m['body'][:95]}\")"

echo
echo "=== F. uplink dies — booth keeps working ==="
pkill -f "uvicorn main:app"; sleep 2
api $H/api/visitors '{"first_name":"Petra","surname":"Wolf","company":"Poland"}' > /dev/null
curl -s $H/api/leaderboard | python3 -c "
import json,sys;d=json.load(sys.stdin);print('  booth still registering:',d['count'],'on board')"
push | sed 's/^/  /'

echo
echo "=== G. uplink returns ==="
start_cloud; sleep 5
push | sed 's/^/  /'
curl -s $C/v1/leaderboard.json | python3 -c "
import json,sys;d=json.load(sys.stdin);print('  cloud caught up:',d['count'],'entries, event',d['event'])"
echo "  no message burst after recovery:"
curl -s $C/admin/notifications -H "Authorization: Bearer dhl-token" | python3 -c "
import json,sys;d=json.load(sys.stdin);print('   ',len(d['messages']),'messages (manual only, so expect 0 new)')"
