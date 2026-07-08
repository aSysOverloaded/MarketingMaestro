package steps

import (
	"context"
	"fmt"
	"log"
	"os"
	"os/exec"
	"path/filepath"
	"time"

	"github.com/chromedp/cdproto/page"
	"github.com/chromedp/chromedp"

	"marketing-agent/internal/workflow"
)

// PDFRenderStep compiles PDF binaries utilizing Chromedp headless browser
type PDFRenderStep struct {
	outputDir string
}

// NewPDFRenderStep creates a new PDFRenderStep instance
func NewPDFRenderStep(outputDir string) *PDFRenderStep {
	return &PDFRenderStep{
		outputDir: outputDir,
	}
}

// Name implements workflow.Step
func (s *PDFRenderStep) Name() string {
	return "PDFRenderStep"
}

// Execute triggers Chromedp to render compiled HTML templates to an A4 PDF
func (s *PDFRenderStep) Execute(ctx *workflow.Context) (workflow.Result, error) {
	// 1. Fetch compiled HTML path from step outputs
	htmlRaw, ok := ctx.State.StepOutputs["HTMLCompileStep"]
	if !ok {
		return nil, fmt.Errorf("missing html compile outcome from previous step")
	}
	htmlFilePath, ok := htmlRaw.(string)
	if !ok {
		return nil, fmt.Errorf("invalid HTML filepath type")
	}

	htmlAbsPath, err := filepath.Abs(htmlFilePath)
	if err != nil {
		return nil, fmt.Errorf("failed to locate absolute path of HTML file: %w", err)
	}

	pdfFileName := fmt.Sprintf("brochure_job_%s.pdf", ctx.JobID)
	pdfFilePath := filepath.Join(s.outputDir, pdfFileName)

	// 2. Headless Chrome detection and Developer Fallback
	chromePath := findChrome()
	if os.Getenv("DISABLE_CHROME_PDF") == "true" {
		log.Printf("[TraceID: %s] [JobID: %s] [INFO] [PDFRenderStep] DISABLE_CHROME_PDF is enabled. Triggering developer fallback.", ctx.TraceID, ctx.JobID)
		return s.executeDeveloperFallback(ctx, pdfFileName, pdfFilePath)
	}
	if chromePath == "" {
		_, pathErr := exec.LookPath("google-chrome")
		_, pathErr2 := exec.LookPath("chrome")
		if pathErr != nil && pathErr2 != nil {
			log.Printf("[TraceID: %s] [JobID: %s] [WARNING] [PDFRenderStep] Google Chrome not detected. Triggering developer fallback.", ctx.TraceID, ctx.JobID)
			return s.executeDeveloperFallback(ctx, pdfFileName, pdfFilePath)
		}
	}

	// 3. Configure headless browser run allocations
	opts := append(chromedp.DefaultExecAllocatorOptions[:],
		chromedp.DisableGPU,
		chromedp.NoSandbox,
	)
	if chromePath != "" {
		opts = append(opts, chromedp.ExecPath(chromePath))
	}
	allocCtx, cancelAlloc := chromedp.NewExecAllocator(ctx.Context, opts...)
	defer cancelAlloc()

	chromeCtx, cancelCtx := chromedp.NewContext(allocCtx)
	defer cancelCtx()

	// Apply 30 second browser context timeout
	chromeCtx, cancelTimeout := context.WithTimeout(chromeCtx, 30*time.Second)
	defer cancelTimeout()

	fileURL := "file://" + filepath.ToSlash(htmlAbsPath)
	var pdfBuf []byte

	log.Printf("[TraceID: %s] [JobID: %s] Chromedp navigating to: %s", ctx.TraceID, ctx.JobID, fileURL)

	// 3. Navigate and Print to PDF
	err = chromedp.Run(chromeCtx,
		chromedp.Navigate(fileURL),
		chromedp.Sleep(600*time.Millisecond), // Pause briefly to allow Tailwind fonts/images to paint
		chromedp.ActionFunc(func(cCtx context.Context) error {
			buf, _, err := page.PrintToPDF().
				WithPrintBackground(true).
				WithPaperWidth(8.27).  // A4 width in inches
				WithPaperHeight(11.69). // A4 height in inches
				WithMarginTop(0).
				WithMarginBottom(0).
				WithMarginLeft(0).
				WithMarginRight(0).
				Do(cCtx)
			if err != nil {
				return err
			}
			pdfBuf = buf
			return nil
		}),
	)
	if err != nil {
		return nil, fmt.Errorf("failed during Chromedp browser run: %w", err)
	}

	// 4. Save file locally
	if err := os.MkdirAll(s.outputDir, 0755); err != nil {
		return nil, fmt.Errorf("failed to create pdf output storage directory: %w", err)
	}

	if err := os.WriteFile(pdfFilePath, pdfBuf, 0644); err != nil {
		return nil, fmt.Errorf("failed to save compiled PDF file: %w", err)
	}

	// Store full local path inside outputs for local email attachments reference
	ctx.State.StepOutputs["PDFRenderStep_LocalPath"] = pdfFilePath

	return workflow.PDFRenderResult{
		PDFObjectKey: pdfFileName,
		PDFBucket:    "generated_brochures",
	}, nil
}

