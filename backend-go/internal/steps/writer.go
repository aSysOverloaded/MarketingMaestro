package steps

import (
	"bytes"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"time"

	"marketing-agent/internal/recommendation"
	"marketing-agent/internal/workflow"
)

type WriterResult struct {
	Headline    string   `json:"headline"`
	Subheadline string   `json:"subheadline"`
	Paragraphs  []string `json:"paragraphs"`
	CTA         string   `json:"cta"`
}

type WriterStep struct{}

func NewWriterStep() *WriterStep {
	return &WriterStep{}
}

func (s *WriterStep) Name() string {
	return "WriterStep"
}

func (s *WriterStep) Execute(ctx *workflow.Context) (workflow.Result, error) {
	// 1. Ingest segment, sections, candidate specs
	segment := "Family"
	if profRaw, ok := ctx.State.StepOutputs["UserProfileStep"]; ok {
		if profResult, ok := profRaw.(workflow.UserProfileResult); ok {
			segment = profResult.Segment
		}
	}

	var sections []SectionOutline
	if planRaw, ok := ctx.State.StepOutputs["PlannerStep"]; ok {
		if planResult, ok := planRaw.(PlannerResult); ok {
			sections = planResult.Sections
		}
	}

	var candidate recommendation.Product
	if specsRaw, ok := ctx.State.StepOutputs["ProductRecommenderStep_Specs"]; ok {
		if specs, ok := specsRaw.([]recommendation.Product); ok && len(specs) > 0 {
			candidate = specs[0]
		}
	}

	// 2. Call Python FastAPI Writer
	pythonUrl := os.Getenv("PYTHON_SERVICE_URL")
	if pythonUrl == "" {
		pythonUrl = "http://localhost:8000"
	}

	payload := map[string]interface{}{
		"segment":   segment,
		"sections":  sections,
		"candidate": candidate,
	}

	jsonBytes, err := json.Marshal(payload)
	if err != nil {
		return nil, fmt.Errorf("failed to marshal writer payload: %w", err)
	}

	client := &http.Client{Timeout: 15 * time.Second}
	reqUrl := fmt.Sprintf("%s/api/write", pythonUrl)
	resp, err := client.Post(reqUrl, "application/json", bytes.NewBuffer(jsonBytes))

	if err != nil {
		log.Printf("[TraceID: %s] [JobID: %s] [WriterStep] [WARNING] Failed to contact Python Writer API: %v. Using local simulation fallback.", ctx.TraceID, ctx.JobID, err)
		return s.executeLocalFallback(segment, candidate)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		log.Printf("[TraceID: %s] [JobID: %s] [WriterStep] [WARNING] Python Writer API returned status code %d. Using local simulation fallback.", ctx.TraceID, ctx.JobID, resp.StatusCode)
		return s.executeLocalFallback(segment, candidate)
	}

	var result WriterResult
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return nil, fmt.Errorf("failed to decode writer response: %w", err)
	}

	ctx.State.StepOutputs["WriterStep"] = result
	return result, nil
}

func (s *WriterStep) executeLocalFallback(segment string, candidate recommendation.Product) (workflow.Result, error) {
	modelName := candidate.Model
	if modelName == "" {
		modelName = "Premium Selection"
	}
	result := WriterResult{
		Headline:    "Dynamic Living Meets Modern Performance",
		Subheadline: fmt.Sprintf("Tailored perfectly for your %s lifestyle parameters.", segment),
		Paragraphs: []string{
			fmt.Sprintf("We are excited to spotlight the %s. Engineered to meet the high standards of a %s profile, this choice blends premium capacity with smart convenience features.", modelName, segment),
			"With state-of-the-art efficiency, whisper-quiet operations, and fingerprint resistant finishes, this selection elevates your everyday routine seamlessly.",
		},
		CTA: "Arrange a live interactive demonstration or consult with a product specialist today.",
	}
	return result, nil
}
