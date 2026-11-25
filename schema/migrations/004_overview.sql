-- Plain-language summaries for readers are stored as analysis rows of kind 'overview'.
ALTER TABLE analysis DROP CONSTRAINT IF EXISTS analysis_kind_check;
ALTER TABLE analysis ADD CONSTRAINT analysis_kind_check CHECK (kind IN (
    'summary', 'citation_check', 'authority_digest', 'analog_assessment', 'type_assessment',
    'score', 'verification', 'citation_use', 'overview'));
