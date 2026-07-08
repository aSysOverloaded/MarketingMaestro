package steps

import (
	"fmt"
	"html/template"
	"os"
	"path/filepath"
	"strings"

	"marketing-agent/internal/workflow"
)

// CompileHTMLStep implements workflow.Step, compiling user specs & copy into a Tailwind HTML document
type CompileHTMLStep struct {
	templatesDir string
	outputDir    string
}

// NewCompileHTMLStep creates a new CompileHTMLStep instance
func NewCompileHTMLStep(templatesDir, outputDir string) *CompileHTMLStep {
	return &CompileHTMLStep{
		templatesDir: templatesDir,
		outputDir:    outputDir,
	}
}

// Name implements workflow.Step
func (s *CompileHTMLStep) Name() string {
	return "HTMLCompileStep"
}

// Execute parses variables and compiles them with the HTML/Tailwind template
func (s *CompileHTMLStep) Execute(ctx *workflow.Context) (workflow.Result, error) {
	// 1. Extract results from previous steps
	userProfileRaw, ok := ctx.State.StepOutputs["UserProfileStep"]
	if !ok {
		return nil, fmt.Errorf("missing user profile result from previous step")
	}
	userProfile, ok := userProfileRaw.(workflow.UserProfileResult)
	if !ok {
		return nil, fmt.Errorf("invalid user profile result format")
	}

	recRaw, ok := ctx.State.StepOutputs["ProductRecommenderStep"]
	if !ok {
		return nil, fmt.Errorf("missing product recommendation result from previous step")
	}
	rec, ok := recRaw.(workflow.RecommendationResult)
	if !ok {
		return nil, fmt.Errorf("invalid recommendation result format")
	}

	// 2. Fetch vehicle spec from static database matching RecommendationResult.VehicleID
	vehicle := s.getVehicleSpecByID(rec.VehicleID)

	// 3. Resolve Brand Guidelines based on vehicle name
	brand := s.getBrandConfigByModel(vehicle.Model)

	// 4. Resolve Copy (use AI output if generated; otherwise choose a deterministic fallback by segment)
	copyText := s.resolveMarketingCopy(ctx, userProfile.Segment)

	// 5. Build template compile data
	templateData := struct {
		Brand struct {
			Name           string
			PrimaryColor   string
			SecondaryColor string
			Initial        string
		}
		User struct {
			Email   string
			Segment string
			TraceID string
		}
		Vehicle struct {
			Model     string
			BasePrice string
			HeroImage string
			Seats     int
			Horsepower int
			Features  []string
		}
		Recommendation struct {
			Score        int
			MatchedRules []string
			Explanation  string
		}
		Copy struct {
			Headline    string
			Subheadline string
			CTAText     string
		}
	}{
		Brand: brand,
		User: struct {
			Email   string
			Segment string
			TraceID string
		}{
			Email:   ctx.State.UserProfileID, // using UserProfileID as placeholder or user_email
			Segment: userProfile.Segment,
			TraceID: ctx.TraceID,
		},
		Vehicle:        vehicle,
		Recommendation: struct {
			Score        int
			MatchedRules []string
			Explanation  string
		}{
			Score:        rec.Score,
			MatchedRules: rec.MatchedRules,
			Explanation:  rec.Explanation,
		},
		Copy:           copyText,
	}

	// Ensure email falls back if empty
	if templateData.User.Email == "" {
		templateData.User.Email = ctx.State.UserProfileID
	}
	if templateData.User.Email == "" {
		templateData.User.Email = "customer@example.com"
	}

	// 6. Execute HTML Template compilation
	templatePath := filepath.Join(s.templatesDir, "brochure_template.html")
	tmpl, err := template.ParseFiles(templatePath)
	if err != nil {
		return nil, fmt.Errorf("failed to parse html brochure template: %w", err)
	}

	// Create temp output directory if missing
	if err := os.MkdirAll(s.outputDir, 0755); err != nil {
		return nil, fmt.Errorf("failed to create output directory: %w", err)
	}

	outputFilePath := filepath.Join(s.outputDir, fmt.Sprintf("compiled_job_%s.html", ctx.JobID))
	outFile, err := os.Create(outputFilePath)
	if err != nil {
		return nil, fmt.Errorf("failed to create output html file: %w", err)
	}
	defer outFile.Close()

	if err := tmpl.Execute(outFile, templateData); err != nil {
		return nil, fmt.Errorf("failed to execute template: %w", err)
	}

	return outputFilePath, nil
}

// Struct representing template-friendly vehicle spec
type templateVehicleSpec struct {
	Model      string
	BasePrice  string
	HeroImage  string
	Seats      int
	Horsepower int
	Features   []string
}

