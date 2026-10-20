export interface Workspace {
  id: string
  name: string
  slug: string
  created_at: string
}

export interface Application {
  id: string
  workspace_id: string
  name: string
  slug: string
  workload_type: string
  desired_generation: number
  current_release_id: string | null
  repository_id?: string | null
  dockerfile_path?: string
  context_dir?: string
  git_branch?: string | null
  created_at: string
}

export interface Repository {
  id: string
  workspace_id: string
  name: string
  repo_url: string
  default_branch: string
  created_at: string
}

export interface Release {
  id: string
  application_id: string
  workspace_id: string
  release_number: number
  image_digest: string
  config_json: {
    port?: number
    health_path?: string
    [key: string]: unknown
  }
  status: 'REQUESTED' | 'BUILDING' | 'IMAGE_READY' | 'DEPLOYING' | 'HEALTHY' | 'BUILD_FAILED' | 'DEPLOY_FAILED' | 'SUPERSEDED'
  status_reason?: string | null
  commit_sha?: string | null
  git_ref?: string | null
  commit_message?: string | null
  build_duration_ms?: number | null
  build_logs?: string | null
  created_at: string
}

export interface DeployPayload {
  image_digest: string
  port: number
  health_path: string
}

function getHeaders(token: string, idempotencyKey?: string): HeadersInit {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
  }
  if (token.startsWith('dev:')) {
    headers['X-Dev-Subject'] = token.replace('dev:', '')
  } else {
    headers['Authorization'] = `Bearer ${token}`
  }
  if (idempotencyKey) {
    headers['Idempotency-Key'] = idempotencyKey
  }
  return headers
}

async function extractErrorMessage(res: Response, fallback: string): Promise<string> {
  try {
    const data = await res.json()
    if (typeof data === 'string') return data
    if (data && typeof data === 'object') {
      return (
        data.message ||
        data.detail ||
        (data.error && data.error.message) ||
        fallback
      )
    }
  } catch {
    // Response body not JSON
  }
  return res.statusText || fallback
}

export async function createWorkspace(token: string, name: string, slug: string): Promise<Workspace> {
  const res = await fetch('/v1/workspaces', {
    method: 'POST',
    headers: getHeaders(token),
    body: JSON.stringify({ name, slug }),
  })
  if (!res.ok) {
    const msg = await extractErrorMessage(res, 'Failed to create workspace')
    throw new Error(msg)
  }
  return res.json()
}

export async function createApplication(
  token: string,
  workspaceId: string,
  name: string,
  slug: string,
  repositoryId?: string | null,
  dockerfilePath: string = 'Dockerfile',
  contextDir: string = '.',
  gitBranch: string = 'main'
): Promise<Application> {
  const res = await fetch(`/v1/workspaces/${workspaceId}/apps`, {
    method: 'POST',
    headers: getHeaders(token),
    body: JSON.stringify({
      name,
      slug,
      repository_id: repositoryId || null,
      dockerfile_path: dockerfilePath,
      context_dir: contextDir,
      git_branch: gitBranch,
    }),
  })
  if (!res.ok) {
    const msg = await extractErrorMessage(res, 'Failed to create application')
    throw new Error(msg)
  }
  return res.json()
}

export async function listApplications(token: string, workspaceId: string): Promise<Application[]> {
  const res = await fetch(`/v1/workspaces/${workspaceId}/apps`, {
    method: 'GET',
    headers: getHeaders(token),
  })
  if (!res.ok) {
    const msg = await extractErrorMessage(res, 'Failed to list applications')
    throw new Error(msg)
  }
  const data = await res.json()
  return data.items || []
}

export async function updateApplication(
  token: string,
  appId: string,
  payload: {
    name?: string
    repository_id?: string | null
    dockerfile_path?: string
    context_dir?: string
    git_branch?: string
  }
): Promise<Application> {
  const res = await fetch(`/v1/apps/${appId}`, {
    method: 'PATCH',
    headers: getHeaders(token),
    body: JSON.stringify(payload),
  })
  if (!res.ok) {
    const msg = await extractErrorMessage(res, 'Failed to update application')
    throw new Error(msg)
  }
  return res.json()
}

export async function deployRelease(
  token: string,
  appId: string,
  payload: DeployPayload,
  idempotencyKey: string
): Promise<{ operation_id: string; status: string; status_url: string }> {
  const res = await fetch(`/v1/apps/${appId}/deployments`, {
    method: 'POST',
    headers: getHeaders(token, idempotencyKey),
    body: JSON.stringify(payload),
  })
  if (!res.ok) {
    const msg = await extractErrorMessage(res, 'Failed to deploy release')
    throw new Error(msg)
  }
  return res.json()
}

