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
