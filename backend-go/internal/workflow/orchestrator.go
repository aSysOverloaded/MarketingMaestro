package workflow

import (
	"fmt"
	"log"
	"math"
	"time"
)

// RetryPolicy defines the execution retry configurations per step
type RetryPolicy struct {
	MaxRetries      int
	BackoffStrategy string // "linear", "exponential", or "none"
	InitialInterval time.Duration
}

// Workflow defines a declarative sequence of steps
type Workflow struct {
	Name          string
	Steps         []Step
	RetryPolicies map[string]RetryPolicy
}

// Orchestrator coordinates the execution of workflows, tracking state and metrics
type Orchestrator struct {
	workflow      Workflow
	metricsLogger func(jobID string, stepName string, latency time.Duration, retries int, err error)
}

// NewOrchestrator creates a new orchestrator instance
func NewOrchestrator(wf Workflow) *Orchestrator {
	return &Orchestrator{
		workflow: wf,
	}
}

// SetMetricsLogger sets an optional telemetry callback
func (o *Orchestrator) SetMetricsLogger(logger func(jobID string, stepName string, latency time.Duration, retries int, err error)) {
	o.metricsLogger = logger
}

// Execute runs the declarative workflow steps sequentially with idempotency, retries, and compensation loops
func (o *Orchestrator) Execute(ctx *Context) error {
	ctx.State.WorkflowState = "Running"
	log.Printf("[TraceID: %s] [JobID: %s] Starting workflow execution: %s", ctx.TraceID, ctx.JobID, o.workflow.Name)

	var executedSteps []Step

	for _, step := range o.workflow.Steps {
		stepName := step.Name()

		// 1. Idempotency Check
		if o.isStepAlreadyCompleted(ctx, stepName) {
			log.Printf("[TraceID: %s] [JobID: %s] [Step: %s] Already completed. Skipping execution.", ctx.TraceID, ctx.JobID, stepName)
			executedSteps = append(executedSteps, step)
			continue
		}

		// 2. Fetch Retry Policy
		policy, exists := o.workflow.RetryPolicies[stepName]
		if !exists {
			policy = RetryPolicy{MaxRetries: 0, BackoffStrategy: "none"}
		}

		var lastErr error
		var result Result
		attempts := 0
		startTime := time.Now()

		// 3. Execution Loop with Retries
		for attempts <= policy.MaxRetries {
			log.Printf("[TraceID: %s] [JobID: %s] [Step: %s] Executing step (Attempt %d/%d)...", ctx.TraceID, ctx.JobID, stepName, attempts+1, policy.MaxRetries+1)
			result, lastErr = step.Execute(ctx)
			if lastErr == nil {
				break
			}

			attempts++
			if attempts <= policy.MaxRetries {
				sleepDuration := o.calculateBackoff(policy, attempts)
				log.Printf("[TraceID: %s] [JobID: %s] [Step: %s] Failed: %v. Retrying in %v...", ctx.TraceID, ctx.JobID, stepName, lastErr, sleepDuration)
				
				select {
				case <-ctx.Done():
					return ctx.Err()
				case <-time.After(sleepDuration):
				}
			}
		}

		latency := time.Since(startTime)

		// Log metrics
		if o.metricsLogger != nil {
			o.metricsLogger(ctx.JobID, stepName, latency, attempts, lastErr)
		}

		// 4. Handle Step Execution Failures
		if lastErr != nil {
			ctx.State.WorkflowState = "Failed"
			log.Printf("[TraceID: %s] [JobID: %s] [Step: %s] Terminated with fatal error: %v. Initiating compensation.", ctx.TraceID, ctx.JobID, stepName, lastErr)
			o.runCompensations(ctx, executedSteps)
			return fmt.Errorf("step %s failed: %w", stepName, lastErr)
		}

		// 5. Update State Context
		o.updateJobStateContext(ctx, stepName, result)
		executedSteps = append(executedSteps, step)
		log.Printf("[TraceID: %s] [JobID: %s] [Step: %s] Success (Duration: %v)", ctx.TraceID, ctx.JobID, stepName, latency)
	}

	ctx.State.WorkflowState = "Completed"
	log.Printf("[TraceID: %s] [JobID: %s] Workflow execution completed successfully.", ctx.TraceID, ctx.JobID)
	return nil
}

