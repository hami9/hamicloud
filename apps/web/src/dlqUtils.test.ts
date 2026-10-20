import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mapWorkspaceQuotasResponse, type DeadLetterRecord } from './api.ts'
import {
  mergeDlqRecords,
  appendDlqPage,
  resolveDlqCursor,
  reconcileSelectedDlqRecord,
  deriveSelectedDlqRecord,
} from './dlqUtils.ts'

function createRecord(id: string, workspaceId: string, name = 'job'): DeadLetterRecord {
  return {
    id,
    job_id: `job-${id}`,
    workspace_id: workspaceId,
    job_name: name,
    last_attempt: 1,
    exit_code: 1,
    failure_reason: 'Error',
    created_at: new Date().toISOString(),
  }
}

test('mergeDlqRecords: replaces list on fresh workspace or unpaged state', () => {
  const existing = [createRecord('old-1', 'ws-1'), createRecord('old-2', 'ws-1')]
  const page1 = [createRecord('rec-1', 'ws-2'), createRecord('rec-2', 'ws-2')]

  const result = mergeDlqRecords(existing, page1, true)
  assert.equal(result.length, 2)
  assert.deepEqual(result.map((r) => r.id), ['rec-1', 'rec-2'])
})

test('mergeDlqRecords: merges page 1 with older loaded records when user has paged', () => {
  const page1 = [createRecord('rec-1', 'ws-1', 'updated-job'), createRecord('rec-2', 'ws-1')]
  const existing = [
    createRecord('rec-1', 'ws-1', 'stale-job'),
    createRecord('rec-2', 'ws-1'),
    createRecord('rec-3', 'ws-1'),
    createRecord('rec-4', 'ws-1'),
  ]

  const result = mergeDlqRecords(existing, page1, false)
  assert.equal(result.length, 4)
  assert.equal(result[0].job_name, 'updated-job')
  assert.deepEqual(result.map((r) => r.id), ['rec-1', 'rec-2', 'rec-3', 'rec-4'])
})

test('appendDlqPage: appends new records and deduplicates', () => {
  const existing = [createRecord('rec-1', 'ws-1'), createRecord('rec-2', 'ws-1')]
  const page2 = [createRecord('rec-2', 'ws-1'), createRecord('rec-3', 'ws-1'), createRecord('rec-4', 'ws-1')]

  const result = appendDlqPage(existing, page2)
  assert.equal(result.length, 4)
  assert.deepEqual(result.map((r) => r.id), ['rec-1', 'rec-2', 'rec-3', 'rec-4'])
})

test('resolveDlqCursor: updates cursor when unpaged, preserves cursor when paged', () => {
  // Case 1: unpaged -> takes incoming page cursor
  assert.equal(resolveDlqCursor(null, 'cursor-page-1', true), 'cursor-page-1')
  assert.equal(resolveDlqCursor('old-cursor', 'cursor-page-1', true), 'cursor-page-1')

  // Case 2: paged -> preserves current cursor set by load more
  assert.equal(resolveDlqCursor('cursor-page-2', 'cursor-page-1', false), 'cursor-page-2')

  // Case 3: all pages loaded (cursor null) -> preserves null, does not restore page-1 cursor
  assert.equal(resolveDlqCursor(null, 'cursor-page-1', false), null)
})

test('poll and load more interaction sequence: cursor and records remain consistent', () => {
  let hasLoadedMore = false
  let records: DeadLetterRecord[] = []
  let cursor: string | null = null

  // 1. Initial poll on fresh workspace
  const page1 = [createRecord('rec-1', 'ws-1'), createRecord('rec-2', 'ws-1')]
  const page1Cursor = 'cursor-page-2'
  records = mergeDlqRecords(records, page1, !hasLoadedMore)
  cursor = resolveDlqCursor(cursor, page1Cursor, !hasLoadedMore)
  assert.equal(records.length, 2)
  assert.equal(cursor, 'cursor-page-2')

  // 2. User clicks "Load more" -> fetches page 2
  const page2 = [createRecord('rec-3', 'ws-1'), createRecord('rec-4', 'ws-1')]
  const page2Cursor = 'cursor-page-3'
  hasLoadedMore = true
  records = appendDlqPage(records, page2)
  cursor = page2Cursor
  assert.equal(records.length, 4)
  assert.equal(cursor, 'cursor-page-3')

  // 3. Periodic poll runs 2s later with page 1
  const pollPage1 = [createRecord('rec-1', 'ws-1', 'updated-title'), createRecord('rec-2', 'ws-1')]
  records = mergeDlqRecords(records, pollPage1, !hasLoadedMore)
  cursor = resolveDlqCursor(cursor, page1Cursor, !hasLoadedMore)
  // Cursor MUST NOT be reset to page1Cursor ('cursor-page-2')
  assert.equal(cursor, 'cursor-page-3')
  // Records must preserve page 2 items while updating page 1 items
  assert.equal(records.length, 4)
  assert.equal(records[0].job_name, 'updated-title')

  // 4. User loads next page (page 3, final page -> next_cursor is null)
  const page3 = [createRecord('rec-5', 'ws-1')]
  records = appendDlqPage(records, page3)
  cursor = null
  assert.equal(records.length, 5)
  assert.equal(cursor, null)

  // 5. Periodic poll runs after all pages loaded
  records = mergeDlqRecords(records, pollPage1, !hasLoadedMore)
  cursor = resolveDlqCursor(cursor, page1Cursor, !hasLoadedMore)
  // Cursor MUST remain null and NOT restore page1Cursor
  assert.equal(cursor, null)
  assert.equal(records.length, 5)
})