export async function listReleases(token: string, appId: string): Promise<Release[]> {
  const res = await fetch(`/v1/apps/${appId}/releases`, {
    method: 'GET',
    headers: getHeaders(token),
  })
  if (!res.ok) {
    const msg = await extractErrorMessage(res, 'Failed to list releases')
    throw new Error(msg)
  }
  const data = await res.json()
  return data.items || []
}

export async function rollbackRelease(
  token: string,
  appId: string,
  targetReleaseId: string,
  idempotencyKey: string
): Promise<{ operation_id: string; status: string; status_url: string }> {
  const res = await fetch(`/v1/apps/${appId}/rollbacks`, {
    method: 'POST',
    headers: getHeaders(token, idempotencyKey),
    body: JSON.stringify({ target_release_id: targetReleaseId }),
  })
  if (!res.ok) {
    const msg = await extractErrorMessage(res, 'Failed to rollback release')
    throw new Error(msg)
  }
  return res.json()
}

export async function connectRepository(
  token: string,
  workspaceId: string,
  name: string,
  repoUrl: string,
  webhookSecret: string,
  defaultBranch: string = 'main'
): Promise<Repository> {
  const res = await fetch(`/v1/workspaces/${workspaceId}/repositories`, {
    method: 'POST',
    headers: getHeaders(token),
    body: JSON.stringify({
      name,
      repo_url: repoUrl,
      webhook_secret: webhookSecret,
      default_branch: defaultBranch,
    }),
  })
  if (!res.ok) {
    const msg = await extractErrorMessage(res, 'Failed to connect repository')
    throw new Error(msg)
  }
  return res.json()
}

export async function listRepositories(token: string, workspaceId: string): Promise<Repository[]> {
  const res = await fetch(`/v1/workspaces/${workspaceId}/repositories`, {
    method: 'GET',
    headers: getHeaders(token),
  })
  if (!res.ok) {
    const msg = await extractErrorMessage(res, 'Failed to list repositories')
    throw new Error(msg)
  }
  const data = await res.json()
  return data.items || []
}

export async function triggerBuild(
  token: string,
  appId: string,
  commitSha?: string,
  gitRef: string = 'main',
  commitMessage?: string
): Promise<Release> {
  const res = await fetch(`/v1/apps/${appId}/builds`, {
    method: 'POST',
    headers: getHeaders(token),
    body: JSON.stringify({
      commit_sha: commitSha || null,
      git_ref: gitRef,
      commit_message: commitMessage || null,
    }),
  })
  if (!res.ok) {
    const msg = await extractErrorMessage(res, 'Failed to trigger build')
    throw new Error(msg)
  }
  return res.json()
}


export type JobState =
  | 'QUEUED'
  | 'ADMITTED'
  | 'STARTING'
  | 'RUNNING'
  | 'SUCCEEDED'
  | 'RETRY_WAIT'
  | 'FAILED'
  | 'CANCEL_REQUESTED'
  | 'CANCELLED'

export interface JobAttempt {
  attempt_number: number
  state: JobState
  resource_uid?: string | null
  lease_epoch: number
  exit_code?: number | null
  failure_reason?: string | null
  started_at: string
  finished_at?: string | null
}

export interface JobDetails {
  id: string
  workspace_id: string
  name: string
  state: JobState
  current_attempt_number: number
  attempts: JobAttempt[]
  created_at: string
}

export interface SubmitJobPayload {
  name: string
  image_digest: string
  command_args: string[]
  env_vars?: Record<string, string>
  timeout_seconds?: number
  max_retries?: number
}

export async function listWorkspaceJobs(token: string, workspaceId: string): Promise<JobDetails[]> {
  const res = await fetch(`/v1/workspaces/${workspaceId}/jobs`, {
    method: 'GET',
    headers: getHeaders(token),
  })
  if (!res.ok) {
    const msg = await extractErrorMessage(res, 'Failed to list jobs')
    throw new Error(msg)
  }
  const data = await res.json()
  return data.items || []
}

export async function submitJob(
  token: string,
  workspaceId: string,
  payload: SubmitJobPayload,
  idempotencyKey: string
): Promise<{ operation_id: string; status: string; status_url: string }> {
  const res = await fetch(`/v1/workspaces/${workspaceId}/jobs`, {
    method: 'POST',
    headers: getHeaders(token, idempotencyKey),
    body: JSON.stringify(payload),
  })
  if (!res.ok) {
    const msg = await extractErrorMessage(res, 'Failed to submit job')
    throw new Error(msg)
  }
  return res.json()
}

export async function cancelJob(
  token: string,
  jobId: string,
  idempotencyKey: string
): Promise<{ operation_id: string; status: string; status_url: string }> {
  const res = await fetch(`/v1/jobs/${jobId}/cancel`, {
    method: 'POST',
    headers: getHeaders(token, idempotencyKey),
    body: JSON.stringify({}),
  })
  if (!res.ok) {
    const msg = await extractErrorMessage(res, 'Failed to cancel job')
    throw new Error(msg)
  }
  return res.json()
}

