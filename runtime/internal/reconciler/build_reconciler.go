package reconciler

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	"github.com/hami9/hamicloud/runtime/internal/store"
)

// BuildResult holds the outcome of a source-to-image build execution.
type BuildResult struct {
	ImageDigest   string
	DurationMs    int
	Logs          string
	FailureReason string
}

// BuildTaskRunner abstracts isolated repository cloning and OCI image building.
type BuildTaskRunner interface {
	Build(ctx context.Context, task *store.ClaimedBuildTask) (*BuildResult, error)
}

// BuildStore abstracts persistence operations for build lifecycle transitions.
type BuildStore interface {
	MarkBuildSucceeded(ctx context.Context, releaseID string, imageDigest string, durationMs int, buildLogs string) error
	MarkBuildFailed(ctx context.Context, releaseID string, reason string, durationMs int, buildLogs string) error
}

// BuildReconciler orchestrates claiming and building source-backed releases.
type BuildReconciler struct {
	store  BuildStore
	runner BuildTaskRunner
	logger *slog.Logger
}

func NewBuildReconciler(st BuildStore, runner BuildTaskRunner, logger *slog.Logger) *BuildReconciler {
	if logger == nil {
		logger = slog.Default()
	}
	return &BuildReconciler{
		store:  st,
		runner: runner,
		logger: logger,
	}
}

func (r *BuildReconciler) ReconcileBuild(ctx context.Context, task *store.ClaimedBuildTask) error {
	r.logger.Info("Starting source build reconciliation",
		"release_id", task.ReleaseID,
		"app_id", task.ApplicationID,
		"app_slug", task.ApplicationSlug,
		"repo_url", redactBuildLogs(task.RepoURL),
		"commit_sha", task.CommitSHA,
		"git_ref", task.GitRef,
	)

	start := time.Now()
	res, err := r.runner.Build(ctx, task)
	elapsedMs := int(time.Since(start).Milliseconds())
	if elapsedMs <= 0 {
		elapsedMs = 1
	}

	var logs string
	var durationMs = elapsedMs
	if res != nil {
		logs = redactBuildLogs(res.Logs)
		if res.DurationMs > 0 {
			durationMs = res.DurationMs
		}
	}

	if err != nil || res == nil || res.FailureReason != "" || strings.TrimSpace(res.ImageDigest) == "" {
		reason := "build execution failed"
		if res != nil && res.FailureReason != "" {
			reason = redactBuildLogs(res.FailureReason)
		} else if err != nil {
			reason = redactBuildLogs(err.Error())
		}
		r.logger.Warn("Source build failed",
			"release_id", task.ReleaseID,
			"app_slug", task.ApplicationSlug,
			"reason", reason,
			"duration_ms", durationMs,
		)
		if markErr := r.store.MarkBuildFailed(ctx, task.ReleaseID, reason, durationMs, logs); markErr != nil {
			r.logger.Error("Failed to record BUILD_FAILED state",
				"release_id", task.ReleaseID,
				"error", markErr,
			)
			return markErr
		}
		if err != nil {
			return err
		}
		return fmt.Errorf("build failed: %s", reason)
	}

	r.logger.Info("Source build succeeded",
		"release_id", task.ReleaseID,
		"app_slug", task.ApplicationSlug,
		"image_digest", res.ImageDigest,
		"duration_ms", durationMs,
	)

	if err := r.store.MarkBuildSucceeded(ctx, task.ReleaseID, res.ImageDigest, durationMs, logs); err != nil {
		r.logger.Error("Failed to record IMAGE_READY state",
			"release_id", task.ReleaseID,
			"error", err,
		)
		return err
	}

	return nil
}

var (
	urlCredsPattern   = regexp.MustCompile(`(https?://)([^/\s:@]+):([^/\s@]+)@`)
	ghTokenPattern    = regexp.MustCompile(`\b(gh[pousr]_[A-Za-z0-9_]{8,}|github_pat_[A-Za-z0-9_]{8,}|whsec_[A-Za-z0-9_]{8,})\b`)
	bearerPattern     = regexp.MustCompile(`(?i)(Bearer\s+)[A-Za-z0-9\-._~+/]+=*`)
	kvSecretPattern   = regexp.MustCompile(`(?i)\b(password|passwd|secret|token|api_key|apikey|access_key|client_secret)(\s*[:=]\s*)([^\s"']+|"[^"]*"|'[^']*')`)
)

