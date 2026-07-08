package recommendation

// Candidate defines a generic interface for items that can be recommended
type Candidate interface {
	GetID() string
	GetAttributes() map[string]interface{}
}

// UserProfile holds demographic data used for recommendation and segmentation rules
type UserProfile struct {
	Age        int      `json:"age"`
	Income     float64  `json:"income"`
	Hobbies    []string `json:"hobbies"`
	FamilySize int      `json:"family_size"`
	Location   string   `json:"location"`
}

// ScoreResult stores the outcomes of matching logic on a single candidate
type ScoreResult struct {
	CandidateID  string   `json:"candidate_id"`
	Score        int      `json:"score"`
	MatchedRules []string `json:"matched_rules"`
	Explanation  string   `json:"explanation"`
}

// RecommendationEngine defines the generic matching contract
type RecommendationEngine interface {
	ScoreCandidate(user UserProfile, candidate Candidate) (ScoreResult, error)
	RankCandidates(user UserProfile, candidates []Candidate) ([]ScoreResult, error)
}
