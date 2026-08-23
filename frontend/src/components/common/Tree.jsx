import React, { useState, useCallback } from 'react'
import { ChevronRight } from 'lucide-react'
import { count as fmtCount, bytes as fmtBytes } from '../../services/format.js'

/*
 * Tree - the left rail group / album tree.
 * Depth is expressed by nesting .gp-tree__children, never by a class, so the
 * indent and the guide rail come from the design system alone.
 */

function TreeNode({ node, selectedKey, onSelect, depth }) {
  const [open, setOpen] = useState(depth < 1)
  const children = node.children || []
  const hasChildren = children.length > 0
  const selected = selectedKey !== null && selectedKey !== undefined &&
    String(selectedKey) === String(node.key)
  const Icon = node.icon

  const toggle = useCallback((event) => {
    event.stopPropagation()
    setOpen((v) => !v)
  }, [])

  return (
    <div className="gp-tree__node" role="none">
      <button
        type="button"
        role="treeitem"
        aria-selected={selected}
        aria-expanded={hasChildren ? open : undefined}
        className={'gp-tree__row' + (selected ? ' gp-tree__row--selected' : '')}
        onClick={() => onSelect(node)}
        title={node.title || node.label}
      >
        <span
          className={'gp-tree__twisty' + (hasChildren
            ? (open ? ' gp-tree__twisty--open' : '')
            : ' gp-tree__twisty--leaf')}
          onClick={hasChildren ? toggle : undefined}
        >
          <ChevronRight size={10} aria-hidden="true" />
        </span>
        {Icon ? <Icon className="gp-tree__icon" aria-hidden="true" /> : null}
        <span className="gp-tree__label">{node.label}</span>
        {node.count !== undefined && node.count !== null
          ? <span className="gp-tree__count">{fmtCount(node.count)}</span>
          : null}
        {node.bytes
          ? <span className="gp-tree__bytes">{fmtBytes(node.bytes)}</span>
          : null}
      </button>
      {hasChildren && open ? (
        <div className="gp-tree__children" role="group">
          {children.map((child) => (
            <TreeNode
              key={child.key}
              node={child}
              selectedKey={selectedKey}
              onSelect={onSelect}
              depth={depth + 1}
            />
          ))}
        </div>
      ) : null}
    </div>
  )
}

export default function Tree({ nodes, selectedKey, onSelect, label }) {
  if (!nodes || !nodes.length) return null
  return (
    <div className="gp-tree" role="tree" aria-label={label || 'Groups'}>
      {nodes.map((node) => (
        <TreeNode
          key={node.key}
          node={node}
          selectedKey={selectedKey}
          onSelect={onSelect}
          depth={0}
        />
      ))}
    </div>
  )
}
