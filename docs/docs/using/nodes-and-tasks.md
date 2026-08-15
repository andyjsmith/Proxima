---
title: Nodes and tasks
sidebar_position: 6
---

# Nodes and tasks

## Node view

![Node view](/img/screenshots/node_view.png)

Opening a Node shows extra details and graphs:

**Meters** for CPU usage, IO delay, load average, RAM, swap and root filesystem
space. Load is drawn against the processor count, so a full bar means no idle
capacity left rather than an arbitrary ceiling.

**Details** for status, uptime, guest count, processor model, kernel version
and pve-manager version.

**Graphs** of CPU with IO delay drawn over it, memory used against installed,
and network traffic in and out, over the hour, day, week, month or year.
Hovering a graph reads off the sample under the pointer.

The graphs come from the node's round robin database, which records one sample a minute.

### Open Shell

**Open Shell** opens the node's own terminal through `termproxy`, as the web interface's Shell button does.

## Task pane

The **Tasks** button in the toolbar opens the cluster task list along the bottom.

![Task list](/img/screenshots/tasks_list.png)

It reads tasks from every connected server. Double click a task to read its log.

The pane polls only while it is open, at the interval set by **Preferences > Polling > Task list**.