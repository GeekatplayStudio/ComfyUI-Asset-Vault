import React from 'react'
import { AlertOctagon } from 'lucide-react'
import EmptyState from './EmptyState.jsx'
import Button from './Button.jsx'

/**
 * ErrorBoundary - wraps every network-touching region so one failing panel
 * never takes the whole shell down with it.
 */
export default class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props)
    this.state = { error: null }
    this.reset = this.reset.bind(this)
  }

  static getDerivedStateFromError(error) {
    return { error }
  }

  reset() {
    this.setState({ error: null })
    if (this.props.onReset) this.props.onReset()
  }

  render() {
    const { error } = this.state
    if (!error) return this.props.children
    return (
      <EmptyState
        icon={AlertOctagon}
        tone="error"
        small={this.props.small}
        title={this.props.title || 'This panel could not be rendered'}
        text={error.message || 'An unexpected error occurred while drawing this view.'}
        actions={<Button onClick={this.reset}>Try again</Button>}
      />
    )
  }
}
