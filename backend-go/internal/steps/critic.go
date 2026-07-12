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

type CriticResult struct {
	Passed   bool   `json:"passed"`
	Feedback string `json:"feedback"`
}

type CriticStep struct{}

func NewCriticStep() *CriticStep {
	return &CriticStep{}
}

func (s *CriticStep) Name() string {
	return "CriticStep"
}

func (s *CriticStep) Execute(ctx *workflow.Context) (workflow.Result, error) {
	// 1. Ingest generated copy and candidate specs
	var copyData WriterResult
	if writerRaw, ok := ctx.State.StepOutputs["WriterStep"]; ok {
		if writerResult, ok := writerRaw.(WriterResult); ok {
			copyData = writerResult
		}
	}

	var candidate recommendation.Product
	if specsRaw, ok := ctx.State.StepOutputs["ProductRecommenderStep_Specs"]; ok {
		if specs, ok := specsRaw.([]recommendation.Product); ok && len(specs) > 0 {
			candidate = specs[0]
		}
	}

	// 2. Call Python FastAPI Critic
	pythonUrl := os.Getenv("PYTHON_SERVICE_URL")
	if pythonUrl == "" {
		pythonUrl = "http://localhost:8000"
	}

	payload := map[string]interface{}{
		"copy":      copyData,
		"candidate": candidate,
	}

	jsonBytes, err := json.Marshal(payload)
	if err != nil {
		return nil, fmt.Errorf("failed to marshal critic payload: %w", err)
	}

	client := &http.Client{Timeout: 10 * time.Second}
	reqUrl := fmt.Sprintf("%s/api/critic", pythonUrl)
	resp, err := client.Post(reqUrl, "application/json", bytes.NewBuffer(jsonBytes))

	if err != nil {
		log.Printf("[TraceID: %s] [JobID: %s] [CriticStep] [WARNING] Failed to contact Python Critic API: %v. Proceeding in safety bypass mode.", ctx.TraceID, ctx.JobID, err)
		return CriticResult{Passed: true, Feedback: "Bypassed: critic worker offline"}, nil
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		log.Printf("[TraceID: %s] [JobID: %s] [CriticStep] [WARNING] Python Critic API status %d. Proceeding in bypass mode.", ctx.TraceID, ctx.JobID, resp.StatusCode)
		return CriticResult{Passed: true, Feedback: "Bypassed: critic error status"}, nil
	}

	var result CriticResult
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return nil, fmt.Errorf("failed to decode critic response: %w", err)
	}

	ctx.State.StepOutputs["CriticStep"] = result

	// If enable_critic flag is true, check outcome
	if os.Getenv("ENABLE_CRITIC") != "false" {
		if !result.Passed {
			return nil, fmt.Errorf("Critic specification audit rejected copy: %s", result.Feedback)
		}
	}

	return result, nil
}
