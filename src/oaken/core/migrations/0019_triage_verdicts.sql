-- triage_verdicts: a triager's verdict for one message (ADR 0017). It was an enrichment named
-- 'triage'; a triager is not an enricher, so it has a table of its own. The cache key is
-- (content_hash, triager, version, model_id), as for an enrichment. Like enrichments these live in
-- raw.db: they cost model time to make.
CREATE TABLE triage_verdicts (
    message_id   TEXT NOT NULL PRIMARY KEY,
    triager      TEXT NOT NULL,
    version      TEXT NOT NULL,
    model_id     TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    data_json    TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE INDEX triage_verdicts_key ON triage_verdicts (content_hash, triager, version, model_id);

-- Move each stored verdict over, then delete the old row. The stored `source` says who rated it:
-- a model, the reader's rules, or the built-in checks. Nothing is lost: the data is copied as is.
INSERT INTO triage_verdicts (message_id, triager, version, model_id, content_hash, data_json, created_at)
SELECT message_id,
       CASE json_extract(data_json, '$.source')
           WHEN 'llm' THEN 'ai'
           WHEN 'custom' THEN 'rules'
           ELSE 'spamcheck'
       END,
       version, model_id, content_hash, data_json, created_at
  FROM enrichments
 WHERE enricher = 'triage';
DELETE FROM enrichments WHERE enricher = 'triage';
