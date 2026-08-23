/* format.js - display formatting. Every number the UI shows passes through here
   so that sizes, counts and dates line up in the tabular monospace columns. */

const KB = 1024
const UNITS = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']

/** 17246978048 -> "16.1 GB" */
export function bytes(n, digits) {
  if (n === null || n === undefined || Number.isNaN(n)) return null
  if (n === 0) return '0 B'
  let value = Math.abs(n)
  let unit = 0
  while (value >= KB && unit < UNITS.length - 1) {
    value /= KB
    unit += 1
  }
  const dp = digits === undefined ? (value < 10 && unit > 0 ? 1 : 0) : digits
  return (n < 0 ? '-' : '') + value.toFixed(dp) + ' ' + UNITS[unit]
}

/** 11901408320 -> "11.9 B" (parameters, not bytes) */
export function params(n) {
  if (!n) return null
  if (n >= 1e9) return (n / 1e9).toFixed(1) + ' B'
  if (n >= 1e6) return (n / 1e6).toFixed(1) + ' M'
  if (n >= 1e3) return (n / 1e3).toFixed(1) + ' K'
  return String(n)
}

/** 1866 -> "1,866" */
export function count(n) {
  if (n === null || n === undefined || Number.isNaN(n)) return '-'
  return Number(n).toLocaleString('en-US')
}

export function percent(n, digits) {
  if (n === null || n === undefined || Number.isNaN(n)) return '-'
  return Number(n).toFixed(digits === undefined ? 1 : digits) + '%'
}

const DATE_FMT = new Intl.DateTimeFormat('en-GB', {
  year: 'numeric', month: 'short', day: '2-digit'
})
const TIME_FMT = new Intl.DateTimeFormat('en-GB', {
  year: 'numeric', month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit'
})

/** epoch ms -> "22 Aug 2026" */
export function date(ms) {
  if (!ms) return null
  return DATE_FMT.format(new Date(ms))
}

/** epoch ms -> "22 Aug 2026, 15:04" */
export function dateTime(ms) {
  if (!ms) return null
  return TIME_FMT.format(new Date(ms))
}

/** epoch ms -> "3 days ago" */
export function ago(ms) {
  if (!ms) return null
  const diff = Date.now() - ms
  if (diff < 0) return 'just now'
  const mins = Math.floor(diff / 60000)
  if (mins < 1) return 'just now'
  if (mins < 60) return mins + (mins === 1 ? ' minute ago' : ' minutes ago')
  const hours = Math.floor(mins / 60)
  if (hours < 24) return hours + (hours === 1 ? ' hour ago' : ' hours ago')
  const days = Math.floor(hours / 24)
  if (days < 31) return days + (days === 1 ? ' day ago' : ' days ago')
  const months = Math.floor(days / 30.44)
  if (months < 12) return months + (months === 1 ? ' month ago' : ' months ago')
  const years = Math.floor(days / 365.25)
  return years + (years === 1 ? ' year ago' : ' years ago')
}

/** milliseconds -> "2 h 48 m" - used for hash ETAs and scan durations. */
export function duration(ms) {
  if (ms === null || ms === undefined || Number.isNaN(ms)) return null
  const secs = Math.round(ms / 1000)
  if (secs < 60) return secs + ' s'
  const mins = Math.floor(secs / 60)
  if (mins < 60) return mins + ' m ' + (secs % 60) + ' s'
  const hours = Math.floor(mins / 60)
  if (hours < 24) return hours + ' h ' + (mins % 60) + ' m'
  return Math.floor(hours / 24) + ' d ' + (hours % 24) + ' h'
}

/** 6800 -> "6.8 s" for elapsed measurements shown next to a result count. */
export function shortDuration(ms) {
  if (ms === null || ms === undefined) return null
  if (ms < 1000) return Math.round(ms) + ' ms'
  return (ms / 1000).toFixed(1) + ' s'
}

export function mbps(v) {
  if (!v) return null
  return v.toFixed(1) + ' MB/s'
}

/** 1024x1024 */
export function dimensions(w, h) {
  if (!w || !h) return null
  return w + ' x ' + h
}

/** Turn snake_case / kebab-case enum values into readable labels. */
export function humanise(value) {
  if (!value) return ''
  return String(value)
    .replace(/[_-]+/g, ' ')
    .replace(/\b\w/g, (c) => c.toUpperCase())
}

/** Frozen enum value -> BEM modifier suffix. not_a_model -> not-a-model */
export function enumClass(value) {
  return String(value || 'unknown').replace(/_/g, '-')
}

/** "model:41" -> { kind: "model", id: 41 } */
export function parseUid(uid) {
  if (!uid) return { kind: null, id: null }
  const idx = String(uid).lastIndexOf(':')
  if (idx < 0) return { kind: String(uid), id: null }
  return { kind: uid.slice(0, idx), id: Number(uid.slice(idx + 1)) }
}

/** Trim a long absolute path to something a 340px panel can show. */
export function tailPath(path, max) {
  if (!path) return null
  const limit = max || 52
  if (path.length <= limit) return path
  return '...' + path.slice(path.length - limit + 3)
}

export function fileStem(name) {
  if (!name) return ''
  const idx = name.lastIndexOf('.')
  return idx > 0 ? name.slice(0, idx) : name
}

export default {
  bytes, params, count, percent, date, dateTime, ago, duration, shortDuration,
  mbps, dimensions, humanise, enumClass, parseUid, tailPath, fileStem
}
