/*
 * amigactld -- System information command handlers (Phase 3)
 *
 * SYSINFO, ASSIGNS, PORTS, VOLUMES, TASKS.
 * Each handler sends its response and returns 0.
 */

#ifndef AMIGACTLD_SYSINFO_H
#define AMIGACTLD_SYSINFO_H

#include "daemon.h"

int cmd_sysinfo(struct client *c, const char *args);
int cmd_assigns(struct client *c, const char *args);
int cmd_ports(struct client *c, const char *args);
int cmd_volumes(struct client *c, const char *args);
int cmd_tasks(struct client *c, const char *args);

#endif /* AMIGACTLD_SYSINFO_H */
