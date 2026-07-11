package workflow

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"mime/multipart"
	"net/http"
	"os"
	"time"

	"marketing-agent/internal/recommendation"
)

// GeminiRequest represents the schema for a Google AI Studio generation request
type GeminiRequest struct {
	Contents         []GeminiContent  `json:"contents"`
	GenerationConfig GenerationConfig `json:"generationConfig,omitempty"`
}

type GeminiContent struct {
	Parts []GeminiPart `json:"parts"`
}

type GeminiPart struct {
	Text       string      `json:"text,omitempty"`
	InlineData *InlineData `json:"inlineData,omitempty"`
}

type InlineData struct {
	MimeType string `json:"mimeType"`
	Data     string `json:"data"` // base64 encoded
}

type GenerationConfig struct {
	ResponseMimeType string `json:"responseMimeType,omitempty"`
}

// GeminiResponse represents the schema returned by Google AI Studio
type GeminiResponse struct {
	Candidates []struct {
		Content struct {
			Parts []struct {
				Text string `json:"text"`
			} `json:"parts"`
		} `json:"content"`
	} `json:"candidates"`
}

func getAPIKey() string {
	return os.Getenv("GEMINI_API_KEY")
}

// CallGemini calls the Gemini API to get a structured JSON response unmarshalled into target
func CallGemini(ctx *Context, prompt string, target interface{}) error {
	apiKey := getAPIKey()
	if apiKey == "" {
		log.Printf("[TraceID: %s] [JobID: %s] [Gemini] GEMINI_API_KEY is empty. Falling back to local simulation.", ctx.TraceID, ctx.JobID)
		return simulateFallback(prompt, target)
	}

	url := fmt.Sprintf("https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash:generateContent?key=%s", apiKey)

	reqBody := GeminiRequest{
		Contents: []GeminiContent{
			{
				Parts: []GeminiPart{
					{Text: prompt},
				},
			},
		},
		GenerationConfig: GenerationConfig{
			ResponseMimeType: "application/json",
		},
	}

	jsonData, err := json.Marshal(reqBody)
	if err != nil {
		return fmt.Errorf("failed to marshal Gemini request body: %w", err)
	}

	req, err := http.NewRequestWithContext(ctx, "POST", url, bytes.NewBuffer(jsonData))
	if err != nil {
		return fmt.Errorf("failed to create http request for Gemini: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")

	client := &http.Client{}
	resp, err := client.Do(req)
	if err != nil {
		log.Printf("[TraceID: %s] [JobID: %s] [Gemini] [WARNING] Network connection to Gemini failed: %v. Falling back to local simulation.", ctx.TraceID, ctx.JobID, err)
		return simulateFallback(prompt, target)
	}
	defer resp.Body.Close()

	bodyBytes, err := io.ReadAll(resp.Body)
	if err != nil {
		return fmt.Errorf("failed to read response body: %w", err)
	}

	if resp.StatusCode != http.StatusOK {
		log.Printf("[TraceID: %s] [JobID: %s] [Gemini] [WARNING] Gemini request failed (Status: %d). Falling back to local simulation. Error body: %s", ctx.TraceID, ctx.JobID, resp.StatusCode, string(bodyBytes))
		return simulateFallback(prompt, target)
	}

	var geminiResp GeminiResponse
	if err := json.Unmarshal(bodyBytes, &geminiResp); err != nil {
		return fmt.Errorf("failed to unmarshal Gemini API outer response: %w", err)
	}

	if len(geminiResp.Candidates) == 0 || len(geminiResp.Candidates[0].Content.Parts) == 0 {
		return fmt.Errorf("gemini API response contained no candidates or content parts: %s", string(bodyBytes))
	}

	responseText := geminiResp.Candidates[0].Content.Parts[0].Text
	if err := json.Unmarshal([]byte(responseText), target); err != nil {
		return fmt.Errorf("failed to parse structured JSON response from Gemini into target interface: %w. Response text was: %s", err, responseText)
	}

	return nil
}

// ParsePDFBrochure uploads a PDF brochure to the Python sidecar's Qdrant index.
func ParsePDFBrochure(ctx *Context, pdfBytes []byte) ([]recommendation.Vehicle, error) {
	pythonUrl := os.Getenv("PYTHON_SERVICE_URL")
	if pythonUrl == "" {
		pythonUrl = "http://localhost:8000"
	}

	bodyBuf := &bytes.Buffer{}
	bodyWriter := multipart.NewWriter(bodyBuf)
	fileWriter, err := bodyWriter.CreateFormFile("file", "catalog.pdf")
	if err != nil {
		return nil, fmt.Errorf("failed to create form file: %w", err)
	}

	if _, err := io.Copy(fileWriter, bytes.NewReader(pdfBytes)); err != nil {
		return nil, fmt.Errorf("failed to copy pdf bytes: %w", err)
	}
	bodyWriter.Close()

	client := &http.Client{Timeout: 30 * time.Second}
	reqUrl := fmt.Sprintf("%s/api/rag/ingest", pythonUrl)
	req, err := http.NewRequestWithContext(ctx, "POST", reqUrl, bodyBuf)
	if err != nil {
		return nil, fmt.Errorf("failed to create RAG ingest request: %w", err)
	}
	req.Header.Set("Content-Type", bodyWriter.FormDataContentType())

	resp, err := client.Do(req)
	if err != nil {
		log.Printf("[TraceID: %s] [JobID: %s] [RAG] [WARNING] Failed to upload catalog to Python Qdrant: %v. Using local simulation fallback.", ctx.TraceID, ctx.JobID, err)
		return runLocalPDFMockFallback()
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		log.Printf("[TraceID: %s] [JobID: %s] [RAG] [WARNING] Python RAG returned status %d. Using local simulation fallback.", ctx.TraceID, ctx.JobID, resp.StatusCode)
		return runLocalPDFMockFallback()
	}

	// Flag that Qdrant RAG is successfully active for downstream steps
	ctx.State.StepOutputs["RAGActive"] = true
	log.Printf("[TraceID: %s] [JobID: %s] [RAG] PDF catalog indexed successfully in local in-memory Qdrant.", ctx.TraceID, ctx.JobID)
	return nil, nil
}

func runLocalPDFMockFallback() ([]recommendation.Vehicle, error) {
	return []recommendation.Vehicle{
		{
			ID:        "appliance_fridge_samsung",
			Model:     "Samsung Family Hub Refrigerator",
			BasePrice: 2499,
			Features:  []string{"Wi-Fi Connected Screen", "Triple Cooling System", "Internal Cameras", "Water & Ice Dispenser"},
			EngineSpecs: map[string]interface{}{
				"seats":    0,
				"type":     "Refrigerator",
				"capacity": "26.5 cu. ft.",
			},
			Colors: []string{"Stainless Steel", "Black Stainless Steel"},
		},
		{
			ID:        "appliance_washer_lg",
			Model:     "LG TurboWash Washing Machine",
			BasePrice: 899,
			Features:  []string{"AI DD Smart Fabric Care", "TurboWash 360", "Steam Technology", "ThinQ Wi-Fi Control"},
			EngineSpecs: map[string]interface{}{
				"seats":    0,
				"type":     "Washing Machine",
				"capacity": "5.0 cu. ft.",
			},
			Colors: []string{"Graphite", "White"},
		},
	}, nil
}

// simulateFallback parses the prompt and generates static/mock response data when API key is missing
func simulateFallback(prompt string, target interface{}) error {
	var mockJSON string

	switch t := target.(type) {
	case *UserProfileResult:
		mockJSON = `{
			"user_profile_id": "simulated_job_profile",
			"segment": "Adventure",
			"budget_tier": "Premium",
			"attributes": {
				"age": 32,
				"family_size": 4,
				"income": 120000.0,
				"hobbies": ["trekking", "camping"],
				"location": "Seattle, WA"
			}
		}`
	case *[]RecommendationResult:
		mockJSON = `[
			{
				"recommendation_id": "rec_job_simulated_1",
				"vehicle_id": "appliance_fridge_samsung",
				"score": 95,
				"matched_rules": [
					"Budget Fits (Fits within premium household guidelines)",
					"Optimal Utility (Perfect capacity for family of 4)",
					"Feature Match (Wi-Fi screen matches smart home interests)"
				],
				"explanation": "Simulated recommendation of Samsung Family Hub as it matches your family size and smart home preferences."
			},
			{
				"recommendation_id": "rec_job_simulated_2",
				"vehicle_id": "appliance_washer_lg",
				"score": 88,
				"matched_rules": [
					"Budget Fits (Well under maximum budget)",
					"Optimal Utility (High capacity washing for family of 4)"
				],
				"explanation": "Simulated recommendation of LG TurboWash as it handles large family laundry loads efficiently."
			}
		]`
	default:
		return fmt.Errorf("unsupported fallback simulation type: %T", t)
	}

	if err := json.Unmarshal([]byte(mockJSON), target); err != nil {
		return fmt.Errorf("failed to unmarshal simulated fallback data: %w", err)
	}

	return nil
}
