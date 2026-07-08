package workflow

import (
	"bytes"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"path/filepath"

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

	url := fmt.Sprintf("https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent?key=%s", apiKey)

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
		return fmt.Errorf("failed to execute HTTP POST to Gemini: %w", err)
	}
	defer resp.Body.Close()

	bodyBytes, err := io.ReadAll(resp.Body)
	if err != nil {
		return fmt.Errorf("failed to read response body: %w", err)
	}

	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("gemini API request failed with status code %d: %s", resp.StatusCode, string(bodyBytes))
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

// ParsePDFBrochure uses Gemini to extract multiple vehicle/product specs from a PDF catalog
func ParsePDFBrochure(ctx *Context, pdfBytes []byte) ([]recommendation.Vehicle, error) {
	// Compute SHA-256 hash of the PDF catalog
	hash := sha256.Sum256(pdfBytes)
	hashStr := hex.EncodeToString(hash[:])
	cacheDir := filepath.Join("storage", "catalog_cache")
	cachePath := filepath.Join(cacheDir, fmt.Sprintf("%s.json", hashStr))

	// Check if cached parsed catalog exists on disk
	if _, err := os.Stat(cachePath); err == nil {
		log.Printf("[TraceID: %s] [JobID: %s] [Cache] Found parsed catalog cache hit for hash %s. Reading from disk...", ctx.TraceID, ctx.JobID, hashStr)
		cachedData, readErr := os.ReadFile(cachePath)
		if readErr == nil {
			var cachedCatalog []recommendation.Vehicle
			if jsonErr := json.Unmarshal(cachedData, &cachedCatalog); jsonErr == nil && len(cachedCatalog) > 0 {
				log.Printf("[TraceID: %s] [JobID: %s] [Cache] Successfully loaded %d product(s) from cache.", ctx.TraceID, ctx.JobID, len(cachedCatalog))
				return cachedCatalog, nil
			}
		}
	}

	apiKey := getAPIKey()
	if apiKey == "" {
		log.Printf("[TraceID: %s] [JobID: %s] [Gemini] GEMINI_API_KEY is empty. Simulating PDF catalog items extraction.", ctx.TraceID, ctx.JobID)
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

	url := fmt.Sprintf("https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent?key=%s", apiKey)

	base64Data := base64.StdEncoding.EncodeToString(pdfBytes)

	prompt := `You are an expert product catalog extraction agent.
Analyze the attached PDF brochure/catalog of products (such as home appliances, electronics, or vehicles) and extract a list of catalog items described.
For each item, respond with a JSON object matching this structure:
{
  "id": "short_unique_id (e.g. appliance_fridge_samsung)",
  "model": "Full Product Model Name (e.g. Samsung Family Hub Refrigerator)",
  "base_price": 2499.0,
  "features": ["Feature 1", "Feature 2", "Feature 3", ...],
  "engine_specs": {
    "type": "Product Category (e.g. Refrigerator, Washer, Sedan)",
    "capacity": "Specifications (e.g. 26 cu. ft., 5.0 cu. ft., or 5 seats)",
    "power": "Power specs or Horsepower if applicable"
  },
  "colors": ["Color 1", "Color 2"]
}
If pricing or specific specs are not listed, make a highly accurate estimate.
Respond with a JSON array containing these objects. Do not output any markdown formatting or commentary. Just output the raw JSON array.`

	reqBody := GeminiRequest{
		Contents: []GeminiContent{
			{
				Parts: []GeminiPart{
					{
						InlineData: &InlineData{
							MimeType: "application/pdf",
							Data:     base64Data,
						},
					},
					{
						Text: prompt,
					},
				},
			},
		},
		GenerationConfig: GenerationConfig{
			ResponseMimeType: "application/json",
		},
	}

	jsonData, err := json.Marshal(reqBody)
	if err != nil {
		return nil, fmt.Errorf("failed to marshal Gemini PDF request body: %w", err)
	}

	req, err := http.NewRequestWithContext(ctx, "POST", url, bytes.NewBuffer(jsonData))
	if err != nil {
		return nil, fmt.Errorf("failed to create http request for Gemini PDF: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")

	client := &http.Client{}
	resp, err := client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("failed to execute HTTP POST to Gemini PDF: %w", err)
	}
	defer resp.Body.Close()

	bodyBytes, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("failed to read response body: %w", err)
	}

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("gemini PDF API request failed with status code %d: %s", resp.StatusCode, string(bodyBytes))
	}

	var geminiResp GeminiResponse
	if err := json.Unmarshal(bodyBytes, &geminiResp); err != nil {
		return nil, fmt.Errorf("failed to unmarshal Gemini PDF API outer response: %w", err)
	}

	if len(geminiResp.Candidates) == 0 || len(geminiResp.Candidates[0].Content.Parts) == 0 {
		return nil, fmt.Errorf("gemini PDF API response contained no candidates or content parts")
	}

	responseText := geminiResp.Candidates[0].Content.Parts[0].Text
	var catalog []recommendation.Vehicle
	if err := json.Unmarshal([]byte(responseText), &catalog); err != nil {
		return nil, fmt.Errorf("failed to parse structured JSON response from Gemini PDF: %w. Response text was: %s", err, responseText)
	}

	// Save parsed catalog to local cache directory for instant future hits
	if err := os.MkdirAll(cacheDir, 0755); err == nil {
		if cacheBytes, marshalErr := json.Marshal(catalog); marshalErr == nil {
			if writeErr := os.WriteFile(cachePath, cacheBytes, 0644); writeErr == nil {
				log.Printf("[TraceID: %s] [JobID: %s] [Cache] Successfully cached parsed catalog under %s.", ctx.TraceID, ctx.JobID, cachePath)
			}
		}
	}

	return catalog, nil
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
