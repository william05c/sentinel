-- SENTINEL warehouse schema (v1)
--
-- Two conventions run through every fact table:
--   1. PROVENANCE columns are mandatory: source_system, source_url, retrieved_at,
--      accession_no, document_sha256, parser_version, extractor_run_id, schema_version.
--      A row that cannot be traced is a bug, not an approximation.
--   2. REVISIONS ARE KEPT. fact_xbrl holds one row per reported fact per filing.
--      A restatement adds rows; it never overwrites. The latest view is a query.

CREATE TABLE IF NOT EXISTS dim_company (
    cik                BIGINT PRIMARY KEY,
    name               VARCHAR NOT NULL,
    former_names       VARCHAR,
    entity_type        VARCHAR,
    sic                VARCHAR,
    sic_description    VARCHAR,
    fiscal_year_end    VARCHAR,
    state_of_inc       VARCHAR,
    status             VARCHAR DEFAULT 'active',
    first_filing_date  DATE,
    last_filing_date   DATE,
    tickers            VARCHAR,
    exchanges          VARCHAR,
    source_system      VARCHAR,
    source_url         VARCHAR,
    retrieved_at       TIMESTAMP,
    schema_version     INTEGER
);

CREATE TABLE IF NOT EXISTS dim_security (
    cik           BIGINT,
    ticker        VARCHAR,
    exchange      VARCHAR,
    share_class   VARCHAR,
    is_primary    BOOLEAN DEFAULT TRUE,
    valid_from    DATE,
    valid_to      DATE,
    source_system VARCHAR,
    retrieved_at  TIMESTAMP,
    schema_version INTEGER
);

CREATE TABLE IF NOT EXISTS dim_index_membership (
    index_code    VARCHAR,
    cik           BIGINT,
    valid_from    DATE,
    valid_to      DATE,
    source        VARCHAR,
    retrieved_at  TIMESTAMP,
    schema_version INTEGER
);

CREATE TABLE IF NOT EXISTS fact_filing (
    accession         VARCHAR PRIMARY KEY,
    cik               BIGINT,
    form_type         VARCHAR,
    filed_date        DATE,
    acceptance_dt     TIMESTAMP,
    period_end        DATE,
    items             VARCHAR,
    document_count    INTEGER,
    primary_document  VARCHAR,
    primary_desc      VARCHAR,
    is_amendment      BOOLEAN,
    document_sha256   VARCHAR,
    source_system     VARCHAR,
    source_url        VARCHAR,
    retrieved_at      TIMESTAMP,
    parser_version    VARCHAR,
    extractor_run_id  VARCHAR,
    schema_version    INTEGER
);

CREATE TABLE IF NOT EXISTS fact_xbrl (
    cik               BIGINT,
    accession         VARCHAR,
    taxonomy          VARCHAR,
    tag               VARCHAR,
    label             VARCHAR,
    unit              VARCHAR,
    value             DOUBLE,
    start_date        DATE,
    end_date          DATE,
    period_type       VARCHAR,
    fiscal_year       INTEGER,
    fiscal_period     VARCHAR,
    form              VARCHAR,
    filed             DATE,
    accepted_at       TIMESTAMP,
    frame             VARCHAR,
    source_system     VARCHAR,
    source_url        VARCHAR,
    retrieved_at      TIMESTAMP,
    document_sha256   VARCHAR,
    parser_version    VARCHAR,
    extractor_run_id  VARCHAR,
    schema_version    INTEGER
);

CREATE TABLE IF NOT EXISTS fact_xbrl_canonical (
    cik               BIGINT,
    metric_id         VARCHAR,
    period_type       VARCHAR,
    start_date        DATE,
    end_date          DATE,
    fiscal_year       INTEGER,
    fiscal_period     VARCHAR,
    value             DOUBLE,
    unit              VARCHAR,
    tag_used          VARCHAR,
    priority          INTEGER,
    n_candidates      INTEGER,
    accession         VARCHAR,
    accepted_at       TIMESTAMP,
    built_at          TIMESTAMP,
    schema_version    INTEGER
);

CREATE TABLE IF NOT EXISTS map_tag (
    taxonomy     VARCHAR,
    tag          VARCHAR,
    metric_id    VARCHAR,
    priority     INTEGER,
    sign         INTEGER DEFAULT 1,
    unit         VARCHAR,
    notes        VARCHAR,
    PRIMARY KEY (taxonomy, tag)
);

CREATE TABLE IF NOT EXISTS fact_price (
    cik            BIGINT,
    ticker         VARCHAR,
    trade_date     DATE,
    open           DOUBLE,
    high           DOUBLE,
    low            DOUBLE,
    close          DOUBLE,
    volume         DOUBLE,
    adj_close      DOUBLE,
    adj_factor     DOUBLE,
    source_system  VARCHAR,
    source_url     VARCHAR,
    retrieved_at   TIMESTAMP,
    schema_version INTEGER
);

