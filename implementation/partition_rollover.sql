\set ON_ERROR_STOP on
SET client_min_messages TO warning;
SET TIME ZONE 'UTC';

DROP SCHEMA IF EXISTS archive CASCADE;
DROP SCHEMA IF EXISTS ops CASCADE;
CREATE SCHEMA ops;
CREATE SCHEMA archive;

CREATE TABLE ops.scada_event (
  source_seq bigint NOT NULL,
  event_id text NOT NULL,
  site_code text NOT NULL,
  turbine_id text NOT NULL,
  event_time timestamptz NOT NULL,
  event_type text NOT NULL,
  severity text NOT NULL CHECK (severity IN ('INFO','WARNING','CRITICAL')),
  power_kw numeric(12,2) NOT NULL CHECK (power_kw >= 0),
  payload jsonb NOT NULL,
  PRIMARY KEY (event_time,event_id),
  UNIQUE (event_time,source_seq)
) PARTITION BY RANGE (event_time);

CREATE TABLE ops.scada_event_202601 PARTITION OF ops.scada_event
  FOR VALUES FROM ('2026-01-01 00:00:00+00') TO ('2026-02-01 00:00:00+00');
CREATE TABLE ops.scada_event_202602 PARTITION OF ops.scada_event
  FOR VALUES FROM ('2026-02-01 00:00:00+00') TO ('2026-03-01 00:00:00+00');
CREATE TABLE ops.scada_event_202603 PARTITION OF ops.scada_event
  FOR VALUES FROM ('2026-03-01 00:00:00+00') TO ('2026-04-01 00:00:00+00');
CREATE TABLE ops.scada_event_202604 PARTITION OF ops.scada_event
  FOR VALUES FROM ('2026-04-01 00:00:00+00') TO ('2026-05-01 00:00:00+00');
CREATE TABLE ops.scada_event_default PARTITION OF ops.scada_event DEFAULT;

CREATE INDEX scada_event_site_time_idx ON ops.scada_event(site_code,event_time);
CREATE INDEX scada_event_time_brin ON ops.scada_event USING brin(event_time);

CREATE TABLE ops.change_stage (
  stage_no integer PRIMARY KEY,
  stage_name text NOT NULL,
  parent_rows bigint NOT NULL,
  default_rows bigint NOT NULL,
  transfer_rows bigint NOT NULL
);

SELECT format('COPY ops.scada_event FROM %L WITH (FORMAT csv,HEADER true)', :'event_csv') \gexec

INSERT INTO ops.change_stage
SELECT 1,'INITIAL_LOAD',count(*),(SELECT count(*) FROM ops.scada_event_default),0
FROM ops.scada_event;

BEGIN;
CREATE TABLE ops.scada_event_202605 (LIKE ops.scada_event INCLUDING ALL);
WITH moved AS (
  DELETE FROM ops.scada_event_default
  WHERE event_time >= TIMESTAMPTZ '2026-05-01 00:00:00+00'
    AND event_time < TIMESTAMPTZ '2026-06-01 00:00:00+00'
  RETURNING *
)
INSERT INTO ops.scada_event_202605 SELECT * FROM moved;

INSERT INTO ops.change_stage
SELECT 2,'MAY_EVACUATED',count(*),(SELECT count(*) FROM ops.scada_event_default),
       (SELECT count(*) FROM ops.scada_event_202605)
FROM ops.scada_event;

ALTER TABLE ops.scada_event_202605 ADD CONSTRAINT scada_event_202605_bound
  CHECK (event_time >= TIMESTAMPTZ '2026-05-01 00:00:00+00'
     AND event_time < TIMESTAMPTZ '2026-06-01 00:00:00+00') NOT VALID;
ALTER TABLE ops.scada_event_202605 VALIDATE CONSTRAINT scada_event_202605_bound;
ALTER TABLE ops.scada_event ATTACH PARTITION ops.scada_event_202605
  FOR VALUES FROM ('2026-05-01 00:00:00+00') TO ('2026-06-01 00:00:00+00');

INSERT INTO ops.change_stage
SELECT 3,'MAY_ATTACHED',count(*),(SELECT count(*) FROM ops.scada_event_default),
       (SELECT count(*) FROM ops.scada_event_202605)
FROM ops.scada_event;

ALTER TABLE ops.scada_event DETACH PARTITION ops.scada_event_202601;
ALTER TABLE ops.scada_event_202601 SET SCHEMA archive;

INSERT INTO ops.change_stage
SELECT 4,'JANUARY_ARCHIVED',count(*),(SELECT count(*) FROM ops.scada_event_default),
       (SELECT count(*) FROM archive.scada_event_202601)
FROM ops.scada_event;
COMMIT;

ANALYZE ops.scada_event;
ANALYZE archive.scada_event_202601;
