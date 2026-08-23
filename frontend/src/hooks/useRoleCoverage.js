import { useMemo } from 'react'
import api from '../services/api.js'
import useResource from './useResource.js'

/*
 * useRoleCoverage - the false-positive guard on "unused".
 *
 * "Referenced by no workflow and no output" is a measured fact, and the storage
 * API reports it as such. What it does NOT say is *why* the count is zero. When
 * an ENTIRE model role comes back at 100% unused, the likelier explanation is
 * that the indexer cannot read that role's references out of a workflow graph -
 * a metadata-extraction gap - than that the owner installed six depth models and
 * never ran one.
 *
 * So the ratio is computed here and the affected rows are marked as a separate,
 * inferred caution. The underlying "0 references" badge stays amber, because it
 * is still true; the doubt is a second, violet signal beside it (DECISIONS C4).
 *
 * Two documented requests, both cached by useResource:
 *   - /models with a sparse fieldset for the uid -> role map (237 rows here),
 *   - /storage/candidates?reason=unused for the uids the server flagged.
 * The unused set is one page: the summary reports it, and the cap is 500.
 */

const MAX_PAGE = 500

export default function useRoleCoverage(epoch) {
  const roles = useResource(
    'storage:role-map',
    (s) => api.models({ limit: MAX_PAGE, fields: 'uid,role,category' }, s),
    { epoch }
  )

  const unused = useResource(
    'storage:unused-uids',
    (s) => api.storageCandidates(
      { reason: 'unused', kind: 'model', limit: MAX_PAGE, sort: 'name' }, s
    ),
    { epoch }
  )

  return useMemo(() => {
    const empty = {
      loading: roles.loading || unused.loading,
      ready: false,
      roles: [],
      flagged: [],
      uids: new Set(),
      categories: [],
      categoryFilterExact: false,
      count: 0,
      truncated: false
    }
    const roleItems = roles.data && roles.data.items
    const unusedItems = unused.data && unused.data.items
    if (!roleItems || !unusedItems) return empty

    // Neither side may be a partial page, or the ratio would be a lie.
    const rolePage = roles.data.page || {}
    const unusedPage = unused.data.page || {}
    if (rolePage.has_more || unusedPage.has_more) {
      return { ...empty, loading: false, truncated: true }
    }

    const roleOf = new Map()
    const totals = new Map()
    for (const m of roleItems) {
      const role = m.role || 'unknown'
      roleOf.set(m.uid, { role, category: m.category })
      totals.set(role, (totals.get(role) || 0) + 1)
    }

    const unusedByRole = new Map()
    for (const item of unusedItems) {
      const meta = roleOf.get(item.uid)
      if (!meta) continue
      if (!unusedByRole.has(meta.role)) unusedByRole.set(meta.role, [])
      unusedByRole.get(meta.role).push(item)
    }

    const rows = []
    for (const [role, total] of totals) {
      const hits = unusedByRole.get(role) || []
      rows.push({
        role,
        total,
        unused: hits.length,
        share: total > 0 ? (hits.length / total) * 100 : 0,
        bytes: hits.reduce((a, i) => a + (i.size || 0), 0),
        items: hits
      })
    }
    rows.sort((a, b) => (b.share - a.share) || (b.unused - a.unused))

    // A whole role at 100% is the signal. One model in a role of one is weaker
    // evidence than six of six, so the count travels with the flag.
    const flagged = rows.filter((r) => r.total > 0 && r.unused === r.total)

    const uids = new Set()
    const categories = new Set()
    for (const row of flagged) {
      for (const item of row.items) {
        uids.add(item.uid)
        if (item.category) categories.add(item.category)
      }
    }

    // Offer the category filter only when those categories hold NOTHING but the
    // flagged roles - otherwise the chip would quietly widen the selection.
    const flaggedRoles = new Set(flagged.map((r) => r.role))
    let categoryFilterExact = categories.size > 0
    for (const m of roleItems) {
      if (categories.has(m.category) && !flaggedRoles.has(m.role || 'unknown')) {
        categoryFilterExact = false
        break
      }
    }

    return {
      loading: false,
      ready: true,
      truncated: false,
      roles: rows,
      flagged,
      uids,
      categories: Array.from(categories),
      categoryFilterExact,
      count: uids.size,
      bytes: flagged.reduce((a, r) => a + r.bytes, 0)
    }
  }, [roles.data, roles.loading, unused.data, unused.loading])
}
