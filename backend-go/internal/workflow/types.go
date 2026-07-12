package workflow

import (
	"context"
	"time"
)

// Context wraps standard context.Context and attaches job/trace properties
type Context struct {
	context.Context
	JobID   string
	TraceID string
	State   *JobContext
}

// NewContext creates a new workflow context
func NewContext(ctx context.Context, jobID, traceID string, state *JobContext) *Context {
	if state == nil {
		state = &JobContext{}
	}
	return &Context{
		Context: ctx,
		JobID:   jobID,
		TraceID: traceID,
		State:   state,
	}
}

// Result is a marker interface for step outputs
type Result interface{}

// Step represents a stateless, forward-only business task inside the engine
type Step interface {
	Name() string
	Execute(ctx *Context) (Result, error)
}

// Compensatable defines steps that require rollback capability on subsequent failures
type Compensatable interface {
	Step
	Compensate(ctx *Context) error
}

// JobContext holds orchestration state references and metadata
type JobContext struct {
	JobID            string               `json:"job_id"`
	TraceID          string               `json:"trace_id"`
	WorkflowState    string               `json:"workflow_state"` // Created, Running, Completed, Failed
	Metadata         JobMetadata          `json:"metadata"`
	UserProfileID    string               `json:"user_profile_id,omitempty"`
	RecommendationID string               `json:"recommendation_id,omitempty"`
	ContentPlanID    string               `json:"content_plan_id,omitempty"`
	GeneratedCopyID  string               `json:"generated_copy_id,omitempty"`
	PDFObjectKey     string               `json:"pdf_object_key,omitempty"`
	PDFBucket        string               `json:"pdf_bucket,omitempty"`
	EmailReceiptID   string               `json:"email_receipt_id,omitempty"`
	StepOutputs      map[string]interface{} `json:"step_outputs,omitempty"` // For local testing and debug logging
}

// JobMetadata stores orchestration parameters and config versions
type JobMetadata struct {
	WorkflowVersion string       `json:"workflow_version"`
	PromptConfig    PromptConfig `json:"prompt_config"`
	UpdatedAt       time.Time    `json:"updated_at"`
}

// PromptConfig manages prompt versioning and model settings
type PromptConfig struct {
	PromptID    string  `json:"prompt_id"`
	PromptHash  string  `json:"prompt_hash"`
	Model       string  `json:"model"`
	Temperature float64 `json:"temperature"`
	TopP        float64 `json:"top_p"`
}

// Standard Step Result Structs

type UserProfileResult struct {
	UserProfileID string                 `json:"user_profile_id"`
	Segment       string                 `json:"segment"`
	BudgetTier    string                 `json:"budget_tier"`
	Attributes    map[string]interface{} `json:"attributes"`
}

type RecommendationResult struct {
	RecommendationID string   `json:"recommendation_id"`
	ProductID        string   `json:"product_id"`
	Score            int      `json:"score"`
	MatchedRules     []string `json:"matched_rules"`
	Explanation      string   `json:"explanation"`
}

type ContentPlanResult struct {
	ContentPlanID  string                   `json:"content_plan_id"`
	LayoutSections []map[string]interface{} `json:"layout_sections"`
}

type GeneratedCopyResult struct {
	GeneratedCopyID string            `json:"generated_copy_id"`
	CopyData        map[string]string `json:"copy_data"` // headline, subheadline, CTA, etc.
}

type PDFRenderResult struct {
	PDFObjectKey string `json:"pdf_object_key"`
	PDFBucket    string `json:"pdf_bucket"`
}

type EmailDispatchResult struct {
	EmailReceiptID string `json:"email_receipt_id"`
	Sent           bool   `json:"sent"`
}
