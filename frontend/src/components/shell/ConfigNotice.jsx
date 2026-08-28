import React from 'react'
import { AlertTriangle, FolderX, Settings } from 'lucide-react'
import Button from '../common/Button.jsx'

/*
 * ConfigNotice - a persistent, actionable banner above the grid.
 *
 * Three situations earn one, in priority order:
 *   1. no ComfyUI folder configured at all (the wizard was skipped),
 *   2. a folder is configured but cannot be reached (drive offline, renamed),
 *   3. one or more extra scan roots are currently offline.
 * Each one links straight to Settings -> Location, where the fix lives.
 * Nothing here is dismissible: the banner disappears when the state does.
 */
export default function ConfigNotice({ config, onOpenSettings }) {
  if (!config) return null

  let tone = null
  let icon = AlertTriangle
  let title = null
  let text = null

  if (!config.is_configured || !config.comfyui_path) {
    tone = 'warn'
    icon = Settings
    title = 'No ComfyUI folder is configured yet'
    text = 'The vault is running, but nothing can be indexed until it knows ' +
           'where your models live. Set the folder in Settings, or add ' +
           'individual model folders from any drive.'
  } else if (config.path_exists === false) {
    tone = 'danger'
    icon = FolderX
    title = 'The ComfyUI folder cannot be found'
    text = 'The configured folder is not reachable right now (drive offline, ' +
           'renamed, or unplugged?): ' + config.comfyui_path + '. Your library ' +
           'is untouched - rows from an offline root are kept, never wiped.'
  } else {
    const offline = (config.roots || []).filter(
      (r) => r.kind !== 'data' && !r.available)
    if (offline.length) {
      tone = 'warn'
      icon = FolderX
      title = offline.length === 1
        ? 'A scan folder is offline'
        : offline.length + ' scan folders are offline'
      text = 'Assets on these folders are hidden until the drive returns; ' +
             'nothing is deleted: ' +
             offline.slice(0, 3).map((r) => r.path).join('; ') +
             (offline.length > 3 ? ' and more' : '')
    }
  }

  if (!tone) return null
  const Icon = icon

  return (
    <div className={'gp-callout gp-callout--' + tone + ' gp-confignotice'}
      role="status">
      <span className="gp-callout__icon"><Icon aria-hidden="true" /></span>
      <div className="gp-callout__body">
        <div className="gp-callout__title">{title}</div>
        {text}
        <div className="gp-callout__actions">
          <Button size="sm" variant="primary" icon={Settings}
            label="Open Settings" onClick={onOpenSettings} />
        </div>
      </div>
    </div>
  )
}
