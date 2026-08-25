# Filter audit — 2026-08-24

## Purpose

This record captures the end-to-end audit of every grid and cleanup filter: the React control or
left-rail album, the HTTP query parameter, and the backend SQL predicate. It exists to prevent a
filter from being displayed while its parameter is silently ignored.

## Corrections made

1. The Models **Missing files** and **Integrity issues** albums previously sent filter keys that
   `/api/v1/models` did not bind. They therefore returned the whole model library. The API now
   implements the intended predicates.
2. **Missing files** is now the explicit, shared `missing_files_only=true` filter for Models,
   Node Packages, Node Classes, Workflows, and Outputs. It means the asset's indexed source is no
   longer on disk.
3. Workflow `missing_only=true` is retained for a different purpose: workflows with unresolved
   model/node dependencies. This is what **Broken workflows** represents.
4. The Models folder tree now carries the exact filter for each node: a top-level category submits
   `category`, while a nested folder submits both `category` and `folder`. It no longer treats a
   category name as though it were a path.
5. The Nodes **Authors** rail is shown only in Package view, because Node Class queries have no
   author field.
6. System albums are now contextual. A tab does not offer a global album when its query has no
   truthful meaning for that asset type. Examples: Favorites is Models/Outputs only; Updates
   available is Models/Packages only; Integrity issues and Unused models are Models only.
7. Node Packages and Workflows now accept `sort=-created`, allowing **Recently added** to work
   rather than produce an unsupported-sort API error.
8. `untagged=true` is implemented for every asset-grid type.

## Verification matrix

| Tab | UI / rail filters audited | Backend result |
|---|---|---|
| Models | Category, Base, Precision, Hash, Folder, Integrity, Missing, Unused, Untagged | Bound by `ModelFilters` and `v_model_list` predicates. |
| Nodes — Packages | Official, Enabled, Author, Update, Missing, Untagged | Bound by `NodeFilters` and `node_packages` predicates. |
| Nodes — Classes | Official, Category, Missing package, Untagged | Bound by `NodeFilters` over the package join. |
| Workflows | Runnable, Folder, Base, Missing source file, Broken dependencies, Untagged | Bound by `WorkflowFilters`; file-missing and dependency-missing are separate. |
| Outputs | Media, Folder, Date, Favorite, Missing, Untagged | Bound by `OutputFilters` and `outputs` predicates. |
| Storage | Kind, Reason, Category, Root, protected toggle, search, sort | Passed to `/storage/candidates` with CSV multi-value encoding and validated server-side. |

## Automated checks

`backend/tests/unit/test_model_system_filters.py` seeds present and missing records in every asset
domain. It proves that `missing_files_only=true` returns only the missing record in each endpoint,
and that Models integrity filtering returns only non-`ok` records. Run:

```powershell
venv\Scripts\python.exe -m pytest backend\tests\unit\test_model_system_filters.py -q
```

The full UI build and test suite should also be run before release:

```powershell
cd frontend
npm run test:run
npm run build
```
