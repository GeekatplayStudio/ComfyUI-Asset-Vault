/*
 * Outbound links, in one place.
 *
 * The top bar shows the SHORT set (support + site); the rail footer shows all
 * of them with the copyright. Both read from here so the two never drift.
 *
 * To add a YouTube channel, add an entry to CHANNELS -- nothing else needs to
 * change; the footer renders whatever is in the list.
 */

export const AUTHOR = 'Vladimir Chopine'
export const STUDIO = 'Geekatplay Studio'

export const SUPPORT_URL = 'https://geekatplay.gumroad.com/coffee'
export const SITE_URL = 'https://www.vladimirchopine.com'

/**
 * YouTube channels, rendered in the footer in this order.
 * `{ label, url }` -- label is what the reader sees.
 */
export const CHANNELS = []

/** The compact pair that sits in the top bar. */
export const TOP_LINKS = [
  { key: 'support', href: SUPPORT_URL, label: 'Buy me a coffee' },
  { key: 'site', href: SITE_URL, label: AUTHOR }
]

export const COPYRIGHT_YEAR = 2026

export function copyright() {
  return `© ${COPYRIGHT_YEAR} ${STUDIO}`
}
