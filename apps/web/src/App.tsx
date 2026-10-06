import { useState, useEffect } from 'react'
import type { Workspace, Application, Release, JobDetails, Repository, DeadLetterRecord } from './api'
import {
  createWorkspace,
  createApplication,
  updateApplication,
  listApplications,
  deployRelease,
  listReleases,
  rollbackRelease,
  connectRepository,
  listRepositories,
  triggerBuild,
  listWorkspaceJobs,
  submitJob,
  cancelJob,
  downloadJobOutput,
  rerunJob,
  listWorkspaceDeadLetterRecords,
} from './api'
import './App.css'

export default function App() {
  const [token, setToken] = useState('dev:test_user_alice')
  const [activeWorkspace, setActiveWorkspace] = useState<Workspace | null>(null)
  const [wsName, setWsName] = useState('')
  const [wsSlug, setWsSlug] = useState('')
  const [showNewWsForm, setShowNewWsForm] = useState(false)

  const [activeTab, setActiveTab] = useState<'services' | 'jobs' | 'dlq'>('services')

  // Services state
  const [apps, setApps] = useState<Application[]>([])
  const [selectedApp, setSelectedApp] = useState<Application | null>(null)
  const [showNewAppForm, setShowNewAppForm] = useState(false)
  const [newAppName, setNewAppName] = useState('Live HTTP Gateway')
  const [newAppSlug, setNewAppSlug] = useState('http-gateway')

  // Repositories & Build state (Milestone M3)
  const [repos, setRepos] = useState<Repository[]>([])
  const [showConnectRepoForm, setShowConnectRepoForm] = useState(false)
  const [newRepoName, setNewRepoName] = useState('demo-service')
  const [newRepoUrl, setNewRepoUrl] = useState('https://github.com/hamicloud/demo-service')
  const [newRepoSecret, setNewRepoSecret] = useState('supersecret-webhook-token')
  const [newRepoBranch, setNewRepoBranch] = useState('main')
  const [isConnectingRepo, setIsConnectingRepo] = useState(false)

  const [buildGitBranch, setBuildGitBranch] = useState('main')
  const [buildCommitSha, setBuildCommitSha] = useState('')
  const [buildCommitMsg, setBuildCommitMsg] = useState('')
  const [isTriggeringBuild, setIsTriggeringBuild] = useState(false)

  const [imageDigest, setImageDigest] = useState('docker.io/library/nginx:1.27-alpine')
  const [servicePort, setServicePort] = useState(8080)
  const [healthPath, setHealthPath] = useState('/healthz')
  const [releases, setReleases] = useState<Release[]>([])
  const [isDeploying, setIsDeploying] = useState(false)

  // Jobs state
  const [jobs, setJobs] = useState<JobDetails[]>([])
  const [selectedJob, setSelectedJob] = useState<JobDetails | null>(null)
  const [showNewJobForm, setShowNewJobForm] = useState(false)
  const [jobName, setJobName] = useState('batch-data-sync')
  const [jobImage, setJobImage] = useState('docker.io/library/python:3.12-alpine')
  const [jobCommand, setJobCommand] = useState('python -c "print(\'Job completed successfully\')"')
  const [jobTimeout, setJobTimeout] = useState(60)
  const [jobMaxRetries, setJobMaxRetries] = useState(2)
  const [isSubmittingJob, setIsSubmittingJob] = useState(false)
  const [jobOutputView, setJobOutputView] = useState<string | null>(null)
  const [isRerunningJob, setIsRerunningJob] = useState(false)

  // DLQ records state
  const [deadLetterRecords, setDeadLetterRecords] = useState<DeadLetterRecord[]>([])
  const [selectedDlqRecord, setSelectedDlqRecord] = useState<DeadLetterRecord | null>(null)

  // Filters & Notifications
  const [filterQuery, setFilterQuery] = useState('')
  const [errorMsg, setErrorMsg] = useState<string | null>(null)
  const [noticeMsg, setNoticeMsg] = useState<string | null>(null)

  // Fetch applications when active workspace changes
  useEffect(() => {
    if (!activeWorkspace) return
    let ignore = false
    listApplications(token, activeWorkspace.id)
      .then((data) => {
        if (!ignore) {
          setApps(data)
          setSelectedApp(data.length > 0 ? data[0] : null)
        }
      })
      .catch((err: unknown) => {
        if (!ignore) setErrorMsg((err as Error).message)
      })
    return () => {
      ignore = true
    }
  }, [activeWorkspace, token])

  // Fetch repositories when active workspace changes
  useEffect(() => {
    if (!activeWorkspace) return
    let ignore = false
    listRepositories(token, activeWorkspace.id)
      .then((data) => {
        if (!ignore) setRepos(data)
      })
      .catch((err: unknown) => {
        if (!ignore) setErrorMsg((err as Error).message)
      })
    return () => {
      ignore = true
    }
  }, [activeWorkspace, token])

  // Poll releases when selected application changes
  useEffect(() => {
    if (!selectedApp) return
    let ignore = false
    const poll = () => {
      listReleases(token, selectedApp.id)
        .then((data) => {
          if (!ignore) setReleases(data)
        })
        .catch((err: unknown) => {
          if (!ignore) setErrorMsg((err as Error).message)
        })
    }
    poll()
    const interval = setInterval(poll, 2000)
    return () => {
      ignore = true
      clearInterval(interval)
    }
  }, [selectedApp, token])

  // Poll jobs when active workspace changes and jobs tab is active
  useEffect(() => {
    if (!activeWorkspace || (activeTab !== 'jobs' && activeTab !== 'dlq')) return
    let ignore = false
    const pollJobs = () => {
      listWorkspaceJobs(token, activeWorkspace.id)
        .then((data) => {
          if (!ignore) {
            setJobs(data)
            setSelectedJob((prevSelected) => {
              if (prevSelected) {
                const fresh = data.find((j) => j.id === prevSelected.id)
                return fresh || prevSelected
              } else if (data.length > 0) {
                return data[0]
              }
              return null
            })
          }
        })
        .catch((err: unknown) => {
          if (!ignore) setErrorMsg((err as Error).message)
        })
    }
    pollJobs()
    const interval = setInterval(pollJobs, 2000)
    return () => {
      ignore = true
      clearInterval(interval)
    }
  }, [activeWorkspace, activeTab, token])

  // Poll dead-letter records when active workspace changes
  useEffect(() => {
    if (!activeWorkspace) return
    let ignore = false
    const pollDlq = () => {
      listWorkspaceDeadLetterRecords(token, activeWorkspace.id)
        .then((data) => {
          if (!ignore) {
            setDeadLetterRecords(data.items || [])
            setSelectedDlqRecord((prevSelected) => {
              if (prevSelected) {
                const fresh = data.items?.find((r) => r.id === prevSelected.id)
                return fresh || prevSelected
              } else if (data.items && data.items.length > 0) {
                return data.items[0]
              }
              return null
            })
          }
        })
        .catch((err: unknown) => {
          if (!ignore) setErrorMsg((err as Error).message)
        })
    }
    pollDlq()
    const interval = setInterval(pollDlq, 2000)
    return () => {
      ignore = true
      clearInterval(interval)
    }
  }, [activeWorkspace, token])

  const manualRefreshReleases = async () => {
    if (!selectedApp) return
    try {
      const data = await listReleases(token, selectedApp.id)
      setReleases(data)
      setNoticeMsg('Releases refreshed.')
    } catch (err: unknown) {
      setErrorMsg((err as Error).message)
    }
  }

  const manualRefreshJobs = async () => {
    if (!activeWorkspace) return
    try {
      const data = await listWorkspaceJobs(token, activeWorkspace.id)
      setJobs(data)
      if (selectedJob) {
        const fresh = data.find((j) => j.id === selectedJob.id)
        if (fresh) setSelectedJob(fresh)
      }
      const dlqData = await listWorkspaceDeadLetterRecords(token, activeWorkspace.id)
      setDeadLetterRecords(dlqData.items || [])
      if (selectedDlqRecord) {
        const freshDlq = dlqData.items?.find((r) => r.id === selectedDlqRecord.id)
        if (freshDlq) setSelectedDlqRecord(freshDlq)
      }
      setNoticeMsg('Jobs and DLQ refreshed.')
    } catch (err: unknown) {
      setErrorMsg((err as Error).message)
    }
  }

  const handleCreateWorkspace = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!wsName.trim() || !wsSlug.trim()) return
    setErrorMsg(null)
    try {
      const ws = await createWorkspace(token, wsName, wsSlug)
      setActiveWorkspace(ws)
      setApps([])
      setSelectedApp(null)
      setReleases([])
      setJobs([])
      setSelectedJob(null)
      setShowNewWsForm(false)
      setWsName('')
      setWsSlug('')
      setNoticeMsg(`Workspace '${ws.name}' created successfully.`)
    } catch (err: unknown) {
      setErrorMsg((err as Error).message)
    }
  }

  const handleCreateApplication = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!activeWorkspace || !newAppName.trim() || !newAppSlug.trim()) return
    setErrorMsg(null)
    try {
      const app = await createApplication(token, activeWorkspace.id, newAppName, newAppSlug)
      setApps((prev) => [app, ...prev])
      setSelectedApp(app)
      setReleases([])
      setShowNewAppForm(false)
      setNoticeMsg(`Application '${app.name}' initialized.`)
    } catch (err: unknown) {
      setErrorMsg((err as Error).message)
    }
  }

  const handleDeployRelease = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!selectedApp) return
    setErrorMsg(null)
    setIsDeploying(true)
    try {
      const idempKey = `deploy-${crypto.randomUUID()}`
      await deployRelease(
        token,
        selectedApp.id,
        {
          image_digest: imageDigest,
          port: Number(servicePort),
          health_path: healthPath,
        },
        idempKey
      )
      setNoticeMsg('Deployment submitted! Active reconciler probing readiness.')
      await manualRefreshReleases()
    } catch (err: unknown) {
      setErrorMsg((err as Error).message)
    } finally {
      setIsDeploying(false)
    }
  }

  const handleRollbackRelease = async (targetReleaseId: string) => {
    if (!selectedApp) return
    setErrorMsg(null)
    try {
      const idempKey = `rollback-${crypto.randomUUID()}`
      await rollbackRelease(token, selectedApp.id, targetReleaseId, idempKey)
      setNoticeMsg('Rollback initiated! Reverting application configuration.')
      await manualRefreshReleases()
    } catch (err: unknown) {
      setErrorMsg((err as Error).message)
    }
  }

  const handleConnectRepository = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!activeWorkspace || !newRepoName.trim() || !newRepoUrl.trim()) return
    setErrorMsg(null)
    setIsConnectingRepo(true)
    try {
      const repo = await connectRepository(
        token,
        activeWorkspace.id,
        newRepoName.trim(),
        newRepoUrl.trim(),
        newRepoSecret.trim() || 'default-secret',
        newRepoBranch.trim() || 'main'
      )
      setRepos((prev) => [repo, ...prev])
      setShowConnectRepoForm(false)
      setNoticeMsg(`Repository '${repo.name}' connected. Webhook active at /v1/webhooks/github/${repo.id}`)

      if (selectedApp && !selectedApp.repository_id) {
        const updated = await updateApplication(token, selectedApp.id, { repository_id: repo.id })
        setSelectedApp(updated)
        setApps((prev) => prev.map((a) => (a.id === updated.id ? updated : a)))
      }
    } catch (err: unknown) {
      setErrorMsg((err as Error).message)
    } finally {
      setIsConnectingRepo(false)
    }
  }

  const handleLinkRepository = async (repoId: string) => {
    if (!selectedApp) return
    setErrorMsg(null)
    try {
      const updated = await updateApplication(token, selectedApp.id, { repository_id: repoId })
      setSelectedApp(updated)
      setApps((prev) => prev.map((a) => (a.id === updated.id ? updated : a)))
      setNoticeMsg(`Linked repository to application '${selectedApp.name}'.`)
    } catch (err: unknown) {
      setErrorMsg((err as Error).message)
    }
  }

  const handleTriggerBuild = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!selectedApp) return
    setErrorMsg(null)
    setIsTriggeringBuild(true)
    try {
      const rel = await triggerBuild(
        token,
        selectedApp.id,
        buildCommitSha.trim() || undefined,
        buildGitBranch.trim() || 'main',
        buildCommitMsg.trim() || undefined
      )
      setNoticeMsg(`Build triggered for release #${rel.release_number}. Dispatching to BuildKit worker.`)
      setBuildCommitSha('')
      setBuildCommitMsg('')
      await manualRefreshReleases()
    } catch (err: unknown) {
      setErrorMsg((err as Error).message)
    } finally {
      setIsTriggeringBuild(false)
    }
  }


  const handleSubmitJob = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!activeWorkspace) return
    setErrorMsg(null)
    setIsSubmittingJob(true)
    try {
      const idempKey = `job-sub-${crypto.randomUUID()}`
      const args = jobCommand.trim() ? jobCommand.trim().split(' ') : []
      await submitJob(
        token,
        activeWorkspace.id,
        {
          name: jobName,
          image_digest: jobImage,
          command_args: args,
          timeout_seconds: Number(jobTimeout),
          max_retries: Number(jobMaxRetries),
        },
        idempKey
      )
      setShowNewJobForm(false)
      setNoticeMsg(`Job '${jobName}' submitted. Go scheduler will admit intent.`)
      await manualRefreshJobs()
    } catch (err: unknown) {
      setErrorMsg((err as Error).message)
    } finally {
      setIsSubmittingJob(false)
    }
  }

  const handleCancelJob = async (jobId: string) => {
    setErrorMsg(null)
    try {
      const idempKey = `job-cancel-${crypto.randomUUID()}`
      await cancelJob(token, jobId, idempKey)
      setNoticeMsg('Cancellation signal recorded. Worker aborting execution.')
      await manualRefreshJobs()
    } catch (err: unknown) {
      setErrorMsg((err as Error).message)
    }
  }

  const handleDownloadJobOutput = async (jobId: string) => {
    setErrorMsg(null)
    try {
      const text = await downloadJobOutput(token, jobId)
      setJobOutputView(text)
      const blob = new Blob([text], { type: 'text/plain;charset=utf-8' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `job-${jobId.slice(0, 8)}-output.txt`
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      URL.revokeObjectURL(url)
    } catch (err: unknown) {
      setErrorMsg((err as Error).message)
    }
  }

  const handleRerunJob = async (jobId: string, customKey?: string) => {
    if (!activeWorkspace) return
    setIsRerunningJob(true)
    setErrorMsg(null)
    try {
      const key = customKey || `idemp-rerun-${Date.now()}`
      const res = await rerunJob(token, jobId, key)
      setNoticeMsg(`Job rerun dispatched: Operation ${res.operation_id.slice(0, 8)}`)
      const freshJobs = await listWorkspaceJobs(token, activeWorkspace.id)
      setJobs(freshJobs)
      const freshDlq = await listWorkspaceDeadLetterRecords(token, activeWorkspace.id)
      setDeadLetterRecords(freshDlq.items || [])
      const newJob = freshJobs.find((j) => j.id === res.operation_id)
      if (newJob) {
        setSelectedJob(newJob)
      }
    } catch (err: unknown) {
      setErrorMsg((err as Error).message)
    } finally {
      setIsRerunningJob(false)
    }
  }

  const filteredApps = apps.filter(
    (a) =>
      a.name.toLowerCase().includes(filterQuery.toLowerCase()) ||
      a.slug.toLowerCase().includes(filterQuery.toLowerCase())
  )

  const filteredJobs = jobs.filter(
    (j) =>
      j.name.toLowerCase().includes(filterQuery.toLowerCase()) ||
      j.id.toLowerCase().includes(filterQuery.toLowerCase())
  )

  const filteredDlqRecords = deadLetterRecords.filter(
    (r) =>
      r.job_name.toLowerCase().includes(filterQuery.toLowerCase()) ||
      r.job_id.toLowerCase().includes(filterQuery.toLowerCase()) ||
      r.id.toLowerCase().includes(filterQuery.toLowerCase()) ||
      (r.failure_reason && r.failure_reason.toLowerCase().includes(filterQuery.toLowerCase()))
  )

  return (
    <div className="dashboard-root">
      {/* 1. Header Navigation Bar */}
      <header className="navbar-clean">
        <div className="navbar-brand-area">
          <a href="/" className="brand-emblem">
            <svg
              className="brand-logo-svg"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth={2}
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="M3 15a4 4 0 004 4h9a5 5 0 10-.1-9.999 5.002 5.002 0 00-9.78 2.096A4.001 4.001 0 003 15z"
              />
            </svg>
            <span>HamiCloud</span>
          </a>
          <span className="platform-badge">M1 & M2 Live Control Plane</span>
        </div>

        <div className="navbar-user-area">
          <div className="auth-token-box">
            <span className="auth-token-label">Actor:</span>
            <select
              className="auth-user-dropdown"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              aria-label="Select Authentication Actor"
            >
              <option value="dev:test_user_alice">Alice (Owner / Admin)</option>
              <option value="dev:test_user_bob">Bob (Engineer / Member)</option>
              <option value="dev:ci_bot">CI Automation Bot</option>
            </select>
          </div>
        </div>
      </header>

      {/* 2. Global Feedback Notifications */}
      {errorMsg && (
        <div className="feedback-banner banner-error" role="alert">
          <div>
            <strong>Error: </strong>
            <span>{errorMsg}</span>
          </div>
          <button className="banner-close-btn" onClick={() => setErrorMsg(null)}>
            ✕
          </button>
        </div>
      )}
      {noticeMsg && (
        <div className="feedback-banner banner-notice" role="status">
          <div>
            <strong>Success: </strong>
            <span>{noticeMsg}</span>
          </div>
          <button className="banner-close-btn" onClick={() => setNoticeMsg(null)}>
            ✕
          </button>
        </div>
      )}

      {/* 3. Workspace Overview Ribbon */}
      <section className="workspace-banner">
        <div className="workspace-left-meta">
          <div className="workspace-logo-cube">
            {activeWorkspace ? activeWorkspace.name.charAt(0).toUpperCase() : 'W'}
          </div>
          <div className="workspace-text-group">
            <div className="workspace-heading-line">
              <span className="workspace-name-text">
                {activeWorkspace ? activeWorkspace.name : 'No Workspace Selected'}
              </span>
              {activeWorkspace && (
                <span className="workspace-slug-badge">{activeWorkspace.slug}</span>
              )}
            </div>
            {activeWorkspace && (
              <span className="workspace-id-muted">ID: {activeWorkspace.id}</span>
            )}
          </div>
        </div>

        <div className="workspace-metrics-bar">
          <div className="stat-metric">
            <span className="stat-label">Services</span>
            <span className="stat-value">{apps.length}</span>
          </div>
          <div className="stat-metric">
            <span className="stat-label">Batch Jobs</span>
            <span className="stat-value">{jobs.length}</span>
          </div>
          <div className="stat-metric">
            <span className="stat-label">DLQ Records</span>
            <span className="stat-value" style={deadLetterRecords.length > 0 ? { color: '#be123c' } : {}}>
              {deadLetterRecords.length}
            </span>
          </div>
          <button
            className="btn-action-secondary"
            onClick={() => setShowNewWsForm(!showNewWsForm)}
          >
            {showNewWsForm ? 'Cancel' : '+ New Workspace'}
          </button>
        </div>
      </section>

      {/* Workspace Creation Collapsible Form */}
      {showNewWsForm && (
        <div className="content-card" style={{ margin: '1rem 2rem 0' }}>
          <h4>Create New Isolated Workspace</h4>
          <form onSubmit={handleCreateWorkspace} className="form-grid-layout">
            <div className="input-field-group">
              <label className="input-label-clean">Workspace Name</label>
              <input
                className="input-control-clean"
                type="text"
                value={wsName}
                onChange={(e) => setWsName(e.target.value)}
                placeholder="e.g. Analytics Platform"
                required
              />
            </div>
            <div className="input-field-group">
              <label className="input-label-clean">Slug (DNS compliant)</label>
              <input
                className="input-control-clean"
                type="text"
                value={wsSlug}
                onChange={(e) => setWsSlug(e.target.value)}
                placeholder="e.g. analytics-prod"
                required
              />
            </div>
            <div style={{ display: 'flex', alignItems: 'flex-end' }}>
              <button type="submit" className="btn-action-primary">
                Confirm & Create
              </button>
            </div>
          </form>
        </div>
      )}

      {/* 4. Tab Navigation Strip */}
      <nav className="tab-strip-container" aria-label="Workload Category">
        <div className="tab-buttons-group">
          <button
            className={`tab-nav-item ${activeTab === 'services' ? 'active' : ''}`}
            onClick={() => setActiveTab('services')}
          >
            <span>HTTP Services & Deployments</span>
            <span className="tab-pill-count">{apps.length}</span>
          </button>
          <button
            className={`tab-nav-item ${activeTab === 'jobs' ? 'active' : ''}`}
            onClick={() => setActiveTab('jobs')}
          >
            <span>Finite Jobs & Batch Tasks</span>
            <span className="tab-pill-count">{jobs.length}</span>
          </button>
          <button
            className={`tab-nav-item ${activeTab === 'dlq' ? 'active' : ''}`}
            onClick={() => {
              setActiveTab('dlq')
              if (deadLetterRecords.length > 0 && !selectedDlqRecord) {
                setSelectedDlqRecord(deadLetterRecords[0])
              }
            }}
          >
            <span>Dead-Letter Queue (DLQ)</span>
            <span className={`tab-pill-count ${deadLetterRecords.length > 0 ? 'dlq-badge' : ''}`}>
              {deadLetterRecords.length}
            </span>
          </button>
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
          <input
            type="search"
            className="input-control-clean"
            style={{ width: '220px', padding: '0.4rem 0.75rem' }}
            placeholder="Filter list..."
            value={filterQuery}
            onChange={(e) => setFilterQuery(e.target.value)}
          />
        </div>
      </nav>

      {/* 5. Main Content Two-Column Grid */}
      <main className="dashboard-main-grid">
        {/* Left Master Sidebar */}
        <aside className="sidebar-panel">
          <div className="panel-header-box">
            <span className="panel-title">
              {activeTab === 'services'
                ? 'Applications'
                : activeTab === 'jobs'
                  ? 'Submitted Jobs'
                  : 'Dead-Letter Workloads'}
            </span>
            {activeTab === 'services' ? (
              <button
                className="btn-action-primary"
                style={{ padding: '0.35rem 0.75rem', fontSize: '0.8rem' }}
                onClick={() => setShowNewAppForm(!showNewAppForm)}
              >
                + New App
              </button>
            ) : activeTab === 'jobs' ? (
              <button
                className="btn-action-primary"
                style={{ padding: '0.35rem 0.75rem', fontSize: '0.8rem' }}
                onClick={() => setShowNewJobForm(!showNewJobForm)}
              >
                + Submit Job
              </button>
            ) : (
              <span className="dlq-tag">
                {deadLetterRecords.length} Quarantined
              </span>
            )}
          </div>

          {/* Quick Create App Form */}
          {activeTab === 'services' && showNewAppForm && (
            <div style={{ padding: '1rem', borderBottom: '1px solid var(--border-light)' }}>
              <form onSubmit={handleCreateApplication} style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
                <input
                  className="input-control-clean"
                  type="text"
                  value={newAppName}
                  onChange={(e) => setNewAppName(e.target.value)}
                  placeholder="Application Name"
                  required
                />
                <input
                  className="input-control-clean"
                  type="text"
                  value={newAppSlug}
                  onChange={(e) => setNewAppSlug(e.target.value)}
                  placeholder="Slug (e.g. web-frontend)"
                  required
                />
                <button type="submit" className="btn-action-primary" style={{ marginTop: '0.25rem' }}>
                  Save Application
                </button>
              </form>
            </div>
          )}

          {/* Quick Submit Job Form */}
          {activeTab === 'jobs' && showNewJobForm && (
            <div style={{ padding: '1rem', borderBottom: '1px solid var(--border-light)' }}>
              <form onSubmit={handleSubmitJob} style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
                <input
                  className="input-control-clean"
                  type="text"
                  value={jobName}
                  onChange={(e) => setJobName(e.target.value)}
                  placeholder="Job Name"
                  required
                />
                <input
                  className="input-control-clean"
                  type="text"
                  value={jobImage}
                  onChange={(e) => setJobImage(e.target.value)}
                  placeholder="Image Digest"
                  required
                />
                <input
                  className="input-control-clean"
                  type="text"
                  value={jobCommand}
                  onChange={(e) => setJobCommand(e.target.value)}
                  placeholder="Command Args"
                  required
                />
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '0.5rem' }}>
                  <input
                    className="input-control-clean"
                    type="number"
                    min={0}
                    max={5}
                    value={jobMaxRetries}
                    onChange={(e) => setJobMaxRetries(Number(e.target.value))}
                    placeholder="Max Retries"
                    title="Max Retries"
                    required
                  />
                  <input
                    className="input-control-clean"
                    type="number"
                    min={5}
                    max={3600}
                    value={jobTimeout}
                    onChange={(e) => setJobTimeout(Number(e.target.value))}
                    placeholder="Timeout (s)"
                    title="Timeout in seconds"
                    required
                  />
                </div>
                <button
                  type="submit"
                  className="btn-action-primary"
                  style={{ marginTop: '0.25rem' }}
                  disabled={isSubmittingJob}
                >
                  {isSubmittingJob ? 'Submitting...' : 'Dispatch Job'}
                </button>
              </form>
            </div>
          )}

          {/* List Scroll Area */}
          <div className="list-scroll-area">
            {activeTab === 'services' ? (
              filteredApps.length === 0 ? (
                <div style={{ padding: '2rem 1rem', textAlign: 'center', color: 'var(--slate-400)', fontSize: '0.875rem' }}>
                  No applications found.
                </div>
              ) : (
                filteredApps.map((app) => (
                  <button
                    key={app.id}
                    className={`sidebar-list-row ${selectedApp?.id === app.id ? 'active' : ''}`}
                    onClick={() => {
                      setSelectedApp(app)
                      setReleases([])
                    }}
                  >
                    <div className="row-title-line">
                      <span className="row-primary-name">{app.name}</span>
                      <span className="meta-tag">gen {app.desired_generation}</span>
                    </div>
                    <div className="row-sub-line">
                      <span className="row-slug-code">{app.slug}</span>
                      <span>•</span>
                      <span>{app.workload_type}</span>
                    </div>
                  </button>
                ))
              )
            ) : activeTab === 'jobs' ? (
              filteredJobs.length === 0 ? (
                <div style={{ padding: '2rem 1rem', textAlign: 'center', color: 'var(--slate-400)', fontSize: '0.875rem' }}>
                  No background jobs found.
                </div>
              ) : (
                filteredJobs.map((j) => (
                  <button
                    key={j.id}
                    className={`sidebar-list-row ${selectedJob?.id === j.id ? 'active' : ''}`}
                    onClick={() => {
                      setSelectedJob(j)
                      setJobOutputView(null)
                    }}
                  >
                    <div className="row-title-line">
                      <span className="row-primary-name">{j.name}</span>
                      <span className={`status-pill-clean ${j.state.toLowerCase()}`}>
                        {j.state}
                      </span>
                    </div>
                    <div className="row-sub-line">
                      <span className="row-slug-code">#{j.current_attempt_number}</span>
                      <span>•</span>
                      <span className="row-slug-code">{j.id.slice(0, 8)}</span>
                    </div>
                  </button>
                ))
              )
            ) : filteredDlqRecords.length === 0 ? (
              <div style={{ padding: '2rem 1rem', textAlign: 'center', color: 'var(--slate-400)', fontSize: '0.875rem' }}>
                No dead-letter workloads.
              </div>
            ) : (
              filteredDlqRecords.map((r) => (
                <button
                  key={r.id}
                  className={`sidebar-list-row ${selectedDlqRecord?.id === r.id ? 'active' : ''}`}
                  onClick={() => {
                    setSelectedDlqRecord(r)
                    const matchingJob = jobs.find((j) => j.id === r.job_id)
                    if (matchingJob) setSelectedJob(matchingJob)
                    setJobOutputView(null)
                  }}
                >
                  <div className="row-title-line">
                    <span className="row-primary-name">{r.job_name}</span>
                    <span className="status-pill-clean failed">
                      FAILED
                    </span>
                  </div>
                  <div className="row-sub-line">
                    <span className="dlq-tag">DLQ #{r.last_attempt}</span>
                    <span>•</span>
                    <span className="row-slug-code">{r.job_id.slice(0, 8)}</span>
                  </div>
                  {r.failure_reason && (
                    <div
                      style={{
                        fontSize: '0.72rem',
                        color: '#be123c',
                        marginTop: '0.2rem',
                        overflow: 'hidden',
                        textOverflow: 'ellipsis',
                        whiteSpace: 'nowrap',
                      }}
                      title={r.failure_reason}
                    >
                      ⚠️ {r.failure_reason}
                    </div>
                  )}
                </button>
              ))
            )}
          </div>
        </aside>

        {/* Right Detail Panel */}
        <section className="detail-view-panel">
          {activeTab === 'services' ? (
            !selectedApp ? (
              <div className="empty-placeholder-card">
                <span className="placeholder-icon">🚀</span>
                <span style={{ fontWeight: 600, color: 'var(--slate-800)' }}>No Application Selected</span>
                <p style={{ maxWidth: '360px', fontSize: '0.875rem' }}>
                  Select an application from the sidebar or click <strong>+ New App</strong> to deploy an approved container workload.
                </p>
              </div>
            ) : (
              <>
                {/* Application Header Card */}
                <div className="content-card">
                  <div className="card-top-row">
                    <div className="detail-header-meta">
                      <h2 className="detail-headline">{selectedApp.name}</h2>
                      <div className="detail-tags-row">
                        <span className="meta-tag">slug: {selectedApp.slug}</span>
                        <span className="meta-tag">id: {selectedApp.id}</span>
                        <span className="meta-tag">generation: {selectedApp.desired_generation}</span>
                        <span className="meta-tag">type: {selectedApp.workload_type}</span>
                      </div>
                    </div>
                    <div>
                      <button className="btn-action-secondary" onClick={manualRefreshReleases}>
                        ↻ Refresh Status
                      </button>
                    </div>
                  </div>

                  {/* Live Ingress URL or Readiness Failure Section */}
                  {releases.length > 0 && (() => {
                    const latest = releases[0]
                    const runtimeUrl = (latest.config_json as Record<string, unknown>)?.ingress_url as string | undefined
                    const isHealthy = latest.status === 'HEALTHY'
                    const isFailed = latest.status === 'DEPLOY_FAILED' || latest.status === 'BUILD_FAILED'

                    if (isHealthy && runtimeUrl) {
                      return (
                        <div className="live-ingress-box">
                          <div className="ingress-meta-block">
                            <span className="ingress-tag-line">
                              <span className="ingress-dot-pulse" /> Live HTTP Ingress (Active)
                            </span>
                            <a
                              href={runtimeUrl}
                              target="_blank"
                              rel="noreferrer"
                              className="ingress-url-link"
                            >
                              {runtimeUrl} ↗
                            </a>
                          </div>
                          <div>
                            <a
                              href={runtimeUrl}
                              target="_blank"
                              rel="noreferrer"
                              className="btn-action-primary"
                            >
                              Open Service In Browser
                            </a>
                          </div>
                        </div>
                      )
                    }

                    if (isFailed) {
                      return (
                        <div className="readiness-failure-box" role="alert">
                          <div className="failure-title-row">
                            <span>⚠️ Readiness Probe Failure (Milestone M1 Validation)</span>
                          </div>
                          <p className="failure-description-text">
                            The reconciler probe for release #{latest.release_number} failed to satisfy readiness requirements.
                            The incident has been logged and the previous serving generation remains protected.
                          </p>
                          <div className="failure-diagnostics-pre">
                            Diagnostics: {latest.status_reason || 'HTTP connection refused or non-2xx status returned'}
                          </div>
                        </div>
                      )
                    }

                    return (
                      <div className="live-ingress-box" style={{ background: 'var(--blue-50)', borderColor: 'var(--blue-200)' }}>
                        <span style={{ color: 'var(--blue-700)', fontWeight: 600 }}>
                          ⏳ Reconciler active: Waiting for deployment rollout & readiness probe...
                        </span>
                      </div>
                    )
                  })()}
                </div>

                {/* Milestone M3: Source-to-URL Git Repository & Build Card */}
                <div className="content-card">
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                    <h3 style={{ fontSize: '1rem', fontWeight: 700, color: 'var(--slate-800)', margin: 0 }}>
                      Source Code & Automated Builds
                    </h3>
                    <div style={{ display: 'flex', gap: '0.5rem' }}>
                      <button
                        className="btn-action-subtle"
                        onClick={() => setShowConnectRepoForm(!showConnectRepoForm)}
                      >
                        {showConnectRepoForm ? 'Cancel' : '+ Connect Repository'}
                      </button>
                    </div>
                  </div>

                  {/* Connect Repository Form Modal/Block */}
                  {showConnectRepoForm && (
                    <div className="repo-card-box" style={{ background: 'var(--color-white)' }}>
                      <span style={{ fontWeight: 600, fontSize: '0.85rem', color: 'var(--slate-800)' }}>
                        Connect Git Repository to Workspace
                      </span>
                      <form onSubmit={handleConnectRepository} className="form-grid-layout" style={{ marginTop: '0.5rem' }}>
                        <div className="input-field-group">
                          <label className="input-label-clean">Repository Name</label>
                          <input
                            className="input-control-clean"
                            value={newRepoName}
                            onChange={(e) => setNewRepoName(e.target.value)}
                            placeholder="e.g. backend-api"
                            required
                          />
                        </div>
                        <div className="input-field-group">
                          <label className="input-label-clean">Git Remote URL</label>
                          <input
                            className="input-control-clean"
                            value={newRepoUrl}
                            onChange={(e) => setNewRepoUrl(e.target.value)}
                            placeholder="https://github.com/org/repo"
                            required
                          />
                        </div>
                        <div className="input-field-group">
                          <label className="input-label-clean">Webhook HMAC Secret</label>
                          <input
                            className="input-control-clean"
                            value={newRepoSecret}
                            onChange={(e) => setNewRepoSecret(e.target.value)}
                            placeholder="secret-token"
                            required
                          />
                        </div>
                        <div className="input-field-group">
                          <label className="input-label-clean">Default Branch</label>
                          <input
                            className="input-control-clean"
                            value={newRepoBranch}
                            onChange={(e) => setNewRepoBranch(e.target.value)}
                            placeholder="main"
                            required
                          />
                        </div>
                        <div style={{ display: 'flex', alignItems: 'flex-end' }}>
                          <button
                            type="submit"
                            className="btn-action-emerald"
                            disabled={isConnectingRepo}
                          >
                            {isConnectingRepo ? 'Connecting...' : 'Connect & Link Repo'}
                          </button>
                        </div>
                      </form>
                    </div>
                  )}

                  {/* Repository Details and Trigger Build */}
                  {selectedApp.repository_id ? (
                    (() => {
                      const repo = repos.find((r) => r.id === selectedApp.repository_id)
                      return (
                        <div className="repo-card-box">
                          <div className="repo-meta-row">
                            <span className="git-badge">
                              📦 {repo ? repo.name : `Repo ${selectedApp.repository_id.slice(0, 8)}`}
                            </span>
                            <span className="git-badge">
                              🌿 branch: {selectedApp.git_branch || 'main'}
                            </span>
                            {repo && (
                              <a
                                href={repo.repo_url}
                                target="_blank"
                                rel="noreferrer"
                                style={{ fontSize: '0.82rem', color: 'var(--blue-600)', textDecoration: 'none' }}
                              >
                                {repo.repo_url} ↗
                              </a>
                            )}
                            <span className="webhook-badge">
                              🔔 Webhook: /v1/webhooks/github/{selectedApp.repository_id}
                            </span>
                          </div>

                          {/* Trigger Build Form */}
                          <form onSubmit={handleTriggerBuild} style={{ display: 'flex', gap: '0.75rem', flexWrap: 'wrap', alignItems: 'flex-end', marginTop: '0.25rem' }}>
                            <div className="input-field-group" style={{ flex: '1', minWidth: '130px' }}>
                              <label className="input-label-clean">Git Branch / Ref</label>
                              <input
                                className="input-control-clean"
                                value={buildGitBranch}
                                onChange={(e) => setBuildGitBranch(e.target.value)}
                                placeholder="main"
                                required
                              />
                            </div>
                            <div className="input-field-group" style={{ flex: '1.5', minWidth: '180px' }}>
                              <label className="input-label-clean">Commit SHA (optional)</label>
                              <input
                                className="input-control-clean"
                                value={buildCommitSha}
                                onChange={(e) => setBuildCommitSha(e.target.value)}
                                placeholder="Auto-generated if empty"
                              />
                            </div>
                            <div className="input-field-group" style={{ flex: '2', minWidth: '220px' }}>
                              <label className="input-label-clean">Commit Message (optional)</label>
                              <input
                                className="input-control-clean"
                                value={buildCommitMsg}
                                onChange={(e) => setBuildCommitMsg(e.target.value)}
                                placeholder="e.g. Update user dashboard styling"
                              />
                            </div>
                            <button
                              type="submit"
                              className="btn-action-primary"
                              disabled={isTriggeringBuild}
                            >
                              {isTriggeringBuild ? 'Dispatching...' : '⚡ Trigger Source Build'}
                            </button>
                          </form>
                        </div>
                      )
                    })()
                  ) : (
                    <div className="repo-card-box" style={{ textAlign: 'center', padding: '1.25rem' }}>
                      <p style={{ margin: 0, fontSize: '0.875rem', color: 'var(--slate-600)' }}>
                        No Git repository connected to this application. Connect a repository above or link an existing one.
                      </p>
                      {repos.length > 0 && (
                        <div style={{ marginTop: '0.75rem', display: 'flex', justifyContent: 'center', gap: '0.5rem', alignItems: 'center' }}>
                          <span style={{ fontSize: '0.8rem', color: 'var(--slate-500)' }}>Link existing repository:</span>
                          {repos.map((r) => (
                            <button
                              key={r.id}
                              className="btn-action-subtle"
                              onClick={() => handleLinkRepository(r.id)}
                            >
                              🔗 {r.name}
                            </button>
                          ))}
                        </div>
                      )}
                    </div>
                  )}
                </div>

                {/* Deploy New Release Section */}
                <div className="content-card">
                  <h3 style={{ fontSize: '1rem', fontWeight: 700, color: 'var(--slate-800)' }}>
                    Deploy Release (Generation {selectedApp.desired_generation + 1})
                  </h3>
                  <form onSubmit={handleDeployRelease} className="form-grid-layout">
                    <div className="input-field-group">
                      <label className="input-label-clean">Approved Image Digest</label>
                      <input
                        className="input-control-clean"
                        type="text"
                        value={imageDigest}
                        onChange={(e) => setImageDigest(e.target.value)}
                        required
                      />
                    </div>
                    <div className="input-field-group">
                      <label className="input-label-clean">Service Port</label>
                      <input
                        className="input-control-clean"
                        type="number"
                        value={servicePort}
                        onChange={(e) => setServicePort(Number(e.target.value))}
                        required
                      />
                    </div>
                    <div className="input-field-group">
                      <label className="input-label-clean">Readiness Health Path</label>
                      <input
                        className="input-control-clean"
                        type="text"
                        value={healthPath}
                        onChange={(e) => setHealthPath(e.target.value)}
                        required
                      />
                    </div>
                    <div style={{ display: 'flex', alignItems: 'flex-end' }}>
                      <button
                        type="submit"
                        className="btn-action-primary"
                        disabled={isDeploying}
                      >
                        {isDeploying ? 'Deploying...' : 'Deploy Release'}
                      </button>
                    </div>
                  </form>
                </div>

                {/* Releases History Table */}
                <div className="content-card">
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                    <h3 style={{ fontSize: '1rem', fontWeight: 700, color: 'var(--slate-800)' }}>
                      Releases & Revision History
                    </h3>
                  </div>

                  <div className="table-container-clean">
                    <table className="clean-table">
                      <thead>
                        <tr>
                          <th>Release #</th>
                          <th>Source / Commit</th>
                          <th>Image Artifact</th>
                          <th>Status</th>
                          <th>Duration</th>
                          <th>Created</th>
                          <th>Actions</th>
                        </tr>
                      </thead>
                      <tbody>
                        {releases.map((rel) => {
                          const isCurrent = selectedApp.current_release_id === rel.id
                          const isFailed = rel.status === 'BUILD_FAILED' || rel.status === 'DEPLOY_FAILED'
                          return (
                            <tr key={rel.id}>
                              <td>
                                <strong>#{rel.release_number}</strong>
                                {isCurrent && (
                                  <span
                                    className="status-pill-clean healthy"
                                    style={{ marginLeft: '0.5rem', fontSize: '0.65rem' }}
                                  >
                                    ACTIVE
                                  </span>
                                )}
                              </td>
                              <td>
                                {rel.commit_sha ? (
                                  <div style={{ display: 'flex', flexDirection: 'column', gap: '0.2rem' }}>
                                    <span className="git-badge" style={{ fontSize: '0.72rem' }}>
                                      {rel.git_ref || 'main'} @ {rel.commit_sha.slice(0, 7)}
                                    </span>
                                    {rel.commit_message && (
                                      <span
                                        style={{
                                          fontSize: '0.75rem',
                                          color: 'var(--slate-500)',
                                          maxWidth: '200px',
                                          overflow: 'hidden',
                                          textOverflow: 'ellipsis',
                                          whiteSpace: 'nowrap',
                                        }}
                                        title={rel.commit_message}
                                      >
                                        {rel.commit_message}
                                      </span>
                                    )}
                                  </div>
                                ) : (
                                  <span style={{ fontSize: '0.75rem', color: 'var(--slate-400)' }}>Direct Deploy</span>
                                )}
                              </td>
                              <td>
                                {rel.image_digest === 'pending' ? (
                                  <span style={{ color: 'var(--brown-600)', fontStyle: 'italic', fontSize: '0.8rem' }}>
                                    ⏳ build pending...
                                  </span>
                                ) : (
                                  <code style={{ fontFamily: 'var(--font-mono)', fontSize: '0.8rem' }}>
                                    {rel.image_digest}
                                  </code>
                                )}
                              </td>
                              <td>
                                <span className={`status-pill-clean ${rel.status.toLowerCase()}`}>
                                  {rel.status}
                                </span>
                                {isFailed && rel.status_reason && (
                                  <div
                                    style={{
                                      fontSize: '0.7rem',
                                      color: '#be123c',
                                      marginTop: '0.25rem',
                                      maxWidth: '180px',
                                      overflow: 'hidden',
                                      textOverflow: 'ellipsis',
                                      whiteSpace: 'nowrap',
                                    }}
                                    title={rel.status_reason}
                                  >
                                    ⚠️ {rel.status_reason}
                                  </div>
                                )}
                              </td>
                              <td style={{ fontSize: '0.8rem', color: 'var(--slate-600)', fontFamily: 'var(--font-mono)' }}>
                                {rel.build_duration_ms ? `${(rel.build_duration_ms / 1000).toFixed(1)}s` : '—'}
                              </td>
                              <td style={{ fontSize: '0.8rem', color: 'var(--slate-500)' }}>
                                {new Date(rel.created_at).toLocaleTimeString()}
                              </td>
                              <td>
                                <div style={{ display: 'flex', gap: '0.4rem', alignItems: 'center' }}>
                                  {!isCurrent && rel.status === 'HEALTHY' && (
                                    <button
                                      className="btn-action-brown"
                                      onClick={() => handleRollbackRelease(rel.id)}
                                    >
                                      Rollback ↺
                                    </button>
                                  )}
                                </div>
                              </td>
                            </tr>
                          )
                        })}
                      </tbody>
                    </table>
                  </div>
                </div>
              </>
            )
          ) : activeTab === 'dlq' ? (
            /* DEAD-LETTER QUEUE (DLQ) TAB DETAIL VIEW */
            !selectedDlqRecord ? (
              <div className="empty-placeholder-card">
                <span className="placeholder-icon">🛡️</span>
                <span style={{ fontWeight: 600, color: 'var(--slate-800)' }}>
                  Dead-Letter Queue Empty
                </span>
                <p style={{ maxWidth: '380px', fontSize: '0.875rem', color: 'var(--slate-600)' }}>
                  No failed or quarantined workloads in this workspace. All batch jobs have succeeded or are actively progressing within their retry budget.
                </p>
              </div>
            ) : (
              <>
                <div className="content-card">
                  <div className="card-top-row">
                    <div className="detail-header-meta">
                      <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
                        <h2 className="detail-headline">{selectedDlqRecord.job_name}</h2>
                        <span className="status-pill-clean failed">
                          FAILED
                        </span>
                        <span className="dlq-tag">DEAD-LETTER</span>
                      </div>
                      <div className="detail-tags-row">
                        <span className="meta-tag">job_id: {selectedDlqRecord.job_id}</span>
                        <span className="meta-tag">record_id: {selectedDlqRecord.id}</span>
                        <span className="meta-tag">attempt: {selectedDlqRecord.last_attempt}</span>
                        <span className="meta-tag">
                          quarantined: {new Date(selectedDlqRecord.created_at).toLocaleTimeString()}
                        </span>
                      </div>
                    </div>

                    <div style={{ display: 'flex', alignItems: 'center', gap: '0.6rem' }}>
                      <button
                        className="btn-action-rerun"
                        disabled={isRerunningJob}
                        onClick={() => handleRerunJob(selectedDlqRecord.job_id, `rerun-dlq-${selectedDlqRecord.id}`)}
                      >
                        {isRerunningJob ? 'Re-running...' : 'Re-run Job ↺'}
                      </button>
                      <button
                        className="btn-action-primary"
                        onClick={() => handleDownloadJobOutput(selectedDlqRecord.job_id)}
                      >
                        Download Logs ⬇
                      </button>
                      <button className="btn-action-secondary" onClick={manualRefreshJobs}>
                        ↻
                      </button>
                    </div>
                  </div>
                </div>

                {/* Dead-Letter Queue (DLQ) Diagnostic Banner */}
                <div className="dlq-banner-card">
                  <div className="dlq-banner-header">
                    <div className="dlq-banner-title">
                      <span>⚠️ Dead-Letter Queue (Exhausted Retry Budget)</span>
                    </div>
                    <span className="dlq-tag">
                      Attempt #{selectedDlqRecord.last_attempt}
                    </span>
                  </div>
                  <div className="dlq-banner-body">
                    This job reached a terminal failure condition and exhausted its retry budget. Logical state is frozen and quarantined in the database dead-letter records. You can inspect stdout/stderr logs below or trigger an idempotent successor run with a fresh retry budget.
                  </div>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '0.35rem', marginTop: '0.25rem' }}>
                    <div style={{ display: 'flex', gap: '1.25rem', fontSize: '0.8rem', fontFamily: 'var(--font-mono)' }}>
                      {selectedDlqRecord.exit_code !== null && selectedDlqRecord.exit_code !== undefined && (
                        <span><strong>Exit Code:</strong> {selectedDlqRecord.exit_code}</span>
                      )}
                    </div>
                    {selectedDlqRecord.failure_reason && (
                      <div className="failure-diagnostics-pre">
                        {selectedDlqRecord.failure_reason}
                      </div>
                    )}
                  </div>
                </div>

                {/* If matching job details loaded, also show Attempt History */}
                {selectedJob && selectedJob.id === selectedDlqRecord.job_id && selectedJob.attempts.length > 0 && (
                  <div className="content-card">
                    <h3 style={{ fontSize: '1rem', fontWeight: 700, color: 'var(--slate-800)' }}>
                      Attempt History & Leases
                    </h3>
                    <div className="table-container-clean">
                      <table className="clean-table">
                        <thead>
                          <tr>
                            <th>Attempt #</th>
                            <th>Status</th>
                            <th>Lease Epoch</th>
                            <th>Exit Code</th>
                            <th>Failure Reason</th>
                            <th>Timeline</th>
                          </tr>
                        </thead>
                        <tbody>
                          {selectedJob.attempts.map((att) => (
                            <tr key={att.attempt_number}>
                              <td><strong>Attempt {att.attempt_number}</strong></td>
                              <td><span className={`status-pill-clean ${att.state.toLowerCase()}`}>{att.state}</span></td>
                              <td><code className="meta-tag">epoch {att.lease_epoch}</code></td>
                              <td><code>{att.exit_code !== null && att.exit_code !== undefined ? att.exit_code : '-'}</code></td>
                              <td>
                                {att.failure_reason ? (
                                  <span style={{ color: '#be123c', fontWeight: 500, fontSize: '0.85rem' }}>
                                    {att.failure_reason}
                                  </span>
                                ) : '-'}
                              </td>
                              <td style={{ fontSize: '0.8rem', color: 'var(--slate-500)' }}>
                                {att.started_at ? new Date(att.started_at).toLocaleTimeString() : '—'}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </div>
                )}

                {/* Terminal Output Viewer */}
                {jobOutputView && (
                  <div className="terminal-card-box">
                    <div className="terminal-top-bar">
                      <span className="terminal-title-text">Execution Log Output ({selectedDlqRecord.job_name})</span>
                      <button
                        className="btn-action-secondary"
                        style={{ padding: '0.2rem 0.6rem', fontSize: '0.75rem' }}
                        onClick={() => setJobOutputView(null)}
                      >
                        Hide
                      </button>
                    </div>
                    <pre className="terminal-raw-stream">{jobOutputView}</pre>
                  </div>
                )}
              </>
            )
          ) : (
            /* JOBS TAB DETAIL VIEW */
            !selectedJob ? (
              <div className="empty-placeholder-card">
                <span className="placeholder-icon">⚙️</span>
                <span style={{ fontWeight: 600, color: 'var(--slate-800)' }}>
                  No Job Selected
                </span>
                <p style={{ maxWidth: '380px', fontSize: '0.875rem', color: 'var(--slate-600)' }}>
                  Select a job from the sidebar or click + Submit Job to dispatch a finite batch workload.
                </p>
              </div>
            ) : (
              <>
                <div className="content-card">
                  <div className="card-top-row">
                    <div className="detail-header-meta">
                      <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
                        <h2 className="detail-headline">{selectedJob.name}</h2>
                        <span className={`status-pill-clean ${selectedJob.state.toLowerCase()}`}>
                          {selectedJob.state}
                        </span>
                        {selectedJob.state === 'FAILED' && (
                          <span className="dlq-tag">DEAD-LETTER</span>
                        )}
                      </div>
                      <div className="detail-tags-row">
                        <span className="meta-tag">id: {selectedJob.id}</span>
                        <span className="meta-tag">attempt: {selectedJob.current_attempt_number}</span>
                        <span className="meta-tag">
                          submitted: {new Date(selectedJob.created_at).toLocaleTimeString()}
                        </span>
                      </div>
                    </div>

                    <div style={{ display: 'flex', alignItems: 'center', gap: '0.6rem' }}>
                      {['QUEUED', 'ADMITTED', 'STARTING', 'RUNNING', 'RETRY_WAIT'].includes(selectedJob.state) && (
                        <button
                          className="btn-action-danger"
                          onClick={() => handleCancelJob(selectedJob.id)}
                        >
                          Cancel Job ✕
                        </button>
                      )}
                      {selectedJob.state === 'FAILED' && (
                        <button
                          className="btn-action-rerun"
                          disabled={isRerunningJob}
                          onClick={() => handleRerunJob(selectedJob.id)}
                        >
                          {isRerunningJob ? 'Re-running...' : 'Re-run Job ↺'}
                        </button>
                      )}
                      {['SUCCEEDED', 'FAILED', 'CANCELLED'].includes(selectedJob.state) && (
                        <button
                          className="btn-action-primary"
                          onClick={() => handleDownloadJobOutput(selectedJob.id)}
                        >
                          Download Logs ⬇
                        </button>
                      )}
                      <button className="btn-action-secondary" onClick={manualRefreshJobs}>
                        ↻
                      </button>
                    </div>
                  </div>
                </div>

                {/* Dead-Letter Queue (DLQ) Diagnostic Banner */}
                {selectedJob.state === 'FAILED' && (
                  <div className="dlq-banner-card">
                    <div className="dlq-banner-header">
                      <div className="dlq-banner-title">
                        <span>⚠️ Dead-Letter Queue (Exhausted Retry Budget)</span>
                      </div>
                      <span className="dlq-tag">
                        Attempt {selectedJob.current_attempt_number} of {selectedJob.attempts.length}
                      </span>
                    </div>
                    <div className="dlq-banner-body">
                      This job reached a terminal failure condition and exhausted its retry budget. Logical state is frozen and quarantined in the database. You can inspect stdout/stderr logs below or trigger an idempotent successor run with a fresh retry budget.
                    </div>
                    {(() => {
                      const lastAtt = selectedJob.attempts[selectedJob.attempts.length - 1]
                      if (!lastAtt) return null
                      return (
                        <div style={{ display: 'flex', flexDirection: 'column', gap: '0.35rem', marginTop: '0.25rem' }}>
                          <div style={{ display: 'flex', gap: '1.25rem', fontSize: '0.8rem', fontFamily: 'var(--font-mono)' }}>
                            {lastAtt.exit_code !== null && lastAtt.exit_code !== undefined && (
                              <span><strong>Exit Code:</strong> {lastAtt.exit_code}</span>
                            )}
                            {lastAtt.resource_uid && (
                              <span><strong>Resource UID:</strong> {lastAtt.resource_uid}</span>
                            )}
                          </div>
                          {lastAtt.failure_reason && (
                            <div className="failure-diagnostics-pre">
                              {lastAtt.failure_reason}
                            </div>
                          )}
                        </div>
                      )
                    })()}
                  </div>
                )}

                {/* Job Attempts Timeline */}
                <div className="content-card">
                  <h3 style={{ fontSize: '1rem', fontWeight: 700, color: 'var(--slate-800)' }}>
                    Attempt History & Leases
                  </h3>

                  <div className="table-container-clean">
                    <table className="clean-table">
                      <thead>
                        <tr>
                          <th>Attempt #</th>
                          <th>Status</th>
                          <th>Lease Epoch</th>
                          <th>Exit Code</th>
                          <th>Failure Reason</th>
                          <th>Timeline</th>
                        </tr>
                      </thead>
                      <tbody>
                        {selectedJob.attempts.map((att) => (
                          <tr key={att.attempt_number}>
                            <td>
                              <strong>Attempt {att.attempt_number}</strong>
                            </td>
                            <td>
                              <span className={`status-pill-clean ${att.state.toLowerCase()}`}>
                                {att.state}
                              </span>
                            </td>
                            <td>
                              <code className="meta-tag">epoch {att.lease_epoch}</code>
                            </td>
                            <td>
                              <code>{att.exit_code !== null && att.exit_code !== undefined ? att.exit_code : '-'}</code>
                            </td>
                            <td>
                              {att.failure_reason ? (
                                <span style={{ color: '#be123c', fontWeight: 500, fontSize: '0.85rem' }}>
                                  {att.failure_reason}
                                </span>
                              ) : (
                                <span style={{ color: 'var(--slate-400)' }}>-</span>
                              )}
                            </td>
                            <td style={{ fontSize: '0.78rem', color: 'var(--slate-500)' }}>
                              {att.started_at && <div>Started: {new Date(att.started_at).toLocaleTimeString()}</div>}
                              {att.finished_at && <div>Ended: {new Date(att.finished_at).toLocaleTimeString()}</div>}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>

                {/* Terminal Output Viewer */}
                {jobOutputView && (
                  <div className="terminal-card-box">
                    <div className="terminal-top-bar">
                      <span className="terminal-title-text">Execution Log Output ({selectedJob.name})</span>
                      <button
                        className="btn-action-secondary"
                        style={{ padding: '0.2rem 0.6rem', fontSize: '0.75rem' }}
                        onClick={() => setJobOutputView(null)}
                      >
                        Hide
                      </button>
                    </div>
                    <pre className="terminal-raw-stream">{jobOutputView}</pre>
                  </div>
                )}
              </>
            )
          )}
        </section>
      </main>
    </div>
  )
}
