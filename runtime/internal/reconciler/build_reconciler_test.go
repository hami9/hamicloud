package reconciler

import (
	"context"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/hami9/hamicloud/runtime/internal/store"
)

type mockBuildStore struct {
	succeededCalled bool
	failedCalled    bool
	releaseID       string
	imageDigest     string
	reason          string
	durationMs      int
	buildLogs       string
	returnErr       error
}

func (m *mockBuildStore) MarkBuildSucceeded(ctx context.Context, releaseID string, imageDigest string, durationMs int, buildLogs string) error {
	m.succeededCalled = true
	m.releaseID = releaseID
	m.imageDigest = imageDigest
	m.durationMs = durationMs
	m.buildLogs = buildLogs
	return m.returnErr
}

func (m *mockBuildStore) MarkBuildFailed(ctx context.Context, releaseID string, reason string, durationMs int, buildLogs string) error {
	m.failedCalled = true
	m.releaseID = releaseID
	m.reason = reason
	m.durationMs = durationMs
	m.buildLogs = buildLogs
	return m.returnErr
}

type mockBuildRunner struct {
	result *BuildResult
	err    error
}

func (m *mockBuildRunner) Build(ctx context.Context, task *store.ClaimedBuildTask) (*BuildResult, error) {
	return m.result, m.err
}

func TestBuildReconciler_SuccessMarksImageReadyAndRedactsLogs(t *testing.T) {
	st := &mockBuildStore{}
	runner := &mockBuildRunner{
		result: &BuildResult{
			ImageDigest: "localhost:5000/ws/app@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
			DurationMs:  1420,
			Logs:        "Cloning https://oauth2:ghp_secretToken123456@github.com/org/repo.git\nStep 1/2 : FROM alpine\npassword=my_super_secret\nSuccessfully built",
		},
	}
	rec := NewBuildReconciler(st, runner, nil)

	task := &store.ClaimedBuildTask{
		ReleaseID:       "rel-100",
		ApplicationID:   "app-100",
		ApplicationSlug: "demo-app",
		WorkspaceID:     "ws-100",
		WorkspaceSlug:   "ws-demo",
		ReleaseNumber:   2,
		RepoURL:         "https://github.com/org/repo.git",
		CommitSHA:       "a1b2c3d4e5f60718293a4b5c6d7e8f9a0b1c2d3e",
		GitRef:          "main",
		DockerfilePath:  "Dockerfile",
		ContextDir:      ".",
	}

	if err := rec.ReconcileBuild(context.Background(), task); err != nil {
		t.Fatalf("expected nil error, got %v", err)
	}
	if !st.succeededCalled {
		t.Fatalf("expected MarkBuildSucceeded to be called")
	}
	if st.failedCalled {
		t.Fatalf("did not expect MarkBuildFailed to be called")
	}
	if st.imageDigest != runner.result.ImageDigest {
		t.Errorf("expected imageDigest %q, got %q", runner.result.ImageDigest, st.imageDigest)
	}
	if strings.Contains(st.buildLogs, "ghp_secretToken123456") || strings.Contains(st.buildLogs, "my_super_secret") {
		t.Errorf("expected secrets to be redacted from buildLogs, got: %s", st.buildLogs)
	}
}

func TestBuildReconciler_FailureMarksBuildFailedAndPreservesServingRelease(t *testing.T) {
	st := &mockBuildStore{}
	runner := &mockBuildRunner{
		result: &BuildResult{
			DurationMs:    530,
			Logs:          "Step 2/2 : RUN exit 1\ntoken=ghp_1234567890abcdef",
			FailureReason: "dockerfile command failed with exit code 1",
		},
		err: errors.New("dockerfile command failed with exit code 1"),
	}
	rec := NewBuildReconciler(st, runner, nil)

	task := &store.ClaimedBuildTask{
		ReleaseID:       "rel-200",
		ApplicationID:   "app-200",
		ApplicationSlug: "demo-app",
		WorkspaceID:     "ws-200",
		WorkspaceSlug:   "ws-demo",
		ReleaseNumber:   3,
		RepoURL:         "https://github.com/org/repo.git",
		CommitSHA:       "b2c3d4e5f60718293a4b5c6d7e8f9a0b1c2d3e4f",
	}

	err := rec.ReconcileBuild(context.Background(), task)
	if err == nil {
		t.Fatalf("expected error from failed build")
	}
	if !st.failedCalled {
		t.Fatalf("expected MarkBuildFailed to be called")
	}
	if st.succeededCalled {
		t.Fatalf("did not expect MarkBuildSucceeded to be called on build failure")
	}
	if st.reason != "dockerfile command failed with exit code 1" {
		t.Errorf("unexpected failure reason: %q", st.reason)
	}
	if strings.Contains(st.buildLogs, "ghp_1234567890abcdef") {
		t.Errorf("expected token in failed build logs to be redacted, got: %s", st.buildLogs)
	}
}

func TestVerifyPathWithinRoot_RejectsTraversal(t *testing.T) {
	root := t.TempDir()

	validPaths := []string{".", "Dockerfile", "apps/web/Dockerfile", "./subdir"}
	for _, p := range validPaths {
		if _, err := verifyPathWithinRoot(root, p); err != nil {
			t.Errorf("expected path %q to be allowed, got error: %v", p, err)
		}
	}

	traversalPaths := []string{
		"../Dockerfile",
		"../../etc/passwd",
		"subdir/../../outside",
		"/etc/passwd",
		`\Windows\System32`,
	}
	for _, p := range traversalPaths {
		if _, err := verifyPathWithinRoot(root, p); err == nil {
			t.Errorf("expected path traversal %q to be rejected, but it was allowed", p)
		}
	}
}

func TestIsolatedBuildKitRunner_RejectsDockerfilePathTraversal(t *testing.T) {
	if _, err := exec.LookPath("git"); err != nil {
		t.Skip("git not available in PATH")
	}

	repoDir := t.TempDir()
	runGit := func(args ...string) {
		t.Helper()
		cmd := exec.Command("git", args...)
		cmd.Dir = repoDir
		if out, err := cmd.CombinedOutput(); err != nil {
			t.Fatalf("git %v failed: %v (%s)", args, err, string(out))
		}
	}

	runGit("init")
	runGit("config", "user.email", "test@hamicloud.local")
	runGit("config", "user.name", "HamiCloud Test")
	if err := os.WriteFile(filepath.Join(repoDir, "Dockerfile"), []byte("FROM scratch\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	runGit("add", "Dockerfile")
	runGit("commit", "-m", "initial commit")

	shaOut, err := exec.Command("git", "-C", repoDir, "rev-parse", "HEAD").Output()
	if err != nil {
		t.Fatal(err)
	}
	commitSHA := strings.TrimSpace(string(shaOut))

	runner := NewIsolatedBuildKitRunner("tcp://127.0.0.1:1234", "localhost:5000")
	res, err := runner.Build(context.Background(), &store.ClaimedBuildTask{
		ReleaseID:       "rel-traversal",
		ApplicationID:   "app-1",
		ApplicationSlug: "app",
		WorkspaceID:     "ws-1",
		WorkspaceSlug:   "ws",
		ReleaseNumber:   1,
		RepoURL:         repoDir,
		CommitSHA:       commitSHA,
		DockerfilePath:  "../../etc/passwd",
		ContextDir:      ".",
	})
	if err == nil {
		t.Fatalf("expected error for DockerfilePath traversal")
	}
	if res == nil || !strings.Contains(res.FailureReason, "path traversal rejected") {
		t.Fatalf("expected path traversal failure reason, got %+v", res)
	}
}
