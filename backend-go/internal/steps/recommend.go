package steps

import (
	"bytes"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"sort"
	"strings"
	"time"

	"marketing-agent/internal/recommendation"
	"marketing-agent/internal/workflow"
)

// RAGDebugInfo captures retrieval diagnostics surfaced to the UI so a bad
// recommendation can be traced back to weak/missing retrieval instead of guessed at.
type RAGDebugInfo struct {
	Active     bool           `json:"active"`
	Query      string         `json:"query,omitempty"`
	MatchCount int            `json:"match_count"`
	Matches    []RAGDebugHit  `json:"matches,omitempty"`
	Error      string         `json:"error,omitempty"`
}

type RAGDebugHit struct {
	PageNumber int     `json:"page_number"`
	Score      float64 `json:"score"`
	ImageCount int     `json:"image_count"`
	ContentLen int      `json:"content_length"`
}

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
		ragCandidates, ragDebug, err := queryRAGAndParse(ctx, userProfile.Hobbies)
		ragDebug.Active = true
		if err != nil {
			log.Printf("[TraceID: %s] [JobID: %s] [ProductRecommenderStep] [FALLBACK] Qdrant search/parse failed: %v. Bypassing to default catalog.", ctx.TraceID, ctx.JobID, err)
			ragDebug.Error = err.Error()
		} else {
			candidates = ragCandidates
		}
		ctx.State.StepOutputs["RAGDebug"] = ragDebug
	} else {
		ctx.State.StepOutputs["RAGDebug"] = RAGDebugInfo{Active: false}
	}

	if len(candidates) == 0 {
		// Check if a custom uploaded catalog array is provided in context (legacy/simulation fallback)
		if catalogRaw, ok := ctx.State.StepOutputs["UploadedCatalog"]; ok {
			if catalogList, ok := catalogRaw.([]recommendation.Product); ok && len(catalogList) > 0 {
				for _, v := range catalogList {
					candidates = append(candidates, v)
				}
			}
		}
	}

	// Check if a custom uploaded product is provided in context
	if uploadedRaw, ok := ctx.State.StepOutputs["UploadedVehicle"]; ok {
		if uploadedProduct, ok := uploadedRaw.(recommendation.Product); ok {
			candidates = append(candidates, uploadedProduct)
		} else if uploadedProductPtr, ok := uploadedRaw.(*recommendation.Product); ok && uploadedProductPtr != nil {
			candidates = append(candidates, *uploadedProductPtr)
		}
	}

	// Fallback to static catalog DB if no custom products are uploaded
	if len(candidates) == 0 {
		log.Printf("[TraceID: %s] [JobID: %s] [ProductRecommenderStep] [FALLBACK] Empty candidate list. Falling back to the default static catalog database.", ctx.TraceID, ctx.JobID)
		candidates = []recommendation.Candidate{
			recommendation.Product{
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
			recommendation.Product{
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
			recommendation.Product{
				ID:        "appliance_dishwasher_bosch",
				Model:     "Bosch 800 Series Dishwasher",
				BasePrice: 1299,
				Features:  []string{"CrystalDry Technology", "Whisper Quiet 42 dBA", "Flexible 3rd Rack", "Home Connect Smart Control"},
				Specs: map[string]interface{}{
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
Select up to the top 4 candidates that are suitable (fewer is fine if the catalog has fewer than 4 items; never invent candidates not present in the catalog below).

User Profile:
%s

Candidate Catalog:
%s

For each recommended product, calculate:
- A match score (0 to 100) based on budget suitability, capacity/size requirements, active hobbies vs product features, and segment fit.
- Matched rules: specific, concise reasons why it matches (e.g. "Fits budget", "Large capacity fits family size").
- Explanation: a 1-sentence summary of why this product is recommended for the user.

Respond with a JSON array containing precisely these objects (ordered by score descending, up to 4 entries):
[
  {
    "recommendation_id": "rec_job_%s_1",
    "product_id": "candidate_id_here",
    "score": 95,
    "matched_rules": ["Rule reason 1", "Rule reason 2"],
    "explanation": "Brief explanation statement."
  },
  {
    "recommendation_id": "rec_job_%s_2",
    "product_id": "candidate_id_here",
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
	var selectedSpecs []recommendation.Product
	for _, rank := range ranks {
		for _, cand := range candidates {
			if p, ok := cand.(recommendation.Product); ok && p.ID == rank.ProductID {
				selectedSpecs = append(selectedSpecs, p)
			}
		}
	}
	ctx.State.StepOutputs["ProductRecommenderStep_Specs"] = selectedSpecs
 
	return ranks, nil
}

// ragMatch mirrors the Python sidecar's /api/rag/search response shape for a single hit.
type ragMatch struct {
	PageNumber int      `json:"page_number"`
	Content    string   `json:"content"`
	Images     []string `json:"images"`
	Score      float64  `json:"score"`
}

// searchRAGOnce issues a single query against the Python sidecar's vector search endpoint.
func searchRAGOnce(ctx *workflow.Context, query string, limit int) ([]ragMatch, error) {
	pythonUrl := os.Getenv("PYTHON_SERVICE_URL")
	if pythonUrl == "" {
		pythonUrl = "http://localhost:8000"
	}

	payload := map[string]interface{}{
		"query": query,
		"limit": limit,
	}
	jsonBytes, err := json.Marshal(payload)
	if err != nil {
		return nil, err
	}

	client := &http.Client{Timeout: 10 * time.Second}
	reqUrl := fmt.Sprintf("%s/api/rag/search", pythonUrl)
	req, err := http.NewRequest("POST", reqUrl, bytes.NewBuffer(jsonBytes))
	if err != nil {
		return nil, fmt.Errorf("failed to create RAG search request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Job-ID", ctx.JobID)

	resp, err := client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("RAG search HTTP error: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("RAG search status code error: %d", resp.StatusCode)
	}

	var searchResp struct {
		Matches []ragMatch `json:"matches"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&searchResp); err != nil {
		return nil, fmt.Errorf("failed to decode RAG search matches: %w", err)
	}
	return searchResp.Matches, nil
}

// queryRAGAndParse runs one retrieval query per hobby instead of one blended query covering the
// whole profile. Catalog text describes products/activities, never customer demographics, so
// keeping family size/income/location out of the embedded query avoids diluting the semantic
// match with vocabulary the catalog never uses. Per-hobby search also ensures a customer with
// multiple distinct interests (e.g. "trekking, swimming") gets coverage across both, instead of
// a single blended query letting one dominant hobby crowd out the other in the results.
func queryRAGAndParse(ctx *workflow.Context, hobbies []string) ([]recommendation.Candidate, RAGDebugInfo, error) {
	debug := RAGDebugInfo{}

	queryHobbies := hobbies
	if len(queryHobbies) == 0 {
		queryHobbies = []string{"general everyday use"}
	}

	const perHobbyLimit = 4
	const maxCombinedMatches = 6

	merged := make(map[int]ragMatch)
	var issuedQueries []string

	for _, hobby := range queryHobbies {
		hobby = strings.TrimSpace(hobby)
		if hobby == "" {
			continue
		}
		queryText := fmt.Sprintf("Gear and equipment for %s.", hobby)
		issuedQueries = append(issuedQueries, queryText)

		matches, err := searchRAGOnce(ctx, queryText, perHobbyLimit)
		if err != nil {
			log.Printf("[TraceID: %s] [JobID: %s] [RAG] [WARNING] search failed for hobby %q: %v", ctx.TraceID, ctx.JobID, hobby, err)
			continue
		}
		for _, m := range matches {
			if existing, ok := merged[m.PageNumber]; !ok || m.Score > existing.Score {
				merged[m.PageNumber] = m
			}
		}
	}

	debug.Query = strings.Join(issuedQueries, " | ")

	if len(merged) == 0 {
		return nil, debug, fmt.Errorf("RAG search returned 0 matches across %d hobby queries", len(issuedQueries))
	}

	mergedList := make([]ragMatch, 0, len(merged))
	for _, m := range merged {
		mergedList = append(mergedList, m)
	}
	sort.Slice(mergedList, func(i, j int) bool { return mergedList[i].Score > mergedList[j].Score })
	if len(mergedList) > maxCombinedMatches {
		mergedList = mergedList[:maxCombinedMatches]
	}

	for _, m := range mergedList {
		debug.Matches = append(debug.Matches, RAGDebugHit{
			PageNumber: m.PageNumber,
			Score:      m.Score,
			ImageCount: len(m.Images),
			ContentLen: len(m.Content),
		})
	}
	debug.MatchCount = len(mergedList)

	log.Printf("[TraceID: %s] [JobID: %s] [RAG] Merged search results across %d hobby queries:", ctx.TraceID, ctx.JobID, len(issuedQueries))
	for i, m := range mergedList {
		log.Printf("[RAG Result #%d] Page: %d, Score: %f, Images: %v, Content Length: %d characters", i+1, m.PageNumber, m.Score, m.Images, len(m.Content))
	}

	var buffer bytes.Buffer
	for _, m := range mergedList {
		buffer.WriteString(fmt.Sprintf("--- PAGE %d ---\n%s\n", m.PageNumber, m.Content))
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
  "specs": {
    "type": "Product Category (e.g. Refrigerator, Washer, Sedan)",
    "capacity": "Specifications (e.g. 26 cu. ft., 5.0 cu. ft., or 5 seats)",
    "power": "Power specs or Horsepower if applicable"
  },
  "colors": ["Color 1", "Color 2"],
  "page_number": X // Keep the page number integer from the matched section title (e.g. 3 if matching --- PAGE 3 ---)
}
If pricing or specific specs are not listed, make a highly accurate estimate.

IMPORTANT: You MUST produce at least one item per matched page below, even if the page reads
like marketing copy rather than a clean spec sheet. If no distinct product name is stated,
infer the product category from context (hobbies, imagery cues, section headings) and use that
as the model name (e.g. "Trekking Backpack" or "Camping Tent Package") rather than omitting the
page. Never return an empty array - a best-effort estimate is always preferred over nothing.

Matched Pages Content:
%s

Respond with a JSON array containing these objects. Do not output any markdown formatting or commentary. Just output the raw JSON array.`, matchedText)

	var parsedItems []recommendation.Product

	err := workflow.CallGemini(ctx, prompt, &parsedItems)
	if err != nil {
		return nil, debug, fmt.Errorf("failed to parse matched pages using Gemini: %w", err)
	}

	var candidates []recommendation.Candidate
	for _, p := range parsedItems {
		// Lookup original matching RAG hit to copy its page-extracted image
		for _, match := range mergedList {
			if match.PageNumber == p.PageNumber && len(match.Images) > 0 {
				p.HeroImage = match.Images[0]
				break
			}
		}

		candidates = append(candidates, p)
	}
	return candidates, debug, nil
}
