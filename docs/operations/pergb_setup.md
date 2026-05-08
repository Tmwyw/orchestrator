# Pay-per-GB setup (Wave B-8 session 2)

End-to-end checklist to bring up the first datacenter_pergb SKU
(`dc_pergb_de`) on `chernika @ 95.217.98.125`. Run as user with
sudo + the orchestrator's `netrun` system user. Order matters:
schema first, seed second, scheduler third, smoke last.

## 1. Pull and install

```bash
cd /opt/netrun-orchestrator
git pull origin main
.venv/bin/pip install -r requirements.txt   # no new deps expected
```

## 2. Apply migrations 024 + 025

```bash
sudo -u postgres psql -d netrun_orchestrator -f migrations/024_sku_tiers.sql
sudo -u postgres psql -d netrun_orchestrator -f migrations/025_pergb_de_sku_seed.sql
```

Both migrations are idempotent (`CREATE TABLE IF NOT EXISTS`,
`ON CONFLICT DO NOTHING`) — re-applying is safe.

Verify rows:

```bash
sudo -u postgres psql -d netrun_orchestrator -c \
  "SELECT id, code, product_kind, geo_code, price_per_gb FROM skus WHERE code='dc_pergb_de';"
sudo -u postgres psql -d netrun_orchestrator -c \
  "SELECT gb, price_per_gb FROM sku_tiers
     WHERE sku_id=(SELECT id FROM skus WHERE code='dc_pergb_de')
     ORDER BY gb;"
```

Expected: 1 SKU row + 6 tier rows (1/3/5/10/20/30 GB at
$1.20/$1.10/$1.00/$0.95/$0.85/$0.80).

## 3. Install + enable traffic-poll systemd unit

The unit template ships in-repo; copy it into systemd's path,
reload, and enable. The unit polls per-account traffic counters
on the nodes every 30s.

```bash
sudo cp deploy/systemd/netrun-orchestrator-traffic-poll.service.template \
        /etc/systemd/system/netrun-orchestrator-traffic-poll.service
sudo systemctl daemon-reload
sudo systemctl enable --now netrun-orchestrator-traffic-poll
```

Verify it's polling:

```bash
sudo journalctl -u netrun-orchestrator-traffic-poll -f
```

You should see a polling cycle log entry roughly every 30 seconds.

## 4. Restart orchestrator (pick up endpoint change)

```bash
sudo systemctl restart netrun-orchestrator
sudo systemctl status netrun-orchestrator --no-pager
```

## 5. Smoke: catalog endpoint returns the new SKU with tiers

```bash
curl -s -H "X-Netrun-Api-Key: $ORCHESTRATOR_API_KEY" \
  http://127.0.0.1:8090/v1/skus/active | jq .
```

Expected shape (truncated):

```json
{
  "success": true,
  "count": 6,
  "items": [
    { "code": "ipv6_jp", "product_kind": "ipv6_per_piece", "tiers": null, ... },
    ...
    {
      "code": "dc_pergb_de",
      "product_kind": "datacenter_pergb",
      "name": "Pay-per-GB Datacenter",
      "tiers": [
        {"gb": 1,  "price_per_gb": "1.20"},
        {"gb": 3,  "price_per_gb": "1.10"},
        {"gb": 5,  "price_per_gb": "1.00"},
        {"gb": 10, "price_per_gb": "0.95"},
        {"gb": 20, "price_per_gb": "0.85"},
        {"gb": 30, "price_per_gb": "0.80"}
      ]
    }
  ]
}
```

## 6. End-to-end via @netrun_test_bot

1. `/products` — confirm the pergb section shows up with all 6 tiers.
2. `/buy` → choose **Germany Pay-per-GB** → choose **1 GB** → confirm.
3. Bot should hand back a SOCKS5 proxy with a 1 GB byte quota
   tracked by the traffic-poll worker. As traffic accumulates,
   the bot's status command reflects bytes_used vs bytes_quota.

## Rollback

If the pergb SKU misbehaves, soft-disable without dropping data:

```bash
sudo -u postgres psql -d netrun_orchestrator -c \
  "UPDATE skus SET is_active=FALSE WHERE code='dc_pergb_de';"
```

The catalog endpoint will stop advertising it on the next call;
existing accounts keep working until expiry.