// getVehicleSpecByID mimics querying PostgreSQL spec tables
func (s *CompileHTMLStep) getVehicleSpecByID(id string) templateVehicleSpec {
	switch id {
	case "car_bmw_x3":
		return templateVehicleSpec{
			Model:      "BMW X3 M Sport",
			BasePrice:  "65,000",
			HeroImage:  "https://images.unsplash.com/photo-1617814076367-b759c7d7e738?auto=format&fit=crop&q=80&w=800",
			Seats:      5,
			Horsepower: 248,
			Features:   []string{"AWD xDrive system", "360 Surround View Camera", "Panoramic Glass Sunroof", "Active Blind Spot Detection", "Sensatec Upholstery", "Harman Kardon Audio"},
		}
	case "suv_7seater":
		return templateVehicleSpec{
			Model:      "Adventure Navigator 7S",
			BasePrice:  "80,000",
			HeroImage:  "https://images.unsplash.com/photo-1533473359331-0135ef1b58bf?auto=format&fit=crop&q=80&w=800",
			Seats:      7,
			Horsepower: 320,
			Features:   []string{"Dynamic 4WD Mode Selector", "High Ground Clearance suspension", "Integrated Roof Rails & Basket", "Fold-Flat Third Row seats", "Heavy Duty Tow Package", "Off-Road Underbody Shields"},
		}
	default:
		return templateVehicleSpec{
			Model:      "Premium Touring Sedan",
			BasePrice:  "43,000",
			HeroImage:  "https://images.unsplash.com/photo-1555215695-3004980ad54e?auto=format&fit=crop&q=80&w=800",
			Seats:      5,
			Horsepower: 180,
			Features:   []string{"Front Wheel Drive traction control", "Acoustic Double-Pane windows", "Smart cruise control", "Heated front and rear seats", "Digital driver display cockpits"},
		}
	}
}

// getBrandConfigByModel maps brand styling configurations deterministically
func (s *CompileHTMLStep) getBrandConfigByModel(model string) struct {
	Name           string
	PrimaryColor   string
	SecondaryColor string
	Initial        string
} {
	if strings.Contains(strings.ToLower(model), "bmw") {
		return struct {
			Name           string
			PrimaryColor   string
			SecondaryColor string
			Initial        string
		}{
			Name:           "BMW",
			PrimaryColor:   "#0066b2", // BMW Blue
			SecondaryColor: "#000000", // Black
			Initial:        "B",
		}
	} else if strings.Contains(strings.ToLower(model), "adventure") {
		return struct {
			Name           string
			PrimaryColor   string
			SecondaryColor string
			Initial        string
		}{
			Name:           "Navigator",
			PrimaryColor:   "#065f46", // Dark Forest Green
			SecondaryColor: "#064e3b",
			Initial:        "N",
		}
	}

	return struct {
		Name           string
		PrimaryColor   string
		SecondaryColor string
		Initial        string
	}{
		Name:           "Elite Motors",
		PrimaryColor:   "#1e3a8a", // Dark Blue
		SecondaryColor: "#0f172a",
		Initial:        "E",
	}
}

// resolveMarketingCopy selects generated copy or applies a fallback copy layout by user segment
func (s *CompileHTMLStep) resolveMarketingCopy(ctx *workflow.Context, segment string) struct {
	Headline    string
	Subheadline string
	CTAText     string
} {
	// First check if AI copy is available in step outputs
	if copyRaw, ok := ctx.State.StepOutputs["CopywriterStep"]; ok {
		if copyResult, ok := copyRaw.(workflow.GeneratedCopyResult); ok {
			headline := copyResult.CopyData["headline"]
			subheadline := copyResult.CopyData["subheadline"]
			cta := copyResult.CopyData["cta"]
			return struct {
				Headline    string
				Subheadline string
				CTAText     string
			}{
				Headline:    headline,
				Subheadline: subheadline,
				CTAText:     cta,
			}
		}
	}

	// Fallback copy structures per segment
	switch strings.ToLower(segment) {
	case "adventure", "adventure family":
		return struct {
			Headline    string
			Subheadline string
			CTAText     string
		}{
			Headline:    "Adventure Starts Here",
			Subheadline: "Designed for every weekend escape.",
			CTAText:     "Explore trails and climb mountains confidently with high clearance and rugged AWD systems. Take control of your next excursion by arranging a test drive.",
		}
	case "family", "family safety":
		return struct {
			Headline    string
			Subheadline string
			CTAText     string
		}{
			Headline:    "Safety & Comfort Redefined",
			Subheadline: "Engineered around the family roadtrip experience.",
			CTAText:     "Designed with top-tier safety features, flexible seating arrangements, and premium cabin isolation, ensuring every journey is secure. Request a demo drive.",
		}
	default:
		return struct {
			Headline    string
			Subheadline string
			CTAText     string
		}{
			Headline:    "Performance Meets Luxury",
			Subheadline: "Precision handling and class-leading comforts.",
			CTAText:     "Experience a drive optimized for quiet daily commutes, responsive cruising speeds, and modern driver-assist systems. Curation begins with a test drive.",
		}
	}
}
