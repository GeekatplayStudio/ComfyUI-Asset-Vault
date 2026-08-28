import React, { useState, useCallback } from 'react'
import { PlugZap } from 'lucide-react'
import { VaultProvider, useVault } from './state/VaultContext.jsx'
import AppShell from './components/shell/AppShell.jsx'
import FirstLaunchWizard from './components/modals/FirstLaunchWizard.jsx'
import ErrorBoundary from './components/common/ErrorBoundary.jsx'
import EmptyState from './components/common/EmptyState.jsx'
import Button from './components/common/Button.jsx'

/*
 * Geekatplay ComfyUI Asset Vault
 * Vladimir Chopine
 *
 * Three states: booting, unconfigured (the wizard), and the shell.
 */

function Booting() {
  return (
    <div className="gp-centered">
      <div className="gp-centered__inner">
        <div className="gp-u-col gp-u-center gp-u-gap-5">
          <span className="gp-spinner" aria-hidden="true" />
          <span className="gp-u-fs-11 gp-u-meta gp-u-caps">Opening the vault</span>
        </div>
      </div>
    </div>
  )
}

function BootFailed({ error, onRetry }) {
  return (
    <div className="gp-centered">
      <div className="gp-centered__inner">
        <EmptyState
          tone="error"
          icon={PlugZap}
          title="The vault service is not answering"
          text={(error && error.message) ||
            'Nothing is listening on port 8127. Start the backend and try again.'}
          actions={<Button variant="primary" onClick={onRetry}>Try again</Button>}
        />
      </div>
    </div>
  )
}

function Root() {
  const { state, refreshConfig } = useVault()
  const [wizardDone, setWizardDone] = useState(false)

  const retry = useCallback(() => window.location.reload(), [])

  if (!state.ready) return <Booting />
  if (state.bootError) return <BootFailed error={state.bootError} onRetry={retry} />

  const configured = state.config && state.config.is_configured

  if (!configured && !wizardDone) {
    return (
      <FirstLaunchWizard
        onDone={async () => {
          await refreshConfig()
          setWizardDone(true)
        }}
        onSkip={() => setWizardDone(true)}
      />
    )
  }

  return <AppShell />
}

export default function App() {
  return (
    <ErrorBoundary title="The application could not start">
      <VaultProvider>
        <Root />
      </VaultProvider>
    </ErrorBoundary>
  )
}
