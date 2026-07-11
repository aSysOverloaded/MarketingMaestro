package recommendation

import (
	"fmt"
	"sort"
	"strings"
)

// Vehicle represents the structural specifications of a car, implementing Candidate
type Vehicle struct {
	ID          string                 `json:"id"`
	Model       string                 `json:"model"`
	BasePrice   float64                `json:"base_price"`
	Features    []string               `json:"features"`
	EngineSpecs map[string]interface{} `json:"engine_specs"`
	Colors      []string               `json:"colors"`
	HeroImage   string                 `json:"hero_image,omitempty"`
}

// GetID implements Candidate
func (v Vehicle) GetID() string {
	return v.ID
}

// GetAttributes implements Candidate
func (v Vehicle) GetAttributes() map[string]interface{} {
	return map[string]interface{}{
		"model":        v.Model,
		"base_price":   v.BasePrice,
		"features":     v.Features,
		"engine_specs": v.EngineSpecs,
		"colors":       v.Colors,
	}
}

// VehicleMatcher implements RecommendationEngine using deterministic scoring rules
type VehicleMatcher struct{}

// NewVehicleMatcher creates a new instance of VehicleMatcher
func NewVehicleMatcher() *VehicleMatcher {
	return &VehicleMatcher{}
}

// ScoreCandidate evaluates a vehicle against the UserProfile
func (m *VehicleMatcher) ScoreCandidate(user UserProfile, candidate Candidate) (ScoreResult, error) {
	vehicle, ok := candidate.(Vehicle)
	if !ok {
		// Attempt parsing from attributes maps if passed as raw candidate
		attrs := candidate.GetAttributes()
		model, _ := attrs["model"].(string)
		price, _ := attrs["base_price"].(float64)
		feats, _ := attrs["features"].([]string)
		specs, _ := attrs["engine_specs"].(map[string]interface{})
		clrs, _ := attrs["colors"].([]string)

		vehicle = Vehicle{
			ID:          candidate.GetID(),
			Model:       model,
			BasePrice:   price,
			Features:    feats,
			EngineSpecs: specs,
			Colors:      clrs,
		}
	}

	score := 50 // Baseline score
	var matchedRules []string

	// 1. Budget Fit Checks
	monthlyIncome := user.Income / 12.0
	estimatedMonthlyPayment := (vehicle.BasePrice / 60.0) * 1.12 // 5-year loan with interest

	if monthlyIncome * 0.20 >= estimatedMonthlyPayment {
		score += 25
		matchedRules = append(matchedRules, "Budget Fits (Estimated payment is under 20% of monthly income)")
	} else if monthlyIncome * 0.35 < estimatedMonthlyPayment {
		score -= 30
		matchedRules = append(matchedRules, "Budget Mismatch (Estimated payment exceeds 35% of monthly income)")
	} else {
		score += 10
		matchedRules = append(matchedRules, "Budget Acceptable (Estimated payment is within comfortable range)")
	}

	// 2. Family Size Capacity Checks
	seatsVal, hasSeats := vehicle.EngineSpecs["seats"]
	seats := 5 // Default
	if hasSeats {
		if sFloat, ok := seatsVal.(float64); ok {
			seats = int(sFloat)
		} else if sInt, ok := seatsVal.(int); ok {
			seats = sInt
		}
	}

	if user.FamilySize >= 5 {
		if seats >= 7 {
			score += 30
			matchedRules = append(matchedRules, fmt.Sprintf("Family Capacity Match (7+ seater matches family size of %d)", user.FamilySize))
		} else {
			score -= 25
			matchedRules = append(matchedRules, fmt.Sprintf("Space Mismatch (5-seater is tight for family of %d)", user.FamilySize))
		}
	} else if user.FamilySize <= 2 {
		if seats <= 5 {
			score += 15
			matchedRules = append(matchedRules, "Optimal Utility (Compact seating is efficient for small household)")
		}
	}

	// 3. Hobbies / Lifestyle Matching
	hasAWD := false
	for _, f := range vehicle.Features {
		fl := strings.ToLower(f)
		if fl == "awd" || fl == "4wd" || fl == "all-wheel drive" {
			hasAWD = true
			break
		}
	}

	isAdventurer := false
	for _, hobby := range user.Hobbies {
		h := strings.ToLower(hobby)
		if h == "trekking" || h == "camping" || h == "skiing" || h == "adventure" || h == "offroading" {
			isAdventurer = true
		}
	}

	if isAdventurer {
		if hasAWD {
			score += 30
			matchedRules = append(matchedRules, "Capability Match (AWD handles trekking/adventure hobbies)")
		} else {
			score -= 10
			matchedRules = append(matchedRules, "Capability Warning (Adventure lifestyle benefits from AWD, which this model lacks)")
		}
	}

	// Utility accessories check
	for _, f := range vehicle.Features {
		fl := strings.ToLower(f)
		if fl == "roof rails" || fl == "tow hitch" || fl == "cargo organizer" {
			for _, hobby := range user.Hobbies {
				h := strings.ToLower(hobby)
				if h == "camping" || h == "skiing" || h == "adventure" {
					score += 15
					matchedRules = append(matchedRules, fmt.Sprintf("Accessory Match (%s supports active outdoor hobbies)", f))
				}
			}
		}
	}

	// 4. Executive/Luxury fit
	isExecutive := user.Income >= 150000.0 && user.Age >= 30
	hasLuxuryFeatures := false
	for _, f := range vehicle.Features {
		fl := strings.ToLower(f)
		if fl == "leather seats" || fl == "panoramic sunroof" || fl == "premium audio" || fl == "ventilated seats" {
			hasLuxuryFeatures = true
		}
	}

	if isExecutive && hasLuxuryFeatures {
		score += 20
		matchedRules = append(matchedRules, "Lifestyle Fit (Premium features suit high-income executive segment)")
	}

	// Clamp score between 0 and 100
	if score > 100 {
		score = 100
	}
	if score < 0 {
		score = 0
	}

	// Generate a concise explanation string
	explanation := fmt.Sprintf("Recommended with a score of %d/100 based on %d matched guidelines.", score, len(matchedRules))

	return ScoreResult{
		CandidateID:  vehicle.ID,
		Score:        score,
		MatchedRules: matchedRules,
		Explanation:  explanation,
	}, nil
}

// RankCandidates scores and orders candidates in descending order
func (m *VehicleMatcher) RankCandidates(user UserProfile, candidates []Candidate) ([]ScoreResult, error) {
	var results []ScoreResult
	for _, c := range candidates {
		res, err := m.ScoreCandidate(user, c)
		if err != nil {
			return nil, err
		}
		results = append(results, res)
	}

	// Sort descending by score
	sort.Slice(results, func(i, j int) bool {
		return results[i].Score > results[j].Score
	})

	return results, nil
}