export async function getJobDetails(token: string, jobId: string): Promise<JobDetails> {
  const res = await fetch(`/v1/jobs/${jobId}`, {
    method: 'GET',
    headers: getHeaders(token),
  })
  if (!res.ok) {
    const msg = await extractErrorMessage(res, 'Failed to get job details')
    throw new Error(msg)
  }
  return res.json()
}

export async function downloadJobOutput(token: string, jobId: string): Promise<string> {
  const res = await fetch(`/v1/jobs/${jobId}/output`, {
    method: 'GET',
    headers: getHeaders(token),
  })
  if (!res.ok) {
    const msg = await extractErrorMessage(res, 'Failed to download job output')
    throw new Error(msg)
  }
  return res.text()
}

export async function rerunJob(
  token: string,
  jobId: string,
  idempotencyKey: string
): Promise<{ operation_id: string; status: string; status_url: string }> {
  const res = await fetch(`/v1/jobs/${jobId}/reruns`, {
    method: 'POST',
    headers: getHeaders(token, idempotencyKey),
  })
  if (!res.ok) {
    const msg = await extractErrorMessage(res, 'Failed to rerun job')
    throw new Error(msg)
  }
  return res.json()
}

export interface DeadLetterRecord {
  id: string
  job_id: string
  workspace_id: string
  job_name: string
  last_attempt: number
  exit_code: number | null
  failure_reason: string | null
  created_at: string
}

export interface DeadLetterRecordListResponse {
  items: DeadLetterRecord[]
  next_cursor: string | null
}

export async function listWorkspaceDeadLetterRecords(
  token: string,
  workspaceId: string,
  cursor?: string
): Promise<DeadLetterRecordListResponse> {
  const url = cursor
    ? `/v1/workspaces/${workspaceId}/dead-letter-records?cursor=${encodeURIComponent(cursor)}`
    : `/v1/workspaces/${workspaceId}/dead-letter-records`
  const res = await fetch(url, {
    method: 'GET',
    headers: getHeaders(token),
  })
  if (!res.ok) {
    const msg = await extractErrorMessage(res, 'Failed to list dead-letter records')
    throw new Error(msg)
  }
  return res.json()
}

export type WorkspaceRole = 'OWNER' | 'ADMIN' | 'DEVELOPER' | 'VIEWER'

export interface WorkspaceMember {
  id: string
  workspace_id: string
  user_subject: string
  role: WorkspaceRole
  created_at: string
  updated_at: string
}

export interface SecretMetadata {
  id: string
  workspace_id: string
  name: string
  key_version: number
  created_by: string
  created_at: string
  updated_at: string
}

export interface QuotaUsageItem {
  resource_class: string
  limit: number
  used: number
  remaining: number
}

export interface QuotaReservationItem {
  id: string
  resource_class: string
  units: number
  status: string
  expires_at: string
  created_at: string
}

export interface WorkspaceQuotas {
  workspace_id: string
  quotas: QuotaUsageItem[]
  active_reservations: QuotaReservationItem[]
}

export interface AuditEventRecord {
  id: string
  workspace_id: string
  actor_subject: string
  action: string
  target_type: string
  target_id: string
  details?: Record<string, unknown> | null
  created_at: string
}

export async function listWorkspaceMembers(token: string, workspaceId: string): Promise<WorkspaceMember[]> {
  const res = await fetch(`/v1/workspaces/${workspaceId}/members`, {
    method: 'GET',
    headers: getHeaders(token),
  })
  if (!res.ok) {
    const msg = await extractErrorMessage(res, 'Failed to list workspace members')
    throw new Error(msg)
  }
  const data = await res.json()
  return data.items || []
}

export async function upsertWorkspaceMember(
  token: string,
  workspaceId: string,
  userSubject: string,
  role: WorkspaceRole
): Promise<WorkspaceMember> {
  const res = await fetch(`/v1/workspaces/${workspaceId}/members`, {
    method: 'POST',
    headers: getHeaders(token),
    body: JSON.stringify({ user_subject: userSubject, role }),
  })
  if (!res.ok) {
    const msg = await extractErrorMessage(res, 'Failed to update workspace member')
    throw new Error(msg)
  }
  return res.json()
}

export async function removeWorkspaceMember(
  token: string,
  workspaceId: string,
  userSubject: string
): Promise<void> {
  const res = await fetch(`/v1/workspaces/${workspaceId}/members/${encodeURIComponent(userSubject)}`, {
    method: 'DELETE',
    headers: getHeaders(token),
  })
  if (!res.ok) {
    const msg = await extractErrorMessage(res, 'Failed to remove workspace member')
    throw new Error(msg)
  }
}

