-- How each citation is used: appropriate, a stretch, or improper, judged from the citing
-- passage and the cited authority as of the citing item's as-of date.
ALTER TABLE analysis DROP CONSTRAINT IF EXISTS analysis_kind_check;
ALTER TABLE analysis ADD CONSTRAINT analysis_kind_check CHECK (kind IN (
    'summary', 'citation_check', 'authority_digest', 'analog_assessment', 'type_assessment',
    'score', 'verification', 'citation_use'));

CREATE TABLE citation_use (
    citation_id     bigint PRIMARY KEY REFERENCES citation(id) ON DELETE CASCADE,
    analysis_id     bigint NOT NULL REFERENCES analysis(id),
    first_verdict   text NOT NULL,
    second_verdict  text,                          -- set when the first pass said improper
    second_analysis bigint REFERENCES analysis(id),
    verdict         text NOT NULL CHECK (verdict IN ('appropriate', 'stretch', 'improper', 'insufficient')),
    reason          text NOT NULL DEFAULT '',
    quote_found     boolean,                       -- free check: the passage's quotation appears in the cited text
    checked_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX citation_use_verdict ON citation_use (verdict);
