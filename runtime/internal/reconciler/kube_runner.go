package reconciler

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	utilintstr "k8s.io/apimachinery/pkg/util/intstr"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/clientcmd"

	"github.com/hami9/hamicloud/runtime/internal/store"
)

// BuildKubeClient constructs a Kubernetes client from KUBECONFIG or in-cluster config,
// verifying connectivity against the API server.
func BuildKubeClient(kubeconfigPath string) (kubernetes.Interface, error) {
	var restCfg *rest.Config
	var err error

	if kubeconfigPath != "" {
		restCfg, err = clientcmd.BuildConfigFromFlags("", kubeconfigPath)
		if err != nil {
			return nil, fmt.Errorf("failed to build kubeconfig from %q: %w", kubeconfigPath, err)
		}
	} else {
		// In-cluster configuration when running inside Kubernetes
		restCfg, err = rest.InClusterConfig()
		if err != nil {
			return nil, fmt.Errorf("in-cluster kubernetes configuration not available: %w", err)
		}
	}

	client, err := kubernetes.NewForConfig(restCfg)
	if err != nil {
		return nil, fmt.Errorf("failed to create kubernetes clientset: %w", err)
	}

	// Verify API server connectivity with a brief timeout
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	_, err = client.Discovery().RESTClient().Get().AbsPath("/version").DoRaw(ctx)
	if err != nil {
		return nil, fmt.Errorf("kubernetes cluster is unreachable: %w", err)
	}

	return client, nil
}

// sanitizeResourceName ensures names satisfy Kubernetes RFC 1123 DNS subdomain requirements.
func sanitizeResourceName(name string) string {
	name = strings.ToLower(name)
	var b strings.Builder
	for _, r := range name {
		if (r >= 'a' && r <= 'z') || (r >= '0' && r <= '9') || r == '-' {
			b.WriteRune(r)
		} else {
			b.WriteRune('-')
		}
	}
	s := strings.Trim(b.String(), "-")
	if len(s) > 63 {
		s = strings.TrimRight(s[:63], "-")
	}
	if s == "" {
		s = "workload"
	}
	return s
}

// KubeWorkloadRunner manages Kubernetes Deployment, Service, and Ingress resources for long-running services.
type KubeWorkloadRunner struct {
	client        kubernetes.Interface
	namespace     string
	ingressDomain string
}

func NewKubeWorkloadRunner(client kubernetes.Interface, namespace string, ingressDomain string) *KubeWorkloadRunner {
	if namespace == "" {
		namespace = "default"
	}
	return &KubeWorkloadRunner{
		client:        client,
		namespace:     namespace,
		ingressDomain: ingressDomain,
	}
}

