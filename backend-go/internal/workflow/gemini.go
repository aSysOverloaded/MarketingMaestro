package workflow

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"math"
	"mime/multipart"
	"net/http"
	"os"
	"reflect"
	"strings"
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

type OpenAIChatRequest struct {
	Model          string                 `json:"model"`
	Messages       []OpenAIMessage        `json:"messages"`
	ResponseFormat map[string]interface{} `json:"response_format,omitempty"`
}

type OpenAIMessage struct {
	Role    string `json:"role"`
	Content string `json:"content"`
}

type OpenAIResponse struct {
	Choices []struct {
		Message struct {
			Content string `json:"content"`
		} `json:"message"`
	} `json:"choices"`
}

func getAPIKey() string {
	return os.Getenv("GEMINI_API_KEY")
}

// CallGemini calls the Gemini API or a configured OpenAI-compatible API to get a structured JSON response unmarshalled into target
func CallGemini(ctx *Context, prompt string, target interface{}) error {
	apiUrl := os.Getenv("LLM_API_URL")
	apiKey := os.Getenv("LLM_API_KEY")
	model := os.Getenv("LLM_MODEL")

	if apiUrl != "" && apiKey != "" {
		if model == "" {
			model = "minimax/minimax-m2.7:free" // Default free OpenRouter model - check LLM_MODEL if this 404s later, OpenRouter's free lineup rotates
		}
		return CallOpenAICompatible(ctx, apiUrl, apiKey, model, prompt, target)
	}

	geminiApiKey := getAPIKey()
	if geminiApiKey == "" {
		log.Printf("[TraceID: %s] [JobID: %s] [Gemini] [FALLBACK] GEMINI_API_KEY is empty. Falling back to local simulation.", ctx.TraceID, ctx.JobID)
		return simulateFallback(prompt, target)
	}

	geminiModel := os.Getenv("GEMINI_MODEL")
	if geminiModel == "" {
		geminiModel = "gemini-3.6-flash"
	}

	url := fmt.Sprintf("https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent?key=%s", geminiModel, geminiApiKey)

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

	var resp *http.Response
	var bodyBytes []byte
	maxRetries := 5

	for attempt := 0; attempt < maxRetries; attempt++ {
		req, err := http.NewRequestWithContext(ctx, "POST", url, bytes.NewBuffer(jsonData))
		if err != nil {
			return fmt.Errorf("failed to create http request for Gemini: %w", err)
		}
		req.Header.Set("Content-Type", "application/json")

		client := &http.Client{}
		resp, err = client.Do(req)
		if err != nil {
			log.Printf("[TraceID: %s] [JobID: %s] [Gemini] [WARNING] Network connection failed (attempt %d): %v", ctx.TraceID, ctx.JobID, attempt+1, err)
			time.Sleep(3 * time.Second)
			continue
		}

		bodyBytes, err = io.ReadAll(resp.Body)
		resp.Body.Close()
		if err != nil {
			return fmt.Errorf("failed to read response body: %w", err)
		}

		if resp.StatusCode == http.StatusOK {
			break
		}

		if resp.StatusCode == 429 || resp.StatusCode == 503 {
			retryDelay := time.Duration(3*math.Pow(2, float64(attempt))) * time.Second
			if retryDelay > 30*time.Second {
				retryDelay = 30 * time.Second
			}
			log.Printf("[TraceID: %s] [JobID: %s] [Gemini] [WARNING] Gemini request returned status %d. Retrying in %v... (attempt %d/%d)", ctx.TraceID, ctx.JobID, resp.StatusCode, retryDelay, attempt+1, maxRetries)
			time.Sleep(retryDelay)
			continue
		} else {
			log.Printf("[TraceID: %s] [JobID: %s] [Gemini] [FALLBACK] Gemini request failed (Status: %d). Falling back to local simulation. Error body: %s", ctx.TraceID, ctx.JobID, resp.StatusCode, string(bodyBytes))
			return simulateFallback(prompt, target)
		}
	}

	if resp == nil || resp.StatusCode != http.StatusOK {
		log.Printf("[TraceID: %s] [JobID: %s] [Gemini] [FALLBACK] Gemini retries exhausted. Falling back to local simulation.", ctx.TraceID, ctx.JobID)
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
	log.Printf("[TraceID: %s] [JobID: %s] [Gemini] Raw response (truncated): %s", ctx.TraceID, ctx.JobID, truncateForLog(responseText, 1000))
	if err := json.Unmarshal([]byte(responseText), target); err != nil {
		return fmt.Errorf("failed to parse structured JSON response from Gemini into target interface: %w. Response text was: %s", err, responseText)
	}
	logIfEmptySlice(ctx, "Gemini", target)

	return nil
}

// truncateForLog caps a string for log output so a large model response doesn't flood the console.
func truncateForLog(s string, maxLen int) string {
	if len(s) <= maxLen {
		return s
	}
	return s[:maxLen] + fmt.Sprintf("... (truncated, %d total chars)", len(s))
}

// logIfEmptySlice flags the case where the LLM call succeeded and produced valid JSON, but the
// JSON was an empty array - a silent "the model chose to extract nothing" outcome that otherwise
// looks identical to a real failure once it triggers a fallback to the default catalog downstream.
func logIfEmptySlice(ctx *Context, provider string, target interface{}) {
	v := reflect.ValueOf(target)
	if v.Kind() == reflect.Ptr && v.Elem().Kind() == reflect.Slice && v.Elem().Len() == 0 {
		log.Printf("[TraceID: %s] [JobID: %s] [%s] [WARNING] Call succeeded and parsed valid JSON, but the model returned an empty array - it found nothing to extract from the given content.", ctx.TraceID, ctx.JobID, provider)
	}
}

// CallOpenAICompatible executes structured chat completions queries on standard OpenAI-compatible endpoints
func CallOpenAICompatible(ctx *Context, apiURL, apiKey, model, prompt string, target interface{}) error {
	url := fmt.Sprintf("%s/chat/completions", strings.TrimSuffix(apiURL, "/"))
	
	reqBody := OpenAIChatRequest{
		Model: model,
		Messages: []OpenAIMessage{
			{Role: "user", Content: prompt},
		},
		ResponseFormat: map[string]interface{}{
			"type": "json_object",
		},
	}

	jsonData, err := json.Marshal(reqBody)
	if err != nil {
		return fmt.Errorf("failed to marshal OpenAI request body: %w", err)
	}

	var resp *http.Response
	var bodyBytes []byte
	maxRetries := 5

	for attempt := 0; attempt < maxRetries; attempt++ {
		req, err := http.NewRequestWithContext(ctx, "POST", url, bytes.NewBuffer(jsonData))
		if err != nil {
			return fmt.Errorf("failed to create http request for OpenAI: %w", err)
		}
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", apiKey))

		client := &http.Client{}
		resp, err = client.Do(req)
		if err != nil {
			log.Printf("[TraceID: %s] [JobID: %s] [OpenAI] [WARNING] Network connection failed (attempt %d): %v", ctx.TraceID, ctx.JobID, attempt+1, err)
			time.Sleep(3 * time.Second)
			continue
		}

		bodyBytes, err = io.ReadAll(resp.Body)
		resp.Body.Close()
		if err != nil {
			return fmt.Errorf("failed to read response body: %w", err)
		}

		if resp.StatusCode == http.StatusOK {
			break
		}

		if resp.StatusCode == 429 || resp.StatusCode == 503 {
			retryDelay := time.Duration(3*math.Pow(2, float64(attempt))) * time.Second
			if retryDelay > 30*time.Second {
				retryDelay = 30 * time.Second
			}
			log.Printf("[TraceID: %s] [JobID: %s] [OpenAI] [WARNING] OpenAI request returned status %d. Retrying in %v... (attempt %d/%d)", ctx.TraceID, ctx.JobID, resp.StatusCode, retryDelay, attempt+1, maxRetries)
			time.Sleep(retryDelay)
			continue
		} else {
			log.Printf("[TraceID: %s] [JobID: %s] [OpenAI] [FALLBACK] Request failed (Status: %d). Error body: %s. Falling back to local simulation.", ctx.TraceID, ctx.JobID, resp.StatusCode, string(bodyBytes))
			return simulateFallback(prompt, target)
		}
	}

	if resp == nil || resp.StatusCode != http.StatusOK {
		log.Printf("[TraceID: %s] [JobID: %s] [OpenAI] [FALLBACK] OpenAI retries exhausted. Falling back to local simulation.", ctx.TraceID, ctx.JobID)
		return simulateFallback(prompt, target)
	}

	var openAIResp OpenAIResponse
	if err := json.Unmarshal(bodyBytes, &openAIResp); err != nil {
		return fmt.Errorf("failed to unmarshal OpenAI API outer response: %w", err)
	}

	if len(openAIResp.Choices) == 0 {
		return fmt.Errorf("OpenAI API response contained no choices: %s", string(bodyBytes))
	}

	responseText := openAIResp.Choices[0].Message.Content
	log.Printf("[TraceID: %s] [JobID: %s] [OpenAI] Raw response (truncated): %s", ctx.TraceID, ctx.JobID, truncateForLog(responseText, 1000))
	cleanText := responseText
	if strings.Contains(cleanText, "```json") {
		cleanText = strings.Split(cleanText, "```json")[1]
		cleanText = strings.Split(cleanText, "```")[0]
	}
	cleanText = strings.TrimSpace(cleanText)

	// If target is a slice, but model returned an outer object wrapping it (common for OpenAI json_object mode)
	if strings.HasPrefix(cleanText, "{") {
		var outerMap map[string]json.RawMessage
		if err := json.Unmarshal([]byte(cleanText), &outerMap); err == nil {
			for _, val := range outerMap {
				trimmedVal := strings.TrimSpace(string(val))
				if strings.HasPrefix(trimmedVal, "[") {
					if err := json.Unmarshal(val, target); err == nil {
						logIfEmptySlice(ctx, "OpenAI", target)
						return nil // Successfully parsed wrapped array!
					}
				}
			}
		}

		// Handle case where target is a slice but API returned a single JSON object (e.g. only one product matched)
		targetVal := reflect.ValueOf(target)
		if targetVal.Kind() == reflect.Ptr && targetVal.Elem().Kind() == reflect.Slice {
			sliceType := targetVal.Elem().Type()
			elemType := sliceType.Elem()
			newElem := reflect.New(elemType)
			if err := json.Unmarshal([]byte(cleanText), newElem.Interface()); err == nil {
				newSlice := reflect.MakeSlice(sliceType, 1, 1)
				newSlice.Index(0).Set(newElem.Elem())
				targetVal.Elem().Set(newSlice)
				return nil
			}
		}
	}

	if err := json.Unmarshal([]byte(cleanText), target); err != nil {
		return fmt.Errorf("failed to parse structured JSON response from OpenAI into target: %w. Response text was: %s", err, responseText)
	}
	logIfEmptySlice(ctx, "OpenAI", target)

	return nil
}

// ParsePDFBrochure uploads a PDF brochure to the Python sidecar's Qdrant index.
func ParsePDFBrochure(ctx *Context, pdfBytes []byte) ([]recommendation.Product, error) {
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

	client := &http.Client{Timeout: 120 * time.Second}
	reqUrl := fmt.Sprintf("%s/api/rag/ingest", pythonUrl)
	req, err := http.NewRequestWithContext(ctx, "POST", reqUrl, bodyBuf)
	if err != nil {
		return nil, fmt.Errorf("failed to create RAG ingest request: %w", err)
	}
	req.Header.Set("Content-Type", bodyWriter.FormDataContentType())
	req.Header.Set("X-Job-ID", ctx.JobID)

	resp, err := client.Do(req)
	if err != nil {
		log.Printf("[TraceID: %s] [JobID: %s] [RAG] [FALLBACK] Failed to upload catalog to Python Qdrant: %v. Using local simulation fallback.", ctx.TraceID, ctx.JobID, err)
		return runLocalPDFMockFallback()
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		log.Printf("[TraceID: %s] [JobID: %s] [RAG] [FALLBACK] Python RAG returned status %d. Using local simulation fallback.", ctx.TraceID, ctx.JobID, resp.StatusCode)
		return runLocalPDFMockFallback()
	}

	// Flag that Qdrant RAG is successfully active for downstream steps
	ctx.State.StepOutputs["RAGActive"] = true
	log.Printf("[TraceID: %s] [JobID: %s] [RAG] PDF catalog indexed successfully in local in-memory Qdrant.", ctx.TraceID, ctx.JobID)
	return nil, nil
}

func runLocalPDFMockFallback() ([]recommendation.Product, error) {
	return []recommendation.Product{
		{
			ID:        "appliance_fridge_samsung",
			Model:     "Samsung Family Hub Refrigerator",
			BasePrice: 2499,
			Features:  []string{"Wi-Fi Connected Screen", "Triple Cooling System", "Internal Cameras", "Water & Ice Dispenser"},
			Specs: map[string]interface{}{
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
			Specs: map[string]interface{}{
				"seats":    0,
				"type":     "Washing Machine",
				"capacity": "5.0 cu. ft.",
			},
			Colors: []string{"Graphite", "White"},
		},
	}, nil
}

// simulateFallback parses the prompt and generates static/mock response data when API key is missing or rate limited
func simulateFallback(prompt string, target interface{}) error {
	switch t := target.(type) {
	case *UserProfileResult:
		mockJSON := `{
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
		if err := json.Unmarshal([]byte(mockJSON), target); err != nil {
			return fmt.Errorf("failed to unmarshal simulated fallback profile: %w", err)
		}
		return nil

	case *[]RecommendationResult:
		// Attempt to extract the IDs of candidates from the prompt catalog dynamically
		var recs []RecommendationResult
		var parsedIds []string
		idx := 0
		for {
			idPos := strings.Index(prompt[idx:], `"id":`)
			if idPos == -1 {
				break
			}
			startIdx := idx + idPos + 5
			// find opening quote of the value
			valStart := strings.Index(prompt[startIdx:], `"`)
			if valStart == -1 {
				break
			}
			valStartIdx := startIdx + valStart + 1
			valEnd := strings.Index(prompt[valStartIdx:], `"`)
			if valEnd == -1 {
				break
			}
			val := prompt[valStartIdx : valStartIdx+valEnd]
			parsedIds = append(parsedIds, val)
			idx = valStartIdx + valEnd
		}

		if len(parsedIds) > 0 {
			for i, id := range parsedIds {
				if i >= 2 { // Limit to top 2 options
					break
				}
				recs = append(recs, RecommendationResult{
					RecommendationID: fmt.Sprintf("rec_job_simulated_%d", i+1),
					ProductID:        id,
					Score:            95 - i*7,
					MatchedRules: []string{
						"Demographic Alignment (Dynamic features fit household specifications)",
						"Budget Matches Guideline Limits",
					},
					Explanation: fmt.Sprintf("Dynamic fallback recommendation for product ID: %s.", id),
				})
			}
			*t = recs
			return nil
		}

		// Fallback to static mock if no catalog found in prompt
		mockJSON := `[
			{
				"recommendation_id": "rec_job_simulated_1",
				"product_id": "appliance_fridge_samsung",
				"score": 95,
				"matched_rules": [
					"Budget Fits (Fits within premium household guidelines)",
					"Optimal Utility (Perfect capacity for family of 4)"
				],
				"explanation": "Simulated recommendation of Samsung Family Hub Refrigerator."
			}
		]`
		return json.Unmarshal([]byte(mockJSON), target)

	case *[]recommendation.Product:
		// Attempt to dynamically parse matching pages and text from the RAG search prompt content
		var products []recommendation.Product
		idx := 0
		for {
			pagePos := strings.Index(prompt[idx:], "--- PAGE ")
			if pagePos == -1 {
				break
			}
			startIdx := idx + pagePos
			endIdx := strings.Index(prompt[startIdx:], " ---")
			if endIdx == -1 {
				break
			}
			
			// Extract page number
			pageStr := prompt[startIdx+9 : startIdx+endIdx]
			var pageNum int
			_, fmtErr := fmt.Sscanf(pageStr, "%d", &pageNum)
			if fmtErr == nil {
				// Find next "--- PAGE " or end of prompt to bound the content
				contentStart := startIdx + endIdx + 4
				contentEnd := len(prompt)
				nextPagePos := strings.Index(prompt[contentStart:], "--- PAGE ")
				if nextPagePos != -1 {
					contentEnd = contentStart + nextPagePos
				}
				pageContent := prompt[contentStart:contentEnd]
				
				// Extract first non-empty line as model name
				lines := strings.Split(pageContent, "\n")
				modelName := fmt.Sprintf("Dynamic Page %d Item", pageNum)
				for _, line := range lines {
					cleaned := strings.Trim(line, " \t\r\n*#•-")
					if len(cleaned) > 5 && len(cleaned) < 80 {
						// Clean up brand names if present or keep clean title
						modelName = cleaned
						break
					}
				}
				
				products = append(products, recommendation.Product{
					ID:          fmt.Sprintf("appliance_dynamic_page_%d", pageNum),
					Model:       modelName,
					BasePrice:   1499.0,
					PageNumber:  pageNum,
					Features:    []string{"Smart dynamic utility", "Premium quality & design", "Energy efficient operation"},
					Specs:       map[string]interface{}{"type": "Appliance", "capacity": "Standard"},
					Colors:      []string{"Premium Steel", "Midnight Black"},
				})
			}
			idx = startIdx + endIdx + 4
		}

		if len(products) > 0 {
			*t = products
			return nil
		}

		// Fallback to static mock if no pages parsed from prompt
		mockJSON := `[
			{
				"id": "appliance_fridge_samsung",
				"model": "Samsung Family Hub Refrigerator",
				"base_price": 2499,
				"features": ["Wi-Fi Connected Screen", "Triple Cooling System", "Water & Ice Dispenser"],
				"specs": {
					"seats": 0,
					"type": "Refrigerator",
					"capacity": "26.5 cu. ft."
				},
				"colors": ["Stainless Steel", "Black Stainless Steel"],
				"page_number": 3
			}
		]`
		return json.Unmarshal([]byte(mockJSON), target)

	default:
		return fmt.Errorf("unsupported fallback simulation type: %T", t)
	}
}
