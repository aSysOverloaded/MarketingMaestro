package workflow

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"
)

// Helper steps for verification

type SimpleStep struct {
	name      string
	executeFn func(ctx *Context) (Result, error)
}

func (s *SimpleStep) Name() string { return s.name }
func (s *SimpleStep) Execute(ctx *Context) (Result, error) {
	if s.executeFn != nil {
		return s.executeFn(ctx)
	}
	return "success", nil
}

type CompensatableStep struct {
	SimpleStep
	compensateFn func(ctx *Context) error
}

func (c *CompensatableStep) Compensate(ctx *Context) error {
	if c.compensateFn != nil {
		return c.compensateFn(ctx)
	}
	return nil
}

func TestWorkflowSuccessSequencing(t *testing.T) {
	var mu sync.Mutex
	executionOrder := []string{}

	step1 := &SimpleStep{
		name: "UserProfileStep",
		executeFn: func(ctx *Context) (Result, error) {
			mu.Lock()
			executionOrder = append(executionOrder, "step1")
			mu.Unlock()
			return UserProfileResult{
				UserProfileID: "profile_123",
				Segment:       "Adventure",
				BudgetTier:    "Premium",
			}, nil
		},
	}

	step2 := &SimpleStep{
		name: "ProductRecommenderStep",
		executeFn: func(ctx *Context) (Result, error) {
			mu.Lock()
			executionOrder = append(executionOrder, "step2")
			mu.Unlock()
			return RecommendationResult{
				RecommendationID: "rec_456",
				ProductID:        "product_789",
				Score:            95,
			}, nil
		},
	}

	wf := Workflow{
		Name: "TestBrochureWorkflow",
		Steps: []Step{step1, step2},
	}

	orch := NewOrchestrator(wf)
	state := &JobContext{}
	ctx := NewContext(context.Background(), "job_1", "trace_1", state)

	err := orch.Execute(ctx)
	if err != nil {
		t.Fatalf("Expected execution to succeed, got error: %v", err)
	}

	if len(executionOrder) != 2 || executionOrder[0] != "step1" || executionOrder[1] != "step2" {
		t.Errorf("Expected steps to execute in sequence, got execution order: %v", executionOrder)
	}

	if state.WorkflowState != "Completed" {
		t.Errorf("Expected WorkflowState to be 'Completed', got: %s", state.WorkflowState)
	}

	if state.UserProfileID != "profile_123" {
		t.Errorf("Expected UserProfileID to be populated, got: %s", state.UserProfileID)
	}

	if state.RecommendationID != "rec_456" {
		t.Errorf("Expected RecommendationID to be populated, got: %s", state.RecommendationID)
	}
}

func TestWorkflowFailureAndCompensations(t *testing.T) {
	var mu sync.Mutex
	step1Compensated := false
	step2Executed := false

	step1 := &CompensatableStep{
		SimpleStep: SimpleStep{
			name: "CompensatableStep1",
			executeFn: func(ctx *Context) (Result, error) {
				return "data_1", nil
			},
		},
		compensateFn: func(ctx *Context) error {
			mu.Lock()
			step1Compensated = true
			mu.Unlock()
			return nil
		},
	}

	step2 := &SimpleStep{
		name: "FailingStep2",
		executeFn: func(ctx *Context) (Result, error) {
			mu.Lock()
			step2Executed = true
			mu.Unlock()
			return nil, errors.New("something went wrong")
		},
	}

	wf := Workflow{
		Name:  "TestCompWorkflow",
		Steps: []Step{step1, step2},
	}

	orch := NewOrchestrator(wf)
	state := &JobContext{}
	ctx := NewContext(context.Background(), "job_2", "trace_2", state)

	err := orch.Execute(ctx)
	if err == nil {
		t.Fatal("Expected workflow execution to fail, got nil")
	}

	if !step2Executed {
		t.Error("Expected Step 2 to execute and fail")
	}

	if !step1Compensated {
		t.Error("Expected Step 1 to be compensated after Step 2 failure")
	}

	if state.WorkflowState != "Failed" {
		t.Errorf("Expected WorkflowState to be 'Failed', got: %s", state.WorkflowState)
	}
}

func TestWorkflowIdempotency(t *testing.T) {
	step1ExecutionCount := 0
	step2ExecutionCount := 0

	step1 := &SimpleStep{
		name: "UserProfileStep",
		executeFn: func(ctx *Context) (Result, error) {
			step1ExecutionCount++
			return UserProfileResult{
				UserProfileID: "profile_123",
				Segment:       "Adventure",
			}, nil
		},
	}

	step2 := &SimpleStep{
		name: "ProductRecommenderStep",
		executeFn: func(ctx *Context) (Result, error) {
			step2ExecutionCount++
			return RecommendationResult{
				RecommendationID: "rec_456",
				ProductID:        "product_789",
			}, nil
		},
	}

	wf := Workflow{
		Name:  "TestIdempotencyWorkflow",
		Steps: []Step{step1, step2},
	}

	orch := NewOrchestrator(wf)

	// Pre-populate state to indicate step 1 has already completed
	state := &JobContext{
		UserProfileID: "profile_123",
	}
	ctx := NewContext(context.Background(), "job_3", "trace_3", state)

	err := orch.Execute(ctx)
	if err != nil {
		t.Fatalf("Expected execution to succeed, got error: %v", err)
	}

	if step1ExecutionCount != 0 {
		t.Errorf("Expected step1 to be skipped due to idempotency, but it was executed %d times", step1ExecutionCount)
	}

	if step2ExecutionCount != 1 {
		t.Errorf("Expected step2 to run exactly once, but it was executed %d times", step2ExecutionCount)
	}
}

func TestWorkflowRetryAndExponentialBackoff(t *testing.T) {
	executionAttempts := 0

	step1 := &SimpleStep{
		name: "FailingStepWithRetries",
		executeFn: func(ctx *Context) (Result, error) {
			executionAttempts++
			return nil, errors.New("transient issue")
		},
	}

	wf := Workflow{
		Name:  "TestRetryWorkflow",
		Steps: []Step{step1},
		RetryPolicies: map[string]RetryPolicy{
			"FailingStepWithRetries": {
				MaxRetries:      2,
				BackoffStrategy: "exponential",
				InitialInterval: 5 * time.Millisecond,
			},
		},
	}

	orch := NewOrchestrator(wf)
	state := &JobContext{}
	ctx := NewContext(context.Background(), "job_4", "trace_4", state)

	err := orch.Execute(ctx)
	if err == nil {
		t.Fatal("Expected workflow execution to fail after retries exhausted")
	}

	// 1 initial try + 2 retries = 3 attempts total
	if executionAttempts != 3 {
		t.Errorf("Expected 3 attempts total (1 execution + 2 retries), got: %d", executionAttempts)
	}
}
