import React from 'react'
import { MousePointerClick, Pencil, FolderInput, Trash2 } from 'lucide-react'
import EmptyState from '../common/EmptyState.jsx'
import Button from '../common/Button.jsx'
import ErrorBoundary from '../common/ErrorBoundary.jsx'
import ModelDetails from '../details/ModelDetails.jsx'
import NodePackageDetails from '../details/NodePackageDetails.jsx'
import NodeClassDetails from '../details/NodeClassDetails.jsx'
import WorkflowDetails from '../details/WorkflowDetails.jsx'
import OutputDetails from '../details/OutputDetails.jsx'
import { parseUid } from '../../services/format.js'

/*
 * DetailsPanel - one panel, five records. The uid carries its own kind, so the
 * panel can render any asset the app links to, whichever tab it came from.
 */
export default function DetailsPanel(props) {
  const { uid, onOpenUid, onLightbox, onRename, onMove, onDelete, onFilterPackage } = props
  const { kind, id } = parseUid(uid)

  let body = null
  if (!uid) {
    body = (
      <>
        <div className="gp-details__header">
          <div className="gp-details__eyebrow">Details</div>
          <h2 className="gp-details__title">No asset selected</h2>
        </div>
        <div className="gp-details__body">
          <EmptyState
            icon={MousePointerClick}
            title="Nothing selected"
            text="Pick an asset to see its technical detail, what it contains, where it came from and which workflows use it."
          />
        </div>
      </>
    )
  } else if (kind === 'model') {
    body = <ModelDetails id={id} onOpenUid={onOpenUid} onLightbox={onLightbox} />
  } else if (kind === 'node_package') {
    body = <NodePackageDetails id={id} onOpenUid={onOpenUid} onFilterPackage={onFilterPackage} />
  } else if (kind === 'node_class') {
    body = <NodeClassDetails id={id} onOpenUid={onOpenUid} />
  } else if (kind === 'workflow') {
    body = <WorkflowDetails id={id} onOpenUid={onOpenUid} onLightbox={onLightbox} />
  } else if (kind === 'output') {
    body = <OutputDetails id={id} onOpenUid={onOpenUid} onLightbox={onLightbox} />
  } else {
    body = (
      <div className="gp-details__body">
        <EmptyState title="Unknown asset" text={'No panel is registered for "' + kind + '".'} />
      </div>
    )
  }

  const canOperate = uid && kind !== 'node_class' && kind !== 'node_package'

  return (
    <aside className="gp-details" aria-label="Details">
      <ErrorBoundary title="The details panel hit an error" small>
        {body}
      </ErrorBoundary>
      {canOperate ? (
        <div className="gp-details__footer">
          <Button size="sm" variant="ghost" icon={Pencil} label="Rename"
            onClick={() => onRename(uid)} />
          <Button size="sm" variant="ghost" icon={FolderInput} label="Move"
            onClick={() => onMove(uid)} />
          <span className="gp-u-auto-l">
            <Button size="sm" variant="dangerGhost" icon={Trash2} label="Delete"
              onClick={() => onDelete(uid)} />
          </span>
        </div>
      ) : null}
    </aside>
  )
}
