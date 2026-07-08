package recommendation

import (
	"testing"
)

func TestScoreCandidateAdventurerBudgetFit(t *testing.T) {
	matcher := NewVehicleMatcher()

	user := UserProfile{
		Age:        32,
		Income:     120000,
		Hobbies:    []string{"trekking", "camping"},
		FamilySize: 4,
		Location:   "Seattle",
	}

	vehicle := Vehicle{
		ID:        "car_bmw_x3",
		Model:     "BMW X3",
		BasePrice: 65000,
		Features:  []string{"AWD", "Panoramic Sunroof", "Roof Rails"},
		EngineSpecs: map[string]interface{}{
			"seats": 5,
		},
		Colors: []string{"Alpine White", "Phytonic Blue"},
	}

	res, err := matcher.ScoreCandidate(user, vehicle)
	if err != nil {
		t.Fatalf("Unexpected scoring error: %v", err)
	}

	// Baseline is 50
	// 1. Budget: monthly income = 10000. monthly payment = (65000/60)*1.12 = 1213.33. 
	// 20% of 10000 is 2000. 1213.33 <= 2000. Matches "Budget Fits" (+25) -> Score = 75
	// 2. Family: 4 members, 5 seater -> no special rule triggers (FamilySize >= 5 or <= 2)
	// 3. Hobbies: trekking/camping & AWD -> "Capability Match" (+30) -> Score = 105 (Clamped to 100)
	// 4. Accessory Match: camping & Roof Rails (+15) -> Score = 120 (Clamped to 100)
	
	if res.Score != 100 {
		t.Errorf("Expected score to be clamped to 100, got: %d", res.Score)
	}

	hasBudgetRule := false
	hasAWDRule := false
	hasAccessoryRule := false
	for _, r := range res.MatchedRules {
		if r == "Budget Fits (Estimated payment is under 20% of monthly income)" {
			hasBudgetRule = true
		}
		if r == "Capability Match (AWD handles trekking/adventure hobbies)" {
			hasAWDRule = true
		}
		if r == "Accessory Match (Roof Rails supports active outdoor hobbies)" {
			hasAccessoryRule = true
		}
	}

	if !hasBudgetRule {
		t.Error("Expected 'Budget Fits' rule to be matched")
	}
	if !hasAWDRule {
		t.Error("Expected 'Capability Match' rule to be matched")
	}
	if !hasAccessoryRule {
		t.Error("Expected 'Accessory Match' rule to be matched")
	}
}

func TestScoreSpaceMismatchAndLowIncome(t *testing.T) {
	matcher := NewVehicleMatcher()

	user := UserProfile{
		Age:        28,
		Income:     40000,
		Hobbies:    []string{"reading"},
		FamilySize: 5,
	}

	vehicle := Vehicle{
		ID:        "car_sedan",
		Model:     "Compact Sedan",
		BasePrice: 43000,
		Features:  []string{"Front Wheel Drive"},
		EngineSpecs: map[string]interface{}{
			"seats": 5,
		},
	}

	res, err := matcher.ScoreCandidate(user, vehicle)
	if err != nil {
		t.Fatalf("Unexpected error: %v", err)
	}

	// Monthly income = 3333.33. estimated payment = (43000/60)*1.12 = 802.66.
	// 35% of 3333.33 is 1166.66, and 20% is 666.66. Estimated payment is under 35% but over 20%. Budget Acceptable (+10).
	// Family size = 5, Seats = 5 -> Space Mismatch (-25).
	// Score: 50 + 10 - 25 = 35.
	if res.Score != 35 {
		t.Errorf("Expected score 35, got: %d", res.Score)
	}
}

func TestRankCandidates(t *testing.T) {
	matcher := NewVehicleMatcher()

	user := UserProfile{
		Age:        35,
		Income:     160000,
		Hobbies:    []string{"trekking", "camping"},
		FamilySize: 5,
	}

	c1 := Vehicle{
		ID:        "suv_7seater",
		Model:     "Large AWD SUV",
		BasePrice: 80000,
		Features:  []string{"AWD", "Roof Rails", "Leather Seats"},
		EngineSpecs: map[string]interface{}{
			"seats": 7,
		},
	}

	c2 := Vehicle{
		ID:        "sedan_5seater",
		Model:     "Luxury Sedan",
		BasePrice: 75000,
		Features:  []string{"Panoramic Sunroof", "Leather Seats"},
		EngineSpecs: map[string]interface{}{
			"seats": 5,
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

	// c1 (suv_7seater):
	// Budget: monthly income = 13333. payment = (80000/60)*1.12 = 1493. 20% limit is 2666. Fits (+25) -> 75
	// Family: size 5, seats 7 -> Family capacity match (+30) -> 105 (clamped 100)
	// Hobbies: trekking & AWD -> Capability match (+30) -> 135 (clamped 100)
	// Accessories: camping & Roof Rails (+15) -> 150 (clamped 100)
	// Luxury executive fit: Income 160k & Age 35 & Luxury features -> Lifestyle Fit (+20) -> Score = 100

	// c2 (sedan_5seater):
	// Budget fits (+25) -> 75
	// Family size 5, seats 5 -> Space mismatch (-25) -> 50
	// No AWD or Adventure hobbies match.
	// Luxury executive fit -> Lifestyle fit (+20) -> 70.

	if ranks[0].CandidateID != "suv_7seater" {
		t.Errorf("Expected top ranked candidate to be suv_7seater, got: %s", ranks[0].CandidateID)
	}

	if ranks[0].Score <= ranks[1].Score {
		t.Errorf("Expected suv_7seater score (%d) to be higher than sedan_5seater score (%d)", ranks[0].Score, ranks[1].Score)
	}
}
