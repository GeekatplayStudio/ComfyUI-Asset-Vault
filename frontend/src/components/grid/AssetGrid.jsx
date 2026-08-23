import React, { useRef, useCallback, useEffect } from 'react'
import useVirtualGrid from '../../hooks/useVirtualGrid.js'
import { thumbTier } from '../../services/api.js'
import AssetCard from './AssetCard.jsx'
import AssetListRow from './AssetListRow.jsx'
import GroupHeader from './GroupHeader.jsx'
import { SkeletonGrid, SkeletonRows } from '../common/Skeleton.jsx'

/*
 * AssetGrid - owns the scrolling region and the virtualiser.
 *
 * Grid and list share one windowing pass; the only difference is the column
 * count, which the shell's --gp-grid-size already encodes.
 */
export default function AssetGrid(props) {
  const {
    items, groups, scope, mode, tile, groupKeyOf,
    selection, focusUid, onSelect, onOpen, onToggleCheck,
    onRename, onDelete, loading, empty, scrollRef: externalRef
  } = props

  const internalRef = useRef(null)
  const scrollRef = externalRef || internalRef
  const tier = thumbTier(tile)

  const { sections, measureRef } = useVirtualGrid({
    scrollRef, items, groups, groupKeyOf, mode, tile
  })

  // A new result set should always start at the top of the scroller.
  const resetKey = scope + ':' + mode + ':' + (items && items.length ? items[0].uid : 'none')
  const lastReset = useRef(resetKey)
  useEffect(() => {
    if (lastReset.current !== resetKey) {
      lastReset.current = resetKey
      if (scrollRef.current) scrollRef.current.scrollTop = 0
    }
  }, [resetKey, scrollRef])

  const isSelected = useCallback(
    (uid) => selection.has(uid),
    [selection]
  )

  const bodyClass = 'gp-main__body' + (mode === 'list' ? ' gp-main__body--flush' : '')

  if (loading && (!items || !items.length)) {
    return (
      <div className={bodyClass} ref={scrollRef}>
        {mode === 'list' ? <SkeletonRows rows={14} /> : <SkeletonGrid cards={18} />}
      </div>
    )
  }

  if (!items || !items.length) {
    return <div className={bodyClass} ref={scrollRef}>{empty}</div>
  }

  return (
    <div className={bodyClass} ref={scrollRef} tabIndex={-1}>
      <div className="gp-vgrid">
        {sections.map((section) => (
          <React.Fragment key={section.key}>
            {section.header ? (
              <GroupHeader
                label={section.header.label}
                count={section.header.count}
                bytes={section.header.bytes}
                shown={section.shown}
              />
            ) : null}
            <div className="gp-vgrid__spacer" style={{ height: section.height }}>
              {section.rows.map((row) => (
                <div
                  key={section.key + ':' + row.index}
                  className="gp-vgrid__row"
                  ref={measureRef}
                  style={{ top: row.top }}
                >
                  {row.items.map((item) => (
                    mode === 'list' ? (
                      <AssetListRow
                        key={item.uid}
                        item={item}
                        scope={scope}
                        selected={focusUid === item.uid}
                        checked={isSelected(item.uid)}
                        onSelect={onSelect}
                        onOpen={onOpen}
                        onRename={onRename}
                        onDelete={onDelete}
                      />
                    ) : (
                      <AssetCard
                        key={item.uid}
                        item={item}
                        scope={scope}
                        tier={tier}
                        selected={focusUid === item.uid}
                        checked={isSelected(item.uid)}
                        onSelect={onSelect}
                        onOpen={onOpen}
                        onToggleCheck={onToggleCheck}
                      />
                    )
                  ))}
                </div>
              ))}
            </div>
          </React.Fragment>
        ))}
      </div>
    </div>
  )
}