export async function listWorkspaceSecrets(token: string, workspaceId: string): Promise<SecretMetadata[]> {
  const res = await fetch(`/v1/workspaces/${workspaceId}/secrets`, {
    method: 'GET',
    headers: getHeaders(token),
  })
  if (!res.ok) {
    const msg = await extractErrorMessage(res, 'Failed to list secrets')
    throw new Error(msg)
  }
  const data = await res.json()
  return data.items || []
}

export async function upsertWorkspaceSecret(
  token: string,
  workspaceId: string,
  name: string,
  value: string
): Promise<SecretMetadata> {
  const res = await fetch(`/v1/workspaces/${workspaceId}/secrets`, {
    method: 'POST',
    headers: getHeaders(token),
    body: JSON.stringify({ name, value }),
  })
  if (!res.ok) {
    const msg = await extractErrorMessage(res, 'Failed to store secret')
    throw new Error(msg)
  }
  return res.json()
}

export async function deleteWorkspaceSecret(
  token: string,
  workspaceId: string,
  secretName: string
): Promise<void> {
  const res = await fetch(`/v1/workspaces/${workspaceId}/secrets/${encodeURIComponent(secretName)}`, {
    method: 'DELETE',
    headers: getHeaders(token),
  })
  if (!res.ok) {
    const msg = await extractErrorMessage(res, 'Failed to delete secret')
    throw new Error(msg)
  }
}

export function mapWorkspaceQuotasResponse(raw: any): WorkspaceQuotas {
  if (raw && !Array.isArray(raw.quotas) && typeof raw.concurrent_jobs_limit === 'number') {
    const quotas: QuotaUsageItem[] = [
      {
        resource_class: 'CONCURRENT_JOB',
        limit: raw.concurrent_jobs_limit,
        used: raw.concurrent_jobs_used,
        remaining: Math.max(0, raw.concurrent_jobs_limit - raw.concurrent_jobs_used),
      },
      {
        resource_class: 'CONCURRENT_BUILD',
        limit: raw.concurrent_builds_limit,
        used: raw.concurrent_builds_used,
        remaining: Math.max(0, raw.concurrent_builds_limit - raw.concurrent_builds_used),
      },
      {
        resource_class: 'DEPLOYED_SERVICE',
        limit: raw.deployed_services_limit,
        used: raw.deployed_services_used,
        remaining: Math.max(0, raw.deployed_services_limit - raw.deployed_services_used),
      },
      {
        resource_class: 'PENDING_OPERATIONS',
        limit: raw.pending_operations_limit,
        used: raw.pending_operations_used,
        remaining: Math.max(0, raw.pending_operations_limit - raw.pending_operations_used),
      },
    ]
    return {
      workspace_id: raw.workspace_id,
      quotas,
      active_reservations: raw.reservations || raw.active_reservations || [],
    }
  }
  return raw as WorkspaceQuotas
}

export async function getWorkspaceQuotas(token: string, workspaceId: string): Promise<WorkspaceQuotas> {
  const res = await fetch(`/v1/workspaces/${workspaceId}/quotas`, {
    method: 'GET',
    headers: getHeaders(token),
  })
  if (!res.ok) {
    const msg = await extractErrorMessage(res, 'Failed to fetch workspace quotas')
    throw new Error(msg)
  }
  const raw = await res.json()
  return mapWorkspaceQuotasResponse(raw)
}

export async function listWorkspaceAuditEvents(token: string, workspaceId: string): Promise<AuditEventRecord[]> {
  const res = await fetch(`/v1/workspaces/${workspaceId}/audit?limit=50`, {
    method: 'GET',
    headers: getHeaders(token),
  })
  if (!res.ok) {
    const msg = await extractErrorMessage(res, 'Failed to export audit logs')
    throw new Error(msg)
  }
  const data = await res.json()
  return data.items || []
}

export interface WorkspaceTelemetry {
  workspace_id: string
  requests_total: number
  status_2xx: number
  status_4xx: number
  status_5xx: number
  error_rate_5xx: number
  latency_p50_ms: number
  latency_p95_ms: number
  latency_p99_ms: number
  outbox_pending_total: number
  active_leases_total: number
  queued_jobs_total: number
  oldest_queued_job_age_seconds: number
  running_jobs_total: number
  healthy_services_total: number
  job_retry_ratio: number
  dead_letter_total: number
  collected_at: string
}

export async function getWorkspaceTelemetry(token: string, workspaceId: string): Promise<WorkspaceTelemetry> {
  const res = await fetch(`/v1/workspaces/${workspaceId}/telemetry`, {
    method: 'GET',
    headers: getHeaders(token),
  })
  if (!res.ok) {
    const msg = await extractErrorMessage(res, 'Failed to fetch workspace telemetry')
    throw new Error(msg)
  }
  return res.json()
}
