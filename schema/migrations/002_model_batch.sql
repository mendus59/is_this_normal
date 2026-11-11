-- Message batches sent by paid runs. A batch can outlast the job that sent it; the next
-- run collects its results instead of paying for them again.
CREATE TABLE model_batch (
    id           text PRIMARY KEY,                 -- Anthropic batch id
    stage        text NOT NULL,
    run_id       bigint REFERENCES run(id),
    requests     jsonb NOT NULL,                   -- custom_id -> {subject, as_of, hash, tier}
    status       text NOT NULL DEFAULT 'submitted' CHECK (status IN ('submitted', 'collected')),
    created_at   timestamptz NOT NULL DEFAULT now(),
    collected_at timestamptz
);
CREATE INDEX model_batch_open ON model_batch (stage) WHERE status = 'submitted';
