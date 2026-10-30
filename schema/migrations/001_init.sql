-- IsThisNormal core schema. Only the pipeline connects to this database;
-- the public site reads exported files from R2.

CREATE EXTENSION IF NOT EXISTS vector;

-- Every document in the corpus: rulings, laws, statute sections, executive
-- documents, manual action events, dockets, reports, datasets.
CREATE TABLE artifact (
    id               text PRIMARY KEY,          -- e.g. scotus/597-us-215, pl/117-169, eo/14028
    type             text NOT NULL CHECK (type IN (
                         'scotus_case', 'lower_court_opinion', 'docket',
                         'public_law', 'bill', 'usc_section', 'statute_page', 'regulation',
                         'executive_document', 'action_event', 'olc_opinion',
                         'report', 'dataset')),
    subtype          text,                       -- e.g. executive_order, proclamation, memorandum
    title            text NOT NULL,
    effective_date   date,                       -- decided / signed / enacted; drives "as of"
    in_scope         boolean NOT NULL DEFAULT false,
    contentious      boolean,                    -- laws only; null until votes are known
    closure_depth    smallint,                   -- 0 = in scope, 1 = cited directly, 2 = cited by depth 1
    sources          jsonb NOT NULL DEFAULT '[]', -- [{label, url}] official links
    official_summary text,                       -- CRS summary, syllabus, FR abstract (free)
    text_key         text,                       -- R2 key of cleaned text
    text_sha256      text,
    metadata         jsonb NOT NULL DEFAULT '{}',
    ingested_at      timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX artifact_type_date ON artifact (type, effective_date);
CREATE INDEX artifact_in_scope ON artifact (in_scope) WHERE in_scope;

-- Parts of an artifact: syllabus, majority, concurrence, dissent, section, findings.
CREATE TABLE component (
    id          text PRIMARY KEY,                -- artifact id + '#' + slug
    artifact_id text NOT NULL REFERENCES artifact(id) ON DELETE CASCADE,
    kind        text NOT NULL,
    author      text,
    joined_by   text[] NOT NULL DEFAULT '{}',
    ord         integer NOT NULL DEFAULT 0,
    text_key    text
);
CREATE INDEX component_artifact ON component (artifact_id);

-- Text of an artifact over time (statutes, regulations).
CREATE TABLE version (
    artifact_id text NOT NULL REFERENCES artifact(id) ON DELETE CASCADE,
    valid_from  date NOT NULL,
    valid_to    date,
    text_key    text NOT NULL,
    PRIMARY KEY (artifact_id, valid_from)
);

CREATE TABLE citation (
    id              bigserial PRIMARY KEY,
    from_component  text NOT NULL REFERENCES component(id) ON DELETE CASCADE,
    to_artifact     text NOT NULL,               -- may point at an artifact not yet ingested
    pinpoint        text,
    raw_text        text NOT NULL,
    context_snippet text,
    citing_date     date,
    extractor       text NOT NULL,
    UNIQUE (from_component, to_artifact, pinpoint, raw_text)
);
CREATE INDEX citation_to ON citation (to_artifact, citing_date);

-- Litigation against an artifact (or an analog of it).
CREATE TABLE challenge (
    id          bigserial PRIMARY KEY,
    artifact_id text NOT NULL REFERENCES artifact(id) ON DELETE CASCADE,
    docket_id   text NOT NULL,
    filed       date,
    decided     date,
    outcome     text CHECK (outcome IN ('upheld', 'struck', 'enjoined', 'mooted', 'dismissed', 'pending', 'mixed')),
    grounds     text[] NOT NULL DEFAULT '{}',
    UNIQUE (artifact_id, docket_id)
);

-- One paid (or manual) pipeline run.
CREATE TABLE run (
    id             bigserial PRIMARY KEY,
    started_at     timestamptz NOT NULL DEFAULT now(),
    finished_at    timestamptz,
    dispatched_by  text,
    scope          jsonb NOT NULL,
    budget_usd     numeric(10, 2),
    dry_run        boolean NOT NULL DEFAULT true,
    input_tokens   bigint NOT NULL DEFAULT 0,
    cached_tokens  bigint NOT NULL DEFAULT 0,
    output_tokens  bigint NOT NULL DEFAULT 0,
    cost_usd       numeric(10, 4) NOT NULL DEFAULT 0,
    status         text NOT NULL DEFAULT 'running'
);

-- Cached analysis results. input_hash covers every input, so an unchanged
-- input set is served from here instead of calling the model again.
CREATE TABLE analysis (
    id             bigserial PRIMARY KEY,
    kind           text NOT NULL CHECK (kind IN (
                       'summary', 'citation_check', 'authority_digest',
                       'analog_assessment', 'type_assessment', 'score', 'verification')),
    subject_id     text NOT NULL,                -- artifact, component, or citation id
    as_of          date,
    input_hash     text NOT NULL,
    rubric_version text NOT NULL,
    model          text NOT NULL,
    output         jsonb NOT NULL,
    cost_usd       numeric(10, 4) NOT NULL DEFAULT 0,
    run_id         bigint REFERENCES run(id),
    superseded_by  bigint REFERENCES analysis(id),
    created_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (kind, input_hash)
);
CREATE INDEX analysis_subject ON analysis (kind, subject_id, as_of);

CREATE TABLE citation_check (
    citation_id bigint PRIMARY KEY REFERENCES citation(id) ON DELETE CASCADE,
    analysis_id bigint NOT NULL REFERENCES analysis(id),
    verdict     text NOT NULL CHECK (verdict IN (
                    'supports', 'partially', 'not_on_point', 'contradicts', 'mischaracterized'))
);

-- Raw score only; the normalized percentile is computed at publish time.
CREATE TABLE score (
    artifact_id text NOT NULL REFERENCES artifact(id) ON DELETE CASCADE,
    analysis_id bigint NOT NULL REFERENCES analysis(id),
    dimensions  jsonb NOT NULL,
    raw         real NOT NULL CHECK (raw BETWEEN 0 AND 1),
    confidence  real NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    current     boolean NOT NULL DEFAULT true,
    PRIMARY KEY (artifact_id, analysis_id)
);
CREATE UNIQUE INDEX score_current ON score (artifact_id) WHERE current;

CREATE TABLE flag (
    id          bigserial PRIMARY KEY,
    citation_id bigint NOT NULL REFERENCES citation(id) ON DELETE CASCADE,
    kind        text NOT NULL CHECK (kind IN ('structural', 'substantive', 'contested')),
    severity    smallint NOT NULL DEFAULT 1,
    evidence    jsonb NOT NULL DEFAULT '[]',
    verified_by bigint REFERENCES analysis(id),
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- Embeddings from a local model (free). Limited to summaries and sections.
CREATE TABLE embedding (
    subject_id text NOT NULL,
    model      text NOT NULL,
    vec        vector(384) NOT NULL,
    PRIMARY KEY (subject_id, model)
);

-- Incremental ingestion bookmarks, one per source.
CREATE TABLE ingest_cursor (
    source     text PRIMARY KEY,
    cursor     jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
