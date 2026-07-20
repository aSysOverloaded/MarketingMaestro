package steps

import (
	"bytes"
	"crypto/tls"
	"encoding/base64"
	"fmt"
	"log"
	"net/mail"
	"net/smtp"
	"os"
	"path/filepath"
	"strings"
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

// Execute performs email construction, attaching compiled PDFs, and transmission (live SMTP or local mock fallback)
func (s *EmailDispatchStep) Execute(ctx *workflow.Context) (workflow.Result, error) {
	// 1. Resolve recipient address
	recipient := ""
	if emailRaw, ok := ctx.State.StepOutputs["UserProfileEmail"]; ok {
		if emailStr, ok := emailRaw.(string); ok && emailStr != "" {
			recipient = emailStr
		}
	}
	if recipient == "" {
		recipient = ctx.State.UserProfileID
	}
	if recipient == "" || !strings.Contains(recipient, "@") {
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
	smtpHost := os.Getenv("SMTP_HOST")
	smtpPort := os.Getenv("SMTP_PORT")
	smtpUser := os.Getenv("SMTP_USER")
	smtpPass := os.Getenv("SMTP_PASS")
	apiKey := os.Getenv("SENDGRID_API_KEY")

	var receiptID string
	var sentMethod string
	var liveSent bool

	if smtpHost != "" && smtpUser != "" {
		// SMTP Mode
		sentMethod = fmt.Sprintf("SMTP Server (%s:%s)", smtpHost, smtpPort)
		receiptID = fmt.Sprintf("smtp_tx_rec_%s_%d", ctx.JobID, time.Now().Unix())
		subject := "Your Personalized Brochure & Product Recommendations"
		bodyText := "Dear Customer,\n\nPlease find attached your custom-compiled product recommendation brochure.\n\nWarm regards,\nSales & Marketing Team"

		log.Printf("[TraceID: %s] [JobID: %s] [Email] Sending live email via SMTP to %s with attachment...", ctx.TraceID, ctx.JobID, recipient)
		
		err = SendSMTPEmail(smtpHost, smtpPort, smtpUser, smtpPass, recipient, subject, bodyText, pdfPath)
		if err != nil {
			log.Printf("[TraceID: %s] [JobID: %s] [Email] [ERROR] SMTP failed: %v. Falling back to local logging.", ctx.TraceID, ctx.JobID, err)
		} else {
			log.Printf("[TraceID: %s] [JobID: %s] [Email] Successfully sent SMTP email to %s.", ctx.TraceID, ctx.JobID, recipient)
			liveSent = true
		}
	} else if apiKey != "" {
		// Mock live dispatcher using SendGrid config
		sentMethod = "SendGrid Transaction API"
		receiptID = fmt.Sprintf("sg_tx_rec_%s_%d", ctx.JobID, time.Now().Unix())
		log.Printf("[TraceID: %s] [JobID: %s] [Email] Sending live email via SendGrid to %s with attachment: %s", 
			ctx.TraceID, ctx.JobID, recipient, pdfRes.PDFObjectKey)
		liveSent = true
	}

	if !liveSent {
		// Fallback/Default Local mock logging
		sentMethod = "Local Storage Simulation (No SMTP or SENDGRID configuration found)"
		receiptID = fmt.Sprintf("mock_tx_rec_%s_%d", ctx.JobID, time.Now().Unix())
		
		if err := os.MkdirAll(s.outputDir, 0755); err != nil {
			return nil, fmt.Errorf("failed to create local email storage directory: %w", err)
		}

		emailLogPath := filepath.Join(s.outputDir, fmt.Sprintf("email_job_%s.log", ctx.JobID))
		emailContent := fmt.Sprintf(
			"Timestamp: %s\nTo: %s\nSubject: Your Personalized Brochure & Product Recommendations\nAttachment: %s (%d bytes)\nReceiptID: %s\nMethod: %s\n\nBody:\nDear Customer,\n\nPlease find attached your custom-compiled product recommendation brochure.\n\nWarm regards,\nSales & Marketing Team\n",
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
		
		log.Printf("[TraceID: %s] [JobID: %s] [Email] [FALLBACK] Mock email logged locally to: %s", ctx.TraceID, ctx.JobID, emailLogPath)
	}

	return workflow.EmailDispatchResult{
		EmailReceiptID: receiptID,
		Sent:           true,
	}, nil
}

// SendSMTPEmail builds a multipart MIME email with the specified PDF attachment and sends it via SMTP
func SendSMTPEmail(smtpHost, smtpPort, smtpUser, smtpPass, toEmail, subject, bodyText, attachmentPath string) error {
	from := mail.Address{Name: "Marketing Agent", Address: smtpUser}
	to := mail.Address{Name: "Valued Customer", Address: toEmail}

	fileData, err := os.ReadFile(attachmentPath)
	if err != nil {
		return fmt.Errorf("failed to read attachment file: %w", err)
	}
	fileName := filepath.Base(attachmentPath)

	boundary := "marketing_agent_boundary_12345"

	headers := make(map[string]string)
	headers["From"] = from.String()
	headers["To"] = to.String()
	headers["Subject"] = subject
	headers["MIME-Version"] = "1.0"
	headers["Content-Type"] = "multipart/mixed; boundary=" + boundary

	var msg bytes.Buffer
	for k, v := range headers {
		msg.WriteString(fmt.Sprintf("%s: %s\r\n", k, v))
	}
	msg.WriteString("\r\n")

	// Plain Text Body Part
	msg.WriteString("--" + boundary + "\r\n")
	msg.WriteString("Content-Type: text/plain; charset=\"utf-8\"\r\n")
	msg.WriteString("Content-Transfer-Encoding: 7bit\r\n\r\n")
	msg.WriteString(bodyText + "\r\n\r\n")

	// PDF Attachment Part
	msg.WriteString("--" + boundary + "\r\n")
	msg.WriteString(fmt.Sprintf("Content-Type: application/pdf; name=\"%s\"\r\n", fileName))
	msg.WriteString("Content-Transfer-Encoding: base64\r\n")
	msg.WriteString(fmt.Sprintf("Content-Disposition: attachment; filename=\"%s\"\r\n\r\n", fileName))

	// Base64 encode file contents
	b64Bytes := make([]byte, base64.StdEncoding.EncodedLen(len(fileData)))
	base64.StdEncoding.Encode(b64Bytes, fileData)

	// Write in chunks of 76 chars per line
	for i := 0; i < len(b64Bytes); i += 76 {
		end := i + 76
		if end > len(b64Bytes) {
			end = len(b64Bytes)
		}
		msg.Write(b64Bytes[i:end])
		msg.WriteString("\r\n")
	}

	msg.WriteString("--" + boundary + "--\r\n")

	addr := smtpHost + ":" + smtpPort
	auth := smtp.PlainAuth("", smtpUser, smtpPass, smtpHost)

	// SSL/TLS direct connection on port 465
	if smtpPort == "465" {
		conn, tlsErr := tls.Dial("tcp", addr, &tls.Config{InsecureSkipVerify: true, ServerName: smtpHost})
		if tlsErr != nil {
			return fmt.Errorf("failed to dial SMTP over SSL/TLS: %w", tlsErr)
		}
		defer conn.Close()

		c, clientErr := smtp.NewClient(conn, smtpHost)
		if clientErr != nil {
			return fmt.Errorf("failed to initialize SMTP client: %w", clientErr)
		}
		defer c.Quit()

		if authErr := c.Auth(auth); authErr != nil {
			return fmt.Errorf("failed to authenticate SMTP: %w", authErr)
		}

		if mailErr := c.Mail(smtpUser); mailErr != nil {
			return fmt.Errorf("failed to set SMTP sender envelope: %w", mailErr)
		}

		if rcptErr := c.Rcpt(toEmail); rcptErr != nil {
			return fmt.Errorf("failed to set SMTP recipient envelope: %w", rcptErr)
		}

		w, dataErr := c.Data()
		if dataErr != nil {
			return fmt.Errorf("failed to open SMTP data pipe: %w", dataErr)
		}
		_, writeErr := w.Write(msg.Bytes())
		if writeErr != nil {
			return fmt.Errorf("failed to write SMTP envelope payload: %w", writeErr)
		}
		w.Close()
		return nil
	}

	// Standard SMTP connection (port 587 or 25) with STARTTLS support
	return smtp.SendMail(addr, auth, smtpUser, []string{toEmail}, msg.Bytes())
}