// Compensate removes the generated PDF binary on downstream steps failure
func (s *PDFRenderStep) Compensate(ctx *workflow.Context) error {
	log.Printf("[TraceID: %s] [JobID: %s] [Compensate: PDFRenderStep] Rolling back file allocations...", ctx.TraceID, ctx.JobID)
	
	pdfPathRaw, ok := ctx.State.StepOutputs["PDFRenderStep_LocalPath"]
	if !ok {
		return nil // No PDF file was created
	}

	pdfPath, ok := pdfPathRaw.(string)
	if !ok {
		return nil
	}

	if _, err := os.Stat(pdfPath); err == nil {
		if removeErr := os.Remove(pdfPath); removeErr != nil {
			return fmt.Errorf("failed to clean up PDF file at %s: %w", pdfPath, removeErr)
		}
		log.Printf("[TraceID: %s] [JobID: %s] [Compensate: PDFRenderStep] Cleaned up PDF: %s", ctx.TraceID, ctx.JobID, pdfPath)
	}

	return nil
}

// findChrome scans default installation paths on Windows systems
func findChrome() string {
	paths := []string{
		`C:\Program Files\Google\Chrome\Application\chrome.exe`,
		`C:\Program Files (x86)\Google\Chrome\Application\chrome.exe`,
		filepath.Join(os.Getenv("USERPROFILE"), `AppData\Local\Google\Chrome\Application\chrome.exe`),
		filepath.Join(os.Getenv("LOCALAPPDATA"), `Google\Chrome\Application\chrome.exe`),
	}

	for _, p := range paths {
		if _, err := os.Stat(p); err == nil {
			return p
		}
	}
	return ""
}

// executeDeveloperFallback creates a mock PDF binary when chrome is not installed locally
func (s *PDFRenderStep) executeDeveloperFallback(ctx *workflow.Context, pdfFileName, pdfFilePath string) (workflow.Result, error) {
	if err := os.MkdirAll(s.outputDir, 0755); err != nil {
		return nil, fmt.Errorf("failed to create pdf output storage directory: %w", err)
	}

	mockPDFContent := fmt.Sprintf("%%PDF-1.4 mock binary content for JobID: %s", ctx.JobID)
	if err := os.WriteFile(pdfFilePath, []byte(mockPDFContent), 0644); err != nil {
		return nil, fmt.Errorf("failed to write developer fallback mock PDF: %w", err)
	}

	ctx.State.StepOutputs["PDFRenderStep_LocalPath"] = pdfFilePath

	return workflow.PDFRenderResult{
		PDFObjectKey: pdfFileName,
		PDFBucket:    "generated_brochures",
	}, nil
}
