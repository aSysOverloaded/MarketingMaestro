package recommendation

import (
	"testing"
)

func TestScoreCandidateBudgetFitAndFeatures(t *testing.T) {
	matcher := NewProductMatcher()

	user := UserProfile{
		Age:        32,
		Income:     120000,
		Hobbies:    []string{"trekking", "camping"},
		FamilySize: 4,
		Location:   "Seattle",
	}

	product := Product{
		ID:        "appliance_fridge_samsung",
		Model:     "Samsung Family Hub Refrigerator",
		BasePrice: 2499,
		Features:  []string{"Wi-Fi Connected Screen", "Triple Cooling System", "Internal Cameras", "Water & Ice Dispenser"},
		Specs: map[string]interface{}{
			"capacity": "26.5 cu. ft.",
			"type":     "Refrigerator",
		},
		Colors: []string{"Stainless Steel", "Black Stainless Steel"},
	}

	res, err := matcher.ScoreCandidate(user, product)
	if err != nil {
		t.Fatalf("Unexpected scoring error: %v", err)
	}

	// Expected calculations:
	// Baseline: 50
	// 1. Budget: monthly income = 10000. monthly payment = (2499/36)*1.05 = 72.88. 15% of 10000 is 1500. Matches "Budget Fits" (+25) -> Score = 75
	// 2. Capacity: Family size = 4, Capacity contains "26" -> Matches "Optimal Capacity" (+25) -> Score = 100
	// 3. Hobbies: no feature matches "trekking" or "camping" directly.
	// 4. Feature list len >= 3: Matches "Feature Rich" (+15) -> Score = 115 (Clamped to 100)
	
	if res.Score != 100 {
		t.Errorf("Expected score to be clamped to 100, got: %d", res.Score)
	}

	hasBudgetRule := false
	hasCapacityRule := false
	hasPremiumRule := false
	for _, r := range res.MatchedRules {
		if r == "Budget Fits (Estimated price is well within comfortable household guidelines)" {
			hasBudgetRule = true
		}
		if r == "Optimal Capacity (Large capacity/size matches a family of 4+ members)" {
			hasCapacityRule = true
		}
		if r == "Feature Rich (Excellent feature set suits segment expectations)" {
			hasPremiumRule = true
		}
	}

	if !hasBudgetRule {
		t.Error("Expected 'Budget Fits' rule to be matched")
	}
	if !hasCapacityRule {
		t.Error("Expected 'Optimal Capacity' rule to be matched")
	}
	if !hasPremiumRule {
		t.Error("Expected 'Feature Rich' rule to be matched")
	}
}

func TestRankCandidates(t *testing.T) {
	matcher := NewProductMatcher()

	user := UserProfile{
		Age:        35,
		Income:     160000,
		Hobbies:    []string{"cooking"},
		FamilySize: 5,
	}

	c1 := Product{
		ID:        "appliance_fridge_samsung",
		Model:     "Samsung Family Hub Refrigerator",
		BasePrice: 2499,
		Features:  []string{"Wi-Fi Connected Screen", "Triple Cooling System", "Internal Cameras"},
		Specs: map[string]interface{}{
			"capacity": "26.5 cu. ft.",
		},
	}

	c2 := Product{
		ID:        "appliance_washer_lg",
		Model:     "LG TurboWash Washing Machine",
		BasePrice: 899,
		Features:  []string{"AI DD Smart Fabric Care"},
		Specs: map[string]interface{}{
			"capacity": "Compact",
		},
	}

	candidates := []Candidate{c2, c1} // passed out of order

	ranks, err := matcher.RankCandidates(user, candidates)
	if err != nil {
		t.Fatalf("RankCandidates failed: %v", err)
	}

	if len(ranks) != 2 {
		t.Fatalf("Expected 2 ranked results, got: %d", len(ranks))
	}

	// c1 (Refrigerator):
	// Budget fits (+25)
	// Family size 5 & capacity contains "26" -> Optimal capacity match (+25)
	// Len features >= 3 -> Feature Rich (+15)
	// Score: 50 + 25 + 25 + 15 = 115 (clamped to 100)

	// c2 (Washer):
	// Budget fits (+25)
	// Family size 5 & capacity contains "Compact" -> Mismatch (no rule added, or not matched)
	// Len features = 1 -> (no rule)
	// Score: 50 + 25 = 75

	if ranks[0].CandidateID != "appliance_fridge_samsung" {
		t.Errorf("Expected top ranked candidate to be Refrigerator, got: %s", ranks[0].CandidateID)
	}

	if ranks[0].Score <= ranks[1].Score {
		t.Errorf("Expected Refrigerator score (%d) to be higher than Washer score (%d)", ranks[0].Score, ranks[1].Score)
	}
}
