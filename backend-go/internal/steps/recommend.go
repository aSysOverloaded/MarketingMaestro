package steps

import (
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

// Execute retrieves user profiles, runs candidate matchers, and logs the top candidate
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
	if hObs, ok := profileResult.Attributes["hobbies"].([]string); ok {
		hobbiesSlice = hObs
	}
	
	incomeVal := 0.0
	if inc, ok := profileResult.Attributes["income"].(float64); ok {
		incomeVal = inc
	}

	famSize := 1
	if fs, ok := profileResult.Attributes["family_size"].(int); ok {
		famSize = fs
	}

	userProfile := recommendation.UserProfile{
		Age:        32,
		Income:     incomeVal,
		Hobbies:    hobbiesSlice,
		FamilySize: famSize,
		Location:   "Seattle, WA",
	}

	// 2. Fetch candidates catalog (mock database fetch)
	candidates := []recommendation.Candidate{
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

	// 3. Rank Candidates
	ranks, err := s.matcher.RankCandidates(userProfile, candidates)
	if err != nil {
		return nil, fmt.Errorf("failed to rank vehicle candidates: %w", err)
	}

	if len(ranks) == 0 {
		return nil, fmt.Errorf("no matching candidates found in the catalog")
	}

	// Select top recommendation
	top := ranks[0]

	return workflow.RecommendationResult{
		RecommendationID: fmt.Sprintf("rec_job_%s", ctx.JobID),
		VehicleID:        top.CandidateID,
		Score:            top.Score,
		MatchedRules:     top.MatchedRules,
		Explanation:      top.Explanation,
	}, nil
}
