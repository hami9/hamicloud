package reconciler

import (
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes/fake"

	"github.com/hami9/hamicloud/runtime/internal/store"
)

func TestSanitizeResourceName_Variants(t *testing.T) {
	assert.Equal(t, "my-app-name", sanitizeResourceName("my_app_name"))
	assert.Equal(t, "uppercase-name", sanitizeResourceName("UPPERCASE-NAME"))
	assert.Equal(t, "valid-123", sanitizeResourceName("---valid-123---"))
	assert.Equal(t, "workload", sanitizeResourceName("---"))
}

func TestKubeWorkloadRunner_DeployAndReadiness(t *testing.T) {
	ctx := context.Background()
	fakeClient := fake.NewSimpleClientset()
	ns := "test-ns"

	runner := NewKubeWorkloadRunner(fakeClient, ns, "")

	workload := &store.ClaimedWorkload{
		WorkspaceID:               "ws-123",
		WorkspaceSlug:             "my-team",
		ApplicationID:             "app-456",
		ApplicationSlug:           "web-frontend",
		ReleaseID:                 "rel-789",
		ReleaseNumber:             1,
		ImageDigest:               "registry.example.com/team/web:v1.0.0",
		Port:                      8080,
		HealthPath:                "/healthz",
		TargetGeneration:          1,
		DeterministicResourceName: "hc-svc-web-frontend-1",
	}

	// 1. Deploy
	uid, err := runner.Deploy(ctx, workload)
	require.NoError(t, err)
	assert.NotEmpty(t, uid)

	// Verify Deployment
	deploy, err := fakeClient.AppsV1().Deployments(ns).Get(ctx, "hc-svc-web-frontend-1", metav1.GetOptions{})
	require.NoError(t, err)
	assert.Equal(t, "hc-svc-web-frontend-1", deploy.Name)
	assert.Equal(t, "registry.example.com/team/web:v1.0.0", deploy.Spec.Template.Spec.Containers[0].Image)
	assert.Equal(t, int32(8080), deploy.Spec.Template.Spec.Containers[0].Ports[0].ContainerPort)
	assert.NotNil(t, deploy.Spec.Template.Spec.Containers[0].ReadinessProbe)
	assert.Equal(t, "/healthz", deploy.Spec.Template.Spec.Containers[0].ReadinessProbe.HTTPGet.Path)

	// Verify Service
	svc, err := fakeClient.CoreV1().Services(ns).Get(ctx, "hc-svc-web-frontend-1", metav1.GetOptions{})
	require.NoError(t, err)
	assert.Equal(t, "hc-svc-web-frontend-1", svc.Name)
	assert.Equal(t, int32(8080), svc.Spec.Ports[0].Port)

	// 2. Check Readiness initially (not ready)
	ready, reason, err := runner.CheckReadiness(ctx, workload)
	require.NoError(t, err)
	assert.False(t, ready)
	assert.Contains(t, reason, "waiting for ready replicas")

	// 3. Update Deployment to Ready
	deploy.Status.ReadyReplicas = 1
	_, err = fakeClient.AppsV1().Deployments(ns).Update(ctx, deploy, metav1.UpdateOptions{})
	require.NoError(t, err)

	ready, reason, err = runner.CheckReadiness(ctx, workload)
	require.NoError(t, err)
	assert.True(t, ready)
	assert.Empty(t, reason)

	// 4. Teardown
	err = runner.Teardown(ctx, workload)
	require.NoError(t, err)

	_, err = fakeClient.AppsV1().Deployments(ns).Get(ctx, "hc-svc-web-frontend-1", metav1.GetOptions{})
	assert.Error(t, err)
	_, err = fakeClient.CoreV1().Services(ns).Get(ctx, "hc-svc-web-frontend-1", metav1.GetOptions{})
	assert.Error(t, err)
}

func TestKubeWorkloadRunner_WithIngress(t *testing.T) {
	ctx := context.Background()
	fakeClient := fake.NewSimpleClientset()
	ns := "test-ns"

	runner := NewKubeWorkloadRunner(fakeClient, ns, "hamicloud.local")

	workload := &store.ClaimedWorkload{
		WorkspaceID:               "ws-123",
		WorkspaceSlug:             "prod-team",
		ApplicationID:             "app-456",
		ApplicationSlug:           "api-gateway",
		ReleaseID:                 "rel-789",
		ImageDigest:               "registry.example.com/api:v1",
		Port:                      3000,
		TargetGeneration:          1,
		DeterministicResourceName: "hc-svc-api-gateway-1",
	}

	_, err := runner.Deploy(ctx, workload)
	require.NoError(t, err)

	ing, err := fakeClient.NetworkingV1().Ingresses(ns).Get(ctx, "hc-svc-api-gateway-1", metav1.GetOptions{})
	require.NoError(t, err)
	assert.Equal(t, "api-gateway.prod-team.hamicloud.local", ing.Spec.Rules[0].Host)
	assert.Equal(t, int32(3000), ing.Spec.Rules[0].HTTP.Paths[0].Backend.Service.Port.Number)

	err = runner.Teardown(ctx, workload)
	require.NoError(t, err)

	_, err = fakeClient.NetworkingV1().Ingresses(ns).Get(ctx, "hc-svc-api-gateway-1", metav1.GetOptions{})
	assert.Error(t, err)
}

