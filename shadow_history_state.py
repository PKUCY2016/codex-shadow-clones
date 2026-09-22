"""Copy presentation links for imported local histories, never execution state."""
from __future__ import annotations
from copy import deepcopy


def merge_history_state(source_state: dict, target_state: dict, id_map: dict[str, str]) -> dict:
    """Return an independent state dict, remapping only successfully imported IDs.

    Existing destination assignments, pins and order take precedence. Project IDs
    are matched through shared root paths, since Core and legacy IDs may differ.
    No active tabs, queued follow-ups, approvals, permissions or account state is
    read from the source. Call only after project seeding and history import.
    """
    target = deepcopy(target_state)
    valid_map = {old:new for old,new in id_map.items()
                 if isinstance(old,str) and isinstance(new,str) and old and new}
    if not valid_map:
        return target
    source_projects = source_state.get('local-projects', {})
    target_projects = target.get('local-projects', {})
    roots_to_project = {tuple(p.get('rootPaths', [])): ident
                        for ident,p in target_projects.items() if p.get('rootPaths')}
    projects = {}
    for ident, project in source_projects.items():
        roots = tuple(project.get('rootPaths', []))
        if roots and roots in roots_to_project:
            projects[ident] = roots_to_project[roots]
    assignments = target.setdefault('thread-project-assignments', {})
    for old, assignment in source_state.get('thread-project-assignments', {}).items():
        new = valid_map.get(old)
        if new is None or new in assignments or not isinstance(assignment, dict):
            continue
        project = projects.get(assignment.get('projectId'))
        if assignment.get('projectKind') == 'local' and project is not None:
            assignments[new] = {'projectKind':'local', 'projectId':project}
    for key in ('pinned-thread-ids', 'projectless-thread-ids'):
        values = list(target.get(key, []))
        for old in source_state.get(key, []):
            new = valid_map.get(old)
            if new is not None and new not in values:
                # Keep a destination's deliberate project assignment authoritative.
                if key == 'projectless-thread-ids' and new in assignments:
                    continue
                values.append(new)
        target[key] = values
    orders = target.setdefault('sidebar-project-thread-orders', {})
    for old_project, order in source_state.get('sidebar-project-thread-orders', {}).items():
        project = projects.get(old_project)
        if project is None or not isinstance(order, dict):
            continue
        imported = [valid_map[old] for old in order.get('threadIds', [])
                    if old in valid_map and assignments.get(valid_map[old], {}).get('projectId') == project]
        if not imported:
            continue
        destination_order = orders.setdefault(project, {'threadIds': []})
        ids = destination_order.setdefault('threadIds', [])
        ids.extend(new for new in imported if new not in ids)
    # Root hints are lookup metadata, not permissions or additional writable roots.
    hints = target.setdefault('thread-workspace-root-hints', {})
    for old, root in source_state.get('thread-workspace-root-hints', {}).items():
        if old in valid_map and isinstance(root, str) and root.startswith('/'):
            hints.setdefault(valid_map[old], root)
    return target


def assign_synced_projects(home, thread_ids, can_write):
    """Attach new histories to existing local projects by cwd, preserving user choices.

    No project, configuration or pin is copied from another instance. The caller
    serializes managed launches; can_write is checked again before each mutation.
    """
    import json
    import time
    from pathlib import Path
    import shadow_history as history
    from shadow_seed import source_projects
    home = Path(home)
    projects = source_projects(home)
    if not projects or not thread_ids:
        return 0
    state_path = home/'.codex-global-state.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    local = state.get('local-projects', {})
    assignments = state.setdefault('thread-project-assignments', {})
    local_by_roots = {tuple(p.get('rootPaths', [])): ident for ident,p in local.items()}
    candidates = []
    for p in projects:
        roots = tuple(r['path'] for r in p['roots'])
        for root in roots:
            candidates.append((root.rstrip('/'), p['id'], local_by_roots.get(roots)))
    candidates.sort(key=lambda row: len(row[0]), reverse=True)
    changed = 0
    json_changed = False
    with history.connect(home/history.STATE) as db:
        for ident in set(thread_ids):
            row = db.execute('SELECT cwd,project_id FROM threads WHERE id=?',(ident,)).fetchone()
            if (not row or row['project_id'] or ident in assignments
                    or ident in state.get('projectless-thread-ids', [])):
                continue
            match = next((p for p in candidates if row['cwd']==p[0] or row['cwd'].startswith(p[0]+'/')),None)
            if match is None:
                continue
            if not can_write():
                break
            db.execute('UPDATE threads SET project_id=? WHERE id=? AND project_id IS NULL',(match[1],ident))
            changed += 1
            if match[2]:
                assignments[ident] = {'projectKind':'local','projectId':match[2]}
                json_changed = True
    if json_changed and can_write():
        backup = home/'.shadow-backups'/('sync-projects-'+str(time.time_ns()))
        backup.mkdir(parents=True, mode=0o700)
        if state_path.exists():
            old = backup/state_path.name
            old.write_bytes(state_path.read_bytes());old.chmod(0o600)
        temporary = state_path.with_suffix('.shadow-tmp')
        temporary.write_text(json.dumps(state,ensure_ascii=False));temporary.chmod(0o600)
        if can_write():
            temporary.replace(state_path)
        else:
            temporary.unlink()
    return changed
