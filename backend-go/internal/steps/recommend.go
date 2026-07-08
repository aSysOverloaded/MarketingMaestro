package steps

import (
	"encoding/json"
	"fmt"

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

	// 2. Fetch candidates catalog (mock database fetch)
	var candidates []recommendation.Candidate

	// Check if a custom uploaded catalog array is provided in context
	if catalogRaw, ok := ctx.State.StepOutputs["UploadedCatalog"]; ok {
		if catalogList, ok := catalogRaw.([]recommendation.Vehicle); ok && len(catalogList) > 0 {
			for _, v := range catalogList {
				candidates = append(candidates, v)
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
				ID:        "car_bmw_x3",
				Model:     "BMW X3",
				BasePrice: 65000,
				Features:  []string{"AWD", "Panoramic Sunroof", "Roof Rails", "Leather Seats"},
				EngineSpecs: map[string]interface{}{
					"seats": 5,
				},
				Colors: []string{"Alpine White", "Black Sapphire"},
			},
			recommendation.Vehicle{
				ID:        "suv_7seater",
				Model:     "Adventure Navigator 7S",
				BasePrice: 80000,
				Features:  []string{"AWD", "Roof Rails", "Tow Hitch", "Heavy Duty Suspension"},
				EngineSpecs: map[string]interface{}{
					"seats": 7,
				},
				Colors: []string{"Forest Green", "Stealth Grey"},
			},
			recommendation.Vehicle{
				ID:        "sedan_compact",
				Model:     "Economy Touring Sedan",
				BasePrice: 43000,
				Features:  []string{"Front Wheel Drive", "Smart Cruise Control"},
				EngineSpecs: map[string]interface{}{
					"seats": 5,
				},
				Colors: []string{"Silver Metallic", "Midnight Black"},
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
Given the following User Profile and Candidate Vehicles Catalog, rank the candidates in descending order of how well they match the user's needs.
Select the top candidates (select 2 options) that are suitable.

User Profile:
%s

Candidate Catalog:
%s

For each recommended vehicle, calculate:
- A match score (0 to 100) based on budget suitability, size requirements, active hobbies vs vehicle features (like AWD/Roof Rails), and segment fit.
- Matched rules: specific, concise reasons why it matches (e.g. "Fits budget", "AWD fits outdoor lifestyle", "7 seats matches family size").
- Explanation: a 1-sentence summary of why this vehicle is recommended for the user.

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