// isStepAlreadyCompleted checks if the step outcome is already present in the JobContext
func (o *Orchestrator) isStepAlreadyCompleted(ctx *Context, stepName string) bool {
	if ctx.State.StepOutputs == nil {
		ctx.State.StepOutputs = make(map[string]interface{})
	}

	// First check dynamic map
	if _, ok := ctx.State.StepOutputs[stepName]; ok {
		return true
	}

	// Then check explicit typed fields
	switch stepName {
	case "UserProfileStep":
		return ctx.State.UserProfileID != ""
	case "ProductRecommenderStep":
		return ctx.State.RecommendationID != ""
	case "ContentPlannerStep":
		return ctx.State.ContentPlanID != ""
	case "CopywriterStep":
		return ctx.State.GeneratedCopyID != ""
	case "PDFRenderStep":
		return ctx.State.PDFObjectKey != ""
	case "EmailDispatchStep":
		return ctx.State.EmailReceiptID != ""
	}

	return false
}

// updateJobStateContext applies a step execution outcome to the Context
func (o *Orchestrator) updateJobStateContext(ctx *Context, stepName string, res Result) {
	if ctx.State.StepOutputs == nil {
		ctx.State.StepOutputs = make(map[string]interface{})
	}

	ctx.State.StepOutputs[stepName] = res

	switch r := res.(type) {
	case UserProfileResult:
		ctx.State.UserProfileID = r.UserProfileID
	case RecommendationResult:
		ctx.State.RecommendationID = r.RecommendationID
	case ContentPlanResult:
		ctx.State.ContentPlanID = r.ContentPlanID
	case GeneratedCopyResult:
		ctx.State.GeneratedCopyID = r.GeneratedCopyID
	case PDFRenderResult:
		ctx.State.PDFObjectKey = r.PDFObjectKey
		ctx.State.PDFBucket = r.PDFBucket
	case EmailDispatchResult:
		ctx.State.EmailReceiptID = r.EmailReceiptID
	}
}

// runCompensations rolls back steps in reverse execution order
func (o *Orchestrator) runCompensations(ctx *Context, executedSteps []Step) {
	log.Printf("[TraceID: %s] [JobID: %s] Running workflow compensations...", ctx.TraceID, ctx.JobID)
	for i := len(executedSteps) - 1; i >= 0; i-- {
		step := executedSteps[i]
		if comp, ok := step.(Compensatable); ok {
			log.Printf("[TraceID: %s] [JobID: %s] [Compensate: %s] Reversing step mutations...", ctx.TraceID, ctx.JobID, step.Name())
			if err := comp.Compensate(ctx); err != nil {
				// We log compensation errors but do not stop rolling back remaining steps
				log.Printf("[TraceID: %s] [JobID: %s] [WARNING] [Compensate: %s] Failed to compensate: %v", ctx.TraceID, ctx.JobID, step.Name(), err)
			} else {
				log.Printf("[TraceID: %s] [JobID: %s] [Compensate: %s] Compensation successful.", ctx.TraceID, ctx.JobID, step.Name())
			}
		}
	}
}

// calculateBackoff returns duration to sleep based on exponential or linear strategy
func (o *Orchestrator) calculateBackoff(policy RetryPolicy, attempt int) time.Duration {
	if policy.InitialInterval <= 0 {
		policy.InitialInterval = 100 * time.Millisecond
	}

	switch policy.BackoffStrategy {
	case "linear":
		return policy.InitialInterval * time.Duration(attempt)
	case "exponential":
		return policy.InitialInterval * time.Duration(math.Pow(2, float64(attempt-1)))
	default:
		return 0
	}
}
