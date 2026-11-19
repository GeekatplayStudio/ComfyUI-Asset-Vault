import '@testing-library/jest-dom/vitest'
import { afterEach } from 'vitest'
import { cleanup } from '@testing-library/react'

// Vitest runs without injected globals here, so Testing Library cannot register
// its own teardown. Without this every render stays mounted and a later test in
// the same file finds the previous test's nodes.
afterEach(cleanup)
