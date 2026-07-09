package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"marketing-agent/internal/recommendation"
	"marketing-agent/internal/steps"
	"marketing-agent/internal/workflow"
)

func main() {
	// Resolve base paths
	baseDir, err := os.Getwd()
	if err != nil {
		log.Fatalf("Failed to resolve current working directory: %v", err)
	}

	templatesDir := filepath.Join(baseDir, "templates")
	tempHtmlDir := filepath.Join(baseDir, "storage", "temp_brochures")
	tempPdfDir := filepath.Join(baseDir, "storage", "generated_brochures")
	tempEmailDir := filepath.Join(baseDir, "storage", "sent_emails")

	// Ensure storage directories exist
	os.MkdirAll(tempHtmlDir, 0755)
	os.MkdirAll(tempPdfDir, 0755)
	os.MkdirAll(tempEmailDir, 0755)

	// API Endpoint for processing uploads and executing recommendations
	http.HandleFunc("/api/recommend", func(w http.ResponseWriter, r *http.Request) {
		// Enable CORS
		w.Header().Set("Access-Control-Allow-Origin", "*")
		w.Header().Set("Access-Control-Allow-Methods", "POST, OPTIONS")
		w.Header().Set("Access-Control-Allow-Headers", "Content-Type")

		if r.Method == "OPTIONS" {
			w.WriteHeader(http.StatusOK)
			return
		}

		if r.Method != "POST" {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}

		log.Printf("[1/3] Received recommendation request. Parsing form data...")

		// 1. Parse Multipart Form (15MB Limit)
		err := r.ParseMultipartForm(15 << 20)
		if err != nil {
			http.Error(w, fmt.Sprintf("Failed to parse form: %v", err), http.StatusBadRequest)
			return
		}

		// 2. Extract profile attributes
		age, _ := strconv.Atoi(r.FormValue("age"))
		income, _ := strconv.ParseFloat(r.FormValue("income"), 64)
		familySize, _ := strconv.Atoi(r.FormValue("family_size"))
		location := r.FormValue("location")
		hobbiesRaw := r.FormValue("hobbies")

		hobbies := []string{}
		for _, h := range strings.Split(hobbiesRaw, ",") {
			trimmed := strings.TrimSpace(h)
			if trimmed != "" {
				hobbies = append(hobbies, trimmed)
			}
		}

		// Set defaults if empty
		if age == 0 {
			age = 32
		}
		if income == 0 {
			income = 120000.0
		}
		if familySize == 0 {
			familySize = 4
		}
		if location == "" {
			location = "Seattle, WA"
		}
		if len(hobbies) == 0 {
			hobbies = []string{"trekking", "camping"}
		}

		// Create dynamic profile input map
		profileInput := map[string]interface{}{
			"age":         age,
			"income":      income,
			"family_size": familySize,
			"hobbies":     hobbies,
			"location":    location,
		}

		// Create workflow tracking identifiers
		jobID := fmt.Sprintf("job_web_%d", time.Now().UnixNano())
		traceID := fmt.Sprintf("trace_web_%d", time.Now().UnixNano())
		state := &workflow.JobContext{
			JobID:       jobID,
			TraceID:     traceID,
			StepOutputs: make(map[string]interface{}),
		}

		// Pre-populate input demographics into the context map
		state.StepOutputs["UserProfileInput"] = profileInput

		// 3. Extract and Process the PDF file if uploaded
		file, _, err := r.FormFile("brochure")
		if err == nil {
			defer file.Close()

			// Read file content
			pdfBytes, readErr := io.ReadAll(file)
			if readErr != nil {
				http.Error(w, fmt.Sprintf("Failed to read PDF brochure file: %v", readErr), http.StatusInternalServerError)
				return
			}

			// Call multimodal parser in Context
			log.Printf("[2/3] Uploading PDF catalog to Gemini for specs extraction (this can take 10-30 seconds depending on size)...")
			ctx := workflow.NewContext(context.Background(), jobID, traceID, state)
			customCatalog, err := workflow.ParsePDFBrochure(ctx, pdfBytes)
			if err != nil {
				log.Printf("Gemini PDF parsing warning: %v. Proceeding without custom catalog.", err)
			} else {
				log.Printf("Successfully parsed %d dynamic product(s) from PDF catalog.", len(customCatalog))
				// Inject the catalog array in StepOutputs
				state.StepOutputs["UploadedCatalog"] = customCatalog
			}
		}

		// 4. Instantiate Steps & Declare Workflow
		stepProfile := steps.NewUserProfileStep()
		stepRecommend := steps.NewProductRecommenderStep(recommendation.NewVehicleMatcher())
		stepCompile := steps.NewCompileHTMLStep(templatesDir, tempHtmlDir)
		stepRender := steps.NewPDFRenderStep(tempPdfDir)
		stepEmail := steps.NewEmailDispatchStep(tempEmailDir)

		mvpWorkflow := workflow.Workflow{
			Name: "PersonalizedBrochureMVP",
			Steps: []workflow.Step{
				stepProfile,
				stepRecommend,
				stepCompile,
				stepRender,
				stepEmail,
			},
			RetryPolicies: map[string]workflow.RetryPolicy{
				"UserProfileStep": {
					MaxRetries:      3,
					BackoffStrategy: "exponential",
					InitialInterval: 2 * time.Second,
				},
				"ProductRecommenderStep": {
					MaxRetries:      3,
					BackoffStrategy: "exponential",
					InitialInterval: 2 * time.Second,
				},
			},
		}

		// 5. Execute Pipeline
		log.Printf("[3/3] Running demographic profiling and recommendation matching workflow...")
		orchestrator := workflow.NewOrchestrator(mvpWorkflow)
		ctx := workflow.NewContext(context.Background(), jobID, traceID, state)
		err = orchestrator.Execute(ctx)
		if err != nil {
			http.Error(w, fmt.Sprintf("Workflow execution failed: %v", err), http.StatusInternalServerError)
			return
		}

		// 6. Return response results in JSON
		recOutputsRaw, ok := state.StepOutputs["ProductRecommenderStep"]
		if !ok {
			http.Error(w, "Missing recommendation outcome", http.StatusInternalServerError)
			return
		}

		// Resolve recommendation slice
		var recs []workflow.RecommendationResult
		if slice, ok := recOutputsRaw.([]workflow.RecommendationResult); ok {
			recs = slice
		} else if single, ok := recOutputsRaw.(workflow.RecommendationResult); ok {
			recs = []workflow.RecommendationResult{single}
		}

		// Formulate the PDF url path
		pdfUrl := fmt.Sprintf("/storage/generated_brochures/brochure_job_%s.pdf", jobID)

		responsePayload := map[string]interface{}{
			"success":         true,
			"job_id":          jobID,
			"trace_id":        traceID,
			"workflow_state":  state.WorkflowState,
			"recommendations": recs,
			"pdf_url":         pdfUrl,
		}

		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(responsePayload)
	})

	// Serve generated files from the storage path
	http.Handle("/storage/", http.StripPrefix("/storage/", http.FileServer(http.Dir("./storage"))))

	// Serve the static SPA frontend
	http.Handle("/", http.FileServer(http.Dir("./static")))

	port := ":8080"
	log.Printf("Starting web server at http://localhost%s...", port)
	if err := http.ListenAndServe(port, nil); err != nil {
		log.Fatalf("Server startup failed: %v", err)
	}
}
