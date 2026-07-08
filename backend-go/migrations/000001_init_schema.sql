-- Initial Schema Setup for Personalized Marketing Content Platform

-- 1. Vehicles specification catalog (Deterministic search source)
CREATE TABLE IF NOT EXISTS vehicles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    model VARCHAR(255) NOT NULL UNIQUE,
    base_price NUMERIC(12, 2) NOT NULL,
    engine_specs JSONB NOT NULL, -- e.g. {"horsepower": 248, "mileage": "28 mpg"}
    features TEXT[] NOT NULL,
    colors TEXT[] NOT NULL,
    brochure_pdf_url TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- 2. Workflow Orchestrator Jobs (State machine logs)
CREATE TABLE IF NOT EXISTS brochure_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trace_id VARCHAR(100) NOT NULL UNIQUE,
    user_email VARCHAR(255) NOT NULL,
    current_state VARCHAR(50) NOT NULL, -- Created, Running, Completed, Failed
    job_context JSONB NOT NULL, -- Serialized JobContext struct (versioned payload & step references)
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- 3. Analytics & conversion funnel tracking
CREATE TABLE IF NOT EXISTS analytics_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id UUID REFERENCES brochure_jobs(id) ON DELETE CASCADE,
    trace_id VARCHAR(100) NOT NULL,
    event_type VARCHAR(50) NOT NULL, -- email_open, cta_click, pdf_download
    ip_address VARCHAR(45),
    user_agent TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- 4. Metrics Dashboard tracking table
CREATE TABLE IF NOT EXISTS job_metrics (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id UUID REFERENCES brochure_jobs(id) ON DELETE CASCADE,
    prompt_tokens INT NOT NULL DEFAULT 0,
    completion_tokens INT NOT NULL DEFAULT 0,
    step_latencies JSONB NOT NULL, -- Execution times per step in ms: {"Profiler": 12, "Copywriter": 1820...}
    retry_counts JSONB NOT NULL,   -- Retries per step: {"Copywriter": 1, "PDF_Renderer": 0}
    fallback_triggered BOOLEAN DEFAULT FALSE,
    recommendation_score INT NOT NULL DEFAULT 0,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_brochure_jobs_trace_id ON brochure_jobs(trace_id);
CREATE INDEX IF NOT EXISTS idx_analytics_events_job_id ON analytics_events(job_id);
CREATE INDEX IF NOT EXISTS idx_job_metrics_job_id ON job_metrics(job_id);
