import React from 'react'
import {
  LayoutGrid, List, Pencil, FolderInput, Trash2, RefreshCw, X, Filter
} from 'lucide-react'
import Button from '../common/Button.jsx'
import Select from '../common/Select.jsx'
import Slider from '../common/Slider.jsx'
import Chip from '../common/Chip.jsx'
import { TILE_MIN, TILE_MAX } from '../../state/actions.js'
import { humanise, count as fmtCount } from '../../services/format.js'

/*
 * GridToolbar - the row above the grid plus the facet row beneath it.
 * Everything here writes straight into the tab's view state, which is what the
 * next list request is built from.
 */
export default function GridToolbar(props) {
  const {
    view, patch, sorts, groupOptions, facets, activeFilters, onSetFilter,
    onClearFilters, selectionCount, actions, leading, resultLabel
  } = props

  const grouped = view.group && view.group !== 'none'

  return (
    <>
      <div className="gp-toolbar">
        <div className="gp-toolbar__group">
          {leading}
          <div className="gp-segment" role="group" aria-label="View mode">
            <button
              type="button"
              className={'gp-segment__item' + (view.view === 'grid' ? ' gp-segment__item--active' : '')}
              aria-pressed={view.view === 'grid'}
              aria-label="Grid view"
              title="Grid view"
              onClick={() => patch({ view: 'grid' }, { keepOffset: true })}
            >
              <LayoutGrid aria-hidden="true" />
            </button>
            <button
              type="button"
              className={'gp-segment__item' + (view.view === 'list' ? ' gp-segment__item--active' : '')}
              aria-pressed={view.view === 'list'}
              aria-label="List view"
              title="List view"
              onClick={() => patch({ view: 'list' }, { keepOffset: true })}
            >
              <List aria-hidden="true" />
            </button>
          </div>

          <Slider
            value={view.tile}
            min={TILE_MIN}
            max={TILE_MAX}
            step={10}
            disabled={view.view === 'list'}
            ariaLabel="Preview size"
            title={view.view === 'list'
              ? 'Preview size applies to the grid view'
              : 'Preview size'}
            valueLabel={view.view === 'list' ? '--' : view.tile + 'px'}
            onChange={(tile) => patch({ tile }, { keepOffset: true })}
          />
        </div>

        <span className="gp-divider gp-divider--v" />

        <div className="gp-toolbar__group">
          <span className="gp-toolbar__label">Sort</span>
          <Select
            bare
            value={view.sort}
            ariaLabel="Sort order"
            options={sorts}
            onChange={(sort) => patch({ sort })}
          />
          <span className="gp-toolbar__label">Group</span>
          <Select
            bare
            value={view.group}
            ariaLabel="Grouping"
            options={groupOptions.map((g) => ({ value: g, label: g === 'none' ? 'None' : humanise(g) }))}
            onChange={(group) => patch({ group })}
          />
        </div>

        <div className="gp-toolbar__spacer" />

        <div className="gp-toolbar__group">
          {resultLabel ? <span className="gp-toolbar__label">{resultLabel}</span> : null}
          {actions}
        </div>
      </div>

      <div className={'gp-facetbar' + (
        (!facets || !facets.length) && !activeFilters.length ? ' gp-facetbar--empty' : ''
      )}
      >
        {activeFilters.length ? (
          <>
            <span className="gp-toolbar__label"><Filter size={11} aria-hidden="true" /></span>
            {activeFilters.map((f) => (
              <Chip
                key={f.field + ':' + f.value}
                label={f.label}
                selected
                onRemove={() => onSetFilter(f.field, f.remove)}
              />
            ))}
            <Button
              size="sm"
              variant="ghost"
              icon={X}
              label="Clear"
              onClick={onClearFilters}
            />
            <span className="gp-divider gp-divider--v" />
          </>
        ) : null}

        {(facets || []).map((facet) => (
          <React.Fragment key={facet.field}>
            <span className="gp-toolbar__label">{facet.label}</span>
            {facet.values.map((v) => (
              <Chip
                key={facet.field + ':' + v.value}
                label={v.label}
                count={v.count}
                selected={v.selected}
                onClick={() => facet.onToggle(v)}
              />
            ))}
          </React.Fragment>
        ))}
      </div>
    </>
  )
}

/** The selection-dependent action cluster shared by every tab. */
export function SelectionActions(props) {
  const { count, onRename, onMove, onDelete, onRefresh, extra } = props
  return (
    <>
      {extra}
      <Button
        size="sm"
        icon={Pencil}
        label="Rename"
        disabled={count !== 1}
        title={count === 1 ? 'Rename the selected asset' : 'Select exactly one asset to rename'}
        onClick={onRename}
      />
      <Button
        size="sm"
        icon={FolderInput}
        label="Move"
        disabled={count === 0}
        title={count === 0 ? 'Select at least one asset to move' : 'Move ' + count + ' asset(s)'}
        onClick={onMove}
      />
      <Button
        size="sm"
        variant="dangerGhost"
        icon={Trash2}
        label="Delete"
        count={count > 1 ? fmtCount(count) : undefined}
        disabled={count === 0}
        title={count === 0 ? 'Select at least one asset to delete' : 'Delete ' + count + ' asset(s)'}
        onClick={onDelete}
      />
      <Button
        size="sm"
        variant="ghost"
        iconOnly
        icon={RefreshCw}
        aria-label="Reload this view"
        title="Reload this view"
        onClick={onRefresh}
      />
    </>
  )
}