func (k *KubeWorkloadRunner) Deploy(ctx context.Context, workload *store.ClaimedWorkload) (string, error) {
	name := sanitizeResourceName(workload.DeterministicResourceName)
	if name == "" {
		name = sanitizeResourceName(fmt.Sprintf("hc-svc-%s-%d", workload.ApplicationSlug, workload.TargetGeneration))
	}

	appSlug := sanitizeResourceName(workload.ApplicationSlug)
	port := int32(workload.Port)
	if port <= 0 {
		port = 8080
	}

	labels := map[string]string{
		"app.kubernetes.io/name":      appSlug,
		"app.kubernetes.io/instance":  name,
		"hamicloud.io/workspace-id":   workload.WorkspaceID,
		"hamicloud.io/application-id": workload.ApplicationID,
		"hamicloud.io/release-id":     workload.ReleaseID,
		"hamicloud.io/generation":     strconv.Itoa(workload.TargetGeneration),
	}

	container := corev1.Container{
		Name:  appSlug,
		Image: workload.ImageDigest,
		Ports: []corev1.ContainerPort{{
			Name:          "http",
			ContainerPort: port,
		}},
	}

	if workload.HealthPath != "" {
		healthPath := workload.HealthPath
		if !strings.HasPrefix(healthPath, "/") {
			healthPath = "/" + healthPath
		}
		container.ReadinessProbe = &corev1.Probe{
			ProbeHandler: corev1.ProbeHandler{
				HTTPGet: &corev1.HTTPGetAction{
					Path: healthPath,
					Port: utilintstr.FromInt(int(port)),
				},
			},
			InitialDelaySeconds: 2,
			PeriodSeconds:       5,
			TimeoutSeconds:      2,
			FailureThreshold:    3,
		}
	}

	replicas := int32(1)
	desiredDeploy := &appsv1.Deployment{
		ObjectMeta: metav1.ObjectMeta{
			Name:      name,
			Namespace: k.namespace,
			Labels:    labels,
		},
		Spec: appsv1.DeploymentSpec{
			Replicas: &replicas,
			Selector: &metav1.LabelSelector{
				MatchLabels: map[string]string{
					"app.kubernetes.io/instance": name,
				},
			},
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{
					Labels: labels,
				},
				Spec: corev1.PodSpec{
					Containers: []corev1.Container{container},
				},
			},
		},
	}

	existingDeploy, err := k.client.AppsV1().Deployments(k.namespace).Get(ctx, name, metav1.GetOptions{})
	var resourceUID string
	if apierrors.IsNotFound(err) {
		created, createErr := k.client.AppsV1().Deployments(k.namespace).Create(ctx, desiredDeploy, metav1.CreateOptions{})
		if createErr != nil {
			return "", fmt.Errorf("create deployment %s: %w", name, createErr)
		}
		resourceUID = string(created.UID)
	} else if err != nil {
		return "", fmt.Errorf("get deployment %s: %w", name, err)
	} else {
		existingDeploy.Spec = desiredDeploy.Spec
		existingDeploy.Labels = desiredDeploy.Labels
		updated, updateErr := k.client.AppsV1().Deployments(k.namespace).Update(ctx, existingDeploy, metav1.UpdateOptions{})
		if updateErr != nil {
			return "", fmt.Errorf("update deployment %s: %w", name, updateErr)
		}
		resourceUID = string(updated.UID)
	}

	desiredSvc := &corev1.Service{
		ObjectMeta: metav1.ObjectMeta{
			Name:      name,
			Namespace: k.namespace,
			Labels:    labels,
		},
		Spec: corev1.ServiceSpec{
			Selector: map[string]string{
				"app.kubernetes.io/instance": name,
			},
			Ports: []corev1.ServicePort{{
				Name:       "http",
				Port:       port,
				TargetPort: utilintstr.FromInt(int(port)),
			}},
			Type: corev1.ServiceTypeClusterIP,
		},
	}

	existingSvc, err := k.client.CoreV1().Services(k.namespace).Get(ctx, name, metav1.GetOptions{})
	if apierrors.IsNotFound(err) {
		if _, createErr := k.client.CoreV1().Services(k.namespace).Create(ctx, desiredSvc, metav1.CreateOptions{}); createErr != nil {
			return "", fmt.Errorf("create service %s: %w", name, createErr)
		}
	} else if err != nil {
		return "", fmt.Errorf("get service %s: %w", name, err)
	} else {
		existingSvc.Spec.Ports = desiredSvc.Spec.Ports
		existingSvc.Spec.Selector = desiredSvc.Spec.Selector
		existingSvc.Labels = desiredSvc.Labels
		if _, updateErr := k.client.CoreV1().Services(k.namespace).Update(ctx, existingSvc, metav1.UpdateOptions{}); updateErr != nil {
			return "", fmt.Errorf("update service %s: %w", name, updateErr)
		}
	}

	if k.ingressDomain != "" {
		pathType := networkingv1.PathTypePrefix
		wsSlug := workload.WorkspaceSlug
		if wsSlug == "" {
			wsSlug = "default"
		}
		// Decision D15: Application ingress routing follows <app-slug>.<workspace-slug>.<domain>
		host := fmt.Sprintf("%s.%s.%s", appSlug, wsSlug, k.ingressDomain)
		desiredIng := &networkingv1.Ingress{
			ObjectMeta: metav1.ObjectMeta{
				Name:      name,
				Namespace: k.namespace,
				Labels:    labels,
			},
			Spec: networkingv1.IngressSpec{
				Rules: []networkingv1.IngressRule{{
					Host: host,
					IngressRuleValue: networkingv1.IngressRuleValue{
						HTTP: &networkingv1.HTTPIngressRuleValue{
							Paths: []networkingv1.HTTPIngressPath{{
								Path:     "/",
								PathType: &pathType,
								Backend: networkingv1.IngressBackend{
									Service: &networkingv1.IngressServiceBackend{
										Name: name,
										Port: networkingv1.ServiceBackendPort{
											Number: port,
										},
									},
								},
							}},
						},
					},
				}},
			},
		}

		existingIng, err := k.client.NetworkingV1().Ingresses(k.namespace).Get(ctx, name, metav1.GetOptions{})
		if apierrors.IsNotFound(err) {
			if _, createErr := k.client.NetworkingV1().Ingresses(k.namespace).Create(ctx, desiredIng, metav1.CreateOptions{}); createErr != nil {
				return "", fmt.Errorf("create ingress %s: %w", name, createErr)
			}
		} else if err == nil {
			existingIng.Spec = desiredIng.Spec
			existingIng.Labels = desiredIng.Labels
			if _, updateErr := k.client.NetworkingV1().Ingresses(k.namespace).Update(ctx, existingIng, metav1.UpdateOptions{}); updateErr != nil {
				return "", fmt.Errorf("update ingress %s: %w", name, updateErr)
			}
		}
	}

	if resourceUID == "" {
		resourceUID = name
	}
	return resourceUID, nil
}

