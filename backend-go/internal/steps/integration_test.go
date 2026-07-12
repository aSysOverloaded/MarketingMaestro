package steps

import (
	"context"
	"os"
	"path/filepath"
	"testing"

	"marketing-agent/internal/recommendation"
	"marketing-agent/internal/workflow"
)

func TestFullMVPWorkflowExecution(t *testing.T) {
	// 1. Setup workspace directories for testing
	baseDir, err := os.Getwd()
	if err != nil {
		t.Fatalf("Failed to resolve current working directory: %v", err)
	}

	// Paths relative to backend-go/internal/steps directory
	templatesDir := filepath.Join(baseDir, "..", "..", "templates")
	tempHtmlDir := filepath.Join(baseDir, "..", "..", "storage", "temp_brochures")
	tempPdfDir := filepath.Join(baseDir, "..", "..", "storage", "generated_brochures")
	tempEmailDir := filepath.Join(baseDir, "..", "..", "storage", "sent_emails")

	// Ensure clean testing workspace
	os.RemoveAll(tempHtmlDir)
	os.RemoveAll(tempPdfDir)
	os.RemoveAll(tempEmailDir)

	// 2. Initialize Steps
	stepProfile := NewUserProfileStep()
	stepRecommend := NewProductRecommenderStep(recommendation.NewProductMatcher())
	stepCompile := NewCompileHTMLStep(templatesDir, tempHtmlDir)
	stepRender := NewPDFRenderStep(tempPdfDir)
	stepEmail := NewEmailDispatchStep(tempEmailDir)

	// 3. Declare Workflow
	mvpWorkflow := workflow.Workflow{
		Name: "PersonalizedBrochureMVP",
		Steps: []workflow.Step{
			stepProfile,
			stepRecommend,
			stepCompile,
			stepRender,
			stepEmail,
		},
		RetryPolicies: map[string]workflow.RetryPolicy{
			"PDFRenderStep": {
				MaxRetries:      1,
				BackoffStrategy: "linear",
			},
		},
	}

	// 4. Instantiate Orchestrator
	orchestrator := workflow.NewOrchestrator(mvpWorkflow)
	state := &workflow.JobContext{}
	ctx := workflow.NewContext(context.Background(), "job_integration_1", "trace_integration_1", state)

	// 5. Execute Pipeline
	err = orchestrator.Execute(ctx)
	if err != nil {
		t.Fatalf("Integration test pipeline execution failed: %v", err)
	}

	// 6. Assert State Updates
	if state.WorkflowState != "Completed" {
		t.Errorf("Expected job state Completed, got: %s", state.WorkflowState)
	}
	if state.UserProfileID == "" {
		t.Error("Expected UserProfileID to be set in state")
	}
	if state.RecommendationID == "" {
		t.Error("Expected RecommendationID to be set in state")
	}
	if state.PDFObjectKey == "" {
		t.Error("Expected PDFObjectKey to be set in state")
	}
	if state.EmailReceiptID == "" {
		t.Error("Expected EmailReceiptID to be set in state")
	}

	// 7. Verify File System Artifacts
	htmlFile := filepath.Join(tempHtmlDir, "compiled_job_job_integration_1.html")
	if _, err := os.Stat(htmlFile); os.IsNotExist(err) {
		t.Errorf("Compiled HTML file was not created: %s", htmlFile)
	}

	pdfFile := filepath.Join(tempPdfDir, "brochure_job_job_integration_1.pdf")
	if _, err := os.Stat(pdfFile); os.IsNotExist(err) {
		t.Errorf("Compiled PDF file was not created: %s", pdfFile)
	}

	emailFile := filepath.Join(tempEmailDir, "email_job_job_integration_1.log")
	if _, err := os.Stat(emailFile); os.IsNotExist(err) {
		t.Errorf("Mock email dispatch log file was not created: %s", emailFile)
	}

	// 8. Clean up created artifacts after successful validation
	if !t.Failed() {
		os.RemoveAll(tempHtmlDir)
		os.RemoveAll(tempPdfDir)
		os.RemoveAll(tempEmailDir)
	}
}
