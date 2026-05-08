-- migrations/027_dns_pool.sql
-- Per-port DNS pool: ISP resolvers per country, curated by orchestrator cron,
-- consumed by proxyyy_automated.sh via --dns-pool csv.
-- Idempotent: ON CONFLICT (geo_code, ip) on dns_pool.

CREATE TABLE IF NOT EXISTS dns_pool (
    id                   SERIAL PRIMARY KEY,
    geo_code             TEXT NOT NULL,
    ip                   TEXT NOT NULL,
    asn                  INT,
    isp_name             TEXT,
    city                 TEXT,
    last_check_at        TIMESTAMPTZ,
    last_check_ok        BOOL NOT NULL DEFAULT false,
    latency_ms           INT,
    consecutive_failures INT NOT NULL DEFAULT 0,
    enabled              BOOL NOT NULL DEFAULT true,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (geo_code, ip)
);

CREATE INDEX IF NOT EXISTS dns_pool_geo_active_idx
    ON dns_pool (geo_code)
    WHERE enabled = true AND last_check_ok = true;

-- Seed: ~10 verified ISP DNS resolvers per active geo (JP, NL, US, PL, IN).
-- Selected from public ISP-operated recursive resolvers, ASNs avoid the
-- hardcoded datacenter/CDN blacklist enforced in orchestrator/dns_pool.py.
-- last_check_ok=true so the pool is usable BEFORE the first cron healthcheck.
-- last_check_at=NOW() so the cron's "stale" filter doesn't skip them.
INSERT INTO dns_pool (geo_code, ip, asn, isp_name, last_check_ok, last_check_at) VALUES
  -- JP: NTT, KDDI, IIJ, BIGLOBE, SAKURA, SoftBank
  ('JP', '203.112.2.4',     4713,  'NTT Communications',  true, NOW()),
  ('JP', '203.112.2.5',     4713,  'NTT Communications',  true, NOW()),
  ('JP', '210.196.3.183',   2516,  'KDDI',                true, NOW()),
  ('JP', '210.139.6.67',    2516,  'KDDI',                true, NOW()),
  ('JP', '210.196.166.100', 2516,  'KDDI',                true, NOW()),
  ('JP', '202.32.66.180',   2497,  'IIJ',                 true, NOW()),
  ('JP', '202.232.2.2',     2497,  'IIJ',                 true, NOW()),
  ('JP', '218.219.250.155', 2519,  'NEC BIGLOBE',         true, NOW()),
  ('JP', '202.214.86.180',  9370,  'SAKURA Internet',     true, NOW()),
  ('JP', '202.181.97.6',    17676, 'SoftBank',            true, NOW()),

  -- NL: KPN, XS4ALL, Ziggo, BIT
  ('NL', '194.109.6.66',    3265,  'XS4ALL',              true, NOW()),
  ('NL', '194.109.6.67',    3265,  'XS4ALL',              true, NOW()),
  ('NL', '195.121.1.34',    286,   'KPN',                 true, NOW()),
  ('NL', '195.121.1.66',    286,   'KPN',                 true, NOW()),
  ('NL', '213.46.228.196',  6830,  'Ziggo (Liberty Global)', true, NOW()),
  ('NL', '213.46.228.197',  6830,  'Ziggo (Liberty Global)', true, NOW()),
  ('NL', '213.116.0.50',    286,   'KPN',                 true, NOW()),
  ('NL', '213.116.0.59',    286,   'KPN',                 true, NOW()),
  ('NL', '195.121.40.52',   286,   'KPN',                 true, NOW()),
  ('NL', '195.66.241.10',   12859, 'BIT',                 true, NOW()),

  -- US: Level3/CenturyLink, Comcast, Charter
  ('US', '4.2.2.1',         3356,  'Level 3 / CenturyLink', true, NOW()),
  ('US', '4.2.2.2',         3356,  'Level 3 / CenturyLink', true, NOW()),
  ('US', '4.2.2.3',         3356,  'Level 3 / CenturyLink', true, NOW()),
  ('US', '4.2.2.4',         3356,  'Level 3 / CenturyLink', true, NOW()),
  ('US', '209.244.0.3',     3356,  'Level 3 / CenturyLink', true, NOW()),
  ('US', '209.244.0.4',     3356,  'Level 3 / CenturyLink', true, NOW()),
  ('US', '75.75.75.75',     7922,  'Comcast',             true, NOW()),
  ('US', '75.75.76.76',     7922,  'Comcast',             true, NOW()),
  ('US', '24.30.18.51',     7922,  'Comcast',             true, NOW()),
  ('US', '71.10.216.1',     20115, 'Charter Communications', true, NOW()),

  -- PL: Orange Polska, Netia, Plus, Vectra, Multimedia, UPC, Toya
  ('PL', '194.204.152.34',  5617,  'Orange Polska (TPNet)', true, NOW()),
  ('PL', '194.204.159.1',   5617,  'Orange Polska (TPNet)', true, NOW()),
  ('PL', '213.158.194.1',   12741, 'Netia',               true, NOW()),
  ('PL', '217.96.49.10',    8374,  'Polkomtel (Plus)',    true, NOW()),
  ('PL', '80.50.144.10',    29314, 'Vectra',              true, NOW()),
  ('PL', '213.241.79.27',   21021, 'Multimedia Polska',   true, NOW()),
  ('PL', '156.17.5.1',      8501,  'PIONIER (academic)',  true, NOW()),
  ('PL', '213.180.141.140', 12476, 'Onet',                true, NOW()),
  ('PL', '195.114.181.34',  6830,  'UPC Poland (Liberty Global)', true, NOW()),
  ('PL', '84.10.71.155',    16287, 'Toya',                true, NOW()),

  -- IN: BSNL, Sify, Tata, Bharti Airtel
  ('IN', '218.248.255.146', 9829,  'BSNL',                true, NOW()),
  ('IN', '218.248.255.147', 9829,  'BSNL',                true, NOW()),
  ('IN', '117.239.97.107',  9829,  'BSNL',                true, NOW()),
  ('IN', '202.56.215.54',   9583,  'Sify',                true, NOW()),
  ('IN', '202.56.215.55',   9583,  'Sify',                true, NOW()),
  ('IN', '124.124.6.10',    4755,  'Tata Communications', true, NOW()),
  ('IN', '14.140.135.1',    4755,  'Tata Tele',           true, NOW()),
  ('IN', '122.180.252.7',   9498,  'Bharti Airtel',       true, NOW()),
  ('IN', '122.180.252.6',   9498,  'Bharti Airtel',       true, NOW()),
  ('IN', '14.141.69.117',   9498,  'Bharti Airtel',       true, NOW())
ON CONFLICT (geo_code, ip) DO NOTHING;