func (k *KubeWorkloadRunner) CheckReadiness(ctx context.Context, workload *store.ClaimedWorkload) (bool, string, error) {
	name := sanitizeResourceName(workload.DeterministicResourceName)
	if name == "" {
		name = sanitizeResourceName(fmt.Sprintf("hc-svc-%s-%d", workload.ApplicationSlug, workload.TargetGeneration))
	}

	deploy, err := k.client.AppsV1().Deployments(k.namespace).Get(ctx, name, metav1.GetOptions{})
	if apierrors.IsNotFound(err) {
		return false, fmt.Sprintf("deployment %s not found in namespace %s", name, k.namespace), nil
	} else if err != nil {
		return false, "", fmt.Errorf("get deployment %s: %w", name, err)
	}

	if deploy.Status.ReadyReplicas > 0 {
		return true, "", nil
	}

	for _, cond := range deploy.Status.Conditions {
		if cond.Type == appsv1.DeploymentProgressing && cond.Status == corev1.ConditionFalse {
			return false, fmt.Sprintf("Deployment progressing failed: %s (%s)", cond.Reason, cond.Message), nil
		}
		if cond.Type == appsv1.DeploymentReplicaFailure && cond.Status == corev1.ConditionTrue {
			return false, fmt.Sprintf("Deployment replica failure: %s (%s)", cond.Reason, cond.Message), nil
		}
	}

	pods, err := k.client.CoreV1().Pods(k.namespace).List(ctx, metav1.ListOptions{
		LabelSelector: fmt.Sprintf("app.kubernetes.io/instance=%s", name),
	})
	if err == nil && len(pods.Items) > 0 {
		for _, pod := range pods.Items {
			for _, cs := range pod.Status.ContainerStatuses {
				if cs.State.Waiting != nil && cs.State.Waiting.Reason != "" {
					return false, fmt.Sprintf("Pod %s container waiting: %s: %s", pod.Name, cs.State.Waiting.Reason, cs.State.Waiting.Message), nil
				}
				if cs.State.Terminated != nil && cs.State.Terminated.ExitCode != 0 {
					return false, fmt.Sprintf("Pod %s container terminated with exit code %d: %s", pod.Name, cs.State.Terminated.ExitCode, cs.State.Terminated.Reason), nil
				}
			}
		}
	}

	desiredReplicas := int32(1)
	if deploy.Spec.Replicas != nil {
		desiredReplicas = *deploy.Spec.Replicas
	}
	return false, fmt.Sprintf("Deployment %s waiting for ready replicas (%d/%d ready)", name, deploy.Status.ReadyReplicas, desiredReplicas), nil
}

