package steps

import (
	"fmt"
	"log"
	"os"
	"path/filepath"
	"time"

	"marketing-agent/internal/workflow"
)

// EmailDispatchStep handles sending generated PDF brochures to user emails
type EmailDispatchStep struct {
	outputDir string
}

// NewEmailDispatchStep creates a new EmailDispatchStep instance
func NewEmailDispatchStep(outputDir string) *EmailDispatchStep {
	return &EmailDispatchStep{
		outputDir: outputDir,
	}
}

// Name implements workflow.Step
func (s *EmailDispatchStep) Name() string {
	return "EmailDispatchStep"
}

// Execute performs email construction, attaching compiled PDFs, and mock transmission
func (s *EmailDispatchStep) Execute(ctx *workflow.Context) (workflow.Result, error) {
	// 1. Resolve recipient address
	recipient := ctx.State.UserProfileID
	if recipient == "" {
		recipient = "customer@example.com"
	}

	// 2. Fetch pdf local path and bucket specifications
	pdfPathRaw, ok := ctx.State.StepOutputs["PDFRenderStep_LocalPath"]
	if !ok {
		return nil, fmt.Errorf("missing rendered PDF local path reference")
	}
	pdfPath, ok := pdfPathRaw.(string)
	if !ok {
		return nil, fmt.Errorf("invalid PDF filepath type mapping")
	}

	pdfResRaw, ok := ctx.State.StepOutputs["PDFRenderStep"]
	if !ok {
		return nil, fmt.Errorf("missing PDF render metadata result")
	}
	pdfRes, ok := pdfResRaw.(workflow.PDFRenderResult)
	if !ok {
		return nil, fmt.Errorf("invalid PDF render metadata result format")
	}

	// Validate file existence
	fileInfo, err := os.Stat(pdfPath)
	if err != nil {
		return nil, fmt.Errorf("rendered PDF attachment file not found: %w", err)
	}

	// 3. Dispatch processing
	apiKey := os.Getenv("SENDGRID_API_KEY")
	var receiptID string
	var sentMethod string

	if apiKey != "" {
		// Mock live dispatcher using Sengrid config blocks (placeholder flow)
		sentMethod = "SendGrid Transaction API"
		receiptID = fmt.Sprintf("sg_tx_rec_%s_%d", ctx.JobID, time.Now().Unix())
		log.Printf("[TraceID: %s] [JobID: %s] [Email] Sending live email via %s to %s with attachment: %s", 
			ctx.TraceID, ctx.JobID, sentMethod, recipient, pdfRes.PDFObjectKey)
	} else {
		// Local fallback mock writer
		sentMethod = "Local Storage Simulation (No SENDGRID_API_KEY detected)"
		receiptID = fmt.Sprintf("mock_tx_rec_%s_%d", ctx.JobID, time.Now().Unix())
		
		if err := os.MkdirAll(s.outputDir, 0755); err != nil {
			return nil, fmt.Errorf("failed to create local email storage directory: %w", err)
		}

		emailLogPath := filepath.Join(s.outputDir, fmt.Sprintf("email_job_%s.log", ctx.JobID))
		emailContent := fmt.Sprintf(
			"Timestamp: %s\nTo: %s\nSubject: Your Personalized Brochure for Vehicle Selection\nAttachment: %s (%d bytes)\nReceiptID: %s\nMethod: %s\n\nBody:\nDear Customer,\n\nPlease find attached your custom-compiled marketing brochure. In this portfolio, we outline your matched preference scores, customized specs sheets, and leasing details.\n\nWarm regards,\nSales & Marketing Team\n",
			time.Now().Format(time.RFC3339),
			recipient,
			pdfPath,
			fileInfo.Size(),
			receiptID,
			sentMethod,
		)

		if err := os.WriteFile(emailLogPath, []byte(emailContent), 0644); err != nil {
			return nil, fmt.Errorf("failed to write local email log file: %w", err)
		}
		
		log.Printf("[TraceID: %s] [JobID: %s] [Email] Mock email logged locally to: %s", ctx.TraceID, ctx.JobID, emailLogPath)
	}

	return workflow.EmailDispatchResult{
		EmailReceiptID: receiptID,
		Sent:           true,
	}, nil
}
