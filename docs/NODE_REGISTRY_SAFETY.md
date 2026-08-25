# Node Registry and missing-node safety

The **Nodes → Registry** view is a read-only catalogue. It combines two sources:

- the official Comfy Registry metadata cache, refreshed only when outbound lookup
  is enabled; and
- the local ComfyUI-Manager `extension-node-map.json` legacy mapping, used to
  identify classes requested by older workflows.

Searching, refreshing metadata, and opening a repository never install a node.
The cache records when it was fetched and explicitly reports stale/offline data.

## Resolving a workflow

The **Resolve missing nodes** action builds a short-lived plan for one workflow.
It lists every unresolved class, its source mapping, exact destination, warnings,
and the immutable Git commit that was resolved for the plan. The user selects
individual packages and confirms the reviewed plan; there is no wildcard or
"install all" request.

Legacy maps are yellow because they establish a useful class-to-repository hint,
not an independently signed package identity. An unavailable remote, an
unapproved host, an ambiguous/no mapping, a missing Git executable, a destination
conflict, or a remote that cannot resolve to a 40-character commit is blocking.

## Fetch rules

For a fetchable Git package the vault:

1. resolves the remote HEAD to a commit while generating the plan;
2. clones into `custom_nodes/.vault-staging`, with credential helpers, file/ext
   protocols, hooks, tags, and submodules disabled;
3. checks out the reviewed commit; a changed remote or unavailable commit fails
   closed;
4. inspects the staged tree and atomically releases it only when the target
   folder does not already exist; and
5. schedules a vault re-index. ComfyUI still needs a user restart before it can
   import the new node code.

The vault never runs `pip`, `requirements.txt`, `install.py`, `setup.py`, hooks,
or submodules. Those are code-execution decisions left to the owner and are
reported after staging. An archive/source without a supplied checksum or
signature is not represented as cryptographically verified.

## Deliberate limits

The catalogue does not provide a generic one-click installer. Installing an
unneeded custom node is still adding third-party code to ComfyUI. Installation is
therefore tied to a workflow's specific missing classes and reviewed source. CNR
metadata is shown in the catalogue, including its published version and declared
dependencies; it remains warning-level until its delivery endpoint exposes a
verifiable archive digest/signature that the vault can check.