// redactBuildLogs scrubs credentials, tokens, and secrets from build output before storage.
func redactBuildLogs(input string) string {
	if input == "" {
		return ""
	}
	out := urlCredsPattern.ReplaceAllString(input, "${1}***:***@")
	out = ghTokenPattern.ReplaceAllString(out, "[REDACTED]")
	out = bearerPattern.ReplaceAllString(out, "${1}[REDACTED]")
	out = kvSecretPattern.ReplaceAllString(out, "${1}${2}[REDACTED]")
	return out
}

// verifyPathWithinRoot ensures that relPath resolved inside rootDir does not escape rootDir.
func verifyPathWithinRoot(rootDir, relPath string) (string, error) {
	cleanedRel := strings.TrimSpace(relPath)
	if cleanedRel == "" {
		cleanedRel = "."
	}
	// Reject paths starting with / or \ or Windows drive letters
	if filepath.IsAbs(cleanedRel) || strings.HasPrefix(cleanedRel, "/") || strings.HasPrefix(cleanedRel, `\`) {
		return "", fmt.Errorf("path traversal rejected: absolute path %q is forbidden", relPath)
	}

	absRoot, err := filepath.Abs(rootDir)
	if err != nil {
		return "", fmt.Errorf("resolve root dir: %w", err)
	}
	absTarget, err := filepath.Abs(filepath.Join(absRoot, filepath.FromSlash(cleanedRel)))
	if err != nil {
		return "", fmt.Errorf("resolve target path: %w", err)
	}

	rel, err := filepath.Rel(absRoot, absTarget)
	if err != nil {
		return "", fmt.Errorf("compute relative path: %w", err)
	}
	if rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
		return "", fmt.Errorf("path traversal rejected: %q escapes repository root", relPath)
	}

	return absTarget, nil
}

// IsolatedBuildKitRunner executes real Git clones in isolated temporary directories
// and builds/pushes OCI container images to the registry via BuildKit / Docker.
type IsolatedBuildKitRunner struct {
	BuildKitAddr string
	RegistryHost string
	GitReposDir  string
}

func NewIsolatedBuildKitRunner(buildKitAddr, registryHost string) *IsolatedBuildKitRunner {
	if buildKitAddr == "" {
		buildKitAddr = os.Getenv("BUILDKIT_HOST")
		if buildKitAddr == "" {
			buildKitAddr = "tcp://127.0.0.1:1234"
		}
	}
	if registryHost == "" {
		registryHost = os.Getenv("REGISTRY_HOST")
		if registryHost == "" {
			registryHost = "localhost:5000"
		}
	}
	return &IsolatedBuildKitRunner{
		BuildKitAddr: buildKitAddr,
		RegistryHost: registryHost,
		GitReposDir:  os.Getenv("BUILD_GIT_REPOS_DIR"),
	}
}

func (r *IsolatedBuildKitRunner) resolveRepoSource(repoURL string) string {
	gitReposDir := strings.TrimSpace(r.GitReposDir)
	if gitReposDir == "" {
		gitReposDir = strings.TrimSpace(os.Getenv("BUILD_GIT_REPOS_DIR"))
	}
	if gitReposDir != "" {
		if u, err := url.Parse(repoURL); err == nil && (u.Scheme == "http" || u.Scheme == "https") {
			cleanPath := strings.TrimSuffix(strings.Trim(u.Path, "/"), ".git")
			repoBase := path.Base(cleanPath)
			candidates := []string{
				filepath.Join(gitReposDir, filepath.FromSlash(cleanPath)),
				filepath.Join(gitReposDir, filepath.FromSlash(cleanPath)+".git"),
				filepath.Join(gitReposDir, repoBase),
				filepath.Join(gitReposDir, repoBase+".git"),
			}
			for _, c := range candidates {
				if info, statErr := os.Stat(c); statErr == nil && info.IsDir() {
					return c
				}
			}
		}
	}
	return repoURL
}

func (r *IsolatedBuildKitRunner) Build(ctx context.Context, task *store.ClaimedBuildTask) (*BuildResult, error) {
	start := time.Now()
	var logBuf bytes.Buffer

	writeLog := func(format string, args ...any) {
		_, _ = fmt.Fprintf(&logBuf, format+"\n", args...)
	}

	tempDir, err := os.MkdirTemp("", "hc-build-*")
	if err != nil {
		return &BuildResult{
			DurationMs:    1,
			FailureReason: fmt.Sprintf("failed to create isolated build workspace: %v", err),
		}, err
	}
	defer os.RemoveAll(tempDir)

	cloneDir := filepath.Join(tempDir, "repo")
	sourceRepo := r.resolveRepoSource(task.RepoURL)
	writeLog("==> Cloning repository %s into isolated workspace", redactBuildLogs(task.RepoURL))

	if out, err := runCommandCombined(ctx, tempDir, "git", "clone", sourceRepo, cloneDir); err != nil {
		writeLog("%s", out)
		reason := fmt.Sprintf("git clone failed: %v", err)
		return &BuildResult{
			DurationMs:    maxInt(int(time.Since(start).Milliseconds()), 1),
			Logs:          redactBuildLogs(logBuf.String()),
			FailureReason: reason,
		}, fmt.Errorf("%s", reason)
	} else if strings.TrimSpace(out) != "" {
		writeLog("%s", strings.TrimSpace(out))
	}

	targetSHA := strings.TrimSpace(task.CommitSHA)
	targetRef := strings.TrimSpace(task.GitRef)

	if targetSHA != "" {
		writeLog("==> Checking out commit %s", targetSHA)
		if out, err := runCommandCombined(ctx, cloneDir, "git", "checkout", "--detach", targetSHA); err != nil {
			writeLog("%s", out)
			reason := fmt.Sprintf("git checkout %s failed: %v", targetSHA, err)
			return &BuildResult{
				DurationMs:    maxInt(int(time.Since(start).Milliseconds()), 1),
				Logs:          redactBuildLogs(logBuf.String()),
				FailureReason: reason,
			}, fmt.Errorf("%s", reason)
		} else if strings.TrimSpace(out) != "" {
			writeLog("%s", strings.TrimSpace(out))
		}
	} else if targetRef != "" {
		writeLog("==> Checking out ref %s", targetRef)
		if out, err := runCommandCombined(ctx, cloneDir, "git", "checkout", targetRef); err != nil {
			writeLog("%s", out)
			reason := fmt.Sprintf("git checkout ref %s failed: %v", targetRef, err)
			return &BuildResult{
				DurationMs:    maxInt(int(time.Since(start).Milliseconds()), 1),
				Logs:          redactBuildLogs(logBuf.String()),
				FailureReason: reason,
			}, fmt.Errorf("%s", reason)
		}
	}

	headOut, err := runCommandCombined(ctx, cloneDir, "git", "rev-parse", "HEAD")
	if err != nil {
		writeLog("%s", headOut)
		reason := fmt.Sprintf("git rev-parse HEAD failed: %v", err)
		return &BuildResult{
			DurationMs:    maxInt(int(time.Since(start).Milliseconds()), 1),
			Logs:          redactBuildLogs(logBuf.String()),
			FailureReason: reason,
		}, fmt.Errorf("%s", reason)
	}
	actualSHA := strings.ToLower(strings.TrimSpace(headOut))
	if targetSHA != "" && !strings.HasPrefix(actualSHA, strings.ToLower(targetSHA)) {
		reason := fmt.Sprintf("checked out commit %s does not match requested commit %s", actualSHA, targetSHA)
		writeLog("ERROR: %s", reason)
		return &BuildResult{
			DurationMs:    maxInt(int(time.Since(start).Milliseconds()), 1),
			Logs:          redactBuildLogs(logBuf.String()),
			FailureReason: reason,
		}, fmt.Errorf("%s", reason)
	}
	writeLog("==> Checked out HEAD at %s", actualSHA)

	// Verify context_dir and dockerfile_path stay within cloneDir
	contextRel := task.ContextDir
	if strings.TrimSpace(contextRel) == "" {
		contextRel = "."
	}
	absContextDir, err := verifyPathWithinRoot(cloneDir, contextRel)
	if err != nil {
		writeLog("ERROR: %v", err)
		return &BuildResult{
			DurationMs:    maxInt(int(time.Since(start).Milliseconds()), 1),
			Logs:          redactBuildLogs(logBuf.String()),
			FailureReason: err.Error(),
		}, err
	}

	dockerfileRel := task.DockerfilePath
	if strings.TrimSpace(dockerfileRel) == "" {
		dockerfileRel = "Dockerfile"
	}
	absDockerfile, err := verifyPathWithinRoot(cloneDir, dockerfileRel)
	if err != nil {
		writeLog("ERROR: %v", err)
		return &BuildResult{
			DurationMs:    maxInt(int(time.Since(start).Milliseconds()), 1),
			Logs:          redactBuildLogs(logBuf.String()),
			FailureReason: err.Error(),
		}, err
	}
	if _, statErr := os.Stat(absDockerfile); statErr != nil && absContextDir != cloneDir {
		if candidate, candErr := verifyPathWithinRoot(absContextDir, dockerfileRel); candErr == nil {
			if _, cStatErr := os.Stat(candidate); cStatErr == nil {
				absDockerfile = candidate
			}
		}
	}

	ctxInfo, err := os.Stat(absContextDir)
	if err != nil || !ctxInfo.IsDir() {
		reason := fmt.Sprintf("build context directory %q not found", contextRel)
		writeLog("ERROR: %s", reason)
		return &BuildResult{
			DurationMs:    maxInt(int(time.Since(start).Milliseconds()), 1),
			Logs:          redactBuildLogs(logBuf.String()),
			FailureReason: reason,
		}, fmt.Errorf("%s", reason)
	}

	dfInfo, err := os.Stat(absDockerfile)
	if err != nil || dfInfo.IsDir() {
		reason := fmt.Sprintf("Dockerfile %q not found in repository", dockerfileRel)
		writeLog("ERROR: %s", reason)
		return &BuildResult{
			DurationMs:    maxInt(int(time.Since(start).Milliseconds()), 1),
			Logs:          redactBuildLogs(logBuf.String()),
			FailureReason: reason,
		}, fmt.Errorf("%s", reason)
	}

	wsSlug := sanitizeResourceName(task.WorkspaceSlug)
	appSlug := sanitizeResourceName(task.ApplicationSlug)
	shortSHA := actualSHA
	if len(shortSHA) > 12 {
		shortSHA = shortSHA[:12]
	}
	if shortSHA == "" {
		shortSHA = fmt.Sprintf("rel-%d", task.ReleaseNumber)
	}

	repoPath := fmt.Sprintf("%s/%s", wsSlug, appSlug)
	imageTag := fmt.Sprintf("%s/%s:%s", r.RegistryHost, repoPath, shortSHA)

	writeLog("==> Building OCI image %s from %s (context %s)", imageTag, dockerfileRel, contextRel)
	digest, buildOut, err := r.buildAndPushImage(ctx, absContextDir, absDockerfile, repoPath, shortSHA, imageTag)
	if strings.TrimSpace(buildOut) != "" {
		writeLog("%s", strings.TrimSpace(buildOut))
	}
	if err != nil {
		reason := fmt.Sprintf("image build failed: %v", err)
		return &BuildResult{
			DurationMs:    maxInt(int(time.Since(start).Milliseconds()), 1),
			Logs:          redactBuildLogs(logBuf.String()),
			FailureReason: reason,
		}, fmt.Errorf("%s", reason)
	}

	fullDigestRef := fmt.Sprintf("%s/%s@%s", r.RegistryHost, repoPath, digest)
	writeLog("==> Successfully built and pushed %s", fullDigestRef)

	return &BuildResult{
		ImageDigest: fullDigestRef,
		DurationMs:  maxInt(int(time.Since(start).Milliseconds()), 1),
		Logs:        redactBuildLogs(logBuf.String()),
	}, nil
}

var sha256DigestRegex = regexp.MustCompile(`sha256:[0-9a-f]{64}`)

func (r *IsolatedBuildKitRunner) buildAndPushImage(
	ctx context.Context,
	absContextDir string,
	absDockerfile string,
	repoPath string,
	tag string,
	imageTag string,
) (string, string, error) {
	var combinedLogs strings.Builder

	// Attempt buildctl against BuildKit daemon first if buildctl is installed on host
	if buildctlPath, lookErr := exec.LookPath("buildctl"); lookErr == nil && buildctlPath != "" {
		buildkitRegistryRef := fmt.Sprintf("hamicloud-registry:5000/%s:%s", repoPath, tag)
		out, err := runCommandCombined(
			ctx,
			absContextDir,
			buildctlPath,
			"--addr", r.BuildKitAddr,
			"build",
			"--frontend", "dockerfile.v0",
			"--local", "context="+absContextDir,
			"--local", "dockerfile="+filepath.Dir(absDockerfile),
			"--opt", "filename="+filepath.Base(absDockerfile),
			"--output", "type=image,name="+buildkitRegistryRef+",registry.insecure=true,push=true",
		)
		combinedLogs.WriteString(out)
		if err == nil {
			if digest := r.fetchRegistryDigest(ctx, repoPath, tag, out); digest != "" {
				return digest, combinedLogs.String(), nil
			}
		}
	}

	// Build with Docker (BuildKit backend) and push to local OCI registry
	buildCmd := exec.CommandContext(ctx, "docker", "build", "-t", imageTag, "-f", absDockerfile, absContextDir)
	buildCmd.Env = append(os.Environ(), "DOCKER_BUILDKIT=1")
	buildOutBytes, buildErr := buildCmd.CombinedOutput()
	combinedLogs.Write(buildOutBytes)
	if buildErr != nil {
		return "", combinedLogs.String(), fmt.Errorf("docker build failed: %w", buildErr)
	}

	pushOut, pushErr := runCommandCombined(ctx, absContextDir, "docker", "push", imageTag)
	combinedLogs.WriteString("\n")
	combinedLogs.WriteString(pushOut)
	if pushErr != nil {
		return "", combinedLogs.String(), fmt.Errorf("docker push failed: %w", pushErr)
	}

	digest := r.fetchRegistryDigest(ctx, repoPath, tag, pushOut)
	if digest == "" {
		inspectOut, inspectErr := runCommandCombined(ctx, absContextDir, "docker", "image", "inspect", "--format", "{{json .RepoDigests}}", imageTag)
		if inspectErr == nil {
			digest = sha256DigestRegex.FindString(inspectOut)
		}
	}
	if digest == "" {
		return "", combinedLogs.String(), fmt.Errorf("failed to resolve sha256 image digest after push")
	}

	// Ensure Kind / desktop-control-plane node can resolve localhost:5000 from hamicloud-registry
	r.syncKindNodeRegistry(ctx, imageTag)

	return digest, combinedLogs.String(), nil
}

func (r *IsolatedBuildKitRunner) fetchRegistryDigest(ctx context.Context, repoPath, tag, fallbackOutput string) string {
	manifestURL := fmt.Sprintf("http://%s/v2/%s/manifests/%s", r.RegistryHost, repoPath, tag)
	req, err := http.NewRequestWithContext(ctx, http.MethodHead, manifestURL, nil)
	if err == nil {
		req.Header.Set("Accept", strings.Join([]string{
			"application/vnd.oci.image.manifest.v1+json",
			"application/vnd.docker.distribution.manifest.v2+json",
			"application/vnd.oci.image.index.v1+json",
			"application/vnd.docker.distribution.manifest.list.v2+json",
		}, ", "))
		client := &http.Client{Timeout: 5 * time.Second}
		if resp, doErr := client.Do(req); doErr == nil {
			_, _ = io.Copy(io.Discard, resp.Body)
			_ = resp.Body.Close()
			if hdr := strings.TrimSpace(resp.Header.Get("Docker-Content-Digest")); sha256DigestRegex.MatchString(hdr) {
				return sha256DigestRegex.FindString(hdr)
			}
		}
	}
	return sha256DigestRegex.FindString(fallbackOutput)
}

func (r *IsolatedBuildKitRunner) syncKindNodeRegistry(ctx context.Context, imageTag string) {
	// Ensure hamicloud-registry is attached to the kind network and containerd hosts.toml is present
	_ = exec.CommandContext(ctx, "docker", "network", "connect", "kind", "hamicloud-registry").Run()
	hostsToml := "server = \"http://hamicloud-registry:5000\"\n\n[host.\"http://hamicloud-registry:5000\"]\n  capabilities = [\"pull\", \"resolve\", \"push\"]\n  skip_verify = true\n"
	cmd := exec.CommandContext(ctx, "docker", "exec", "-i", "desktop-control-plane", "sh", "-c", "mkdir -p /etc/containerd/certs.d/localhost:5000 && cat > /etc/containerd/certs.d/localhost:5000/hosts.toml")
	cmd.Stdin = strings.NewReader(hostsToml)
	_ = cmd.Run()
	_ = imageTag
}

func runCommandCombined(ctx context.Context, dir string, name string, args ...string) (string, error) {
	cmd := exec.CommandContext(ctx, name, args...)
	if dir != "" {
		cmd.Dir = dir
	}
	cmd.Env = append(os.Environ(), "GIT_TERMINAL_PROMPT=0")
	out, err := cmd.CombinedOutput()
	return string(out), err
}

func maxInt(a, b int) int {
	if a > b {
		return a
	}
	return b
}