func TestKubeJobTaskRunner_Success(t *testing.T) {
	ctx := context.Background()
	fakeClient := fake.NewSimpleClientset()
	ns := "test-ns"
	artifactsDir := t.TempDir()

	runner, err := NewKubeJobTaskRunner(fakeClient, ns, artifactsDir)
	require.NoError(t, err)
	runner.pollInterval = 10 * time.Millisecond

	workload := &store.ClaimedJobWorkload{
		JobID:                     "job-999",
		WorkspaceID:               "ws-111",
		JobName:                   "data-sync",
		ImageDigest:               "registry.example.com/data:v1",
		CommandArgs:               []string{"python", "sync.py"},
		EnvVars:                   map[string]string{"ENV_A": "VAL_A"},
		AttemptNumber:             1,
		DeterministicResourceName: "hc-job-job-999-1",
	}

	// In background, simulate Kubernetes marking the job succeeded
	go func() {
		time.Sleep(50 * time.Millisecond)
		job, getErr := fakeClient.BatchV1().Jobs(ns).Get(ctx, "hc-job-job-999-1", metav1.GetOptions{})
		if getErr == nil {
			job.Status.Succeeded = 1
			_, _ = fakeClient.BatchV1().Jobs(ns).Update(ctx, job, metav1.UpdateOptions{})
		}
	}()

	exitCode, reason, err := runner.RunJob(ctx, workload)
	require.NoError(t, err)
	assert.Equal(t, 0, exitCode)
	assert.Empty(t, reason)

	// Verify artifact directory was created
	expectedArtifact := filepath.Join(artifactsDir, "ws-111", "job-999", "output.txt")
	_, statErr := os.Stat(expectedArtifact)
	assert.NoError(t, statErr)
}

func TestKubeJobTaskRunner_Failure(t *testing.T) {
	ctx := context.Background()
	fakeClient := fake.NewSimpleClientset()
	ns := "test-ns"
	artifactsDir := t.TempDir()

	runner, err := NewKubeJobTaskRunner(fakeClient, ns, artifactsDir)
	require.NoError(t, err)
	runner.pollInterval = 10 * time.Millisecond

	workload := &store.ClaimedJobWorkload{
		JobID:                     "job-fail-1",
		WorkspaceID:               "ws-111",
		JobName:                   "failing-task",
		ImageDigest:               "registry.example.com/fail:v1",
		CommandArgs:               []string{"sh", "-c", "exit 42"},
		AttemptNumber:             1,
		DeterministicResourceName: "hc-job-job-fail-1",
	}

	go func() {
		time.Sleep(50 * time.Millisecond)
		// Create pod with termination exit code 42
		pod := &corev1.Pod{
			ObjectMeta: metav1.ObjectMeta{
				Name:      "hc-job-job-fail-1-pod-x",
				Namespace: ns,
				Labels: map[string]string{
					"app.kubernetes.io/instance": "hc-job-job-fail-1",
				},
			},
			Status: corev1.PodStatus{
				ContainerStatuses: []corev1.ContainerStatus{{
					State: corev1.ContainerState{
						Terminated: &corev1.ContainerStateTerminated{
							ExitCode: 42,
							Reason:   "Error",
						},
					},
				}},
			},
		}
		_, _ = fakeClient.CoreV1().Pods(ns).Create(ctx, pod, metav1.CreateOptions{})

		job, getErr := fakeClient.BatchV1().Jobs(ns).Get(ctx, "hc-job-job-fail-1", metav1.GetOptions{})
		if getErr == nil {
			job.Status.Failed = 1
			job.Status.Conditions = []batchv1.JobCondition{{
				Type:    batchv1.JobFailed,
				Status:  corev1.ConditionTrue,
				Reason:  "BackoffLimitExceeded",
				Message: "Job has reached the specified backoff limit",
			}}
			_, _ = fakeClient.BatchV1().Jobs(ns).Update(ctx, job, metav1.UpdateOptions{})
		}
	}()

	exitCode, reason, err := runner.RunJob(ctx, workload)
	require.NoError(t, err)
	assert.Equal(t, 42, exitCode)
	assert.Contains(t, reason, "42")
}

func TestKubeJobTaskRunner_Timeout(t *testing.T) {
	ctx := context.Background()
	fakeClient := fake.NewSimpleClientset()
	ns := "test-ns"
	artifactsDir := t.TempDir()

	runner, err := NewKubeJobTaskRunner(fakeClient, ns, artifactsDir)
	require.NoError(t, err)
	runner.pollInterval = 10 * time.Millisecond

	workload := &store.ClaimedJobWorkload{
		JobID:                     "job-timeout-1",
		WorkspaceID:               "ws-111",
		JobName:                   "slow-task",
		ImageDigest:               "registry.example.com/slow:v1",
		CommandArgs:               []string{"sleep", "100"},
		TimeoutSeconds:            1, // 1 second timeout
		AttemptNumber:             1,
		DeterministicResourceName: "hc-job-job-timeout-1",
	}

	exitCode, reason, err := runner.RunJob(ctx, workload)
	assert.Error(t, err)
	assert.Equal(t, -1, exitCode)
	assert.Contains(t, reason, "timed out")
}