test('reconcileSelectedDlqRecord: clears selection on workspace mismatch or deleted record', () => {
  const recA = createRecord('rec-a', 'ws-1')
  const recB = createRecord('rec-b', 'ws-1')

  // Normal: match found in same workspace
  const updatedA = { ...recA, failure_reason: 'Updated diagnostic' }
  const match = reconcileSelectedDlqRecord(recA, [updatedA, recB], 'ws-1')
  assert.equal(match?.id, 'rec-a')
  assert.equal(match?.failure_reason, 'Updated diagnostic')

  // Workspace mismatch: selection from ws-1 in ws-2 -> null
  assert.equal(reconcileSelectedDlqRecord(recA, [createRecord('rec-c', 'ws-2')], 'ws-2'), null)

  // Record deleted / no longer in list -> null
  assert.equal(reconcileSelectedDlqRecord(recA, [recB], 'ws-1'), null)

  // Null input
  assert.equal(reconcileSelectedDlqRecord(null, [recA], 'ws-1'), null)
})

test('deriveSelectedDlqRecord: derives record by ID, verifies workspace, handles removal', () => {
  const rec1 = createRecord('rec-1', 'ws-1', 'original-name')
  const rec2 = createRecord('rec-2', 'ws-1')

  // Normal: selected record present in current workspace
  assert.equal(deriveSelectedDlqRecord('rec-1', [rec1, rec2], 'ws-1')?.job_name, 'original-name')

  // Fresh update reflected directly
  const updated1 = { ...rec1, job_name: 'refreshed-name' }
  assert.equal(deriveSelectedDlqRecord('rec-1', [updated1, rec2], 'ws-1')?.job_name, 'refreshed-name')

  // Workspace mismatch returns null
  assert.equal(deriveSelectedDlqRecord('rec-1', [rec1, rec2], 'ws-2'), null)

  // Record deleted / purged from list returns null
  assert.equal(deriveSelectedDlqRecord('rec-1', [rec2], 'ws-1'), null)

  // Null selectedId or null activeWorkspace returns null
  assert.equal(deriveSelectedDlqRecord(null, [rec1, rec2], 'ws-1'), null)
  assert.equal(deriveSelectedDlqRecord('rec-1', [rec1, rec2], null), null)
})

test('mapWorkspaceQuotasResponse: maps backend flattened response into frontend WorkspaceQuotas shape', () => {
  const backendResponse = {
    workspace_id: '550e8400-e29b-41d4-a716-446655440000',
    concurrent_jobs_limit: 2,
    concurrent_jobs_used: 1,
    concurrent_builds_limit: 1,
    concurrent_builds_used: 0,
    deployed_services_limit: 2,
    deployed_services_used: 1,
    pending_operations_limit: 100,
    pending_operations_used: 5,
    reservations: [
      {
        id: 'res-1',
        workspace_id: '550e8400-e29b-41d4-a716-446655440000',
        resource_class: 'CONCURRENT_BUILD',
        units: 1,
        status: 'ACTIVE',
        expires_at: '2026-10-20T12:00:00Z',
        created_at: '2026-10-20T11:00:00Z',
      },
    ],
  }

  const mapped = mapWorkspaceQuotasResponse(backendResponse)
  assert.equal(mapped.workspace_id, '550e8400-e29b-41d4-a716-446655440000')
  assert.equal(mapped.quotas.length, 4)
  assert.deepEqual(mapped.quotas[0], {
    resource_class: 'CONCURRENT_JOB',
    limit: 2,
    used: 1,
    remaining: 1,
  })
  assert.deepEqual(mapped.quotas[1], {
    resource_class: 'CONCURRENT_BUILD',
    limit: 1,
    used: 0,
    remaining: 1,
  })
  assert.deepEqual(mapped.quotas[2], {
    resource_class: 'DEPLOYED_SERVICE',
    limit: 2,
    used: 1,
    remaining: 1,
  })
  assert.deepEqual(mapped.quotas[3], {
    resource_class: 'PENDING_OPERATIONS',
    limit: 100,
    used: 5,
    remaining: 95,
  })
  assert.equal(mapped.active_reservations.length, 1)
  assert.equal(mapped.active_reservations[0].id, 'res-1')
})
