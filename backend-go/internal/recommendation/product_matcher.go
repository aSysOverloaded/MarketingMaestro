package recommendation

import (
	"fmt"
	"sort"
	"strings"
)

// Product represents the structural specifications of a general catalog item, implementing Candidate
type Product struct {
	ID          string                 `json:"id"`
	Model       string                 `json:"model"`
	BasePrice   float64                `json:"base_price"`
	Features    []string               `json:"features"`
	Specs       map[string]interface{} `json:"specs"`
	Colors      []string               `json:"colors"`
	HeroImage   string                 `json:"hero_image,omitempty"`
	PageNumber  int                    `json:"page_number,omitempty"`
}

// GetID implements Candidate
func (p Product) GetID() string {
	return p.ID
}

// GetAttributes implements Candidate
func (p Product) GetAttributes() map[string]interface{} {
	return map[string]interface{}{
		"model":      p.Model,
		"base_price": p.BasePrice,
		"features":   p.Features,
		"specs":      p.Specs,
		"colors":     p.Colors,
	}
}

// ProductMatcher implements RecommendationEngine using deterministic scoring rules
type ProductMatcher struct{}

// NewProductMatcher creates a new instance of ProductMatcher
func NewProductMatcher() *ProductMatcher {
	return &ProductMatcher{}
}

// ScoreCandidate evaluates a product against the UserProfile
func (m *ProductMatcher) ScoreCandidate(user UserProfile, candidate Candidate) (ScoreResult, error) {
	product, ok := candidate.(Product)
	if !ok {
		// Attempt parsing from attributes maps if passed as raw candidate
		attrs := candidate.GetAttributes()
		model, _ := attrs["model"].(string)
		price, _ := attrs["base_price"].(float64)
		feats, _ := attrs["features"].([]string)
		specs, _ := attrs["specs"].(map[string]interface{})
		clrs, _ := attrs["colors"].([]string)

		product = Product{
			ID:        candidate.GetID(),
			Model:     model,
			BasePrice: price,
			Features:  feats,
			Specs:     specs,
			Colors:    clrs,
		}
	}

	score := 50 // Baseline score
	var matchedRules []string

	// 1. Budget Fit Checks
	monthlyIncome := user.Income / 12.0
	estimatedMonthlyPayment := (product.BasePrice / 36.0) * 1.05 // standard 3-year financing check

	if monthlyIncome*0.15 >= estimatedMonthlyPayment {
		score += 25
		matchedRules = append(matchedRules, "Budget Fits (Estimated price is well within comfortable household guidelines)")
	} else if monthlyIncome*0.30 < estimatedMonthlyPayment {
		score -= 30
		matchedRules = append(matchedRules, "Budget Warning (Estimated price exceeds standard budget guidelines)")
	} else {
		score += 10
		matchedRules = append(matchedRules, "Budget Acceptable (Estimated cost is within reasonable household parameters)")
	}

	// 2. Capacity & Family Size Fit
	capacityVal, hasCapacity := product.Specs["capacity"]
	if hasCapacity {
		capStr := fmt.Sprintf("%v", capacityVal)
		if user.FamilySize >= 4 && (strings.Contains(capStr, "26") || strings.Contains(capStr, "5.0") || strings.Contains(capStr, "Large") || strings.Contains(capStr, "16")) {
			score += 25
			matchedRules = append(matchedRules, "Optimal Capacity (Large capacity/size matches a family of 4+ members)")
		} else if user.FamilySize <= 2 && (strings.Contains(capStr, "Compact") || strings.Contains(capStr, "18")) {
			score += 15
			matchedRules = append(matchedRules, "Optimal Utility (Compact capacity fits smaller household requirements)")
		}
	}

	// 3. Hobbies / Lifestyle Matching
	matchedHobbies := 0
	for _, hobby := range user.Hobbies {
		hl := strings.ToLower(hobby)
		for _, feature := range product.Features {
			fl := strings.ToLower(feature)
			if strings.Contains(fl, hl) || strings.Contains(hl, fl) {
				matchedHobbies++
			}
		}
	}
	if matchedHobbies > 0 {
		score += matchedHobbies * 10
		matchedRules = append(matchedRules, fmt.Sprintf("Lifestyle Fit (Product features align directly with household hobbies)"))
	}

	// 4. Premium Value
	if len(product.Features) >= 3 {
		score += 15
		matchedRules = append(matchedRules, "Feature Rich (Excellent feature set suits segment expectations)")
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
		CandidateID:  product.ID,
		Score:        score,
		MatchedRules: matchedRules,
		Explanation:  explanation,
	}, nil
}

// RankCandidates scores and orders candidates in descending order
func (m *ProductMatcher) RankCandidates(user UserProfile, candidates []Candidate) ([]ScoreResult, error) {
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
