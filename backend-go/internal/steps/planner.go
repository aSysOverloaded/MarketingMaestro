package steps

import (
	"bytes"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"time"

	"marketing-agent/internal/workflow"
)

// SectionOutline defines the structure of brochure sections
type SectionOutline struct {
	Title  string   `json:"title"`
	Points []string `json:"points"`
}

// PlannerResult holds the output from the Content Planner agent
type PlannerResult struct {
	Sections []SectionOutline `json:"sections"`
}

type PlannerStep struct{}

func NewPlannerStep() *PlannerStep {
	return &PlannerStep{}
}

func (s *PlannerStep) Name() string {
	return "PlannerStep"
}

func (s *PlannerStep) Execute(ctx *workflow.Context) (workflow.Result, error) {
	// 1. Resolve recommendation candidates from ProductRecommenderStep
	recsRaw, ok := ctx.State.StepOutputs["ProductRecommenderStep"]
	if !ok {
		return nil, fmt.Errorf("missing recommendation candidates from ProductRecommenderStep")
	}

	var rec workflow.RecommendationResult
	if slice, ok := recsRaw.([]workflow.RecommendationResult); ok && len(slice) > 0 {
		rec = slice[0]
	} else if single, ok := recsRaw.(workflow.RecommendationResult); ok {
		rec = single
	} else {
		return nil, fmt.Errorf("invalid recommendation candidates structure format")
	}

	// 2. Fetch User Profile segment
	segment := "Family"
	if profRaw, ok := ctx.State.StepOutputs["UserProfileStep"]; ok {
		if profResult, ok := profRaw.(workflow.UserProfileResult); ok {
			segment = profResult.Segment
		}
	}

	// 3. Make HTTP request to Python FastAPI Sidecar Planner
	pythonUrl := os.Getenv("PYTHON_SERVICE_URL")
	if pythonUrl == "" {
		pythonUrl = "http://localhost:8000"
	}

	payload := map[string]interface{}{
		"segment":        segment,
		"recommendation": rec,
	}

	jsonBytes, err := json.Marshal(payload)
	if err != nil {
		return nil, fmt.Errorf("failed to marshal planner payload: %w", err)
	}

	client := &http.Client{Timeout: 10 * time.Second}
	reqUrl := fmt.Sprintf("%s/api/plan", pythonUrl)
	resp, err := client.Post(reqUrl, "application/json", bytes.NewBuffer(jsonBytes))

	if err != nil {
		log.Printf("[TraceID: %s] [JobID: %s] [PlannerStep] [FALLBACK] Failed to contact Python Planner API: %v. Using Go simulation fallback.", ctx.TraceID, ctx.JobID, err)
		return s.executeLocalFallback(ctx, segment, rec)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		log.Printf("[TraceID: %s] [JobID: %s] [PlannerStep] [FALLBACK] Python Planner API returned status code %d. Using Go simulation fallback.", ctx.TraceID, ctx.JobID, resp.StatusCode)
		return s.executeLocalFallback(ctx, segment, rec)
	}

	var result PlannerResult
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return nil, fmt.Errorf("failed to decode planner response: %w", err)
	}

	ctx.State.StepOutputs["PlannerStep"] = result
	return result, nil
}

func (s *PlannerStep) executeLocalFallback(ctx *workflow.Context, segment string, rec workflow.RecommendationResult) (workflow.Result, error) {
	log.Printf("[TraceID: %s] [JobID: %s] [PlannerStep] [FALLBACK] Serving Go content planner simulation fallback.", ctx.TraceID, ctx.JobID)
	// Mock content planner structure
	result := PlannerResult{
		Sections: []SectionOutline{
			{
				Title:  "Welcome and Overview",
				Points: []string{"Acknowledge segment goals", "Intro summary of matched selection suitability"},
			},
			{
				Title:  "Quality & Efficiency Spotlight",
				Points: []string{"Showcase smart features", "Verify certified capabilities"},
			},
		},
	}
	return result, nil
}
