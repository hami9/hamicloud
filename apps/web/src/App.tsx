import { useState, useEffect } from 'react'
import type { Workspace, Application, Release, JobDetails } from './api'
import {
  createWorkspace,
  createApplication,
  listApplications,
  deployRelease,
  listReleases,
  rollbackRelease,
  listWorkspaceJobs,
  submitJob,
  cancelJob,
  downloadJobOutput,
} from './api'
import './App.css'

export default function App() {
  const [token, setToken] = useState('dev:test_user_alice')
  const [activeWorkspace, setActiveWorkspace] = useState<Workspace | null>(null)
  const [workspaceIdInput, setWorkspaceIdInput] = useState('')
  const [wsName, setWsName] = useState('Production Space')
  const [wsSlug, setWsSlug] = useState('prod-space')

  const [activeTab, setActiveTab] = useState<'services' | 'jobs'>('services')

  // Services state
  const [apps, setApps] = useState<Application[]>([])
  const [selectedApp, setSelectedApp] = useState<Application | null>(null)
  const [newAppName, setNewAppName] = useState('Live HTTP Service')
  const [newAppSlug, setNewAppSlug] = useState('live-service')

  const [imageDigest, setImageDigest] = useState('docker.io/library/nginx:alpine')
  const [servicePort, setServicePort] = useState(8080)
  const [healthPath, setHealthPath] = useState('/healthz')
  const [releases, setReleases] = useState<Release[]>([])
  const [isDeploying, setIsDeploying] = useState(false)

  // Jobs state
  const [jobs, setJobs] = useState<JobDetails[]>([])
  const [selectedJob, setSelectedJob] = useState<JobDetails | null>(null)
  const [jobName, setJobName] = useState('batch-report-task')
  const [jobImage, setJobImage] = useState('docker.io/library/alpine:latest')
  const [jobCommand, setJobCommand] = useState('echo "Running financial report calculation..."')
  const [jobTimeout, setJobTimeout] = useState(600)
  const [jobMaxRetries, setJobMaxRetries] = useState(3)
  const [isSubmittingJob, setIsSubmittingJob] = useState(false)
  const [jobOutputView, setJobOutputView] = useState<string | null>(null)

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
    if (!activeWorkspace || activeTab !== 'jobs') return
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

  const manualRefreshReleases = async () => {
    if (!selectedApp) return
    try {
      const data = await listReleases(token, selectedApp.id)
      setReleases(data)
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
    } catch (err: unknown) {
      setErrorMsg((err as Error).message)
    }
  }

  const handleClearWorkspace = () => {
    setActiveWorkspace(null)
    setApps([])
    setSelectedApp(null)
    setReleases([])
    setJobs([])
    setSelectedJob(null)
    setJobOutputView(null)
  }

  const handleSelectApp = (app: Application) => {
    setSelectedApp(app)
    setReleases([])
  }

  const handleSelectJob = (job: JobDetails) => {
    setSelectedJob(job)
    setJobOutputView(null)
  }

  const handleCreateWorkspace = async (e: React.FormEvent) => {
    e.preventDefault()
    setErrorMsg(null)
    try {
      const ws = await createWorkspace(token, wsName, wsSlug)
      setApps([])
      setSelectedApp(null)
      setReleases([])
      setJobs([])
      setSelectedJob(null)
      setActiveWorkspace(ws)
      setWorkspaceIdInput(ws.id)
      setNoticeMsg(`Workspace '${ws.name}' created successfully`)
    } catch (err: unknown) {
      setErrorMsg((err as Error).message)
    }
  }

  const handleUseExistingWorkspace = (e: React.FormEvent) => {
    e.preventDefault()
    if (!workspaceIdInput.trim()) return
    setApps([])
    setSelectedApp(null)
    setReleases([])
    setJobs([])
    setSelectedJob(null)
    setActiveWorkspace({
      id: workspaceIdInput.trim(),
      name: 'Active Workspace',
      slug: 'active-ws',
      created_at: new Date().toISOString(),
    })
    setNoticeMsg(`Switched to workspace: ${workspaceIdInput.trim()}`)
  }

  const handleCreateApplication = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!activeWorkspace) return
    setErrorMsg(null)
    try {
      const app = await createApplication(token, activeWorkspace.id, newAppName, newAppSlug)
      setApps((prev) => [app, ...prev])
      setSelectedApp(app)
      setReleases([])
      setNoticeMsg(`Application '${app.name}' created!`)
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
      setNoticeMsg('Deployment submitted! Reconciler and readiness probes active.')
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
      setNoticeMsg('Rollback initiated! Reverting to target release.')
      await manualRefreshReleases()
    } catch (err: unknown) {
      setErrorMsg((err as Error).message)
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
      setNoticeMsg(`Job '${jobName}' submitted! Scheduler will admit for execution.`)
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
      setNoticeMsg('Cancellation signal sent to active job.')
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

      // Trigger file download in browser
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

  return (
    <div className="dashboard-container">
      {/* Header */}
      <header className="header">
        <div className="logo-area">
          <span className="logo-icon">☁️</span>
          <span className="logo-title">HamiCloud Dashboard</span>
          <span className="badge milestone-badge">M2 Usable MVP</span>
        </div>
        <div className="auth-area">
          <label htmlFor="auth-token" className="auth-label">Auth Token:</label>
          <input
            id="auth-token"
            type="text"
            className="auth-input"
            value={token}
            onChange={(e) => setToken(e.target.value)}
            placeholder="Bearer token or dev:subject"
          />
        </div>
      </header>

      {/* Notifications */}
      {errorMsg && (
        <div className="alert alert-error" role="alert">
          <strong>Error: </strong> {errorMsg}
          <button className="close-btn" onClick={() => setErrorMsg(null)}>✕</button>
        </div>
      )}
      {noticeMsg && (
        <div className="alert alert-success" role="status">
          <strong>Notice: </strong> {noticeMsg}
          <button className="close-btn" onClick={() => setNoticeMsg(null)}>✕</button>
        </div>
      )}

      {/* Primary Workload Tab Selector */}
      {activeWorkspace && (
        <nav className="tab-nav" aria-label="Workload Type">
          <button
            className={`tab-btn ${activeTab === 'services' ? 'active' : ''}`}
            onClick={() => setActiveTab('services')}
          >
            🚀 HTTP Services
          </button>
          <button
            className={`tab-btn ${activeTab === 'jobs' ? 'active' : ''}`}
            onClick={() => setActiveTab('jobs')}
          >
            ⚙️ Background Jobs
          </button>
        </nav>
      )}

      <main className="main-layout">
        {/* Sidebar */}
        <aside className="sidebar">
          {/* Workspace Management */}
          <section className="card workspace-card">
            <h3>1. Workspace</h3>
            {activeWorkspace ? (
              <div className="active-ws-box">
                <p className="ws-name">{activeWorkspace.name}</p>
                <code className="ws-id">{activeWorkspace.id}</code>
                <button
                  className="btn btn-secondary btn-sm"
                  onClick={handleClearWorkspace}
                >
                  Change Workspace
                </button>
              </div>
            ) : (
              <div>
                <form onSubmit={handleCreateWorkspace} className="stacked-form">
                  <input
                    type="text"
                    value={wsName}
                    onChange={(e) => setWsName(e.target.value)}
                    placeholder="Workspace Name"
                    required
                  />
                  <input
                    type="text"
                    value={wsSlug}
                    onChange={(e) => setWsSlug(e.target.value)}
                    placeholder="slug (e.g. prod-space)"
                    required
                  />
                  <button type="submit" className="btn btn-primary">
                    Create Workspace
                  </button>
                </form>
                <hr className="divider" />
                <form onSubmit={handleUseExistingWorkspace} className="stacked-form">
                  <input
                    type="text"
                    value={workspaceIdInput}
                    onChange={(e) => setWorkspaceIdInput(e.target.value)}
                    placeholder="Or enter existing Workspace UUID"
                  />
                  <button type="submit" className="btn btn-secondary">
                    Select Workspace
                  </button>
                </form>
              </div>
            )}
          </section>

          {/* Sub-item Sidebar based on active tab */}
          {activeWorkspace && activeTab === 'services' && (
            <section className="card apps-card">
              <h3>2. Applications</h3>
              <div className="apps-list">
                {apps.length === 0 ? (
                  <p className="muted-text">No applications found in this workspace.</p>
                ) : (
                  apps.map((app) => (
                    <button
                      key={app.id}
                      className={`app-item ${selectedApp?.id === app.id ? 'active' : ''}`}
                      onClick={() => handleSelectApp(app)}
                    >
                      <div className="app-item-title">{app.name}</div>
                      <div className="app-item-sub">{app.slug}</div>
                    </button>
                  ))
                )}
              </div>

              <hr className="divider" />
              <h4>New Application</h4>
              <form onSubmit={handleCreateApplication} className="stacked-form">
                <input
                  type="text"
                  value={newAppName}
                  onChange={(e) => setNewAppName(e.target.value)}
                  placeholder="App Name"
                  required
                />
                <input
                  type="text"
                  value={newAppSlug}
                  onChange={(e) => setNewAppSlug(e.target.value)}
                  placeholder="App Slug"
                  required
                />
                <button type="submit" className="btn btn-primary">
                  Create App
                </button>
              </form>
            </section>
          )}

          {activeWorkspace && activeTab === 'jobs' && (
            <section className="card jobs-sidebar-card">
              <h3>2. Workspace Jobs</h3>
              <div className="apps-list">
                {jobs.length === 0 ? (
                  <p className="muted-text">No background jobs submitted yet.</p>
                ) : (
                  jobs.map((j) => (
                    <button
                      key={j.id}
                      className={`app-item ${selectedJob?.id === j.id ? 'active' : ''}`}
                      onClick={() => handleSelectJob(j)}
                    >
                      <div className="app-item-title">{j.name}</div>
                      <div className="app-item-sub">
                        <span className={`status-pill-small status-${j.state.toLowerCase()}`}>
                          {j.state}
                        </span>
                        <span className="text-xs muted-text">#{j.current_attempt_number}</span>
                      </div>
                    </button>
                  ))
                )}
              </div>
            </section>
          )}
        </aside>

        {/* Content Area */}
        <section className="content-area">
          {!activeWorkspace ? (
            <div className="card placeholder-card">
              <p>Select or create a workspace to view services and background jobs.</p>
            </div>
          ) : activeTab === 'services' ? (
            /* SERVICES TAB CONTENT */
            !selectedApp ? (
              <div className="card placeholder-card">
                <p>Select or create an application to manage releases and live ingress.</p>
              </div>
            ) : (
              <div>
                {/* App Banner */}
                <div className="card app-banner">
                  <div className="app-info">
                    <h2>{selectedApp.name}</h2>
                    <p className="app-meta">
                      Slug: <code>{selectedApp.slug}</code> | Generation:{' '}
                      <strong>{selectedApp.desired_generation}</strong> | Type:{' '}
                      <code>{selectedApp.workload_type}</code>
                    </p>
                  </div>
                </div>

                {/* Deployment Form */}
                <div className="card deployment-form-card">
                  <h3>3. Deploy Release</h3>
                  <form onSubmit={handleDeployRelease} className="deploy-grid">
                    <div className="form-group">
                      <label htmlFor="image-digest">Approved Image Digest / URI:</label>
                      <input
                        id="image-digest"
                        type="text"
                        value={imageDigest}
                        onChange={(e) => setImageDigest(e.target.value)}
                        required
                      />
                    </div>
                    <div className="form-group">
                      <label htmlFor="service-port">Service Port:</label>
                      <input
                        id="service-port"
                        type="number"
                        value={servicePort}
                        onChange={(e) => setServicePort(Number(e.target.value))}
                        required
                      />
                    </div>
                    <div className="form-group">
                      <label htmlFor="health-path">Readiness Health Path:</label>
                      <input
                        id="health-path"
                        type="text"
                        value={healthPath}
                        onChange={(e) => setHealthPath(e.target.value)}
                        required
                      />
                    </div>
                    <div className="form-action">
                      <button
                        type="submit"
                        className="btn btn-primary btn-deploy"
                        disabled={isDeploying}
                      >
                        {isDeploying ? 'Deploying...' : 'Deploy Release'}
                      </button>
                    </div>
                  </form>
                </div>

                {/* Releases & Ingress Status */}
                <div className="card releases-card">
                  <div className="card-header">
                    <h3>4. Releases & Ingress Status</h3>
                    <button className="btn btn-secondary btn-sm" onClick={manualRefreshReleases}>
                      ↻ Refresh
                    </button>
                  </div>

                  {releases.length === 0 ? (
                    <p className="muted-text">No releases have been deployed yet.</p>
                  ) : (
                    <div className="releases-table-wrapper">
                      <table className="releases-table">
                        <thead>
                          <tr>
                            <th>Release</th>
                            <th>Image</th>
                            <th>Status</th>
                            <th>Ingress / Service URL & Diagnostics</th>
                            <th>Actions</th>
                          </tr>
                        </thead>
                        <tbody>
                          {releases.map((rel) => {
                            const port = rel.config_json?.port || 8080
                            const path = rel.config_json?.health_path || '/'
                            const serviceUrl = `http://localhost:${port}${path}`
                            const isHealthy = rel.status === 'HEALTHY'
                            const isFailed = rel.status === 'DEPLOY_FAILED'
                            const isCurrent = selectedApp.current_release_id === rel.id

                            return (
                              <tr key={rel.id} className={`release-row status-${rel.status.toLowerCase()}`}>
                                <td>
                                  <strong>#{rel.release_number}</strong>
                                  {isCurrent && <span className="current-badge">ACTIVE</span>}
                                  <div className="text-xs muted-text">{rel.id.slice(0, 8)}</div>
                                </td>
                                <td className="code-cell">
                                  <code>{rel.image_digest}</code>
                                </td>
                                <td>
                                  <span className={`status-pill status-${rel.status.toLowerCase()}`}>
                                    {rel.status}
                                  </span>
                                </td>
                                <td>
                                  {isHealthy && (
                                    <div className="service-healthy-box">
                                      <span className="live-indicator">● LIVE</span>
                                      <a
                                        href={serviceUrl}
                                        target="_blank"
                                        rel="noreferrer"
                                        className="service-link"
                                      >
                                        {serviceUrl} ↗
                                      </a>
                                    </div>
                                  )}
                                  {isFailed && (
                                    <div className="service-failed-box" role="alert">
                                      <span className="failed-title">⚠️ Readiness Probe Failed:</span>
                                      <span className="failed-reason">
                                        {rel.status_reason || 'Connection refused or timeout on target port'}
                                      </span>
                                    </div>
                                  )}
                                  {!isHealthy && !isFailed && (
                                    <div className="service-pending-box">
                                      <span className="spinner">⏳</span> Reconciling & probing readiness...
                                    </div>
                                  )}
                                </td>
                                <td>
                                  {!isCurrent && (
                                    <button
                                      className="btn btn-secondary btn-sm"
                                      onClick={() => handleRollbackRelease(rel.id)}
                                      title="Roll back application to this release"
                                    >
                                      Rollback ↺
                                    </button>
                                  )}
                                </td>
                              </tr>
                            )
                          })}
                        </tbody>
                      </table>
                    </div>
                  )}
                </div>
              </div>
            )
          ) : (
            /* JOBS TAB CONTENT */
            <div>
              {/* Job Submission Form */}
              <div className="card deployment-form-card">
                <h3>3. Submit Finite Job</h3>
                <form onSubmit={handleSubmitJob} className="deploy-grid">
                  <div className="form-group">
                    <label htmlFor="job-name">Job Name:</label>
                    <input
                      id="job-name"
                      type="text"
                      value={jobName}
                      onChange={(e) => setJobName(e.target.value)}
                      required
                    />
                  </div>
                  <div className="form-group">
                    <label htmlFor="job-image">Approved Image Digest:</label>
                    <input
                      id="job-image"
                      type="text"
                      value={jobImage}
                      onChange={(e) => setJobImage(e.target.value)}
                      required
                    />
                  </div>
                  <div className="form-group">
                    <label htmlFor="job-command">Command & Arguments:</label>
                    <input
                      id="job-command"
                      type="text"
                      value={jobCommand}
                      onChange={(e) => setJobCommand(e.target.value)}
                      placeholder='echo "Hello world"'
                      required
                    />
                  </div>
                  <div className="form-group">
                    <label htmlFor="job-retries">Max Retries:</label>
                    <input
                      id="job-retries"
                      type="number"
                      min={0}
                      max={5}
                      value={jobMaxRetries}
                      onChange={(e) => setJobMaxRetries(Number(e.target.value))}
                      required
                    />
                  </div>
                  <div className="form-group">
                    <label htmlFor="job-timeout">Timeout (seconds):</label>
                    <input
                      id="job-timeout"
                      type="number"
                      min={10}
                      max={3600}
                      value={jobTimeout}
                      onChange={(e) => setJobTimeout(Number(e.target.value))}
                      required
                    />
                  </div>
                  <div className="form-action">
                    <button
                      type="submit"
                      className="btn btn-primary btn-deploy"
                      disabled={isSubmittingJob}
                    >
                      {isSubmittingJob ? 'Submitting...' : 'Submit Job'}
                    </button>
                  </div>
                </form>
              </div>

              {/* Selected Job Inspection */}
              {selectedJob && (
                <div className="card job-details-card">
                  <div className="card-header">
                    <div>
                      <h3>Job: {selectedJob.name}</h3>
                      <p className="app-meta">
                        ID: <code>{selectedJob.id}</code> | Created:{' '}
                        {new Date(selectedJob.created_at).toLocaleTimeString()}
                      </p>
                    </div>
                    <div className="job-action-buttons">
                      <span className={`status-pill status-${selectedJob.state.toLowerCase()}`}>
                        {selectedJob.state}
                      </span>
                      {['QUEUED', 'ADMITTED', 'STARTING', 'RUNNING', 'RETRY_WAIT'].includes(selectedJob.state) && (
                        <button
                          className="btn btn-danger btn-sm"
                          onClick={() => handleCancelJob(selectedJob.id)}
                        >
                          Cancel Job ✕
                        </button>
                      )}
                      {['SUCCEEDED', 'FAILED', 'CANCELLED'].includes(selectedJob.state) && (
                        <button
                          className="btn btn-primary btn-sm"
                          onClick={() => handleDownloadJobOutput(selectedJob.id)}
                        >
                          Download Output ⬇
                        </button>
                      )}
                      <button className="btn btn-secondary btn-sm" onClick={manualRefreshJobs}>
                        ↻
                      </button>
                    </div>
                  </div>

                  <h4>Attempt Timeline</h4>
                  {selectedJob.attempts.length === 0 ? (
                    <p className="muted-text">Awaiting admission by Go Scheduler...</p>
                  ) : (
                    <div className="releases-table-wrapper">
                      <table className="releases-table">
                        <thead>
                          <tr>
                            <th>Attempt</th>
                            <th>State</th>
                            <th>Exit Code</th>
                            <th>Failure Reason</th>
                            <th>Timeline</th>
                          </tr>
                        </thead>
                        <tbody>
                          {selectedJob.attempts.map((att) => (
                            <tr key={att.attempt_number} className={`release-row status-${att.state.toLowerCase()}`}>
                              <td>
                                <strong>Attempt #{att.attempt_number}</strong>
                                <div className="text-xs muted-text">Epoch {att.lease_epoch}</div>
                              </td>
                              <td>
                                <span className={`status-pill status-${att.state.toLowerCase()}`}>
                                  {att.state}
                                </span>
                              </td>
                              <td>
                                <code>{att.exit_code !== null && att.exit_code !== undefined ? att.exit_code : '-'}</code>
                              </td>
                              <td>
                                {att.failure_reason ? (
                                  <span className="text-danger text-sm">{att.failure_reason}</span>
                                ) : (
                                  <span className="muted-text text-sm">None</span>
                                )}
                              </td>
                              <td className="text-xs muted-text">
                                {att.started_at && <div>Started: {new Date(att.started_at).toLocaleTimeString()}</div>}
                                {att.finished_at && <div>Finished: {new Date(att.finished_at).toLocaleTimeString()}</div>}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}

                  {/* Output viewer modal / inline card */}
                  {jobOutputView && (
                    <div className="job-output-box">
                      <div className="card-header">
                        <h5>Downloaded Output / Logs</h5>
                        <button className="btn btn-secondary btn-sm" onClick={() => setJobOutputView(null)}>
                          Close
                        </button>
                      </div>
                      <pre className="output-pre">{jobOutputView}</pre>
                    </div>
                  )}
                </div>
              )}
            </div>
          )}
        </section>
      </main>
    </div>
  )
}
