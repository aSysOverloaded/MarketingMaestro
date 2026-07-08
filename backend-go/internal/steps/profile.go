package steps

import (
	"strings"

	"marketing-agent/internal/workflow"
)

// UserProfileStep runs deterministic rules to translate user demographics into a segment
type UserProfileStep struct{}

func NewUserProfileStep() *UserProfileStep {
	return &UserProfileStep{}
}

func (s *UserProfileStep) Name() string {
	return "UserProfileStep"
}

func (s *UserProfileStep) Execute(ctx *workflow.Context) (workflow.Result, error) {
	// Mock retrieving raw profile payload from database/metadata config
	// In production, this would load from a PostgreSQL table by user_id
	rawAge := 32
	rawIncome := 120000.0
	rawFamilySize := 4
	rawHobbies := []string{"trekking", "camping"}
	rawLocation := "Seattle, WA"

	segment := "Standard"
	budgetTier := "Economy"

	// 1. Determine Segment based on Hobbies
	isAdventurer := false
	for _, hobby := range rawHobbies {
		h := strings.ToLower(hobby)
		if h == "trekking" || h == "camping" || h == "skiing" || h == "adventure" || h == "offroading" {
			isAdventurer = true
		}
	}

	if isAdventurer {
		segment = "Adventure"
	} else if rawIncome >= 150000.0 && rawAge >= 30 {
		segment = "Executive"
	} else if rawFamilySize >= 5 {
		segment = "Family"
	}

	// 2. Determine Budget Tier
	if rawIncome >= 150000.0 {
		budgetTier = "Ultra Luxury"
	} else if rawIncome >= 90000.0 {
		budgetTier = "Premium"
	} else if rawIncome >= 50000.0 {
		budgetTier = "Mid-Range"
	}

	// Result details
	result := workflow.UserProfileResult{
		UserProfileID: ctx.JobID + "_profile",
		Segment:       segment,
		BudgetTier:    budgetTier,
		Attributes: map[string]interface{}{
			"age":         rawAge,
			"income":      rawIncome,
			"family_size": rawFamilySize,
			"hobbies":     rawHobbies,
			"location":    rawLocation,
		},
	}

	return result, nil
}