func (k *KubeWorkloadRunner) Teardown(ctx context.Context, workload *store.ClaimedWorkload) error {
	name := sanitizeResourceName(workload.DeterministicResourceName)
	if name == "" {
		name = sanitizeResourceName(fmt.Sprintf("hc-svc-%s-%d", workload.ApplicationSlug, workload.TargetGeneration))
	}

	bg := metav1.DeletePropagationBackground
	delOpts := metav1.DeleteOptions{PropagationPolicy: &bg}

	_ = k.client.AppsV1().Deployments(k.namespace).Delete(ctx, name, delOpts)
	_ = k.client.CoreV1().Services(k.namespace).Delete(ctx, name, delOpts)
	if k.ingressDomain != "" {
		_ = k.client.NetworkingV1().Ingresses(k.namespace).Delete(ctx, name, delOpts)
	}
	return nil
}

// KubeJobTaskRunner executes finite jobs as Kubernetes Batch v1 Jobs.
type KubeJobTaskRunner struct {
	client       kubernetes.Interface
	namespace    string
	artifactsDir string
	pollInterval time.Duration
}

func NewKubeJobTaskRunner(client kubernetes.Interface, namespace string, artifactsDir string) (*KubeJobTaskRunner, error) {
	if namespace == "" {
		namespace = "default"
	}
	if artifactsDir == "" {
		artifactsDir = os.Getenv("ARTIFACTS_DIR")
		if artifactsDir == "" {
			artifactsDir = filepath.Join("var", "artifacts")
		}
	}
	absDir, err := filepath.Abs(artifactsDir)
	if err != nil {
		return nil, fmt.Errorf("resolve artifacts dir: %w", err)
	}
	return &KubeJobTaskRunner{
		client:       client,
		namespace:    namespace,
		artifactsDir: absDir,
		pollInterval: 250 * time.Millisecond,
	}, nil
}

func (r *KubeJobTaskRunner) RunJob(ctx context.Context, workload *store.ClaimedJobWorkload) (int, string, error) {
	jobName := sanitizeResourceName(workload.DeterministicResourceName)
	if jobName == "" {
		jobName = sanitizeResourceName(fmt.Sprintf("hc-job-%s-%d", workload.JobID, workload.AttemptNumber))
	}

	if workload.TimeoutSeconds > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, time.Duration(workload.TimeoutSeconds)*time.Second)
		defer cancel()
	}

	labels := map[string]string{
		"app.kubernetes.io/name":      sanitizeResourceName(workload.JobName),
		"app.kubernetes.io/instance":  jobName,
		"hamicloud.io/job-id":         workload.JobID,
		"hamicloud.io/attempt-number": strconv.Itoa(workload.AttemptNumber),
		"hamicloud.io/workspace-id":   workload.WorkspaceID,
		"hamicloud.io/job-attempt-id": workload.JobAttemptID,
	}

	var envVars []corev1.EnvVar
	for k, v := range workload.EnvVars {
		envVars = append(envVars, corev1.EnvVar{Name: k, Value: v})
	}
	sort.Slice(envVars, func(i, j int) bool { return envVars[i].Name < envVars[j].Name })

	jobContainer := corev1.Container{
		Name:  "job",
		Image: workload.ImageDigest,
		Env:   envVars,
	}
	if len(workload.CommandArgs) > 0 {
		jobContainer.Command = workload.CommandArgs
	}

	backoffLimit := int32(0)
	job := &batchv1.Job{
		ObjectMeta: metav1.ObjectMeta{
			Name:      jobName,
			Namespace: r.namespace,
			Labels:    labels,
		},
		Spec: batchv1.JobSpec{
			BackoffLimit: &backoffLimit,
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{
					Labels: labels,
				},
				Spec: corev1.PodSpec{
					RestartPolicy: corev1.RestartPolicyNever,
					Containers:    []corev1.Container{jobContainer},
				},
			},
		},
	}

	_, err := r.client.BatchV1().Jobs(r.namespace).Create(ctx, job, metav1.CreateOptions{})
	if apierrors.IsAlreadyExists(err) {
		bg := metav1.DeletePropagationBackground
		_ = r.client.BatchV1().Jobs(r.namespace).Delete(ctx, jobName, metav1.DeleteOptions{PropagationPolicy: &bg})
		_, err = r.client.BatchV1().Jobs(r.namespace).Create(ctx, job, metav1.CreateOptions{})
	}
	if err != nil {
		return 1, fmt.Sprintf("failed to create kubernetes job %s: %v", jobName, err), err
	}

	ticker := time.NewTicker(r.pollInterval)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			bg := metav1.DeletePropagationBackground
			_ = r.client.BatchV1().Jobs(r.namespace).Delete(context.Background(), jobName, metav1.DeleteOptions{PropagationPolicy: &bg})
			if errors.Is(ctx.Err(), context.DeadlineExceeded) {
				return -1, "job execution timed out", ctx.Err()
			}
			return -1, "job execution cancelled", ctx.Err()

		case <-ticker.C:
			currentJob, getErr := r.client.BatchV1().Jobs(r.namespace).Get(ctx, jobName, metav1.GetOptions{})
			if getErr != nil {
				if apierrors.IsNotFound(getErr) {
					continue
				}
				return 1, fmt.Sprintf("failed to get job %s status: %v", jobName, getErr), getErr
			}

			if currentJob.Status.Succeeded > 0 {
				outputBytes := r.fetchPodLogs(ctx, jobName)
				r.persistArtifacts(workload, outputBytes)
				bg := metav1.DeletePropagationBackground
				_ = r.client.BatchV1().Jobs(r.namespace).Delete(context.Background(), jobName, metav1.DeleteOptions{PropagationPolicy: &bg})
				return 0, "", nil
			}

			if currentJob.Status.Failed > 0 {
				outputBytes := r.fetchPodLogs(ctx, jobName)
				r.persistArtifacts(workload, outputBytes)
				exitCode, reason := r.extractFailureDetails(ctx, jobName, currentJob)
				bg := metav1.DeletePropagationBackground
				_ = r.client.BatchV1().Jobs(r.namespace).Delete(context.Background(), jobName, metav1.DeleteOptions{PropagationPolicy: &bg})
				return exitCode, reason, nil
			}
		}
	}
}

