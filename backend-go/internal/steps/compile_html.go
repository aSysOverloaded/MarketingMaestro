package steps

import (
	"encoding/base64"
	"fmt"
	"html/template"
	"log"
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
	Product struct {
		Model      string
		BasePrice  string
		HeroImage  template.URL // template.URL, not string: marks base64 data URIs as pre-vetted
		// safe so html/template's URL-context auto-escaper doesn't replace them with "#ZgotmplZ"
		Seats      int
		Horsepower int
		Capacity   string
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
	storageDir := filepath.Dir(s.outputDir)
	for _, r := range recs {
		var prod templateProductSpec
		found := false
		if specsRaw, ok := ctx.State.StepOutputs["ProductRecommenderStep_Specs"]; ok {
			if specsSlice, ok := specsRaw.([]recommendation.Product); ok {
				for _, spec := range specsSlice {
					if spec.ID == r.ProductID {
						seats := 5
						if sVal, ok := spec.Specs["seats"]; ok {
							if sFloat, ok := sVal.(float64); ok {
								seats = int(sFloat)
							} else if sInt, ok := sVal.(int); ok {
								seats = sInt
							}
						}
						
						descFeatures := spec.Features
						if pCat, ok := spec.Specs["type"].(string); ok {
							descFeatures = append([]string{fmt.Sprintf("Category: %s", pCat)}, descFeatures...)
						}
						if capVal, ok := spec.Specs["capacity"].(string); ok {
							descFeatures = append([]string{fmt.Sprintf("Capacity: %s", capVal)}, descFeatures...)
						}

						capacityStr := ""
						if capVal, ok := spec.Specs["capacity"].(string); ok {
							capacityStr = capVal
						}

						prod = templateProductSpec{
							Model:      spec.Model,
							BasePrice:  fmt.Sprintf("%.2f", spec.BasePrice),
							HeroImage:  spec.HeroImage,
							Seats:      seats,
							Horsepower: 0,
							Capacity:   capacityStr,
							Features:   descFeatures,
						}
						
						if prod.HeroImage == "" {
							prodType := ""
							if tVal, ok := spec.Specs["type"].(string); ok {
								prodType = strings.ToLower(tVal)
							}
							modelLower := strings.ToLower(spec.Model)

							if strings.Contains(prodType, "refrigerator") || strings.Contains(prodType, "fridge") || strings.Contains(modelLower, "refrigerator") || strings.Contains(modelLower, "fridge") {
								prod.HeroImage = "https://images.unsplash.com/photo-1588854337236-6889d631faa8?auto=format&fit=crop&q=80&w=800" // Real Refrigerator
							} else if strings.Contains(prodType, "dishwasher") || strings.Contains(modelLower, "dishwasher") {
								prod.HeroImage = "https://images.unsplash.com/photo-1581578731548-c64695cc6952?auto=format&fit=crop&q=80&w=800" // Real Dishwasher
							} else if strings.Contains(prodType, "washer") || strings.Contains(prodType, "washing") || strings.Contains(prodType, "dryer") || strings.Contains(modelLower, "washer") || strings.Contains(modelLower, "washing") || strings.Contains(modelLower, "dryer") {
								prod.HeroImage = "https://images.unsplash.com/photo-1626806787461-102c1bfaaea1?auto=format&fit=crop&q=80&w=800" // Real Washer/dryer
							} else if strings.Contains(prodType, "range") || strings.Contains(prodType, "oven") || strings.Contains(prodType, "stove") || strings.Contains(prodType, "cooktop") || strings.Contains(prodType, "microwave") || strings.Contains(modelLower, "range") || strings.Contains(modelLower, "oven") || strings.Contains(modelLower, "stove") || strings.Contains(modelLower, "cooktop") || strings.Contains(modelLower, "microwave") {
								prod.HeroImage = "https://images.unsplash.com/photo-1590794056226-79ef3a8147e1?auto=format&fit=crop&q=80&w=800" // Real Oven/Stove
							} else if strings.Contains(prodType, "tesla") || strings.Contains(prodType, "car") || strings.Contains(prodType, "suv") || strings.Contains(modelLower, "tesla") || strings.Contains(modelLower, "car") || strings.Contains(modelLower, "suv") {
								prod.HeroImage = "https://images.unsplash.com/photo-1617788138017-80ad40651399?auto=format&fit=crop&q=80&w=800" // Tesla/car
							} else {
								prod.HeroImage = "https://images.unsplash.com/photo-1556910103-1c02745aae4d?auto=format&fit=crop&q=80&w=800" // Default: Modern kitchen
							}
						}

						if hpVal, ok := spec.Specs["horsepower"]; ok {
							if hpFloat, ok := hpVal.(float64); ok {
								prod.Horsepower = int(hpFloat)
							} else if hpInt, ok := hpVal.(int); ok {
								prod.Horsepower = hpInt
							}
						}

						found = true
						break
					}
				}
			}
		}

		if !found {
			dbProd := s.getProductSpecByID(r.ProductID)
			prod = templateProductSpec{
				Model:      dbProd.Model,
				BasePrice:  dbProd.BasePrice,
				HeroImage:  dbProd.HeroImage,
				Seats:      dbProd.Seats,
				Horsepower: dbProd.Horsepower,
				Capacity:   dbProd.Capacity,
				Features:   dbProd.Features,
			}
		}

		brnd := s.getBrandConfigByModel(prod.Model)

		// chromedp renders this HTML via a file:// URL (see PDFRenderStep), where an absolute
		// path like "/storage/extracted_images/x.jpg" resolves against the filesystem root, not
		// the Go webserver - so locally-extracted RAG images 404 silently inside the rendered
		// PDF even though they load fine in the live web UI. Embed them as base64 data URIs
		// instead, which work identically regardless of file:// vs http:// context.
		embeddedHeroImage := embedLocalImage(ctx, prod.HeroImage, storageDir)

		item := recommendationItem{
			Brand: brnd,
			Product: struct {
				Model      string
				BasePrice  string
				HeroImage  template.URL
				Seats      int
				Horsepower int
				Capacity   string
				Features   []string
			}{
				Model:      prod.Model,
				BasePrice:  prod.BasePrice,
				HeroImage:  template.URL(embeddedHeroImage),
				Seats:      prod.Seats,
				Horsepower: prod.Horsepower,
				Capacity:   prod.Capacity,
				Features:   prod.Features,
			},
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

// embedLocalImage converts a locally-served "/storage/..." image path into a base64 data URI so
// it renders correctly when the compiled HTML is opened via file:// (as chromedp does for PDF
// rendering), where absolute paths can't resolve back to the Go webserver. External URLs (e.g.
// the Unsplash stock photo fallbacks) are left untouched since those work fine either way.
func embedLocalImage(ctx *workflow.Context, heroImage, storageDir string) string {
	if !strings.HasPrefix(heroImage, "/storage/") {
		return heroImage
	}

	relPath := strings.TrimPrefix(heroImage, "/storage/")
	fullPath := filepath.Join(storageDir, relPath)

	data, err := os.ReadFile(fullPath)
	if err != nil {
		log.Printf("[TraceID: %s] [JobID: %s] [HTMLCompileStep] [WARNING] Failed to read local hero image %s for embedding: %v. Image will be broken in the rendered PDF.", ctx.TraceID, ctx.JobID, fullPath, err)
		return heroImage
	}

	var mimeType string
	switch strings.ToLower(filepath.Ext(fullPath)) {
	case ".jpg", ".jpeg":
		mimeType = "image/jpeg"
	case ".png":
		mimeType = "image/png"
	case ".gif":
		mimeType = "image/gif"
	case ".webp":
		mimeType = "image/webp"
	default:
		// Unsupported format (e.g. .jp2 - browsers can't render JPEG 2000 either way).
		// Leave it as-is rather than embed bytes the browser won't display anyway.
		log.Printf("[TraceID: %s] [JobID: %s] [HTMLCompileStep] [WARNING] Unsupported image format for %s, leaving as local path (will be broken in PDF).", ctx.TraceID, ctx.JobID, fullPath)
		return heroImage
	}

	return fmt.Sprintf("data:%s;base64,%s", mimeType, base64.StdEncoding.EncodeToString(data))
}

// Struct representing template-friendly product spec
type templateProductSpec struct {
	Model      string
	BasePrice  string
	HeroImage  string
	Seats      int
	Horsepower int
	Capacity   string
	Features   []string
}

// getProductSpecByID mimics querying spec database tables
func (s *CompileHTMLStep) getProductSpecByID(id string) templateProductSpec {
	switch id {
	case "appliance_fridge_samsung":
		return templateProductSpec{
			Model:     "Samsung Family Hub Refrigerator",
			BasePrice: "2,499.00",
			HeroImage: "https://images.unsplash.com/photo-1588854337236-6889d631faa8?auto=format&fit=crop&q=80&w=800",
			Seats:     0,
			Capacity:  "26.5 cu. ft.",
			Features:  []string{"Wi-Fi Connected Screen", "Triple Cooling System", "Internal Cameras", "Water & Ice Dispenser"},
		}
	case "appliance_washer_lg":
		return templateProductSpec{
			Model:     "LG TurboWash Washing Machine",
			BasePrice: "899.00",
			HeroImage: "https://images.unsplash.com/photo-1626806787461-102c1bfaaea1?auto=format&fit=crop&q=80&w=800",
			Seats:     0,
			Capacity:  "5.0 cu. ft.",
			Features:  []string{"AI DD Smart Fabric Care", "TurboWash 360", "Steam Technology", "ThinQ Wi-Fi Control"},
		}
	default:
		return templateProductSpec{
			Model:     "Bosch 800 Series Dishwasher",
			BasePrice: "1,299.00",
			HeroImage: "https://images.unsplash.com/photo-1581578731548-c64695cc6952?auto=format&fit=crop&q=80&w=800",
			Seats:     0,
			Capacity:  "16 Place Settings",
			Features:  []string{"CrystalDry Technology", "Whisper Quiet 42 dBA", "Flexible 3rd Rack", "Home Connect Smart Control"},
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
	// Check if dynamic AI copy writer step output is present
	if writerRaw, ok := ctx.State.StepOutputs["WriterStep"]; ok {
		if writerRes, ok := writerRaw.(WriterResult); ok {
			return struct {
				Headline    string
				Subheadline string
				CTAText     string
			}{
				Headline:    writerRes.Headline,
				Subheadline: writerRes.Subheadline,
				CTAText:     writerRes.CTA,
			}
		}
	}

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
