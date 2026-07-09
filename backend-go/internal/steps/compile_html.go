package steps

import (
	"fmt"
	"html/template"
	"os"
	"path/filepath"
	"strings"

	"marketing-agent/internal/recommendation"
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
type recommendationItem struct {
	Brand struct {
		Name           string
		PrimaryColor   string
		SecondaryColor string
		Initial        string
	}
	Vehicle struct {
		Model      string
		BasePrice  string
		HeroImage  string
		Seats      int
		Horsepower int
		Features   []string
	}
	Recommendation struct {
		Score        int
		MatchedRules []string
		Explanation  string
	}
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

	var recs []workflow.RecommendationResult
	if slice, ok := recRaw.([]workflow.RecommendationResult); ok {
		recs = slice
	} else if single, ok := recRaw.(workflow.RecommendationResult); ok {
		recs = []workflow.RecommendationResult{single}
	} else if interfaceSlice, ok := recRaw.([]interface{}); ok {
		for _, item := range interfaceSlice {
			if rec, ok := item.(workflow.RecommendationResult); ok {
				recs = append(recs, rec)
			}
		}
	}

	if len(recs) == 0 {
		return nil, fmt.Errorf("invalid or empty recommendation results")
	}

	// 2. Fetch specifications and brand guidelines for all recommended options
	var recommendationItems []recommendationItem
	for _, r := range recs {
		var veh templateVehicleSpec
		found := false
		if specsRaw, ok := ctx.State.StepOutputs["ProductRecommenderStep_Specs"]; ok {
			if specsSlice, ok := specsRaw.([]recommendation.Vehicle); ok {
				for _, spec := range specsSlice {
					if spec.ID == r.VehicleID {
						seats := 5
						if sVal, ok := spec.EngineSpecs["seats"]; ok {
							if sFloat, ok := sVal.(float64); ok {
								seats = int(sFloat)
							} else if sInt, ok := sVal.(int); ok {
								seats = sInt
							}
						}
						
						descFeatures := spec.Features
						if pCat, ok := spec.EngineSpecs["type"].(string); ok {
							descFeatures = append([]string{fmt.Sprintf("Category: %s", pCat)}, descFeatures...)
						}
						if capVal, ok := spec.EngineSpecs["capacity"].(string); ok {
							descFeatures = append([]string{fmt.Sprintf("Capacity: %s", capVal)}, descFeatures...)
						}

						veh = templateVehicleSpec{
							Model:      spec.Model,
							BasePrice:  fmt.Sprintf("%.2f", spec.BasePrice),
							HeroImage:  "https://images.unsplash.com/photo-1584622650111-993a426fbf0a?auto=format&fit=crop&q=80&w=800",
							Seats:      seats,
							Horsepower: 0,
							Features:   descFeatures,
						}
						
						if strings.Contains(strings.ToLower(spec.Model), "refrigerator") {
							veh.HeroImage = "https://images.unsplash.com/photo-1584622650111-993a426fbf0a?auto=format&fit=crop&q=80&w=800"
						} else if strings.Contains(strings.ToLower(spec.Model), "washer") || strings.Contains(strings.ToLower(spec.Model), "washing") {
							veh.HeroImage = "https://images.unsplash.com/photo-1545173168-9f1947eebd01?auto=format&fit=crop&q=80&w=800"
						} else if strings.Contains(strings.ToLower(spec.Model), "tesla") {
							veh.HeroImage = "https://images.unsplash.com/photo-1617788138017-80ad40651399?auto=format&fit=crop&q=80&w=800"
						}

						if hpVal, ok := spec.EngineSpecs["horsepower"]; ok {
							if hpFloat, ok := hpVal.(float64); ok {
								veh.Horsepower = int(hpFloat)
							} else if hpInt, ok := hpVal.(int); ok {
								veh.Horsepower = hpInt
							}
						}

						found = true
						break
					}
				}
			}
		}

		if !found {
			dbVeh := s.getVehicleSpecByID(r.VehicleID)
			veh = templateVehicleSpec{
				Model:      dbVeh.Model,
				BasePrice:  dbVeh.BasePrice,
				HeroImage:  dbVeh.HeroImage,
				Seats:      dbVeh.Seats,
				Horsepower: dbVeh.Horsepower,
				Features:   dbVeh.Features,
			}
		}

		brnd := s.getBrandConfigByModel(veh.Model)

		item := recommendationItem{
			Brand:   brnd,
			Vehicle: veh,
			Recommendation: struct {
				Score        int
				MatchedRules []string
				Explanation  string
			}{
				Score:        r.Score,
				MatchedRules: r.MatchedRules,
				Explanation:  r.Explanation,
			},
		}
		recommendationItems = append(recommendationItems, item)
	}

	globalBrand := recommendationItems[0].Brand

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
		Recommendations []recommendationItem
		Copy            struct {
			Headline    string
			Subheadline string
			CTAText     string
		}
	}{
		Brand: globalBrand,
		User: struct {
			Email   string
			Segment string
			TraceID string
		}{
			Email:   ctx.State.UserProfileID,
			Segment: userProfile.Segment,
			TraceID: ctx.TraceID,
		},
		Recommendations: recommendationItems,
		Copy:            copyText,
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
	tmpl := template.New(filepath.Base(templatePath)).Funcs(template.FuncMap{
		"add": func(a, b int) int {
			return a + b
		},
	})
	tmpl, err := tmpl.ParseFiles(templatePath)
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
	lowerModel := strings.ToLower(model)
	if strings.Contains(lowerModel, "bmw") {
		return struct {
			Name           string
			PrimaryColor   string
			SecondaryColor string
			Initial        string
		}{
			Name:           "BMW",
			PrimaryColor:   "#0066b2", // BMW Blue
			SecondaryColor: "#000000",
			Initial:        "B",
		}
	} else if strings.Contains(lowerModel, "adventure") || strings.Contains(lowerModel, "navigator") {
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
	} else if strings.Contains(lowerModel, "samsung") || strings.Contains(lowerModel, "nq70") || strings.Contains(lowerModel, "nv51") || strings.Contains(lowerModel, "rf28") || strings.Contains(lowerModel, "dw80") {
		return struct {
			Name           string
			PrimaryColor   string
			SecondaryColor string
			Initial        string
		}{
			Name:           "Samsung",
			PrimaryColor:   "#1428a0", // Samsung Blue
			SecondaryColor: "#000000",
			Initial:        "S",
		}
	} else if strings.Contains(lowerModel, "lg") || strings.Contains(lowerModel, "wash") || strings.Contains(lowerModel, "dryer") {
		return struct {
			Name           string
			PrimaryColor   string
			SecondaryColor string
			Initial        string
		}{
			Name:           "LG Electronics",
			PrimaryColor:   "#a50034", // LG Red
			SecondaryColor: "#3c3c3c",
			Initial:        "L",
		}
	}

	return struct {
		Name           string
		PrimaryColor   string
		SecondaryColor string
		Initial        string
	}{
		Name:           "Premium Home",
		PrimaryColor:   "#1e3a8a", // Dark Blue
		SecondaryColor: "#0f172a",
		Initial:        "P",
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
			Headline:    "Innovative Living & Performance",
			Subheadline: "Engineered to elevate your daily experience.",
			CTAText:     "Designed with advanced technology and premium materials, these options deliver top-tier efficiency and modern control. Schedule a live demonstration or contact a sales specialist to learn more.",
		}
	case "family", "family safety":
		return struct {
			Headline    string
			Subheadline string
			CTAText     string
		}{
			Headline:    "Reliability & Comfort Redefined",
			Subheadline: "Built around your family's daily needs.",
			CTAText:     "Offering spacious capacities, quiet operation, and certified reliability, this selection ensures every daily routine runs smoothly. Contact us to learn more or request a product demo.",
		}
	default:
		return struct {
			Headline    string
			Subheadline string
			CTAText     string
		}{
			Headline:    "Premium Quality & Design",
			Subheadline: "Sophisticated style meets high-efficiency features.",
			CTAText:     "Experience a selection curated for premium performance, elegant aesthetics, and modern convenience features. Schedule a live demo or contact our sales support for details.",
		}
	}
}
