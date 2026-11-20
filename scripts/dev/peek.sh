#!/usr/bin/env sh
# Dev helper: print recent anomalies, incidents and the latest RCA. Usage: scripts/dev/peek.sh <token-file>
TOK=$(cat "${1:-/tmp/tok}")
API=${API:-http://localhost:8000/api/v1}
date -u +%T
curl -s "$API/anomalies?limit=15" -H "Authorization: Bearer $TOK" | python -c "
import sys,json; d=json.load(sys.stdin); print('anomalies total', d['total'])
for a in d['items']: print(' ', a['detected_at'][11:19], a['service'], a['metric'], a['method'], round(a['score'],3), a['severity'], 'inc', a['incident_id'])"
curl -s "$API/incidents" -H "Authorization: Bearer $TOK" | python -c "
import sys,json; d=json.load(sys.stdin)
for i in d['items'][:5]: print('INC', i['id'], i['title'], i['status'], i['severity'], i['services'], 'recovered', i['recovery_verified_at'])"
ID=$(curl -s "$API/incidents?limit=1" -H "Authorization: Bearer $TOK" | python -c "import sys,json; d=json.load(sys.stdin)['items']; print(d[0]['id'] if d else '')")
[ -n "$ID" ] && curl -s "$API/rca/$ID" -H "Authorization: Bearer $TOK" | python -c "
import sys,json; d=json.load(sys.stdin)
if 'findings' not in d: print(d); sys.exit()
print('RCA', d['status'], d['duration_ms'], 'ms |', d['summary']); print('sources', d['evidence'].get('sources'))
for f in d['findings']:
  print(f\"  #{f['rank']} {f['score']:.3f} {f['confidence']:6s} {f['cause_type']:20s} {f['service']}  loc={f['evidence']['locality']}\")
  if f['rank']<=2:
    for s in f['signals']: print(f\"       {s['contribution']:.3f} = {s['weight']}*{s['strength']:.2f} {s['name']}: {s['value']}\")"
