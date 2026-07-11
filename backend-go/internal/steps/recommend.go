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

// ProductRecommenderStep scores and matches catalog items using the matcher engine
type ProductRecommenderStep struct {
	matcher recommendation.RecommendationEngine
}

// NewProductRecommenderStep creates a new ProductRecommenderStep instance
func NewProductRecommenderStep(matcher recommendation.RecommendationEngine) *ProductRecommenderStep {
	return &ProductRecommenderStep{
		matcher: matcher,
	}
}

// Name implements workflow.Step
func (s *ProductRecommenderStep) Name() string {
	return "ProductRecommenderStep"
}

// Execute retrieves user profiles, runs candidate matchers via Gemini, and logs recommendations
func (s *ProductRecommenderStep) Execute(ctx *workflow.Context) (workflow.Result, error) {
	// 1. Fetch User Profile from step outputs
	profileRaw, ok := ctx.State.StepOutputs["UserProfileStep"]
	if !ok {
		return nil, fmt.Errorf("missing user profile result from previous step")
	}
	profileResult, ok := profileRaw.(workflow.UserProfileResult)
	if !ok {
		return nil, fmt.Errorf("invalid user profile format")
	}

	// Translate profileResult to recommendation.UserProfile struct
	hobbiesSlice := []string{}
	if hObs, ok := profileResult.Attributes["hobbies"].([]interface{}); ok {
		for _, h := range hObs {
			if hStr, ok := h.(string); ok {
				hobbiesSlice = append(hobbiesSlice, hStr)
			}
		}
	} else if hObsStr, ok := profileResult.Attributes["hobbies"].([]string); ok {
		hobbiesSlice = hObsStr
	}
	
	incomeVal := 0.0
	if inc, ok := profileResult.Attributes["income"].(float64); ok {
		incomeVal = inc
	}

	famSize := 1
	if fs, ok := profileResult.Attributes["family_size"].(float64); ok {
		famSize = int(fs)
	} else if fsInt, ok := profileResult.Attributes["family_size"].(int); ok {
		famSize = fsInt
	}

	userProfile := recommendation.UserProfile{
		Age:        32,
		Income:     incomeVal,
		Hobbies:    hobbiesSlice,
		FamilySize: famSize,
		Location:   "Seattle, WA",
	}

	// 2. Fetch candidates catalog (RAG or cache resolution)
	var candidates []recommendation.Candidate

	// Check if local Qdrant RAG index is active
	ragActiveRaw, hasRAG := ctx.State.StepOutputs["RAGActive"]
	ragActive, _ := ragActiveRaw.(bool)

	if hasRAG && ragActive {
		log.Printf("[TraceID: %s] [JobID: %s] [RAG] Searching in-memory Qdrant database...", ctx.TraceID, ctx.JobID)
		queryText := fmt.Sprintf("hobbies: %v, family size: %d, location: %s", userProfile.Hobbies, userProfile.FamilySize, userProfile.Location)
		ragCandidates, err := queryRAGAndParse(ctx, queryText)
		if err != nil {
			log.Printf("[TraceID: %s] [JobID: %s] [RAG] [WARNING] Qdrant search/parse failed: %v. Bypassing to default catalog.", ctx.TraceID, ctx.JobID, err)
		} else {
			candidates = ragCandidates
		}
	}

	if len(candidates) == 0 {
		// Check if a custom uploaded catalog array is provided in context (legacy/simulation fallback)
		if catalogRaw, ok := ctx.State.StepOutputs["UploadedCatalog"]; ok {
			if catalogList, ok := catalogRaw.([]recommendation.Vehicle); ok && len(catalogList) > 0 {
				for _, v := range catalogList {
					candidates = append(candidates, v)
				}
			}
		}
	}

	// Check if a custom uploaded vehicle is provided in context
	if uploadedRaw, ok := ctx.State.StepOutputs["UploadedVehicle"]; ok {
		if uploadedVehicle, ok := uploadedRaw.(recommendation.Vehicle); ok {
			candidates = append(candidates, uploadedVehicle)
		} else if uploadedVehiclePtr, ok := uploadedRaw.(*recommendation.Vehicle); ok && uploadedVehiclePtr != nil {
			candidates = append(candidates, *uploadedVehiclePtr)
		}
	}

	// Fallback to static catalog DB if no custom products are uploaded
	if len(candidates) == 0 {
		candidates = []recommendation.Candidate{
			recommendation.Vehicle{
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
			recommendation.Vehicle{
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
			recommendation.Vehicle{
				ID:        "appliance_dishwasher_bosch",
				Model:     "Bosch 800 Series Dishwasher",
				BasePrice: 1299,
				Features:  []string{"CrystalDry Technology", "Whisper Quiet 42 dBA", "Flexible 3rd Rack", "Home Connect Smart Control"},
				EngineSpecs: map[string]interface{}{
					"seats":    0,
					"type":     "Dishwasher",
					"capacity": "16 Place Settings",
				},
				Colors: []string{"Stainless Steel", "Black Stainless Steel"},
			},
		}
	}

	// 3. Score and Rank candidates via Gemini API
	userProfileBytes, err := json.Marshal(userProfile)
	if err != nil {
		return nil, fmt.Errorf("failed to marshal user profile: %w", err)
	}

	candidatesBytes, err := json.Marshal(candidates)
	if err != nil {
		return nil, fmt.Errorf("failed to marshal candidates: %w", err)
	}

	prompt := fmt.Sprintf(`You are an expert sales and recommendation agent.
Given the following User Profile and Candidate Products Catalog, rank the candidates in descending order of how well they match the user's needs.
Select the top candidates (select 2 options) that are suitable.

User Profile:
%s

Candidate Catalog:
%s

For each recommended product, calculate:
- A match score (0 to 100) based on budget suitability, capacity/size requirements, active hobbies vs product features, and segment fit.
- Matched rules: specific, concise reasons why it matches (e.g. "Fits budget", "Large capacity fits family size").
- Explanation: a 1-sentence summary of why this product is recommended for the user.

Respond with a JSON array containing precisely these objects (ordered by score descending):
[
  {
    "recommendation_id": "rec_job_%s_1",
    "vehicle_id": "candidate_id_here",
    "score": 95,
    "matched_rules": ["Rule reason 1", "Rule reason 2"],
    "explanation": "Brief explanation statement."
  },
  {
    "recommendation_id": "rec_job_%s_2",
    "vehicle_id": "candidate_id_here",
    "score": 85,
    "matched_rules": ["Rule reason 1", "Rule reason 2"],
    "explanation": "Brief explanation statement."
  }
]
Do not output any markdown formatting or commentary. Just output the raw JSON array.`,
		string(userProfileBytes), string(candidatesBytes), ctx.JobID, ctx.JobID,
	)

	var ranks []workflow.RecommendationResult
	err = workflow.CallGemini(ctx, prompt, &ranks)
	if err != nil {
		return nil, fmt.Errorf("failed to rank candidates using Gemini: %w", err)
	}

	if len(ranks) == 0 {
		return nil, fmt.Errorf("no matching candidates returned by Gemini")
	}

	// Find full spec structs for the selected recommendations and store them in context
	var selectedSpecs []recommendation.Vehicle
	for _, rank := range ranks {
		for _, cand := range candidates {
			if v, ok := cand.(recommendation.Vehicle); ok && v.ID == rank.VehicleID {
				selectedSpecs = append(selectedSpecs, v)
			}
		}
	}
	ctx.State.StepOutputs["ProductRecommenderStep_Specs"] = selectedSpecs
 
	return ranks, nil
}

func queryRAGAndParse(ctx *workflow.Context, query string) ([]recommendation.Candidate, error) {
	pythonUrl := os.Getenv("PYTHON_SERVICE_URL")
	if pythonUrl == "" {
		pythonUrl = "http://localhost:8000"
	}

	payload := map[string]interface{}{
		"query": query,
		"limit": 3,
	}
	jsonBytes, err := json.Marshal(payload)
	if err != nil {
		return nil, err
	}

	client := &http.Client{Timeout: 10 * time.Second}
	reqUrl := fmt.Sprintf("%s/api/rag/search", pythonUrl)
	resp, err := client.Post(reqUrl, "application/json", bytes.NewBuffer(jsonBytes))
	if err != nil {
		return nil, fmt.Errorf("RAG search HTTP error: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("RAG search status code error: %d", resp.StatusCode)
	}

	var searchResp struct {
		Matches []struct {
			PageNumber int      `json:"page_number"`
			Content    string   `json:"content"`
			Images     []string `json:"images"`
			Score      float64  `json:"score"`
		} `json:"matches"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&searchResp); err != nil {
		return nil, fmt.Errorf("failed to decode RAG search matches: %w", err)
	}

	if len(searchResp.Matches) == 0 {
		return nil, fmt.Errorf("RAG search returned 0 matches")
	}

	log.Printf("[TraceID: %s] [JobID: %s] [RAG] Qdrant search results received from Python sidecar:", ctx.TraceID, ctx.JobID)
	for i, match := range searchResp.Matches {
		log.Printf("[RAG Result #%d] Page: %d, Score: %f, Images: %v, Content Length: %d characters", i+1, match.PageNumber, match.Score, match.Images, len(match.Content))
	}

	var buffer bytes.Buffer
	for _, match := range searchResp.Matches {
		buffer.WriteString(fmt.Sprintf("--- PAGE %d ---\n%s\n", match.PageNumber, match.Content))
	}
	matchedText := buffer.String()

	prompt := fmt.Sprintf(`You are an expert product catalog extraction agent.
Analyze the following text matching relevant product pages from a PDF brochure and extract a list of catalog items described.
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
  "colors": ["Color 1", "Color 2"],
  "page_number": X // Keep the page number integer from the matched section title (e.g. 3 if matching --- PAGE 3 ---)
}
If pricing or specific specs are not listed, make a highly accurate estimate.

Matched Pages Content:
%s

Respond with a JSON array containing these objects. Do not output any markdown formatting or commentary. Just output the raw JSON array.`, matchedText)

	var parsedItems []struct {
		ID          string                 `json:"id"`
		Model       string                 `json:"model"`
		BasePrice   float64                `json:"base_price"`
		Features    []string               `json:"features"`
		EngineSpecs map[string]interface{} `json:"engine_specs"`
		Colors      []string               `json:"colors"`
		PageNumber  int                    `json:"page_number"`
	}

	err = workflow.CallGemini(ctx, prompt, &parsedItems)
	if err != nil {
		return nil, fmt.Errorf("failed to parse matched pages using Gemini: %w", err)
	}

	var candidates []recommendation.Candidate
	for _, parsed := range parsedItems {
		v := recommendation.Vehicle{
			ID:          parsed.ID,
			Model:       parsed.Model,
			BasePrice:   parsed.BasePrice,
			Features:    parsed.Features,
			EngineSpecs: parsed.EngineSpecs,
			Colors:      parsed.Colors,
		}

		// Lookup original matching RAG hit to copy its page-extracted image
		for _, match := range searchResp.Matches {
			if match.PageNumber == parsed.PageNumber && len(match.Images) > 0 {
				v.HeroImage = match.Images[0]
				break
			}
		}

		candidates = append(candidates, v)
	}
	return candidates, nil
}