CREATE TABLE IF NOT EXISTS fact_ownership (
    form_type          VARCHAR,
    accession          VARCHAR,
    filer_cik          BIGINT,
    issuer_cik         BIGINT,
    insider_name       VARCHAR,
    relationship       VARCHAR,
    transaction_code   VARCHAR,
    shares             DOUBLE,
    price              DOUBLE,
    shares_after       DOUBLE,
    transaction_date   DATE,
    filed_date         DATE,
    source_system      VARCHAR,
    source_url         VARCHAR,
    retrieved_at       TIMESTAMP,
    schema_version     INTEGER
);

CREATE TABLE IF NOT EXISTS fact_13f (
    accession      VARCHAR,
    manager_cik    BIGINT,
    period         DATE,
    cusip          VARCHAR,
    issuer_cik     BIGINT,
    shares         DOUBLE,
    value_usd      DOUBLE,
    put_call       VARCHAR,
    discretion     VARCHAR,
    filed_date     DATE,
    source_system  VARCHAR,
    retrieved_at   TIMESTAMP,
    schema_version INTEGER
);

CREATE TABLE IF NOT EXISTS fact_corporate_action (
    cik            BIGINT,
    action_type    VARCHAR,
    effective_date DATE,
    ratio          DOUBLE,
    cash_amount    DOUBLE,
    counterparty   VARCHAR,
    accession      VARCHAR,
    source_system  VARCHAR,
    retrieved_at   TIMESTAMP,
    schema_version INTEGER
);

CREATE TABLE IF NOT EXISTS dim_document_section (
    accession      VARCHAR,
    section_id     VARCHAR,
    section_name   VARCHAR,
    text           VARCHAR,
    char_count     INTEGER,
    sentiment_pos  DOUBLE,
    sentiment_neg  DOUBLE,
    sentiment_unc  DOUBLE,
    fog_index      DOUBLE,
    content_sha256 VARCHAR,
    parser_version VARCHAR,
    retrieved_at   TIMESTAMP,
    schema_version INTEGER
);

CREATE TABLE IF NOT EXISTS fact_news (
    news_id       VARCHAR,
    source        VARCHAR,
    url           VARCHAR,
    published_at  TIMESTAMP,
    title         VARCHAR,
    body          VARCHAR,
    tickers       VARCHAR,
    sentiment     DOUBLE,
    cluster_id    VARCHAR,
    retrieved_at  TIMESTAMP,
    schema_version INTEGER
);

CREATE TABLE IF NOT EXISTS fact_macro (
    series_id      VARCHAR,
    obs_date       DATE,
    value          DOUBLE,
    unit           VARCHAR,
    source_system  VARCHAR,
    retrieved_at   TIMESTAMP,
    schema_version INTEGER
);

CREATE TABLE IF NOT EXISTS crawl_log (
    ts          TIMESTAMP,
    url         VARCHAR,
    status      INTEGER,
    bytes       INTEGER,
    ms          INTEGER,
    attempt     INTEGER,
    cache_hit   BOOLEAN,
    user_agent  VARCHAR,
    source      VARCHAR
);

CREATE TABLE IF NOT EXISTS dq_issue (
    check_id    VARCHAR,
    severity    VARCHAR,
    entity      VARCHAR,
    detail      VARCHAR,
    detected_at TIMESTAMP,
    schema_version INTEGER
);

CREATE TABLE IF NOT EXISTS ingest_run (
    run_id       VARCHAR PRIMARY KEY,
    started_at   TIMESTAMP,
    finished_at  TIMESTAMP,
    source       VARCHAR,
    subject      VARCHAR,
    rows_written BIGINT,
    status       VARCHAR,
    detail       VARCHAR,
    parser_version VARCHAR,
    schema_version INTEGER
);

CREATE TABLE IF NOT EXISTS finding (
    finding_id        VARCHAR PRIMARY KEY,
    created_at        TIMESTAMP,
    title             VARCHAR,
    claim             VARCHAR,
    entities          VARCHAR,
    sample_period     VARCHAR,
    method            VARCHAR,
    data_refs         VARCHAR,
    effect_size       DOUBLE,
    ci_low            DOUBLE,
    ci_high           DOUBLE,
    p_value           DOUBLE,
    q_value           DOUBLE,
    n_obs             INTEGER,
    stability         VARCHAR,
    grade             VARCHAR,
    falsifier         VARCHAR,
    status            VARCHAR,
    rerun_command     VARCHAR,
    reviewer_note     VARCHAR,
    schema_version    INTEGER
);

CREATE TABLE IF NOT EXISTS report_run (
    run_id          VARCHAR PRIMARY KEY,
    kind            VARCHAR,
    subject         VARCHAR,
    as_of           DATE,
    generated_at    TIMESTAMP,
    snapshot_hash   VARCHAR,
    artifact_paths  VARCHAR,
    verified        BOOLEAN,
    generator_version VARCHAR,
    schema_version  INTEGER
);

CREATE TABLE IF NOT EXISTS data_snapshot (
    snapshot_hash VARCHAR PRIMARY KEY,
    created_at    TIMESTAMP,
    table_counts  VARCHAR,
    detail        VARCHAR,
    schema_version INTEGER
);
