package steps

import (
	"encoding/json"
	"fmt"

	"marketing-agent/internal/workflow"
)

// UserProfileStep translates user demographics into a segment using Gemini
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

	// Check if dynamic profile input was passed from a frontend/client
	if inputRaw, ok := ctx.State.StepOutputs["UserProfileInput"]; ok {
		if inputProfile, ok := inputRaw.(map[string]interface{}); ok {
			if age, ok := inputProfile["age"].(int); ok {
				rawAge = age
			}
			if income, ok := inputProfile["income"].(float64); ok {
				rawIncome = income
			}
			if familySize, ok := inputProfile["family_size"].(int); ok {
				rawFamilySize = familySize
			}
			if hobbies, ok := inputProfile["hobbies"].([]string); ok {
				rawHobbies = hobbies
			}
			if location, ok := inputProfile["location"].(string); ok {
				rawLocation = location
			}
		}
	}

	hobbiesBytes, err := json.Marshal(rawHobbies)
	if err != nil {
		return nil, fmt.Errorf("failed to marshal hobbies: %w", err)
	}

	prompt := fmt.Sprintf(`You are an expert marketing profiling agent.
Classify the following user demographics into a segment and budget tier:
- Age: %d
- Annual Income: $%.2f
- Family Size: %d
- Hobbies: %s
- Location: %s

Respond with a JSON object containing precisely these fields:
{
  "user_profile_id": "%s_profile",
  "segment": "Segment Name (choose the best from: Adventure, Executive, Family, Standard)",
  "budget_tier": "Budget Tier Name (choose the best from: Economy, Mid-Range, Premium, Ultra Luxury)",
  "attributes": {
    "age": %d,
    "income": %.2f,
    "family_size": %d,
    "hobbies": %s,
    "location": "%s"
  }
}
Do not output any markdown formatting or commentary. Just output the raw JSON object.`,
		rawAge, rawIncome, rawFamilySize, string(hobbiesBytes), rawLocation,
		ctx.JobID,
		rawAge, rawIncome, rawFamilySize, string(hobbiesBytes), rawLocation,
	)

	var result workflow.UserProfileResult
	err = workflow.CallGemini(ctx, prompt, &result)
	if err != nil {
		return nil, fmt.Errorf("failed to classify user profile using Gemini: %w", err)
	}

	return result, nil
}
