import type { DeadLetterRecord } from './api'

/**
 * Merge the first page of DLQ records into the loaded list during periodic polling.
 * If the workspace is new or the user hasn't loaded more pages, replace with incomingPage.
 * If the user has paged, update the first page while preserving older loaded records.
 */
export function mergeDlqRecords(
  existingRecords: DeadLetterRecord[],
  incomingPage: DeadLetterRecord[],
  isNewWorkspaceOrNotPaged: boolean
): DeadLetterRecord[] {
  if (isNewWorkspaceOrNotPaged) {
    return incomingPage
  }
  const firstPageIds = new Set(incomingPage.map((r) => r.id))
  const older = existingRecords.filter((r) => !firstPageIds.has(r.id))
  return [...incomingPage, ...older]
}

/**
 * Append a newly fetched page of DLQ records from "Load more", filtering out duplicates.
 */
export function appendDlqPage(
  existingRecords: DeadLetterRecord[],
  newPage: DeadLetterRecord[]
): DeadLetterRecord[] {
  const existingIds = new Set(existingRecords.map((r) => r.id))
  const uniqueNewItems = newPage.filter((r) => !existingIds.has(r.id))
  return [...existingRecords, ...uniqueNewItems]
}

/**
 * Resolve the next pagination cursor during periodic polling.
 * When a workspace is fresh or unpaged, update to the incoming cursor.
 * Once the user has paged, preserve the current cursor (including null when exhausted).
 */
export function resolveDlqCursor(
  currentCursor: string | null,
  incomingPageCursor: string | null,
  isNewWorkspaceOrNotPaged: boolean
): string | null {
  if (isNewWorkspaceOrNotPaged) {
    return incomingPageCursor
  }
  return currentCursor
}

/**
 * Reconcile selected DLQ record against the active workspace and current record list.
 * Returns null if the selection belongs to a different workspace or is no longer present.
 */
export function reconcileSelectedDlqRecord(
  selected: DeadLetterRecord | null,
  records: DeadLetterRecord[],
  activeWorkspaceId: string | undefined | null
): DeadLetterRecord | null {
  if (!selected || !activeWorkspaceId) {
    return null
  }
  if (selected.workspace_id !== activeWorkspaceId) {
    return null
  }
  const match = records.find((r) => r.id === selected.id)
  return match || null
}

/**
 * Derive the currently selected DLQ record directly from the loaded records list.
 * Returns null if no ID is selected, the workspace is inactive, or the record does not exist.
 */
export function deriveSelectedDlqRecord(
  selectedId: string | null,
  records: DeadLetterRecord[],
  activeWorkspaceId: string | undefined | null
): DeadLetterRecord | null {
  if (!selectedId || !activeWorkspaceId) {
    return null
  }
  return records.find((r) => r.id === selectedId && r.workspace_id === activeWorkspaceId) || null
}
