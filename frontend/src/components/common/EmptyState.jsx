import React from 'react'
import { Inbox } from 'lucide-react'

/**
 * EmptyState - every empty region explains itself and offers a next action.
 * A blank panel is never shipped.
 */
export default function EmptyState(props) {
  const { icon: Icon = Inbox, title, text, actions, tone, small } = props
  const classes = ['gp-empty']
  if (tone === 'error') classes.push('gp-empty--error')
  if (tone === 'ai') classes.push('gp-empty--ai')
  if (small) classes.push('gp-empty--sm')
  return (
    <div className={classes.join(' ')}>
      <span className="gp-empty__icon"><Icon aria-hidden="true" /></span>
      <h3 className="gp-empty__title">{title}</h3>
      {text ? <p className="gp-empty__text">{text}</p> : null}
      {actions ? <div className="gp-empty__actions">{actions}</div> : null}
    </div>
  )
}