func (r *KubeJobTaskRunner) fetchPodLogs(ctx context.Context, jobName string) []byte {
	pods, err := r.client.CoreV1().Pods(r.namespace).List(ctx, metav1.ListOptions{
		LabelSelector: fmt.Sprintf("app.kubernetes.io/instance=%s", jobName),
	})
	if err != nil || len(pods.Items) == 0 {
		return nil
	}

	podName := pods.Items[0].Name
	req := r.client.CoreV1().Pods(r.namespace).GetLogs(podName, &corev1.PodLogOptions{})
	stream, err := req.Stream(ctx)
	if err != nil {
		return nil
	}
	defer stream.Close()

	data, _ := io.ReadAll(stream)
	return data
}

func (r *KubeJobTaskRunner) extractFailureDetails(ctx context.Context, jobName string, job *batchv1.Job) (int, string) {
	exitCode := 1
	reason := "job failed"

	for _, cond := range job.Status.Conditions {
		if cond.Type == batchv1.JobFailed {
			reason = fmt.Sprintf("%s: %s", cond.Reason, cond.Message)
		}
	}

	pods, err := r.client.CoreV1().Pods(r.namespace).List(ctx, metav1.ListOptions{
		LabelSelector: fmt.Sprintf("app.kubernetes.io/instance=%s", jobName),
	})
	if err == nil && len(pods.Items) > 0 {
		for _, pod := range pods.Items {
			for _, cs := range pod.Status.ContainerStatuses {
				if cs.State.Terminated != nil {
					exitCode = int(cs.State.Terminated.ExitCode)
					if cs.State.Terminated.Reason != "" {
						reason = fmt.Sprintf("container exited with code %d: %s", exitCode, cs.State.Terminated.Reason)
					}
					return exitCode, reason
				}
			}
		}
	}

	return exitCode, reason
}

func (r *KubeJobTaskRunner) persistArtifacts(workload *store.ClaimedJobWorkload, content []byte) {
	if workload.WorkspaceID == "" || workload.JobID == "" {
		return
	}
	artifactDir := filepath.Join(r.artifactsDir, workload.WorkspaceID, workload.JobID)
	_ = os.MkdirAll(artifactDir, 0755)
	_ = os.WriteFile(filepath.Join(artifactDir, fmt.Sprintf("attempt-%d-output.txt", workload.AttemptNumber)), content, 0644)
	_ = os.WriteFile(filepath.Join(artifactDir, "output.txt"), content, 0644)
}
